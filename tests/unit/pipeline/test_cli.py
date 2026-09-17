"""``wintersar plan/run/cache`` through the mounted CLI (plan §4.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tests.unit.pipeline._support import write_fake_config
from wintersar.cli import app
from wintersar.pipeline import cache
from wintersar.pipeline.cli import parse_set

runner = CliRunner()
SET_SMALL = ["--set", "interferogram.n_dates=5", "--set", "interferogram.shape=[24, 24]"]


def _json(result) -> dict:
    assert result.output.strip(), result.output
    return json.loads(result.output)


def test_parse_set_nested_and_yaml_scalars() -> None:
    ov = parse_set(
        [
            "unwrap.coherence_threshold=0.4",
            "interferogram.shape=[24,24]",
            "unwrap.unwrap.cost=smooth",
            "timeseries.flag=true",
        ]
    )
    assert ov == {
        "unwrap": {"coherence_threshold": 0.4, "unwrap": {"cost": "smooth"}},
        "interferogram": {"shape": [24, 24]},
        "timeseries": {"flag": True},
    }
    assert parse_set(None) == {}
    with pytest.raises(typer.Exit):
        parse_set(["novalue"])
    with pytest.raises(typer.Exit):
        parse_set(["nostage=1"])
    with pytest.raises(typer.Exit):
        parse_set(["nope.key=1"])


def test_plan_json_then_run_then_cached(tmp_path: Path, cache_dir: Path) -> None:
    cfg = write_fake_config(tmp_path)
    cfg_path = str(cfg.config_path)
    r = runner.invoke(app, ["--json", "plan", "--config", cfg_path, *SET_SMALL])
    assert r.exit_code == 0, r.output
    data = _json(r)
    assert data["ok"] and data["command"] == "plan"
    assert len(data["data"]["to_run"]) == 8 and data["data"]["cached"] == []
    assert [s["stage"] for s in data["data"]["stages"]][:3] == ["search", "precheck", "fetch"]

    r = runner.invoke(app, ["--json", "run", "--config", cfg_path, *SET_SMALL])
    assert r.exit_code == 0, r.output
    data = _json(r)
    assert data["ok"] and data["data"]["ok"]
    assert "velocity" in data["data"]["artifacts"]
    assert Path(
        data["data"]["artifacts"]["velocity"]["path"].replace("~", str(Path.home()))
    ).exists()

    r = runner.invoke(app, ["--json", "plan", "--config", cfg_path, *SET_SMALL])
    data = _json(r)
    assert len(data["data"]["cached"]) == 8 and data["data"]["to_run"] == []

    # human output paths
    r = runner.invoke(app, ["plan", "--config", cfg_path, *SET_SMALL])
    assert r.exit_code == 0, r.output
    assert "geocode" in r.output
    r = runner.invoke(app, ["run", "--config", cfg_path, *SET_SMALL])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["run", "--config", cfg_path, "--dry-run", *SET_SMALL])
    assert r.exit_code == 0, r.output


def test_run_failure_exits_1_with_findings(tmp_path: Path, cache_dir: Path) -> None:
    cfg = write_fake_config(tmp_path)
    cfg_path = str(cfg.config_path)
    args = ["run", "--config", cfg_path, *SET_SMALL, "--set", "unwrap.fail_stage=unwrap"]
    r = runner.invoke(app, ["--json", *args])
    assert r.exit_code == 1, r.output
    data = _json(r)
    assert not data["ok"] and data["data"]["failed_stage"] == "unwrap"
    assert "PIPELINE-001" in {f["rule_id"] for f in data["findings"]}
    r = runner.invoke(app, args)
    assert r.exit_code == 1
    assert "PIPELINE-001" in r.output


def test_unknown_stage_and_missing_config(tmp_path: Path, cache_dir: Path) -> None:
    cfg = write_fake_config(tmp_path)
    r = runner.invoke(app, ["plan", "--config", str(cfg.config_path), "--until", "nope"])
    assert r.exit_code == 2
    r = runner.invoke(app, ["plan", "--config", str(tmp_path / "missing.yaml")])
    assert r.exit_code == 2
    (tmp_path / "bad.yaml").write_text("project: {name: x}\n", encoding="utf-8")
    r = runner.invoke(app, ["plan", "--config", str(tmp_path / "bad.yaml")])
    assert r.exit_code == 2


def test_cache_ls_and_gc(tmp_path: Path, cache_dir: Path) -> None:
    cfg = write_fake_config(tmp_path)
    cfg_path = str(cfg.config_path)
    r = runner.invoke(app, ["cache", "ls", "--config", cfg_path])
    assert r.exit_code == 0 and "work" in r.output  # empty cache message
    assert runner.invoke(app, ["run", "--config", cfg_path, *SET_SMALL]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["run", "--config", cfg_path, *SET_SMALL, "--set", "unwrap.coherence_threshold=0.5"],
        ).exit_code
        == 0
    )
    r = runner.invoke(app, ["--json", "cache", "ls", "--config", cfg_path])
    entries = _json(r)["data"]["entries"]
    assert sum(1 for e in entries if e["stage"] == "unwrap") == 2
    assert len(entries) == 12
    r = runner.invoke(
        app, ["--json", "cache", "ls", "--workdir", str(cfg.workdir), "--stage", "unwrap"]
    )
    assert len(_json(r)["data"]["entries"]) == 2
    r = runner.invoke(
        app, ["--json", "cache", "gc", "--config", cfg_path, "--keep", "1", "--dry-run"]
    )
    data = _json(r)["data"]
    assert data["dry_run"] and len(data["removed"]) == 4
    r = runner.invoke(app, ["cache", "gc", "--config", cfg_path, "--keep", "1"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["cache", "ls", "--config", cfg_path])
    assert r.exit_code == 0
    r = runner.invoke(app, ["--json", "cache", "ls", "--config", cfg_path])
    assert len(_json(r)["data"]["entries"]) == 8


# ---------------------------------------------------------------------- usage errors (exit 2)


def test_usage_errors_emit_the_json_envelope(tmp_path: Path, cache_dir: Path) -> None:
    """A ``--json`` caller must never get an empty stdout: exit 2 carries PIPELINE-014."""
    cfg = write_fake_config(tmp_path)
    cfg_path = str(cfg.config_path)
    (tmp_path / "bad.yaml").write_text("project: {name: x}\n", encoding="utf-8")
    cases = [
        (["--json", "plan", "--config", str(tmp_path / "missing.yaml")], "plan"),
        (["--json", "plan", "--config", str(tmp_path / "bad.yaml")], "plan"),
        (["--json", "plan", "--config", cfg_path, "--until", "nope"], "plan"),
        (["--json", "run", "--config", cfg_path, "--force", "nope"], "run"),
        (["--json", "run", "--config", cfg_path, "--set", "novalue"], "run"),
        (["--json", "run", "--config", cfg_path, "--set", "nope.key=1"], "run"),
        (["--json", "cache", "ls", "--config", str(tmp_path / "missing.yaml")], "cache ls"),
        (["--json", "cache", "gc", "--config", str(tmp_path / "missing.yaml")], "cache gc"),
    ]
    for args, command in cases:
        r = runner.invoke(app, args)
        assert r.exit_code == 2, (args, r.output)
        data = _json(r)
        assert data["ok"] is False and data["command"] == command, args
        finding = data["findings"][0]
        assert finding["rule_id"] == "PIPELINE-014" and finding["severity"] == "FAIL"
        assert finding["scope"] == command and finding["params"]["command"] == command
        assert finding["params"]["detail"], args
    # without --json the message still goes to stderr only
    r = runner.invoke(app, ["plan", "--config", str(tmp_path / "missing.yaml")])
    assert r.exit_code == 2 and not r.stdout.strip().startswith("{")


def test_cache_ls_shows_orphan_node_dirs(tmp_path: Path, cache_dir: Path) -> None:
    """A run killed before its first manifest leaves a node dir that must stay visible."""
    cfg = write_fake_config(tmp_path)
    cfg_path = str(cfg.config_path)
    assert runner.invoke(app, ["run", "--config", cfg_path, *SET_SMALL]).exit_code == 0
    orphan = cache.stage_dir(cfg.workdir, "unwrap", "0" * 16)
    out, _ = cache.prepare_node_dir(orphan)
    (out / "half-written.npz").write_bytes(b"x" * 1000)
    r = runner.invoke(app, ["--json", "cache", "ls", "--config", cfg_path, "--stage", "unwrap"])
    assert r.exit_code == 0, r.output
    entries = {e["node_hash"]: e for e in _json(r)["data"]["entries"]}
    assert "0" * 16 in entries
    assert entries["0" * 16]["status"] == "orphan" and entries["0" * 16]["orphan"] is True
    assert entries["0" * 16]["engine"] is None and entries["0" * 16]["finished_at"] is None
    r = runner.invoke(app, ["cache", "ls", "--config", cfg_path, "--stage", "unwrap"])
    assert r.exit_code == 0, r.output  # the table must not trip over the missing manifest
    assert "orphan" in r.output
    assert runner.invoke(app, ["cache", "gc", "--config", cfg_path, "--keep", "3"]).exit_code == 0
    assert not orphan.exists()


def test_cache_gc_max_size_budget(tmp_path: Path, cache_dir: Path) -> None:
    """PERF-03: ``--keep`` cannot bound the work-directory size; ``--max-size`` can."""
    cfg = write_fake_config(tmp_path)
    cfg_path = str(cfg.config_path)
    assert runner.invoke(app, ["run", "--config", cfg_path, *SET_SMALL]).exit_code == 0
    r = runner.invoke(
        app, ["--json", "cache", "gc", "--config", cfg_path, "--max-size", "100", "--dry-run"]
    )
    data = _json(r)["data"]
    assert data["max_size_gb"] == 100.0 and data["removed"] == []  # everything fits
    r = runner.invoke(app, ["--json", "cache", "gc", "--config", cfg_path, "--max-size", "0"])
    data = _json(r)["data"]
    assert data["kept"] == [] and len(data["removed"]) == 8 and data["freed_bytes"] > 0
    r = runner.invoke(app, ["--json", "cache", "ls", "--config", cfg_path])
    assert _json(r)["data"]["entries"] == []


def test_help_is_english_whatever_the_language() -> None:
    """``register()`` runs before ``--lang`` is parsed, so help must not go through t()."""
    for args in (
        ["--lang", "en", "plan", "--help"],
        ["--lang", "en", "run", "--help"],
        ["--lang", "en", "cache", "--help"],
        ["--lang", "en", "cache", "gc", "--help"],
        ["plan", "--help"],
    ):
        r = runner.invoke(app, args)
        assert r.exit_code == 0, r.output
        assert not any("가" <= ch <= "힣" for ch in r.output), (args, r.output)
    top = runner.invoke(app, ["--lang", "en", "--help"]).output
    assert "Build the DAG" in top and "Run the pipeline" in top
