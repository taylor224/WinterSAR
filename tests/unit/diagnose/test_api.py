"""diagnose_text / diagnose_logs / diagnose_exception / attach_retry_hint."""

from __future__ import annotations

from pathlib import Path

import pytest

from wintersar.diagnose.api import (
    attach_retry_hint,
    diagnose_exception,
    diagnose_logs,
    diagnose_text,
    iter_log_files,
    read_log,
)
from wintersar.io.schemas import Finding

SNAPHU_TEXT = (
    "snaphu -f snaphu.conf --tile 2 2 200 200\n"
    "Exceeded maximum number of secondary nodes\n"
    "Decrease TILECOSTTHRESH and/or increase MINREGIONSIZE\n"
)


def test_diagnose_text_basic_and_sorted() -> None:
    text = SNAPHU_TEXT + "WARNING: something\n"
    findings = diagnose_text(text)
    assert [f.rule_id for f in findings] == ["KB-SNAPHU-001"]
    f = findings[0]
    assert f.severity == "FAIL"
    assert f.evidence["line"] == 2
    assert f.evidence["count"] == 2  # both alternatives match (one per line)
    assert f.evidence["pattern_verified"] is True
    assert f.evidence["engine"] == "snaphu"
    assert f.refs


def test_diagnose_text_rejects_unknown_engine() -> None:
    with pytest.raises(ValueError):
        diagnose_text("x", engine="gamma")


def test_diagnose_text_empty_and_assume_failed() -> None:
    assert diagnose_text("") == []
    assert diagnose_text("all good\n") == []
    (f,) = diagnose_text("all good\n", assume_failed=True)
    assert f.rule_id == "KB-UNKNOWN"
    assert "all good" in f.evidence["excerpt"]


def test_diagnose_text_unknown_when_only_generic_error() -> None:
    (f,) = diagnose_text("ERROR: totally new failure mode\n", engine="mintpy")
    assert f.rule_id == "KB-UNKNOWN"
    assert f.evidence["engine"] == "mintpy"
    assert f.params["engine"] == "mintpy"


def test_diagnose_logs_file_dir_missing(tmp_path: Path) -> None:
    assert diagnose_logs(tmp_path / "nope") == []
    p = tmp_path / "unwrap.log"
    p.write_text(SNAPHU_TEXT, encoding="utf-8")
    (f,) = diagnose_logs(p)
    assert f.rule_id == "KB-SNAPHU-001" and f.scope == "unwrap.log"
    (f2,) = diagnose_logs(tmp_path)
    assert f2.scope == "unwrap.log"


def test_diagnose_logs_assume_failed_directory(tmp_path: Path) -> None:
    (tmp_path / "a.log").write_text("fine\n", encoding="utf-8")
    (tmp_path / "b.log").write_text("also fine\nlast line\n", encoding="utf-8")
    assert diagnose_logs(tmp_path) == []
    (f,) = diagnose_logs(tmp_path, assume_failed=True)
    assert f.rule_id == "KB-UNKNOWN" and f.scope == "b.log"
    assert "last line" in f.evidence["excerpt"]


def test_read_log_tail_and_binary_skip(tmp_path: Path) -> None:
    big = tmp_path / "big.log"
    big.write_text("x" * 5000 + "\nOut of memory\n", encoding="utf-8")
    text = read_log(big, max_bytes=100)
    assert len(text) <= 100 and "Out of memory" in text
    (tmp_path / "bin.log").write_bytes(b"\x00\x00abc")
    (tmp_path / "noext").write_text("plain text log\n", encoding="utf-8")
    (tmp_path / "data.npz").write_bytes(b"PK\x03\x04")
    files = {p.name for p in iter_log_files(tmp_path)}
    assert files == {"big.log", "noext"}
    (f,) = diagnose_logs(big, max_bytes=100)
    assert f.rule_id == "KB-SNAPHU-002"  # 'Out of memory' with no engine -> all entries tried


def test_read_log_replaces_undecodable_bytes(tmp_path: Path) -> None:
    p = tmp_path / "latin.log"
    p.write_bytes(b"caf\xe9 Out of memory\n")
    assert "Out of memory" in read_log(p)


def test_diagnose_exception() -> None:
    try:
        raise RuntimeError(
            "No pixel with average spatial coherence > 0.85 are found for automatic reference point selection!"
        )
    except RuntimeError as e:
        findings = diagnose_exception(e, engine="mintpy", scope="timeseries")
    assert [f.rule_id for f in findings] == ["KB-MINTPY-001"]
    assert findings[0].params == {"threshold": "0.85"}
    assert findings[0].scope == "timeseries"

    try:
        raise ValueError("something brand new")
    except ValueError as e:
        (f,) = diagnose_exception(e)
    assert f.rule_id == "KB-UNKNOWN" and "ValueError: something brand new" in f.evidence["excerpt"]


def test_attach_retry_hint_prefers_most_severe_with_hint() -> None:
    findings = diagnose_text(SNAPHU_TEXT)
    hint = attach_retry_hint(findings)
    assert hint is not None
    assert hint["rule_id"] == "KB-SNAPHU-001"
    assert hint["stage"] == "unwrap"
    assert hint["action"] == "retry"
    assert hint["params"] == {"tile_cost_thresh": 250, "min_region_size": 200}
    assert hint["note"]
    assert hint["extracted"] == {}
    assert attach_retry_hint([]) is None
    unknown = Finding(rule_id="KB-UNKNOWN", severity="WARN", message_key="k")
    assert attach_retry_hint([unknown]) is None
    # WARN finding with hint after a FAIL without hint: FAIL without hint is skipped
    no_hint = Finding(rule_id="SEL-01", severity="FAIL", message_key="k")
    hyp3 = diagnose_text(
        "<Response [400]> These jobs would cost 30 credits, but you have only 5 remaining."
    )
    hint2 = attach_retry_hint([no_hint, *hyp3])
    assert hint2 is not None and hint2["rule_id"] == "KB-HYP3-001"
    assert hint2["extracted"] == {"cost": "30", "remaining": "5"}


def test_masking_of_home_and_tokens_in_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/user")
    text = (
        "snaphu -f /home/user/work/snaphu.conf token=ABCDEFGHIJKLMNOP\n"
        "Out of memory\n"
        "Unexpected or abnormal exit of child process 7\n"
    )
    (f,) = diagnose_text(text)
    ex = f.evidence["excerpt"]
    assert "/home/user" not in ex and "~/work" in ex
    assert "ABCDEFGHIJKLMNOP" not in ex


def test_retry_hint_embedded_in_evidence_for_executor() -> None:
    from wintersar.diagnose.matcher import RETRY_HINT_KEY

    (f,) = diagnose_text(SNAPHU_TEXT)
    hint = f.evidence[RETRY_HINT_KEY]
    assert hint["rule_id"] == "KB-SNAPHU-001" and hint["stage"] == "unwrap"
    assert hint["params"]["tile_cost_thresh"] == 250
    (u,) = diagnose_text("ERROR: brand new\n")
    assert RETRY_HINT_KEY not in u.evidence


# --------------------------------------------------------------- wintersar's own run records

SNAPHU_OOM = (
    "snaphu -f snaphu.conf -t 2 2\n"
    "Out of memory: Killed process 4711 (snaphu)\n"
    "Unexpected or abnormal exit of child process 7\n"
)
SNAPHU_NODES = "Exceeded maximum number of secondary nodes\n"


def _failed_workdir(tmp_path: Path) -> Path:
    """A work dir shaped like a real failed run: engine log, StageRecord, run summary."""
    import json

    from wintersar.io.schemas import Artifacts, Plan, StageRecord
    from wintersar.pipeline.api import RunResult

    work = tmp_path / "work"
    log_dir = work / "logs" / "unwrap"
    log_dir.mkdir(parents=True)
    (log_dir / "snaphu.log").write_text(SNAPHU_OOM, encoding="utf-8")
    # engine JSON inside the log dir stays diagnosable (ADR-0027 runfiles_summary.json,
    # <stage>.findings.json): it is not a StageRecord/run summary
    (log_dir / "runfiles_summary.json").write_text(
        json.dumps({"written": "2024-01-01T00:00:00+00:00", "steps": [], "stderr": SNAPHU_NODES}),
        encoding="utf-8",
    )
    (log_dir / "unwrap.findings.json").write_text(json.dumps([]), encoding="utf-8")

    record = StageRecord(
        stage="unwrap",
        node_hash="c25d412d897d0b6c",
        engine="snaphu",
        status="failed",
        log_path=log_dir / "snaphu.log",
        extra={"error": "RuntimeError: snaphu failed", "log_excerpt": SNAPHU_OOM},
    )
    node_dir = work / "unwrap" / record.node_hash
    node_dir.mkdir(parents=True)
    (node_dir / "manifest.json").write_text(
        json.dumps(record.model_dump(mode="json"), default=str), encoding="utf-8"
    )
    runs = work / "runs"
    runs.mkdir()
    result = RunResult(
        records=[record],
        artifacts=Artifacts(),
        findings=[],
        plan=Plan(stages=[record], to_run=[record.node_hash]),
        ok=False,
        error="RuntimeError: snaphu failed",
        failed_stage="unwrap",
        run_id="20240101-000000",
    )
    (runs / "20240101-000000.json").write_text(
        json.dumps(result.to_dict(), default=str), encoding="utf-8"
    )
    return work


def test_iter_log_files_skips_wintersar_run_records(tmp_path: Path) -> None:
    """``diagnose <workdir>`` reports each failure once, from the engine log.

    ``work/<stage>/<hash>/manifest.json`` and ``work/runs/<id>.json`` embed the failed
    stage's error and log excerpt, so diagnosing them re-reported the same failure under a
    second scope with raw JSON as the excerpt. Engine JSON in a log dir is not a record.
    """
    work = _failed_workdir(tmp_path)
    found = {str(p.relative_to(work)) for p in iter_log_files(work)}
    assert found == {
        "logs/unwrap/snaphu.log",
        "logs/unwrap/runfiles_summary.json",
        "logs/unwrap/unwrap.findings.json",
    }


def test_diagnose_workdir_reports_each_failure_once(tmp_path: Path) -> None:
    work = _failed_workdir(tmp_path)
    findings = diagnose_logs(work)
    assert {(f.rule_id, f.scope) for f in findings} == {
        ("KB-SNAPHU-002", "logs/unwrap/snaphu.log"),
        ("KB-SNAPHU-001", "logs/unwrap/runfiles_summary.json"),
    }


def test_explicitly_named_record_is_still_diagnosed(tmp_path: Path) -> None:
    """Skipping applies to a directory scan only; naming the file asks for it."""
    work = _failed_workdir(tmp_path)
    manifest = work / "unwrap" / "c25d412d897d0b6c" / "manifest.json"
    assert iter_log_files(manifest) == [manifest]
    (f,) = diagnose_logs(manifest)
    assert f.rule_id == "KB-SNAPHU-002"


def test_is_run_record_only_for_our_json(tmp_path: Path) -> None:
    from wintersar.diagnose.api import RECORD_PROBE_MAX_BYTES, is_run_record

    work = _failed_workdir(tmp_path)
    assert is_run_record(work / "unwrap" / "c25d412d897d0b6c" / "manifest.json")
    assert is_run_record(work / "runs" / "20240101-000000.json")
    assert not is_run_record(work / "logs" / "unwrap" / "runfiles_summary.json")
    assert not is_run_record(work / "logs" / "unwrap" / "unwrap.findings.json")
    assert not is_run_record(work / "logs" / "unwrap" / "snaphu.log")
    # a manifest.json that is not ours (no marker keys) is diagnosed like any other JSON
    other = tmp_path / "manifest.json"
    other.write_text('{"name": "not ours", "error": "ERROR: boom"}', encoding="utf-8")
    assert not is_run_record(other)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert not is_run_record(broken)
    assert not is_run_record(tmp_path / "missing.json")
    assert RECORD_PROBE_MAX_BYTES > 0
