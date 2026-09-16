"""research/experiments.py — YAML experiments end-to-end on tiny synthetic data
(results JSON + Markdown table; requires-skip for the R-15 A/B, ADR-0060/0064)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from wintersar.research import experiments as ex
from wintersar.research.repr_phase import ResearchError

TINY_REPR = {
    "name": "tiny_repr",
    "kind": "repr_phase",
    "description": "tiny",
    "factor": 4,
    "seeds": [0, 1],
    "data": {"kind": "synthetic_slc_stack", "n_dates": 6, "shape": [24, 24], "pair": [0, 2]},
    "methods": [
        {"name": "ml"},
        {"name": "coh_weighted", "params": {"p": 2.0}},
        {"name": "shp", "params": {"window": 5}},
        {"name": "phase_link", "params": {"link_method": "emi"}, "label": "pl-emi"},
        {"name": "filtered", "params": {"window": 8}},
    ],
    "metrics": ["phase_rmse_rad", "mean_magnitude", "wall_s"],
}

TINY_STITCH = {
    "name": "tiny_stitch",
    "kind": "stitching",
    "factor": 2,
    "seeds": [0],
    "data": {"shape": [40, 40], "rows": 2, "cols": 2, "overlap": 6, "max_offset_cycles": 2},
    "methods": ["coarse_ref", {"name": "overlap_consensus", "params": {"consensus": "median"}}],
    "metrics": [
        "offsets_exact",
        "n_wrong_offsets",
        "seam_boundaries_with_jump",
        "unwrap_error_fraction",
    ],
}


def _write(tmp_path: Path, spec: dict) -> Path:
    p = tmp_path / f"{spec['name']}.yaml"
    p.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    return p


def test_repr_phase_experiment_end_to_end(tmp_path):
    exp = ex.load_experiment(_write(tmp_path, TINY_REPR))
    assert [m.key for m in exp.methods] == [
        "ml",
        "coh_weighted(p=2.0)",
        "shp(window=5)",
        "pl-emi",
        "filtered(window=8)",
    ]
    res = ex.run_experiment(exp, tmp_path / "out", docs_dir=tmp_path / "docs")
    assert res.status == "ok" and len(res.rows) == 10
    assert res.json_path.exists() and res.md_path.exists() and res.docs_md_path.exists()
    payload = json.loads(res.json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["ml"]["phase_rmse_rad"]["n"] == 2
    assert all(
        0 <= payload["summary"][m]["phase_rmse_rad"]["mean"] < 3.2 for m in payload["summary"]
    )
    md = res.md_path.read_text(encoding="utf-8")
    assert "| ml |" in md and "pl-emi" in md and "tiny_repr.json" in md
    assert md.count("\n#") <= 2  # title (+ optional section), no prose claims
    assert (tmp_path / "docs" / "tiny_repr.json").exists()


def test_stitching_experiment_end_to_end(tmp_path):
    exp = ex.load_experiment(_write(tmp_path, TINY_STITCH))
    res = ex.run_experiment(exp, tmp_path / "out")
    assert res.status == "ok" and len(res.rows) == 2
    for r in res.rows:
        assert r["offsets_exact"] == 1.0 and r["n_wrong_offsets"] == 0
        assert r["seam_boundaries_with_jump"] == 0.0
    assert "| coarse_ref |" in res.md_path.read_text(encoding="utf-8")


def test_synthetic_igram_dataset_skips_stack_methods(tmp_path):
    spec = dict(
        TINY_REPR,
        name="igram_only",
        data={"kind": "synthetic_igram", "shape": [32, 32], "looks": 16},
    )
    res = ex.run_experiment(ex.load_experiment(_write(tmp_path, spec)), tmp_path / "out")
    statuses = {r["method"]: r["status"] for r in res.rows}
    assert statuses["ml"] == "ok" and statuses["shp(window=5)"] == "skipped"
    assert "skipped" in res.md_path.read_text(encoding="utf-8")


def test_requires_skip_and_bundled_yamls(tmp_path):
    bundled = {p.stem: p for p in ex.list_bundled()}
    assert {"S_synth_repr_phase", "S_synth_stitching", "S_synth_seq_estimator_ab"} <= set(bundled)
    ab = ex.load_experiment(bundled["S_synth_seq_estimator_ab"])
    assert ab.requires == ["dolphin", "mintpy"] and ab.protocol
    missing = ex.missing_requirements(ab)
    assert "dolphin" in missing  # never installed here (rule 11.2 / CLAUDE.md)
    res = ex.run_experiment(ab, tmp_path / "out")
    assert res.status == "skipped" and res.reason == "requires" and res.missing == missing
    md = res.md_path.read_text(encoding="utf-8")
    assert "dolphin" in md and "MintPy" in md and "|" not in md.split("\n")[0]
    # the other two bundled experiments parse and validate
    for name in ("S_synth_repr_phase", "S_synth_stitching"):
        e = ex.load_experiment(bundled[name])
        assert e.methods and e.seeds and e.metrics


def test_manual_protocol_skip_when_requirements_present(tmp_path, monkeypatch):
    ab = ex.load_experiment(ex.bundled_experiment_dir() / "S_synth_seq_estimator_ab.yaml")
    monkeypatch.setattr(ex, "missing_requirements", lambda _e: [])
    res = ex.run_experiment(ab, tmp_path / "out")
    assert res.status == "skipped" and res.reason == "manual_protocol"


@pytest.mark.parametrize(
    ("patch", "detail"),
    [
        ({"kind": "bogus"}, "kind"),
        ({"methods": [{"name": "nope"}]}, "unknown methods"),
        ({"methods": []}, "empty"),
        ({"metrics": ["speed"]}, "unknown metrics"),
        ({"factor": 0}, "factor"),
    ],
)
def test_invalid_yaml_is_res_008(tmp_path, patch, detail):
    spec = dict(TINY_STITCH, name="bad", **patch)
    with pytest.raises(ResearchError) as ei:
        ex.load_experiment(_write(tmp_path, spec))
    assert ei.value.rule_id == "RES-008" and detail in ei.value.params["detail"]
    p = tmp_path / "notmap.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ResearchError):
        ex.load_experiment(p)


def test_summarise_is_nan_aware():
    rows = [
        {"method": "a", "x": 1.0},
        {"method": "a", "x": 3.0},
        {"method": "a", "x": float("nan")},
        {"method": "b", "y": 2.0},
    ]
    s = ex.summarise(rows, ["x", "y"])
    assert s["a"]["x"]["mean"] == 2.0 and s["a"]["x"]["n"] == 2 and "y" not in s["a"]
    assert s["b"]["y"]["std"] == 0.0
