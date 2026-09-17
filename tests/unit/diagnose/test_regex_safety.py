"""Regex cost guards for the diagnose patterns (robustness).

``diagnose_logs`` runs synchronously on the failure path (``pipeline.executor._on_failure``),
so a pattern whose cost is quadratic in the length of a *line* stalls the whole pipeline: a
wrapper that redraws progress with a bare ``\\r`` writes a single line of a few hundred KB, and
``read_log`` opens the file in binary mode, so nothing splits it for the regex engine.

These tests pin the two halves of the fix:

* every KB / parser pattern stays roughly linear on a 1 MB single line, and
* :func:`wintersar.diagnose.api.read_log` folds ``\\r`` back into ordinary lines.

The budgets below are generous safety bounds, not measurements (rule 11.8): a linear scan is
milliseconds, the quadratic patterns needed minutes.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from wintersar.diagnose import parsers
from wintersar.diagnose.api import diagnose_logs, diagnose_text, normalise_newlines, read_log
from wintersar.diagnose.kb_loader import KBEntry, load_kb
from wintersar.diagnose.parsers.generic import GENERIC_EVENT_PATTERNS, SUBPROCESS_EXIT

#: one long line built from tokens that every quadratic pattern anchors on ("Command", "job",
#: "IW1"/"burst") without ever completing a match -> worst case for a backtracking wildcard
ADVERSARIAL_UNIT = "Command /bin/foo --opt IW1 burst 3 job x "
LONG_LINE_BYTES = 1_000_000
#: per-pattern budget on ``LONG_LINE_BYTES``; the unbounded wildcards took minutes
PATTERN_BUDGET_S = 3.0
#: budget for a full ``diagnose_text`` (engine detection + every KB entry + event scan)
TEXT_BUDGET_S = 25.0


def _long_line() -> str:
    return ADVERSARIAL_UNIT * (LONG_LINE_BYTES // len(ADVERSARIAL_UNIT))


def _all_patterns() -> list[tuple[str, re.Pattern[str]]]:
    out: list[tuple[str, re.Pattern[str]]] = [(e.id, e.regex) for e in load_kb()]
    for name, spec in parsers.SPECS.items():
        out += [(f"{name}.marker[{i}]", p) for i, p in enumerate(spec.content_markers)]
        out += [(f"{name}.event[{i}]", p) for i, (_, p) in enumerate(spec.event_patterns)]
    out += [(f"generic.{kind}", p) for kind, p in GENERIC_EVENT_PATTERNS]
    return out


@pytest.mark.parametrize(
    "name,pattern", _all_patterns(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_pattern_is_not_quadratic_on_a_long_line(name: str, pattern: re.Pattern[str]) -> None:
    text = _long_line()
    t0 = time.perf_counter()
    list(pattern.finditer(text))
    assert time.perf_counter() - t0 < PATTERN_BUDGET_S, name


def test_no_pattern_uses_an_unbounded_wildcard_before_a_literal() -> None:
    """``.+``/``.*`` followed by more pattern is the shape that backtracks quadratically."""
    unbounded = re.compile(r"\.[*+]\??(?![)|]*$)")
    offenders = [
        name
        for name, pattern in _all_patterns()
        if unbounded.search(pattern.pattern.replace(r"\.", ""))
    ]
    assert offenders == [], offenders


def test_diagnose_text_on_a_carriage_return_progress_line() -> None:
    body = "wintersar fetch: start\n" + _long_line() + "\nERROR: download failed\n"
    t0 = time.perf_counter()
    findings = diagnose_text(body)
    assert time.perf_counter() - t0 < TEXT_BUDGET_S
    assert [f.rule_id for f in findings] == ["KB-UNKNOWN"]


def test_diagnose_logs_on_a_carriage_return_progress_log(tmp_path: Path) -> None:
    unit = ADVERSARIAL_UNIT.rstrip() + "\r"
    p = tmp_path / "download.log"
    p.write_bytes(
        ("wintersar fetch: start\n" + unit * (500_000 // len(unit)) + "\nERROR: failed\n").encode()
    )
    t0 = time.perf_counter()
    findings = diagnose_logs(tmp_path)
    assert time.perf_counter() - t0 < TEXT_BUDGET_S
    assert [f.rule_id for f in findings] == ["KB-UNKNOWN"]


def test_read_log_normalises_carriage_returns(tmp_path: Path) -> None:
    p = tmp_path / "progress.log"
    p.write_bytes(b"start\r\n 10%\r 50%\r100%\rKilled\nend\n")
    text = read_log(p)
    assert "\r" not in text
    assert text.splitlines() == ["start", " 10%", " 50%", "100%", "Killed", "end"]
    # ``^Killed`` (KB-ENV-002) only anchors once the CR line has been split
    (f,) = diagnose_logs(p)
    assert f.rule_id == "KB-ENV-002"


def test_normalise_newlines_leaves_lf_text_untouched() -> None:
    text = "a\nb\n"
    assert normalise_newlines(text) is text
    assert normalise_newlines("a\r\nb\rc") == "a\nb\nc"


def _entry(kb_id: str) -> KBEntry:
    return next(e for e in load_kb() if e.id == kb_id)


@pytest.mark.parametrize(
    "line",
    [
        "subprocess.CalledProcessError: Command '/usr/bin/snaphu -f snaphu.conf'"
        " died with <Signals.SIGKILL: 9>.",
        "Command '" + "/very/long/path" * 20 + "/snaphu' died with <Signals.SIGKILL: 9>.",
    ],
)
def test_bounded_command_wildcard_still_matches_upstream_string(line: str) -> None:
    """The ``{1,500}`` bound must not shorten the real CPython message (rule 11.3)."""
    assert _entry("KB-ENV-002").regex.search(line) is not None
    assert SUBPROCESS_EXIT.search(line) is not None


def test_bounded_subprocess_exit_matches_both_calledprocesserror_forms() -> None:
    for line in (
        "subprocess.CalledProcessError: Command '['snaphu', '-f', 'c.conf']'"
        " returned non-zero exit status 1.",
        "subprocess.CalledProcessError: Command 'x' died with unknown signal 42.",
    ):
        assert SUBPROCESS_EXIT.search(line) is not None


def test_bounded_isce2_burst_marker_still_detects_the_engine() -> None:
    text = "topsApp.py: extracting burst 3 of swath IW2\nstackSentinel.py: done\n"
    assert parsers.score_engines(text)["isce2"] > 0
