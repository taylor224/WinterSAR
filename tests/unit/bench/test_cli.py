"""``wintersar bench`` through the mounted CLI (plan §4.5: honours --json / --lang)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from wintersar.cli import app

runner = CliRunner()
SITE = str(Path(__file__).resolve().parents[3] / "benchmarks" / "sites" / "S_synthetic.yaml")


def _json(result) -> dict:
    assert result.output.strip(), result.output
    return json.loads(result.output)


def test_bench_json_fake_runner(tmp_path: Path) -> None:
    out = tmp_path / "res.json"
    r = runner.invoke(
        app,
        [
            "--json",
            "bench",
            "--site",
            SITE,
            "--runner",
            "fake",
            "--repeats",
            "1",
            "--out",
            str(out),
        ],
    )
    assert r.exit_code == 0, r.output
    data = _json(r)
    assert data["ok"] and data["command"] == "bench"
    assert set(data["data"]["stages"]) == {
        "fetch",
        "coregister",
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    }
    assert data["data"]["repeats"] == 1 and out.exists()


def test_bench_compare_and_fail_on_regression(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    r = runner.invoke(
        app,
        [
            "--json",
            "bench",
            "--site",
            SITE,
            "--runner",
            "fake",
            "--repeats",
            "1",
            "--out",
            str(base),
        ],
    )
    assert r.exit_code == 0, r.output
    slow = json.loads(base.read_text())
    for s in slow["stages"].values():
        s["wall_time_s"] = 1e-9
    (tmp_path / "slow.json").write_text(json.dumps(slow))
    md = tmp_path / "cmp.md"
    r = runner.invoke(
        app,
        [
            "--lang",
            "en",
            "bench",
            "--site",
            SITE,
            "--runner",
            "fake",
            "--repeats",
            "1",
            "--out",
            str(tmp_path / "cur.json"),
            "--compare",
            str(tmp_path / "slow.json"),
            "--markdown",
            str(md),
        ],
    )
    assert r.exit_code == 0, r.output  # WARN only without --fail-on-regression
    assert "BENCH-001" in r.output and md.exists() and "regression" in md.read_text()
    r = runner.invoke(
        app,
        [
            "--json",
            "bench",
            "--site",
            SITE,
            "--runner",
            "fake",
            "--repeats",
            "1",
            "--out",
            str(tmp_path / "cur2.json"),
            "--compare",
            str(tmp_path / "slow.json"),
            "--fail-on-regression",
        ],
    )
    assert r.exit_code == 1, r.output
    data = _json(r)
    assert not data["ok"] and data["findings"][0]["rule_id"] == "BENCH-001"
    assert data["data"]["compare"]["ok"] is False


def test_bench_rich_output_korean(tmp_path: Path) -> None:
    r = runner.invoke(
        app,
        [
            "bench",
            "--site",
            SITE,
            "--runner",
            "fake",
            "--repeats",
            "1",
            "--out",
            str(tmp_path / "k.json"),
        ],
    )
    assert r.exit_code == 0, r.output
    assert (
        "벤치마크 시작" in r.output and "단계별 측정값" in r.output and "폐합 위상 RMS" in r.output
    )


def test_bench_bad_site_and_runner(tmp_path: Path) -> None:
    r = runner.invoke(app, ["--json", "bench", "--site", str(tmp_path / "missing.yaml")])
    assert r.exit_code == 2
    assert _json(r)["findings"][0]["rule_id"] == "BENCH-004"
    r = runner.invoke(app, ["bench", "--site", SITE, "--runner", "warp"])
    assert r.exit_code == 2


def test_bench_template_site_is_refused(tmp_path: Path) -> None:
    site = str(Path(SITE).parent / "S.yaml")
    r = runner.invoke(app, ["--json", "bench", "--site", site, "--out", str(tmp_path / "t.json")])
    assert r.exit_code == 1
    ids = {f["rule_id"] for f in _json(r)["findings"]}
    assert {"BENCH-002", "BENCH-004"} <= ids
