"""Markdown report (cause -> fix -> refs) and the ``wintersar diagnose`` command."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from tests.unit.diagnose.conftest import LOGS_DIR
from wintersar.diagnose.api import attach_retry_hint, diagnose_logs
from wintersar.diagnose.report import findings_report
from wintersar.i18n import t


def test_report_cause_before_fix_before_refs(fake_home: str) -> None:
    findings = diagnose_logs(LOGS_DIR / "hyp3" / "insufficient_credits.log")
    hint = attach_retry_hint(findings)
    for lang in ("ko", "en"):
        md = findings_report(findings, lang, source="/home/user/work/logs", retry_hint=hint)
        assert md.startswith("# " + t("diagnose.report.title", lang))
        assert "KB-HYP3-001" in md
        i_cause = md.index(t("common.cause", lang))
        i_fix = md.index(t("common.fix", lang))
        i_refs = md.index(t("common.refs", lang))
        assert i_cause < i_fix < i_refs
        assert "120" in md and "35" in md  # extracted params rendered into the cause
        assert "hyp3-docs.asf.alaska.edu" in md
        assert "```text" in md
        assert "/home/user" not in md and "~/work/logs" in md
        assert t("diagnose.report.retry_hint", lang) in md


def test_report_no_findings_and_unknown(fake_home: str) -> None:
    md = findings_report([], "en")
    assert t("cli.no_findings", "en") in md
    findings = diagnose_logs(LOGS_DIR / "generic" / "unknown_traceback.log")
    md = findings_report(findings, "ko")
    assert "KB-UNKNOWN" in md
    assert "FakeEngineFailureError" in md
    assert "docs/kb/index.md" in md


def test_cli_diagnose_json_and_report(tmp_path: Path, fake_home: str) -> None:
    from wintersar.cli import app

    out = tmp_path / "report.md"
    r = CliRunner().invoke(
        app,
        ["--json", "diagnose", str(LOGS_DIR / "snaphu" / "secondary_nodes.log"), "--out", str(out)],
    )
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["command"] == "diagnose"
    assert payload["ok"] is False
    assert [f["rule_id"] for f in payload["findings"]] == ["KB-SNAPHU-001"]
    assert payload["data"]["retry_hint"]["params"]["tile_cost_thresh"] == 250
    assert payload["data"]["n_files"] == 1
    assert out.exists() and "KB-SNAPHU-001" in out.read_text(encoding="utf-8")


def test_cli_diagnose_text_output_and_engine_option(fake_home: str) -> None:
    from wintersar.cli import app

    r = CliRunner().invoke(
        app, ["--lang", "en", "diagnose", str(LOGS_DIR / "mintpy"), "--engine", "mintpy"]
    )
    assert r.exit_code == 0, r.output
    assert "KB-MINTPY-001" in r.output and "KB-MINTPY-002" in r.output
    assert "Retry hint" in r.output


def test_cli_diagnose_errors() -> None:
    from wintersar.cli import app

    r = CliRunner().invoke(app, ["diagnose", "/definitely/not/here.log"])
    assert r.exit_code == 2
    r = CliRunner().invoke(app, ["diagnose", str(LOGS_DIR), "--engine", "gamma"])
    assert r.exit_code == 2
    r = CliRunner().invoke(app, ["diagnose"])
    assert r.exit_code == 2


def test_cli_list_kb() -> None:
    from wintersar.cli import app

    r = CliRunner().invoke(app, ["--json", "diagnose", "--list-kb"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    ids = {e["id"] for e in payload["data"]["entries"]}
    assert "KB-SNAPHU-001" in ids and payload["data"]["n"] >= 14
    r = CliRunner().invoke(app, ["diagnose", "--list-kb"])
    assert r.exit_code == 0 and "KB-SNAPHU-001" in r.output


def test_cli_empty_directory(tmp_path: Path) -> None:
    from wintersar.cli import app

    r = CliRunner().invoke(app, ["--json", "diagnose", str(tmp_path)])
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["findings"] == [] and payload["data"]["n_files"] == 0
