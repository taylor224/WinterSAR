"""Engine-agnostic log scanning (ADR-0037).

The generic parser finds *events* — places in a log that look like a failure — so that
(1) the KB-UNKNOWN fallback can quote a meaningful excerpt and (2) engine parsers can add
their own markers on top. It catches, in this order of confidence:

* Python tracebacks (``Traceback (most recent call last):`` … final exception line),
* bare exception lines (``SomeError: message`` at the start of a line),
* ``Command '...' returned non-zero exit status N.`` / ``died with <Signals...>``
  (CPython ``subprocess.CalledProcessError.__str__``),
* out-of-memory markers: ``Out of memory`` (SNAPHU MAlloc / Linux oom_kill.c),
  ``Killed`` (``signal.strsignal(SIGKILL)``), ``MemoryError``,
  ``Cannot allocate memory`` (``os.strerror(errno.ENOMEM)``),
* upper-case severity tokens ``ERROR`` / ``FATAL`` / ``CRITICAL`` near the start of a line
  (logging-style prefixes such as ``ERROR: ...``, ``[ERROR]``, ``ERROR 1: PROJ: ...``),
* SNAPHU's bare ``Abort`` line.

Warnings are deliberately *not* events: a warning alone never yields KB-UNKNOWN.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from wintersar.util.masking import mask_text


@dataclass(frozen=True)
class LogEvent:
    """One failure-looking region of a log."""

    line_no: int = field(metadata={"doc": "1-based line of the anchor"})
    kind: str = field(metadata={"doc": "traceback | exception | exit | oom | error | abort"})
    text: str = field(metadata={"doc": "anchor line (masked)"})
    context: str = field(metadata={"doc": "surrounding lines (masked)"})


@dataclass(frozen=True)
class ParserSpec:
    """Static description of an engine log parser."""

    name: str
    filename_hints: tuple[str, ...]
    content_markers: tuple[re.Pattern[str], ...]
    event_patterns: tuple[tuple[str, re.Pattern[str]], ...] = ()


# --------------------------------------------------------------------------- patterns
# source: CPython Lib/traceback.py prints "Traceback (most recent call last):"
TRACEBACK_START: Final = re.compile(r"^Traceback \(most recent call last\):\s*$", re.MULTILINE)
# exception line: dotted class name ending in Error/Exception/... followed by ': msg' or nothing
EXCEPTION_LINE: Final = re.compile(
    r"^(?P<exc>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*(?:Error|Exception|Exit|Interrupt|Fault))"
    r"(?::\s?(?P<msg>.*))?$",
    re.MULTILINE,
)
# source: CPython Lib/subprocess.py CalledProcessError.__str__
SUBPROCESS_EXIT: Final = re.compile(
    r"Command .+? (?:returned non-zero exit status \d+\.|died with (?:<Signals\.\w+: \d+>|unknown signal \d+)\.)"
)
# sources: snaphu_util.c MAlloc "Out of memory"; linux mm/oom_kill.c "Out of memory: Killed process";
# signal.strsignal(SIGKILL) == "Killed" ("Killed: 9" on macOS); os.strerror(errno.ENOMEM);
# MemoryError = Python builtin
OOM: Final = re.compile(
    r"Out of memory|^Killed(?::\s*9)?\b|\bMemoryError\b|Cannot allocate memory",
    re.MULTILINE,
)
SEVERITY_TOKEN: Final = re.compile(r"^.{0,80}?\b(?:ERROR|FATAL|CRITICAL)\b", re.MULTILINE)
# source: snaphu C sources print "...\nAbort\n" before exit(ABNORMAL_EXIT)
ABORT_LINE: Final = re.compile(r"^Abort\s*$", re.MULTILINE)

GENERIC_EVENT_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("exit", SUBPROCESS_EXIT),
    ("oom", OOM),
    ("error", SEVERITY_TOKEN),
    ("abort", ABORT_LINE),
)

GENERIC_SPEC: Final = ParserSpec(name="generic", filename_hints=(), content_markers=())


# --------------------------------------------------------------------------- helpers
def line_starts(text: str) -> list[int]:
    """Offsets at which each line starts (index 0 = line 1)."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def line_of(offset: int, starts: list[int]) -> int:
    """1-based line number containing character ``offset``."""
    import bisect

    return bisect.bisect_right(starts, offset)


def context_at(lines: list[str], line_no: int, before: int = 3, after: int = 3) -> str:
    lo = max(0, line_no - 1 - before)
    hi = min(len(lines), line_no + after)
    return "\n".join(lines[lo:hi])


def _traceback_events(text: str, lines: list[str], context_lines: int) -> list[LogEvent]:
    events: list[LogEvent] = []
    starts = line_starts(text)
    for m in TRACEBACK_START.finditer(text):
        start_line = line_of(m.start(), starts)  # 1-based
        i = start_line  # index of the line after "Traceback" (0-based == start_line)
        exc_line = None
        while i < len(lines):
            ln = lines[i]
            if ln.startswith((" ", "\t")) or ln.strip() == "":
                i += 1
                continue
            exc_line = i + 1
            break
        if exc_line is None:
            exc_line = min(len(lines), start_line + 1)
        events.append(
            LogEvent(
                line_no=exc_line,
                kind="traceback",
                text=mask_text(lines[exc_line - 1]),
                context=mask_text(context_at(lines, exc_line, before=context_lines + 3, after=1)),
            )
        )
    return events


def find_events(
    text: str,
    extra_patterns: tuple[tuple[str, re.Pattern[str]], ...] = (),
    context_lines: int = 3,
) -> list[LogEvent]:
    """Scan ``text`` and return failure-looking events sorted by line (one per line)."""
    if not text:
        return []
    lines = text.split("\n")
    starts = line_starts(text)
    events: dict[int, LogEvent] = {}

    for ev in _traceback_events(text, lines, context_lines):
        events.setdefault(ev.line_no, ev)

    covered = set(events)
    for m in EXCEPTION_LINE.finditer(text):
        ln = line_of(m.start(), starts)
        if ln in covered:
            continue
        # skip Python 'raise X' style source lines and traceback frames
        if lines[ln - 1].startswith((" ", "\t")):
            continue
        events.setdefault(
            ln,
            LogEvent(
                line_no=ln,
                kind="exception",
                text=mask_text(lines[ln - 1]),
                context=mask_text(context_at(lines, ln, context_lines, context_lines)),
            ),
        )

    for kind, pat in (*GENERIC_EVENT_PATTERNS, *extra_patterns):
        for m in pat.finditer(text):
            ln = line_of(m.start(), starts)
            if ln in events:
                continue
            events[ln] = LogEvent(
                line_no=ln,
                kind=kind,
                text=mask_text(lines[ln - 1]),
                context=mask_text(context_at(lines, ln, context_lines, context_lines)),
            )
    return [events[k] for k in sorted(events)]


def tail_excerpt(text: str, n_lines: int = 12, max_chars: int = 1500) -> str:
    """Masked last ``n_lines`` of ``text`` (used when nothing else was found)."""
    lines = [ln for ln in text.rstrip("\n").split("\n") if ln.strip()]
    excerpt = "\n".join(lines[-n_lines:])
    if len(excerpt) > max_chars:
        excerpt = excerpt[-max_chars:]
    return mask_text(excerpt)
