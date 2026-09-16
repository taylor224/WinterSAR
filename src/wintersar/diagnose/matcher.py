"""KB matching: regex over the whole log text -> :class:`Finding` (plan §5.5).

Multiline-safe: every KB pattern is compiled with ``re.MULTILINE`` so ``^``/``$`` anchor
lines, and matching runs on the full text (not line by line) so messages that span lines
(e.g. SNAPHU's two-line "Exceeded maximum number of secondary nodes\\nDecrease ...") are
found. One :class:`Finding` per KB entry per log (``evidence.count`` says how often it
matched). Everything copied into a Finding goes through ``mask_text`` (rule 11.11).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wintersar.diagnose.kb_loader import UNKNOWN_RULE_ID, KBEntry
from wintersar.diagnose.parsers.generic import (
    LogEvent,
    context_at,
    line_of,
    line_starts,
    tail_excerpt,
)
from wintersar.io.schemas import Finding, Severity
from wintersar.util.masking import mask_mapping, mask_text

MAX_MATCH_CHARS = 400
#: key under which a KB retry hint is embedded in ``Finding.evidence`` (pipeline executor contract)
RETRY_HINT_KEY = "retry_hint"
MAX_EXCERPT_CHARS = 2000


@dataclass(frozen=True)
class KBMatch:
    entry: KBEntry
    line_no: int
    matched_text: str
    groups: dict[str, str]
    context: str
    count: int


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def match_entries(text: str, entries: list[KBEntry], context_lines: int = 3) -> list[KBMatch]:
    """Match every KB entry against ``text``; apply ``supersedes``; sort by line."""
    if not text:
        return []
    lines = text.split("\n")
    starts = line_starts(text)
    matches: dict[str, KBMatch] = {}
    for entry in entries:
        it = entry.regex.finditer(text)
        first = next(it, None)
        if first is None:
            continue
        # groups are merged over every occurrence: the first non-empty value of each named
        # group wins (an alternation may hit a class name before the line carrying the value)
        wanted = entry.extract_groups
        groups: dict[str, str] = {}
        count = 0
        for occurrence in (first, *it):
            count += 1
            gd = occurrence.groupdict()
            for k in wanted:
                if k not in groups and gd.get(k) is not None:
                    groups[k] = gd[k]
        ln = line_of(first.start(), starts)
        matches[entry.id] = KBMatch(
            entry=entry,
            line_no=ln,
            matched_text=mask_text(_clip(first.group(0), MAX_MATCH_CHARS)),
            groups={k: mask_text(v) for k, v in groups.items()},
            context=mask_text(
                _clip(context_at(lines, ln, context_lines, context_lines), MAX_EXCERPT_CHARS)
            ),
            count=count,
        )
    for m in list(matches.values()):
        for other in m.entry.supersedes:
            matches.pop(other, None)
    return sorted(matches.values(), key=lambda m: (m.line_no, m.entry.id))


def to_finding(match: KBMatch, scope: str | None = None, engine: str | None = None) -> Finding:
    e = match.entry
    evidence: dict[str, Any] = {
        "line": match.line_no,
        "count": match.count,
        "matched": match.matched_text,
        "excerpt": match.context,
        "engine": engine or e.engine,
        "kb_engine": e.engine,
        "stage": e.stage,
        "pattern_verified": e.pattern_verified,
    }
    if engine is not None and e.engine not in (engine, "any"):
        evidence["engine_mismatch"] = True
    if e.retry_hint is not None:
        # source: src/wintersar/pipeline/executor.py::_retry_hint reads Finding.evidence[RETRY_HINT_KEY]
        evidence[RETRY_HINT_KEY] = {"rule_id": e.id, **e.retry_hint.model_dump()}
    return Finding(
        rule_id=e.id,
        severity=e.severity,
        message_key=e.message_key,
        fix_key=e.fix_key,
        params=dict(match.groups),
        evidence=mask_mapping(evidence),
        refs=list(e.refs),
        scope=scope,
    )


def unknown_finding(
    events: list[LogEvent],
    text: str,
    scope: str | None = None,
    engine: str | None = None,
    severity: Severity = "WARN",
) -> Finding:
    """KB-UNKNOWN: something failed but no KB pattern matched (Phase 3 DoD)."""
    if events:
        first = events[0]
        excerpt = first.context
        line = first.line_no
    else:
        excerpt = tail_excerpt(text)
        line = max(1, text.count("\n"))
    excerpt = _clip(excerpt, MAX_EXCERPT_CHARS)
    evidence: dict[str, Any] = {
        "line": line,
        "n_events": len(events),
        "kinds": sorted({ev.kind for ev in events}),
        "events": [
            {"line": ev.line_no, "kind": ev.kind, "text": _clip(ev.text, MAX_MATCH_CHARS)}
            for ev in events[:8]
        ],
        "excerpt": excerpt,
        "engine": engine,
    }
    return Finding(
        rule_id=UNKNOWN_RULE_ID,
        severity=severity,
        message_key=f"diagnose.{UNKNOWN_RULE_ID}.cause",
        fix_key=f"diagnose.{UNKNOWN_RULE_ID}.fix",
        params={"excerpt": excerpt, "n_events": len(events), "engine": engine or "-"},
        evidence=mask_mapping(evidence),
        refs=["docs/kb/index.md"],
        scope=scope,
    )
