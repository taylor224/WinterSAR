"""Matcher (multiline, supersedes, extract) and generic/engine parsers (ADR-0037)."""

from __future__ import annotations

from wintersar.diagnose import parsers
from wintersar.diagnose.kb_loader import KBEntry, kb_index, load_kb
from wintersar.diagnose.matcher import match_entries, to_finding, unknown_finding
from wintersar.diagnose.parsers.generic import find_events, tail_excerpt


def _entry(**kw: object) -> KBEntry:
    base: dict[str, object] = {
        "id": "KB-TEST-001",
        "engine": "any",
        "pattern": "boom",
        "cause": {"ko": "원인", "en": "cause"},
        "fix": {"ko": "조치", "en": "fix"},
        "refs": ["ref"],
        "severity": "FAIL",
        "pattern_verified": False,
    }
    base.update(kw)
    return KBEntry.model_validate(base)


def test_multiline_pattern_and_count() -> None:
    e = _entry(pattern=r"^first line\nsecond line$")
    text = "ok\nfirst line\nsecond line\nmore\nfirst line\nsecond line\n"
    (m,) = match_entries(text, [e])
    assert m.line_no == 2
    assert m.count == 2
    assert "first line" in m.context and "more" in m.context


def test_extract_groups_to_params_and_masking() -> None:
    e = _entry(pattern=r"cost (?P<cost>\d+) token=(?P<tok>\S+)", extract=["cost"])
    text = "cost 12 token=SECRETVALUE123\n"
    (m,) = match_entries(text, [e])
    assert m.groups == {"cost": "12"}
    f = to_finding(m, scope="x.log", engine="snaphu")
    assert f.params == {"cost": "12"}
    assert f.rule_id == "KB-TEST-001"
    assert f.message_key == "diagnose.KB-TEST-001.cause"
    assert "SECRETVALUE123" not in f.evidence["excerpt"]
    assert "engine_mismatch" not in f.evidence  # entry engine 'any' never mismatches
    g = to_finding(m, scope="x.log", engine="mintpy")
    assert "engine_mismatch" not in g.evidence


def test_all_named_groups_extracted_by_default() -> None:
    e = _entry(pattern=r"a=(?P<a>\d) b=(?P<b>\d)")
    (m,) = match_entries("a=1 b=2", [e])
    assert m.groups == {"a": "1", "b": "2"}


def test_supersedes_drops_generic_entry() -> None:
    generic = _entry(id="KB-TEST-002", pattern="^Killed$")
    specific = _entry(id="KB-TEST-001", pattern="child process", supersedes=["KB-TEST-002"])
    text = "Killed\nUnexpected or abnormal exit of child process 3\n"
    ms = match_entries(text, [generic, specific])
    assert [m.entry.id for m in ms] == ["KB-TEST-001"]
    # without the specific entry the generic one is reported
    assert [m.entry.id for m in match_entries(text, [generic])] == ["KB-TEST-002"]


def test_real_kb_snaphu_oom_supersedes_env_oom() -> None:
    text = "Unwrapping tile at row 0, column 0 (pid 5)\nKilled\nUnexpected or abnormal exit of child process 5\nAbort\n"
    ms = match_entries(text, load_kb())
    assert [m.entry.id for m in ms] == ["KB-SNAPHU-002"]


def test_unknown_finding_without_events_uses_tail() -> None:
    f = unknown_finding([], "line1\nline2\nline3\n", scope="s", engine=None)
    assert f.rule_id == "KB-UNKNOWN"
    assert "line3" in f.evidence["excerpt"]
    assert f.evidence["n_events"] == 0
    assert f.params["engine"] == "-"


# ----------------------------------------------------------------------- generic parser
def test_generic_events_traceback_error_oom_exit() -> None:
    text = (
        "INFO start\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "    raise RuntimeError('bad')\n"
        "RuntimeError: bad\n"
        "2024-01-01 ERROR something\n"
        "Killed\n"
        "Command '['snaphu']' returned non-zero exit status 1.\n"
        "ERROR_CODE=3 is not an error token\n"
        "WARNING: only a warning\n"
    )
    events = find_events(text)
    kinds = {ev.line_no: ev.kind for ev in events}
    assert kinds == {5: "traceback", 6: "error", 7: "oom", 8: "exit"}
    assert events[0].text == "RuntimeError: bad"
    assert "Traceback" in events[0].context


def test_generic_bare_exception_line_and_no_false_positive_in_frames() -> None:
    text = "asf_search.exceptions.ASFSearch5xxError: HTTP 503: boom\n    ValueError: indented\n"
    events = find_events(text)
    assert [(ev.line_no, ev.kind) for ev in events] == [(1, "exception")]


def test_tail_excerpt_masks_and_limits() -> None:
    text = "\n".join(f"line {i} password=hunter2" for i in range(30))
    ex = tail_excerpt(text, n_lines=3)
    assert ex.count("\n") == 2
    assert "hunter2" not in ex


# ----------------------------------------------------------------------- engine detection
def test_detect_engine_by_name_and_content() -> None:
    assert parsers.detect_engine_from_name("run_08_esd.log") == "isce2"
    assert parsers.detect_engine_from_name("unwrap_20240101.log") == "snaphu"
    assert parsers.detect_engine_from_name("smallbaselineApp.log") == "mintpy"
    assert parsers.detect_engine_from_name("hyp3_submit.log") == "hyp3"
    assert parsers.detect_engine_from_name("random.log") is None
    assert (
        parsers.detect_engine("x.log", "hyp3_sdk.exceptions.HyP3Error: <Response [400]>") == "hyp3"
    )
    assert parsers.detect_engine("x.log", "asf_search.exceptions.ASFSearch5xxError") == "asf"
    assert parsers.detect_engine(None, "nothing here") is None
    # name wins when its markers are present; content wins over a misleading name
    assert parsers.detect_engine("unwrap.log", "snaphu -f snaphu.conf") == "snaphu"
    assert (
        parsers.detect_engine("unwrap.log", "smallbaselineApp.py mintpy.reference.yx") == "mintpy"
    )


def test_score_engines_caps_hits() -> None:
    text = "snaphu " * 100
    assert parsers.score_engines(text)["snaphu"] <= 5 * len(parsers.SPECS["snaphu"].content_markers)


def test_get_spec_unknown_engine() -> None:
    import pytest

    with pytest.raises(ValueError):
        parsers.get_spec("gamma")
    assert parsers.get_spec(None).name == "generic"


def test_kb_index_contains_all_ids() -> None:
    assert set(kb_index()) == {e.id for e in load_kb()}
