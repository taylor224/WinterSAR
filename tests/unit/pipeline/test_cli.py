"""``wintersar plan/run/cache`` through the mounted CLI (plan §4.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tests.unit.pipeline._support import write_fake_config
from wintersar.cli import app
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
