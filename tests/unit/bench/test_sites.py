"""bench.sites: YAML validation, templates, config construction."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from wintersar.bench.sites import ENGINE_STAGES, Site, build_config, load_site, site_config_mapping


def test_synthetic_site_yaml_is_runnable(synthetic_site: Site) -> None:
    s = synthetic_site
    assert s.name == "S_synthetic" and s.size == "S" and s.synthetic and not s.network
    assert not s.is_template and s.placeholders == []
    assert s.repeats == 3 and s.max_wall_time_s == 60
    assert s.param_overrides["interferogram"]["n_dates"] == 6
    assert s.param_overrides["interferogram"]["shape"] == [96, 96]
    assert s.stages == list(ENGINE_STAGES) and s.metrics == ["closure_rms", "unwrap_error_fraction"]
    assert (
        s.config["engine"]["interferogram"] == "fake" and s.config["timeseries"]["engine"] == "fake"
    )
    assert s.path is not None and s.path.name == "S_synthetic.yaml"


@pytest.mark.parametrize("name", ["S.yaml", "M.yaml", "L.yaml"])
def test_real_site_templates(sites_dir: Path, name: str) -> None:
    s = load_site(sites_dir / name)
    assert s.network and not s.synthetic and s.is_template
    assert any("AOI" in p for p in s.placeholders)
    assert s.repeats == 3 and s.regression_threshold == 0.15
    if name == "S.yaml":
        assert s.ground_truth and s.ground_truth.startswith("<") and s.ground_truth_path is None
        assert "gt_rmse" in s.metrics


def test_site_validation_errors() -> None:
    base = {"name": "x", "size": "S", "synthetic": True}
    with pytest.raises(ValueError, match="unknown stages"):
        Site.model_validate({**base, "stages": ["fetch", "nope"]})
    with pytest.raises(ValueError, match="unknown metrics"):
        Site.model_validate({**base, "metrics": ["rmse"]})
    with pytest.raises(ValueError, match="network"):
        Site.model_validate({**base, "network": True})
    with pytest.raises(ValueError, match="aoi"):
        Site.model_validate({"name": "r", "size": "M", "synthetic": False})
    with pytest.raises(ValueError):
        Site.model_validate({**base, "repeats": 0})
    with pytest.raises(ValueError):
        Site.model_validate({**base, "size": "XL"})
    with pytest.raises(ValueError):
        Site.model_validate({**base, "unknown_key": 1})
    with pytest.raises(ValueError, match="empty"):
        Site.model_validate({**base, "stages": []})


def test_load_site_rejects_non_mapping(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="mapping"):
        load_site(p)


def test_resolve_relative_paths(tmp_path: Path) -> None:
    p = tmp_path / "site.yaml"
    (tmp_path / "gt.csv").write_text("site_id,lat,lon,date,method\n")
    p.write_text(
        yaml.safe_dump(
            {
                "name": "k",
                "size": "S",
                "synthetic": True,
                "ground_truth": "gt.csv",
                "baseline": "/abs/base.json",
            }
        )
    )
    s = load_site(p)
    assert s.ground_truth_path == (tmp_path / "gt.csv").resolve()
    assert s.baseline_path == Path("/abs/base.json")


def test_site_config_mapping_and_build_config(tmp_path: Path, synthetic_site: Site) -> None:
    m = site_config_mapping(synthetic_site, tmp_path)
    assert m["engine"]["interferogram"] == "fake" and m["compute"]["cores"] == 2
    assert m["unwrap"]["coherence_threshold"] == 0.3 and m["project"]["name"] == "S_synthetic"
    cfg = build_config(synthetic_site, tmp_path / "w")
    assert cfg.engine.interferogram == "fake" and cfg.timeseries.engine == "fake"
    assert cfg.aoi.exists() and (tmp_path / "w" / "config.yaml").exists()
    assert cfg.workdir == (tmp_path / "w" / "work").resolve()
    real = Site.model_validate(
        {
            "name": "r",
            "size": "M",
            "aoi": "/a/aoi.geojson",
            "time_range": {"start": "2024-01-01", "end": "2024-06-30"},
            "ground_truth": "/g/gt.csv",
            "config": {"engine": {"interferogram": "hyp3"}},
        }
    )
    mr = site_config_mapping(real, tmp_path)
    assert mr["aoi"] == "/a/aoi.geojson" and mr["time_range"] == {
        "start": "2024-01-01",
        "end": "2024-06-30",
    }
    assert mr["validate"]["leveling_csv"] == "/g/gt.csv" and mr["engine"]["interferogram"] == "hyp3"
    summ = synthetic_site.summary()
    assert summ["is_template"] is False and "notes" not in summ
