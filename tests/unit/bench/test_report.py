"""bench.report: before/after comparison, regression threshold, markdown rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wintersar.bench.report import CompareReport, check_regression, compare, delta_pct, load_result


def _result(unwrap: float, ts: float = 2.0, rss: float = 0.5, closure: float = 0.4) -> dict:
    return {
        "site": {"name": "S_synthetic"},
        "git_sha": "abcdef0123456789",
        "created_at": "2026-09-16T00:00:00+00:00",
        "stages": {
            "unwrap": {
                "wall_time_s": unwrap,
                "peak_rss_gb": rss,
                "disk_peak_gb": 0.01,
                "network_bytes": 0,
            },
            "timeseries": {
                "wall_time_s": ts,
                "peak_rss_gb": rss,
                "disk_peak_gb": None,
                "network_bytes": 1e6,
            },
        },
        "total": {"wall_time_s": unwrap + ts, "peak_rss_gb": rss},
        "metrics": {"closure_rms": closure, "unwrap_error_fraction": 0.0},
    }


def test_regression_detected_only_above_threshold() -> None:
    rep = compare(_result(1.0), _result(1.16))
    assert not rep.ok and [r.name for r in rep.regressions] == ["unwrap"]
    assert rep.row("unwrap", "wall_time_s").delta_pct == pytest.approx(16.0)
    assert compare(_result(1.0), _result(1.14)).ok
    assert compare(_result(1.0), _result(1.16), threshold=0.2).ok
    # a speed-up is never a regression; RSS growth is not a regression by default
    assert compare(_result(1.0), _result(0.5, rss=2.0)).ok
    assert not compare(_result(1.0), _result(1.0, rss=2.0), regress_on=("peak_rss_gb",)).ok
    # total row is compared too
    rep2 = compare(_result(1.0, ts=1.0), _result(1.0, ts=1.5))
    assert {r.name for r in rep2.regressions} == {"timeseries", "total"}


def test_missing_stages_and_undefined_deltas() -> None:
    before = _result(1.0)
    after = _result(1.0)
    del after["stages"]["timeseries"]
    after["stages"]["geocode"] = {"wall_time_s": 0.1}
    rep = compare(before, after)
    assert rep.ok and rep.stage_names() == ["unwrap", "timeseries", "geocode", "total"]
    assert rep.row("timeseries", "wall_time_s").after is None
    assert (
        rep.row("geocode", "wall_time_s").before is None
        and rep.row("geocode", "wall_time_s").delta_pct is None
    )
    assert (
        delta_pct(0.0, 1.0) == float("inf")
        and delta_pct(0.0, 0.0) is None
        and delta_pct(None, 1.0) is None
    )
    md = rep.to_markdown("en")
    assert "| geocode | - | 0.10 | - |" in md


def test_markdown_in_both_languages() -> None:
    rep = compare(_result(1.0, closure=0.4), _result(1.3, closure=0.5))
    ko = rep.to_markdown("ko")
    en = rep.to_markdown("en")
    assert "### 기준선 대비 비교" in ko and "회귀 1건" in ko and "unwrap **!**" in ko
    assert "### Comparison with baseline" in en and "1 regression(s): unwrap (+30%)" in en
    assert "| Stage |" in en and "| 단계 |" in ko
    assert "Closure phase RMS (rad) | 0.4000 | 0.5000 | +25.0%" in en
    assert "abcdef01" in en and "S_synthetic" in en
    assert compare(_result(1.0), _result(1.0)).to_markdown("en").rstrip().endswith("baseline.")


def test_to_dict_and_check_regression(tmp_path: Path) -> None:
    b, a = _result(1.0), _result(2.0)
    rep = compare(b, a)
    d = rep.to_dict()
    assert d["ok"] is False and d["threshold"] == 0.15 and d["before"]["site"] == "S_synthetic"
    assert any(r["name"] == "unwrap" and r["regression"] for r in d["rows"])
    assert d["metrics"][0]["name"] == "closure_rms"
    pb, pa = tmp_path / "b.json", tmp_path / "a.json"
    pb.write_text(json.dumps(b))
    pa.write_text(json.dumps(a))
    ok, regs = check_regression(pb, pa)
    assert not ok and regs[0].name == "unwrap"
    assert load_result(pb)["stages"]["unwrap"]["wall_time_s"] == 1.0
    (tmp_path / "bad.json").write_text("[1, 2]")
    with pytest.raises(ValueError, match="bench_result"):
        load_result(tmp_path / "bad.json")
    empty = CompareReport(threshold=0.1)
    assert empty.ok and empty.to_markdown("en")
