"""Plan §6.3 item 5 / §5.9: the synthetic S site runs end-to-end through the pipeline
(fake engine, no network) in well under 60 s and produces a self-consistent
``bench_result.json`` that compares against itself without regression."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from wintersar.bench.report import compare
from wintersar.bench.runner import run_site
from wintersar.bench.sites import load_site

SITE = Path(__file__).resolve().parents[2] / "benchmarks" / "sites" / "S_synthetic.yaml"


def test_s_synthetic_site_pipeline_runner(tmp_path: Path, cache_dir: Path) -> None:
    site = load_site(SITE)
    assert site.runner == "pipeline" and site.repeats == 3 and site.max_wall_time_s == 60
    out = tmp_path / "bench_result.json"
    t0 = time.perf_counter()
    res = run_site(site, out, workdir=tmp_path / "w")
    elapsed = time.perf_counter() - t0
    assert res.ok, [f.model_dump() for f in res.findings]
    assert elapsed < 60.0, f"S_synthetic took {elapsed:.1f} s (CI budget 60 s)"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["repeats"] == 3 and data["runner"] == "pipeline"
    assert list(data["stages"]) == site.stages
    for name, s in data["stages"].items():
        assert s["n_runs"] == 3, name
        assert s["wall_time_s"] is not None and s["wall_time_s"] >= 0.0
        assert s["peak_rss_gb"] is not None and s["peak_rss_gb"] > 0.0
    assert data["total"]["wall_time_s"] < 60.0 and data["total"]["disk_peak_gb"] is not None
    assert (
        np.isfinite(data["metrics"]["closure_rms"]) and 0.0 < data["metrics"]["closure_rms"] < np.pi
    )
    assert data["metrics"]["unwrap_error_fraction"] == 0.0
    assert all(r["ok"] for r in data["runs"]) and all(r.get("run_id") for r in data["runs"])
    # three repeats used three separate work directories (no cache short-circuit)
    assert len(list((tmp_path / "w").glob("run*/work"))) == 3
    rep = compare(data, data)
    assert rep.ok and rep.to_markdown("ko")
    assert [f.rule_id for f in res.findings if f.severity == "FAIL"] == []
