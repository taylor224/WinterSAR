"""Public diagnosis API (plan §5.5, R-02, R-14, PERF-13).

* :func:`diagnose_text` — diagnose one log text.
* :func:`diagnose_logs` — diagnose a file or every log file under a directory.
* :func:`diagnose_exception` — diagnose a caught exception (used by ``run`` on failure).
* :func:`attach_retry_hint` — machine-readable retry suggestion for the pipeline.

Flow: detect engine (name + content) → try the engine's KB entries (+ engine-independent
ones) → if nothing matched, try every entry → if still nothing but the generic parser saw
a failure (traceback / ERROR / OOM …) emit ``KB-UNKNOWN`` with a masked excerpt.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from wintersar.diagnose import parsers
from wintersar.diagnose.kb_loader import KB_ENGINES, KBEntry, entries_for_engine, kb_index
from wintersar.diagnose.matcher import match_entries, to_finding, unknown_finding
from wintersar.io.schemas import Finding, sort_findings
from wintersar.util.masking import mask_mapping, mask_text

LOG_SUFFIXES: frozenset[str] = frozenset(
    {".log", ".txt", ".out", ".err", ".stdout", ".stderr", ".e", ".o", ".json"}
)
DEFAULT_MAX_BYTES = 20_000_000


#: registry / config engine names that map onto a KB engine family (``None`` = auto-detect)
ENGINE_ALIASES: dict[str, str | None] = {
    "isce2_topsstack": "isce2",
    "compass_isce3": "isce2",
    "tophu": "snaphu",
    "fake": None,
    "spurt": None,
    "dolphin": None,
    "asf_search": "asf",
}


def _check_engine(engine: str | None) -> str | None:
    if engine is None:
        return None
    eng = engine.lower()
    if eng in ENGINE_ALIASES:
        return ENGINE_ALIASES[eng]
    if eng not in KB_ENGINES:
        msg = f"unknown engine {engine!r}; choose one of {KB_ENGINES}"
        raise ValueError(msg)
    return eng


def diagnose_text(
    text: str,
    engine: str | None = None,
    *,
    scope: str | None = None,
    context_lines: int = 3,
    assume_failed: bool = False,
) -> list[Finding]:
    """Diagnose one log text and return findings (FAIL first).

    ``engine`` restricts which KB entries are tried *first*; when it is ``None`` the engine is
    detected from ``scope`` (file name) and the content. ``assume_failed=True`` emits
    ``KB-UNKNOWN`` even when the generic parser sees no error marker (the caller knows the
    stage failed, e.g. a non-zero exit code).
    """
    requested = _check_engine(engine)
    detected = parsers.detect_engine(scope, text)
    eng = requested or detected
    matches = match_entries(text, entries_for_engine(eng), context_lines)
    if not matches and eng is not None:
        matches = match_entries(text, entries_for_engine(None), context_lines)
    findings = [to_finding(m, scope=scope, engine=eng) for m in matches]
    if not findings:
        events = parsers.events_for(eng, text, context_lines)
        if events or assume_failed:
            findings.append(unknown_finding(events, text, scope=scope, engine=eng))
    return sort_findings(findings)


def iter_log_files(path: Path) -> list[Path]:
    """Log files under ``path`` (a file is returned as-is)."""
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(path.rglob("*")):
        if not p.is_file():
            continue
        if (p.suffix.lower() in LOG_SUFFIXES or p.suffix == "") and _looks_text(p):
            out.append(p)
    return out


def _looks_text(p: Path, probe: int = 4096) -> bool:
    try:
        with p.open("rb") as fh:
            chunk = fh.read(probe)
    except OSError:
        return False
    return b"\x00" not in chunk


def read_log(path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Read a log as text (last ``max_bytes`` when larger; undecodable bytes replaced)."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        data = fh.read()
    return data.decode("utf-8", errors="replace")


def diagnose_logs(
    path: Path,
    engine: str | None = None,
    *,
    assume_failed: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    context_lines: int = 3,
) -> list[Finding]:
    """Diagnose a log file or every log file under a directory.

    ``Finding.scope`` is the (masked) path relative to ``path``'s parent so that a report
    can say which job failed. Identical findings from different files are kept (they have
    different scopes); identical findings within one file are merged by the matcher.
    """
    path = Path(path)
    _check_engine(engine)
    files = iter_log_files(path)
    if not files:
        return []
    base = path.parent if path.is_file() else path
    findings: list[Finding] = []
    for f in files:
        text = read_log(f, max_bytes)
        try:
            rel = f.relative_to(base)
        except ValueError:
            rel = f
        scope = mask_text(str(rel))
        findings.extend(
            diagnose_text(
                text,
                engine,
                scope=scope,
                context_lines=context_lines,
                assume_failed=assume_failed and len(files) == 1,
            )
        )
    if assume_failed and not findings:
        # directory: nothing matched anywhere -> one KB-UNKNOWN with the last file's tail
        last = files[-1]
        text = read_log(last, max_bytes)
        findings.append(unknown_finding([], text, scope=mask_text(str(last.name)), engine=engine))
    return sort_findings(findings)


def diagnose_exception(
    exc: BaseException, engine: str | None = None, *, scope: str | None = None
) -> list[Finding]:
    """Diagnose a caught exception via its formatted traceback (``run`` failure path)."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return diagnose_text(text, engine, scope=scope, assume_failed=True)


def attach_retry_hint(
    findings: list[Finding], kb: dict[str, KBEntry] | None = None
) -> dict[str, Any] | None:
    """Retry suggestion from the most severe finding that carries a KB ``retry_hint``.

    Returns ``{"rule_id", "stage", "action", "params", "note", "scope", "extracted"}`` or
    ``None``. ``params`` are parameter overrides for the stage named in ``stage`` (names
    follow the engine adapter / config field they refer to); ``extracted`` carries the
    values captured from the log (e.g. credits cost/remaining) for the caller's own logic.
    """
    index = kb if kb is not None else kb_index()
    for f in sort_findings(findings):
        entry = index.get(f.rule_id)
        if entry is None or entry.retry_hint is None:
            continue
        hint = entry.retry_hint
        out: dict[str, Any] = mask_mapping(
            {
                "rule_id": f.rule_id,
                "stage": hint.stage,
                "action": hint.action,
                "params": dict(hint.params),
                "note": hint.note,
                "scope": f.scope,
                "extracted": dict(f.params),
            }
        )
        return out
    return None


__all__ = [
    "DEFAULT_MAX_BYTES",
    "LOG_SUFFIXES",
    "attach_retry_hint",
    "diagnose_exception",
    "diagnose_logs",
    "diagnose_text",
    "iter_log_files",
    "read_log",
]
