"""DAG hashing and build (PERF-03; ADR-0031)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.unit.pipeline._support import write_fake_config
from wintersar.i18n import t
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
    # incremental stages take their identity from the producer's node hash (ADR-0080), so
    # the whole engine chain up to unwrap is identified before anything ran; the time-series
    # stages hash the *content* of their inputs and stay unresolved until unwrap has run
    assert by["coregister"].resolved and by["unwrap"].resolved
    assert by["coregister"].identity_inputs == {"slc_manifest": by["fetch"].node_hash}
    assert by["coregister"].input_hashes == {}  # no content yet
    assert by["timeseries"].node_hash is None  # upstream not cached yet
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


def test_node_hash_normalises_integral_floats_and_dates() -> None:
    """``--set unwrap.nproc_per_igram=1.0`` is the same parameter as a config ``1``.

    ``--set`` values are parsed by PyYAML, so an override can differ from the config value
    only in type (``1.0`` vs ``1``, ``date`` vs ISO string); hashing them as-is produced a
    cache miss and a duplicate work directory for an identical run.
    """
    a = node_hash("unwrap", {"nproc": 1}, {}, "snaphu", "1")
    assert node_hash("unwrap", {"nproc": 1.0}, {}, "snaphu", "1") == a
    assert node_hash("unwrap", {"nproc": [1.0, {"k": 2.0}]}, {}, "snaphu", "1") == node_hash(
        "unwrap", {"nproc": [1, {"k": 2}]}, {}, "snaphu", "1"
    )
    assert node_hash("unwrap", {"nproc": 1.5}, {}, "snaphu", "1") != a
    assert node_hash("unwrap", {"flag": True}, {}, "snaphu", "1") != node_hash(
        "unwrap", {"flag": 1}, {}, "snaphu", "1"
    )
    iso = node_hash("search", {"start": "2024-01-01"}, {}, None, None)
    assert node_hash("search", {"start": date(2024, 1, 1)}, {}, None, None) == iso


def _real_path_cfg(tmp_path: Path, ts_engine: str = "mintpy") -> object:
    lev = tmp_path / "leveling.csv"
    lev.write_text("date,value\n", encoding="utf-8")
    return write_fake_config(
        tmp_path,
        engine={"interferogram": "hyp3"},
        timeseries={"engine": ts_engine},
        validate={"leveling_csv": str(lev)},
    )


def test_real_path_geocode_runs_and_validate_is_not_blocked(
    tmp_path: Path, cache_dir: Path
) -> None:
    """hyp3 + mintpy + ground truth: geocode belongs to MintPy, so nothing is blocked.

    With ``geocode`` keyed on ``engine.interferogram`` the stage was skipped on every real
    path and ``velocity`` (the validate stage's required input) had no producer, so
    ``plan``/``run`` failed with PIPELINE-002 before executing anything.
    """
    dag = Dag(_real_path_cfg(tmp_path))
    dag.build()
    assert dag.node("geocode").engine == "mintpy"
    assert dag.node("geocode").skip_reason is None
    assert dag.node("validate").skip_reason is None
    assert dag.node("validate").producers["velocity"] == "geocode"
    assert not [f for n in dag.nodes for f in n.findings if f.rule_id == "PIPELINE-002"]
    assert dag.blocked() == []


def test_validate_survives_a_timeseries_engine_without_geocode(
    tmp_path: Path, cache_dir: Path
) -> None:
    """dolphin implements ``timeseries`` only: velocity is optional, not a blocker."""
    dag = Dag(_real_path_cfg(tmp_path, ts_engine="dolphin"))
    dag.build()
    assert dag.node("geocode").skip_reason == "engine_no_stage"
    assert dag.node("validate").skip_reason is None
    assert dag.blocked() == []


def test_missing_ground_truth_file_warns_at_plan_time(tmp_path: Path, cache_dir: Path) -> None:
    """PIPELINE-012: validate.leveling_csv that does not exist is a plan-time WARN.

    Without it the run executes the whole DAG and only the last stage discovers VAL-006.
    """
    cfg = write_fake_config(tmp_path, validate={"leveling_csv": str(tmp_path / "nope.csv")})
    dag = Dag(cfg)
    dag.build()
    node = dag.node("validate")
    assert node.skip_reason is None
    warn = [f for f in node.findings if f.rule_id == "PIPELINE-012"]
    assert len(warn) == 1 and warn[0].severity == "WARN"
    assert warn[0].params["key"] == "validate.leveling_csv"
    assert "nope.csv" in warn[0].params["path"]
    assert not any(f.is_fail for f in node.findings)
    # an existing file produces no finding
    (tmp_path / "nope.csv").write_text("date,value\n", encoding="utf-8")
    dag2 = Dag(write_fake_config(tmp_path, validate={"leveling_csv": str(tmp_path / "nope.csv")}))
    dag2.build()
    assert [f.rule_id for f in dag2.node("validate").findings] == []


def test_from_stage_without_cache_uses_the_no_cache_message(
    tmp_path: Path, cache_dir: Path
) -> None:
    """PIPELINE-002 must not claim that 'fetch' needs an input 'produced by fetch'."""
    dag = Dag(write_fake_config(tmp_path))
    dag.build(from_stage="timeseries")
    finding = dag.blocked()[0].findings[0]
    assert finding.message_key == "pipeline.PIPELINE-002.cause_no_cache"
    assert finding.fix_key == "pipeline.PIPELINE-002.fix_no_cache"
    assert finding.params == {"stage": "fetch", "artifact": "slc_manifest"}
    assert finding.evidence["reason"] == "no_cached_result"
    assert "producer" not in finding.evidence  # no leftover '*' / '?' placeholders
    cause, fix = (
        t(finding.message_key, "en", **finding.params),
        t(finding.fix_key, "en", **finding.params),
    )
    assert "fetch" in cause and "--until fetch" in fix
