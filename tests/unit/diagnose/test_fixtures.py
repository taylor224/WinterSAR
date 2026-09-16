"""Phase 3 DoD: exact KB id per log fixture + KB-UNKNOWN fallback with a masked excerpt."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.diagnose.conftest import LOGS_DIR, load_manifest
from wintersar.diagnose import parsers
from wintersar.diagnose.api import diagnose_logs, diagnose_text, read_log
from wintersar.i18n import t

CASES = load_manifest()


def _ids(case: dict[str, Any]) -> str:
    return str(case["file"])


def test_fixture_counts() -> None:
    matched = [c for c in CASES if c["expect"] and c["expect"] != ["KB-UNKNOWN"]]
    unknown = [c for c in CASES if c["expect"] == ["KB-UNKNOWN"]]
    assert len(matched) >= 12
    assert len(unknown) >= 2
    for c in CASES:
        assert (LOGS_DIR / c["file"]).is_file()


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_fixture_matches_exact_kb_id(case: dict[str, Any], fake_home: str) -> None:
    path = LOGS_DIR / case["file"]
    findings = diagnose_logs(path)
    assert [f.rule_id for f in findings] == case["expect"], [
        (f.rule_id, f.evidence.get("matched")) for f in findings
    ]
    for f in findings:
        # i18n keys resolve in both languages (no raw key leaks) and read cause -> fix
        assert t(f.message_key, "ko") != f.message_key
        assert t(f.message_key, "en") != f.message_key
        assert f.fix_key and t(f.fix_key, "ko") != f.fix_key
        # scope names the file, evidence carries a masked excerpt
        assert f.scope == case["file"].split("/")[-1] or f.scope.endswith(
            case["file"].split("/")[-1]
        )
        assert f.evidence["excerpt"]
        assert "/home/user" not in f.evidence["excerpt"]
        assert "eyJ0eXAi" not in f.evidence["excerpt"]
        assert "abcdef1234567890" not in f.evidence["excerpt"]
    for k, v in (case.get("params") or {}).items():
        assert findings[0].params[k] == v


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_fixture_engine_detection(case: dict[str, Any]) -> None:
    path = LOGS_DIR / case["file"]
    detected = parsers.detect_engine(path.name, read_log(path))
    assert detected == case["engine"]


@pytest.mark.parametrize("case", [c for c in CASES if c["expect"] == ["KB-UNKNOWN"]], ids=_ids)
def test_unknown_fallback_has_excerpt(case: dict[str, Any], fake_home: str) -> None:
    path = LOGS_DIR / case["file"]
    (f,) = diagnose_logs(path)
    assert f.rule_id == "KB-UNKNOWN"
    assert f.severity == "WARN"
    assert f.message_key == "diagnose.KB-UNKNOWN.cause"
    assert f.fix_key == "diagnose.KB-UNKNOWN.fix"
    assert f.evidence["n_events"] >= 1
    assert f.evidence["events"][0]["kind"] in {"traceback", "error", "exception", "oom", "exit"}
    assert "~/" in f.evidence["excerpt"] or "/home/user" not in f.evidence["excerpt"]
    rendered = t(f.message_key, "ko", **f.params)
    assert "KB" not in rendered.split("\n")[0] or "발췌" in rendered


def test_clean_log_has_no_findings() -> None:
    assert diagnose_logs(LOGS_DIR / "generic" / "clean.log") == []


def test_explicit_engine_does_not_hide_other_engines_match(fake_home: str) -> None:
    text = read_log(LOGS_DIR / "mintpy" / "reference_point.log")
    findings = diagnose_text(text, engine="snaphu")
    assert [f.rule_id for f in findings] == ["KB-MINTPY-001"]
    assert findings[0].evidence.get("engine_mismatch") is True


def test_directory_scan_returns_all_scopes(tmp_path: Path, fake_home: str) -> None:
    work = tmp_path / "logs"
    work.mkdir()
    (work / "unwrap.log").write_text(
        (LOGS_DIR / "snaphu" / "secondary_nodes.log").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (work / "sub").mkdir()
    (work / "sub" / "timeseries.log").write_text(
        (LOGS_DIR / "mintpy" / "not_enough_pixels.log").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (work / "binary.log").write_bytes(b"\x00\x01\x02 Out of memory")
    (work / "notes.pdf").write_text("Out of memory", encoding="utf-8")
    findings = diagnose_logs(work)
    assert {(f.rule_id, f.scope) for f in findings} == {
        ("KB-SNAPHU-001", "unwrap.log"),
        ("KB-MINTPY-002", "sub/timeseries.log"),
    }
