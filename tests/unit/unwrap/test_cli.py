"""``wintersar unwrap plan|run`` (plan §4.5: every command honours --json)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from typer.testing import CliRunner

from wintersar.cli import app

runner = CliRunner()


def _json(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_plan_json_single_tile():
    res = runner.invoke(
        app,
        [
            "--json",
            "unwrap",
            "plan",
            "--shape",
            "2000",
            "2000",
            "--n",
            "20",
            "--memory-gb",
            "64",
            "--cores",
            "10",
        ],
    )
    payload = _json(res)
    assert payload["ok"] and payload["command"] == "unwrap plan"
    plan = payload["data"]["plan"]
    assert plan["rows"] == 1 and plan["cols"] == 1
    assert plan["n_parallel"] == 10
    assert payload["data"]["machine"]["cores"] == 10
    assert payload["data"]["reasons"]


def test_plan_json_tiled_and_explicit_tiles():
    res = runner.invoke(
        app,
        [
            "--json",
            "unwrap",
            "plan",
            "--shape",
            "30000",
            "30000",
            "--n",
            "5",
            "--memory-gb",
            "64",
            "--cores",
            "10",
            "--available",
            "snaphu",
        ],
    )
    plan = _json(res)["data"]["plan"]
    assert plan["n_tiles"] > 1 and plan["method"] == "snaphu"
    res2 = runner.invoke(
        app,
        [
            "--json",
            "unwrap",
            "plan",
            "--shape",
            "1000",
            "1000",
            "--tiles",
            "2x3",
            "--overlap",
            "0.1",
            "--min-overlap-px",
            "5",
            "--memory-gb",
            "16",
            "--cores",
            "4",
            "--method",
            "truth",
        ],
    )
    plan2 = _json(res2)["data"]["plan"]
    assert (plan2["rows"], plan2["cols"]) == (2, 3) and plan2["method"] == "truth"


def test_plan_table_output_in_both_languages():
    for lang in ("ko", "en"):
        res = runner.invoke(
            app,
            [
                "--lang",
                lang,
                "unwrap",
                "plan",
                "--shape",
                "500",
                "500",
                "--memory-gb",
                "8",
                "--cores",
                "2",
            ],
        )
        assert res.exit_code == 0, res.output
        assert "snaphu" in res.stdout


def test_plan_rejects_bad_method():
    res = runner.invoke(app, ["unwrap", "plan", "--shape", "10", "10", "--method", "bogus"])
    assert res.exit_code == 2


def test_run_identity_json(synth_stack_npz: Path, tmp_path: Path):
    out = tmp_path / "out"
    res = runner.invoke(
        app,
        [
            "--json",
            "unwrap",
            "run",
            str(synth_stack_npz),
            "--out",
            str(out),
            "--method",
            "identity",
            "--memory-gb",
            "8",
            "--cores",
            "4",
        ],
    )
    payload = _json(res)
    assert payload["ok"]
    assert Path(payload["data"]["artifacts"]["unw"]).exists()
    stats = payload["data"]["stats"]
    assert stats["method"] == "identity" and stats["n_pairs"] > 0
    assert (out / "stats.json").exists()
    unw = np.load(out / "unw.npz")["unw"]
    assert unw.shape[0] == stats["n_pairs"]


def test_run_table_output(synth_stack_npz: Path, tmp_path: Path):
    out = tmp_path / "out"
    res = runner.invoke(
        app,
        [
            "--lang",
            "en",
            "unwrap",
            "run",
            str(synth_stack_npz),
            "--out",
            str(out),
            "--method",
            "truth",
            "--tiles",
            "2x2",
            "--min-overlap-px",
            "4",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "Unwrapping done" in res.stdout


def test_run_reports_missing_backend(synth_stack_npz: Path, tmp_path: Path):
    res = runner.invoke(
        app,
        [
            "--json",
            "unwrap",
            "run",
            str(synth_stack_npz),
            "--out",
            str(tmp_path / "o"),
            "--method",
            "snaphu",
        ],
    )
    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    assert payload["ok"] is False
