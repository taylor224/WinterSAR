"""DAG hashing and build (PERF-03; ADR-0031)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.pipeline._support import write_fake_config
from wintersar.pipeline import dag as dagmod
from wintersar.pipeline.dag import Dag, canonical_params, node_hash


def test_node_hash_is_deterministic_and_ignores_private_keys() -> None:
    a = node_hash("unwrap", {"b": 1, "a": [1, 2]}, {"igrams": "x"}, "fake", "1")
    b = node_hash("unwrap", {"a": [1, 2], "b": 1, "_out_dir": "/tmp"}, {"igrams": "x"}, "fake", "1")
    assert a == b
    assert len(a) == 16
    assert node_hash("unwrap", {"a": 2}, {"igrams": "x"}, "fake", "1") != a
    assert node_hash("unwrap", {"b": 1, "a": [1, 2]}, {"igrams": "y"}, "fake", "1") != a
    assert node_hash("unwrap", {"b": 1, "a": [1, 2]}, {"igrams": "x"}, "fake", "2") != a
    assert node_hash("unwrap", {"b": 1, "a": [1, 2]}, {"igrams": "x"}, "snaphu", "1") != a
    assert node_hash("multilook", {"b": 1, "a": [1, 2]}, {"igrams": "x"}, "fake", "1") != a


def test_canonical_params_merges_overrides(tmp_path: Path) -> None:
    cfg = write_fake_config(tmp_path)
    base = canonical_params(cfg, "unwrap")
    assert base["unwrap"]["coherence_threshold"] == 0.3
    merged = canonical_params(
        cfg, "unwrap", {"n": 5, "unwrap": {"coherence_threshold": 0.5}, "_private": 1}
    )
    assert merged["n"] == 5
    assert merged["unwrap"]["coherence_threshold"] == 0.5
    assert merged["unwrap"]["cost"] == "defo"  # nested merge keeps siblings
    assert "_private" not in merged
    assert list(merged) == sorted(merged)


def test_canonical_params_sections_per_stage(tmp_path: Path) -> None:
    cfg = write_fake_config(tmp_path)
    assert set(canonical_params(cfg, "interferogram")) == {"engine"}
    assert set(canonical_params(cfg, "timeseries")) == {"timeseries"}
    assert "unwrap" not in canonical_params(cfg, "interferogram")


def test_build_fake_path_marks_skips_and_resolution(tmp_path: Path, cache_dir: Path) -> None:
    cfg = write_fake_config(tmp_path)
    dag = Dag(cfg)
    nodes = dag.build({"interferogram": {"n_dates": 5}})
    by = {n.stage: n for n in nodes}
    assert by["search"].skip_reason == "fake_path"
    assert by["precheck"].skip_reason == "fake_path"
    assert by["validate"].skip_reason == "not_configured"
    assert by["validate"].findings[0].rule_id == "PIPELINE-010"
    assert by["fetch"].resolved and by["fetch"].status == "to_run"  # no inputs -> hash known
    assert by["coregister"].node_hash is None  # upstream not cached yet
    assert by["interferogram"].params["n_dates"] == 5
    assert all(n.engine == "fake" for n in nodes if not n.spec.is_python)
    assert all(n.engine_version for n in nodes if not n.spec.is_python)
    assert [n.stage for n in dag.to_run()] == [
        "fetch",
        "coregister",
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    ]
    assert dag.cached() == []
    assert [n.stage for n in dag.skipped()] == ["search", "precheck", "validate"]


def test_force_propagates_downstream(tmp_path: Path, cache_dir: Path) -> None:
    dag = Dag(write_fake_config(tmp_path))
    dag.build(force=["interferogram"])
    forced = [n.stage for n in dag.nodes if n.forced]
    assert forced == [
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    ]
    assert dag.downstream("unwrap") == ["timeseries", "corrections", "geocode"]
    assert dag.downstream("geocode") == []


def test_build_rejects_unknown_stage(tmp_path: Path, cache_dir: Path) -> None:
    dag = Dag(write_fake_config(tmp_path))
    with pytest.raises(KeyError):
        dag.build(until="nope")
    with pytest.raises(KeyError):
        dag.build(force=["nope"])
    with pytest.raises(KeyError):
        dag.build({"nope": {}})


def test_unregistered_engine_blocks_node(tmp_path: Path, cache_dir: Path, monkeypatch) -> None:
    cfg = write_fake_config(tmp_path, engine={"interferogram": "hyp3"})

    def missing(name: str):
        raise KeyError(name)

    monkeypatch.setattr(dagmod, "get_engine", missing)
    dag = Dag(cfg)
    dag.build(until="interferogram")
    fetch = dag.node("fetch")
    assert fetch.status == "blocked"
    assert fetch.findings[0].rule_id == "PIPELINE-005"
    assert fetch.engine_version is None


def test_until_limits_window(tmp_path: Path, cache_dir: Path) -> None:
    dag = Dag(write_fake_config(tmp_path))
    dag.build(until="unwrap")
    assert [n.stage for n in dag.nodes][-1] == "unwrap"
    assert dag.node("unwrap").engine == "fake"
    with pytest.raises(KeyError):
        dag.node("timeseries")


def test_from_stage_without_cache_blocks(tmp_path: Path, cache_dir: Path) -> None:
    dag = Dag(write_fake_config(tmp_path))
    dag.build(from_stage="timeseries")
    blocked = dag.blocked()
    assert blocked and blocked[0].stage == "fetch"
    assert blocked[0].findings[0].rule_id == "PIPELINE-002"
