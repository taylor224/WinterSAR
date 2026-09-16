"""Log parsers: engine detection + event extraction (plan §5.5).

Each engine module exposes a :class:`~wintersar.diagnose.parsers.generic.ParserSpec` with
file-name hints, content markers (for auto-detection) and extra event patterns. Matching
against the KB is engine-agnostic (regex over the whole text, see ``matcher.py``); the
engine only decides which KB entries are tried first and how the excerpt is extracted.
"""

from __future__ import annotations

from pathlib import Path

from wintersar.diagnose.parsers import asf, hyp3, isce2, mintpy, snaphu
from wintersar.diagnose.parsers.generic import (
    GENERIC_SPEC,
    LogEvent,
    ParserSpec,
    find_events,
    tail_excerpt,
)

SPECS: dict[str, ParserSpec] = {
    s.name: s for s in (snaphu.SPEC, isce2.SPEC, mintpy.SPEC, hyp3.SPEC, asf.SPEC)
}
ENGINES: tuple[str, ...] = tuple(SPECS)

_MAX_HITS_PER_MARKER = 5


def get_spec(engine: str | None) -> ParserSpec:
    if engine is None:
        return GENERIC_SPEC
    try:
        return SPECS[engine]
    except KeyError as e:
        msg = f"unknown engine {engine!r}; known: {ENGINES}"
        raise ValueError(msg) from e


def detect_engine_from_name(name: str | None) -> str | None:
    """Engine implied by a log file name (``None`` when ambiguous or unknown)."""
    if not name:
        return None
    lowered = Path(name).name.lower()
    hits = [eng for eng, spec in SPECS.items() if any(h in lowered for h in spec.filename_hints)]
    return hits[0] if len(hits) == 1 else None


def score_engines(text: str) -> dict[str, int]:
    """Marker hit counts per engine (each marker contributes at most a few hits)."""
    scores: dict[str, int] = {}
    for eng, spec in SPECS.items():
        n = 0
        for pat in spec.content_markers:
            hits = 0
            for _ in pat.finditer(text):
                hits += 1
                if hits >= _MAX_HITS_PER_MARKER:
                    break
            n += hits
        scores[eng] = n
    return scores


def detect_engine(name: str | None, text: str) -> str | None:
    """Best-guess engine from the file name, then from content markers.

    Returns ``None`` when nothing matches or the content scores tie.
    """
    by_name = detect_engine_from_name(name)
    scores = score_engines(text)
    if by_name is not None and scores.get(by_name, 0) > 0:
        return by_name
    best = max(scores.values(), default=0)
    if best == 0:
        return by_name
    winners = [eng for eng, s in scores.items() if s == best]
    if len(winners) == 1:
        return winners[0]
    return by_name


def events_for(engine: str | None, text: str, context_lines: int = 3) -> list[LogEvent]:
    spec = get_spec(engine)
    return find_events(text, spec.event_patterns, context_lines=context_lines)


__all__ = [
    "ENGINES",
    "GENERIC_SPEC",
    "SPECS",
    "LogEvent",
    "ParserSpec",
    "detect_engine",
    "detect_engine_from_name",
    "events_for",
    "find_events",
    "get_spec",
    "score_engines",
    "tail_excerpt",
]
