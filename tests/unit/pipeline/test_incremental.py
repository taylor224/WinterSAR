"""PERF-06 incremental mode building blocks (ADR-0080): the per-pair manifest, the hash
rule (pair set is data, upstream identity by node hash), freshness and the plan counts."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from tests.unit.pipeline._support import write_fake_config
from wintersar.engines.fake import FakeEngine, fake_pairs
from wintersar.io.schemas import Artifact, Artifacts, StageRecord
from wintersar.pipeline import cache, incremental
from wintersar.pipeline.dag import Dag, node_hash

# ---------------------------------------------------------------------- hash rule


def test_hash_rule_drops_data_keys_and_data_inputs_for_incremental_stages() -> None:
    assert incremental.is_incremental_stage("interferogram")
    assert incremental.is_incremental_stage("unwrap")
    assert not incremental.is_incremental_stage("timeseries")
    params = {"n_dates": 6, "shape": [24, 24], "seed": 0}
    assert incremental.hash_view("interferogram", params) == {"shape": [24, 24], "seed": 0}
    assert incremental.data_view("interferogram", params) == {"n_dates": 6}
    assert incremental.hash_view("timeseries", params) == params  # untouched
    assert incremental.data_view("timeseries", params) == {}
    inputs = {"coreg_manifest": "c", "stack": "s"}
    assert incremental.identity_inputs("interferogram", inputs) == {"coreg_manifest": "c"}
    assert incremental.identity_inputs("timeseries", inputs) == inputs


def test_node_hash_is_stable_when_a_date_is_added_but_not_when_a_param_changes() -> None:
    six = node_hash(
        "interferogram", {"n_dates": 6, "seed": 0}, {"coreg_manifest": "c"}, "fake", "1"
    )
    seven = node_hash(
        "interferogram", {"n_dates": 7, "seed": 0}, {"coreg_manifest": "c"}, "fake", "1"
    )
    assert six == seven
    assert (
        node_hash("interferogram", {"n_dates": 6, "seed": 1}, {"coreg_manifest": "c"}, "fake", "1")
        != six
    )
    assert (
        node_hash("interferogram", {"n_dates": 6, "seed": 0}, {"coreg_manifest": "d"}, "fake", "1")
        != six
    )
    # a non-incremental stage keeps every key in its hash
    a = node_hash("timeseries", {"n_dates": 6}, {"unw": "u"}, "fake", "1")
    assert node_hash("timeseries", {"n_dates": 7}, {"unw": "u"}, "fake", "1") != a


def test_dag_identity_chain_and_freshness(tmp_path: Path, cache_dir: Path) -> None:
    """fetch..unwrap take their identity from the producer's node hash; a cached result is
    fresh only when the recorded input content and data keys match the request."""
    cfg = write_fake_config(tmp_path)
    dag = Dag(cfg)
    dag.build({"interferogram": {"n_dates": 6, "shape": [16, 16]}})
    by = {n.stage: n for n in dag.nodes}
    for stage, upstream, name in (
        ("coregister", "fetch", "slc_manifest"),
        ("interferogram", "coregister", "coreg_manifest"),
        ("multilook", "interferogram", "igrams"),
        ("unwrap", "multilook", "igrams"),
    ):
        assert by[stage].identity_inputs == {name: by[upstream].node_hash}, stage
    assert by["timeseries"].node_hash is None  # content-hashed: unknown until unwrap ran
    # the same DAG with one more date resolves to the very same node hashes
    other = Dag(cfg)
    other.build({"interferogram": {"n_dates": 7, "shape": [16, 16]}})
    for stage in ("fetch", "coregister", "interferogram", "multilook", "unwrap"):
        assert other.node(stage).node_hash == by[stage].node_hash, stage
    # freshness: same data + same input content -> fresh; different data -> stale
    node = by["interferogram"]
    art = Artifact(name="coreg_manifest", path=tmp_path / "c.json", sha256="abc")
    node.input_hashes = {"coreg_manifest": "abc"}
    record = StageRecord(
        stage="interferogram",
        node_hash=node.node_hash or "",
        params={"n_dates": 6, "shape": [16, 16]},
        inputs={"coreg_manifest": "abc"},
        status="ok",
    )
    available = Artifacts().add(art)
    assert Dag.fresh(node, record, available)
    assert not Dag.fresh(other.node("interferogram"), record, available)  # n_dates 7 vs 6
    changed = Artifacts().add(art.model_copy(update={"sha256": "xyz"}))
    assert not Dag.fresh(node, record, changed)  # upstream content changed
    ts = by["timeseries"]
    assert Dag.fresh(ts, record, changed)  # non-incremental: the hash already says it all


def test_expected_pairs_from_engine_hook_meta_and_stack(tmp_path: Path, cache_dir: Path) -> None:
    eng = FakeEngine()
    params = {"n_dates": 7}
    hook = incremental.expected_pairs(eng, "interferogram", params, Artifacts())
    assert hook == [p.key for p in fake_pairs(params)]
    assert len(hook or []) == 18  # 7 dates, 12-day repeat, <= 48 days
    assert incremental.expected_pairs(eng, "unwrap", {}, Artifacts()) is None
    meta = Artifacts().add(Artifact(name="igrams", path=tmp_path / "i", meta={"pairs": ["a_b"]}))
    assert incremental.expected_pairs(None, "unwrap", {}, meta) == ["a_b"]
    stack = tmp_path / "stack.json"
    stack.write_text(
        json.dumps(
            {
                "relative_orbit": 61,
                "flight_direction": "DESCENDING",
                "polarization": "VV",
                "burst_ids": ["b"],
                "dates": ["2024-01-01", "2024-01-13"],
                "coverage_of_aoi": 1.0,
                "pairs": [
                    {
                        "reference": "2024-01-01",
                        "secondary": "2024-01-13",
                        "temporal_baseline_days": 12,
                    },
                    "20240113_20240125",
                ],
            }
        ),
        encoding="utf-8",
    )
    arts = Artifacts().add(Artifact(name="stack", path=stack, kind="json"))
    assert incremental.expected_pairs(None, "interferogram", {}, arts) == [
        "20240101_20240113",
        "20240113_20240125",
    ]
    assert incremental.expected_pairs(None, "interferogram", {}, Artifacts()) is None
    # the DAG propagates the interferogram's pair set down the chain
    dag = Dag(write_fake_config(tmp_path))
    dag.build({"interferogram": {"n_dates": 7}})
    assert dag.node("unwrap").pairs_expected == hook
    assert dag.node("multilook").pairs_expected == hook
    assert dag.node("fetch").pairs_expected is None


# ---------------------------------------------------------------------- pair manifest


def test_pair_cache_roundtrip_lookup_store_and_prune(tmp_path: Path) -> None:
    node_dir = tmp_path / "unwrap" / "h"
    pc = incremental.PairCache(incremental.pairs_dir(node_dir), "unwrap")
    assert pc.lookup("a_b", "id1") is None  # nothing done yet
    p = pc.path_for("a_b")
    np.savez(p, unw=np.zeros((2, 2), np.float32))
    pc.store("a_b", "id1", p, meta={"n_conncomp": 1})
    stale = pc.path_for("c_d")
    np.savez(stale, unw=np.ones((2, 2), np.float32))
    pc.store("c_d", "id2", stale)
    manifest = pc.save()
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    assert raw["stage"] == "unwrap" and set(raw["pairs"]) == {"a_b", "c_d"}
    assert raw["pairs"]["a_b"]["path"] == "a_b.npz"  # relative, portable
    assert raw["pairs"]["a_b"]["meta"] == {"n_conncomp": 1} and raw["pairs"]["a_b"]["file_hash"]
    assert pc.summary() == {"pairs_reused": [], "pairs_computed": ["a_b", "c_d"]}

    loaded = incremental.PairCache.load(node_dir)
    assert set(loaded.done) == {"a_b", "c_d"} and loaded.stage == "unwrap"
    done = loaded.done_entries()
    assert Path(done["a_b"]["path"]).is_absolute()
    # engine side: identity must match and the file must be intact
    engine = incremental.PairCache.from_params(
        {
            incremental.PAIRS_DIR_KEY: str(incremental.pairs_dir(node_dir)),
            incremental.PAIRS_DONE_KEY: done,
        },
        "unwrap",
    )
    assert engine is not None
    assert engine.lookup("a_b", "id1") == p and engine.reused == ["a_b"]
    assert engine.entry("a_b") is not None and engine.entry("a_b").meta == {"n_conncomp": 1}
    assert engine.lookup("a_b", "other-identity") is None  # parameter/input changed
    assert engine.lookup("nope", "id1") is None
    stale.write_bytes(b"tampered")
    assert engine.lookup("c_d", "id2") is None  # file hash no longer matches
    # saving with only a_b prunes the orphaned c_d result file
    engine.save()
    assert p.exists() and not stale.exists()
    assert incremental.PairCache.from_params({"x": 1}) is None  # sub-caching not requested


def test_pair_content_hash_and_identity() -> None:
    a = np.arange(6, dtype=np.float32).reshape(2, 3)
    assert incremental.pair_content_hash(a) == incremental.pair_content_hash(a.copy())
    assert incremental.pair_content_hash(a) != incremental.pair_content_hash(a + 1)
    assert incremental.pair_content_hash(a) != incremental.pair_content_hash(a.astype(np.float64))
    assert incremental.pair_content_hash(a, None) != incremental.pair_content_hash(a)
    assert incremental.pair_identity("p", "i") == incremental.pair_identity("p", "i")
    assert incremental.pair_identity("p", "i") != incremental.pair_identity("q", "i")


def test_pair_counts_and_partial_cache(tmp_path: Path) -> None:
    done = {"a": object(), "b": object()}
    assert incremental.pair_counts(["a", "b", "c"], done) == {
        "expected": 3,
        "done": 2,
        "cached": 2,
        "new": 1,
    }
    assert incremental.pair_counts(None, done) == {
        "expected": None,
        "done": 2,
        "cached": 2,
        "new": None,
    }
    empty = incremental.PartialCache(node_dir=tmp_path, record=None, done={})
    assert not empty.usable
    partial = incremental.PartialCache(
        node_dir=tmp_path,
        record=None,
        done={"a": incremental.PairEntry("a", "h", str(tmp_path / "a.npz"))},
        expected=["a", "b"],
    )
    assert partial.usable and partial.to_extra() == {
        "status": "partial",
        "expected": 2,
        "done": 1,
        "cached": 1,
        "new": 1,
    }


# ---------------------------------------------------------------------- plan status


def test_plan_shows_incremental_status_and_counts_only_in_incremental_mode(
    tmp_path: Path, cache_dir: Path
) -> None:
    from wintersar.pipeline import api

    cfg = write_fake_config(tmp_path)
    small = {"interferogram": {"n_dates": 6, "shape": [16, 16]}}
    first = api.run(cfg, param_overrides=small, until="unwrap", incremental=True)
    assert first.ok
    node_dir = cache.stage_dir(cfg.workdir, "interferogram", first.records[4].node_hash)
    assert (incremental.pairs_dir(node_dir) / incremental.PAIRS_MANIFEST).exists()

    more = {"interferogram": {"n_dates": 7, "shape": [16, 16]}}
    plan = api.plan(cfg, param_overrides=more, incremental=True)
    by = {s.stage: s for s in plan.stages}
    assert by["fetch"].extra.get("cache_hit") and by["coregister"].extra.get("cache_hit")
    for stage in ("interferogram", "multilook", "unwrap"):
        info = by[stage].extra["incremental"]
        assert info == {"status": "partial", "expected": 18, "done": 14, "cached": 14, "new": 4}, (
            stage
        )
        assert by[stage].extra.get("stale") or stage != "interferogram"
        assert not by[stage].extra.get("provisional")
    assert by["timeseries"].extra.get("provisional") and "incremental" not in by["timeseries"].extra
    infos = [f for f in plan.findings if f.rule_id == "PIPELINE-015"]
    assert [f.scope for f in infos] == ["interferogram", "multilook", "unwrap"]
    assert infos[0].params == {"stage": "interferogram", "n_cached": 14, "n_new": 4}
    assert [h for h in plan.to_run] and len(plan.cached) == 2
    # the unwrap estimate is sized by the 4 new pairs, not the whole stack
    est = plan.resources.notes["estimates"]
    assert "unwrap" in est
    # default mode: same hashes, but the stale nodes are plain to_run (full recompute)
    default = api.plan(cfg, param_overrides=more)
    dby = {s.stage: s for s in default.stages}
    assert dby["interferogram"].node_hash == by["interferogram"].node_hash
    assert "incremental" not in dby["interferogram"].extra and dby["interferogram"].extra.get(
        "stale"
    )
    assert not any(f.rule_id == "PIPELINE-015" for f in default.findings)


def test_forced_node_is_never_partial(tmp_path: Path, cache_dir: Path) -> None:
    from wintersar.pipeline import api

    cfg = write_fake_config(tmp_path)
    small = {"interferogram": {"n_dates": 5, "shape": [16, 16]}}
    assert api.run(cfg, param_overrides=small, until="interferogram", incremental=True).ok
    plan = api.plan(cfg, param_overrides=small, force=["interferogram"], incremental=True)
    rec = next(s for s in plan.stages if s.stage == "interferogram")
    assert rec.extra["forced"] and "incremental" not in rec.extra


@pytest.mark.parametrize("incremental_mode", [False, True])
def test_run_result_reports_incremental_flag(
    tmp_path: Path, cache_dir: Path, incremental_mode: bool
) -> None:
    from wintersar.pipeline import api

    cfg = write_fake_config(tmp_path)
    result = api.run(
        cfg,
        param_overrides={"interferogram": {"n_dates": 4, "shape": [8, 8]}},
        until="fetch",
        incremental=incremental_mode,
    )
    assert result.ok and result.incremental is incremental_mode
    assert result.to_dict()["incremental"] is incremental_mode
    assert result.pair_summary() == {"reused": 0, "computed": 0, "stage": None, "by_stage": {}}
    assert result.partial == []


# ---------------------------------------------------------------------- robustness


def _corrupt_pair_manifest(node_dir: Path, how: str) -> Path:
    manifest = incremental.pairs_dir(node_dir) / incremental.PAIRS_MANIFEST
    if how == "truncated":
        manifest.write_text(manifest.read_text(encoding="utf-8")[:200], encoding="utf-8")
    elif how == "pairs_not_a_mapping":
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["pairs"] = ["a_b"]
        manifest.write_text(json.dumps(raw), encoding="utf-8")
    elif how == "not_an_object":
        manifest.write_text("[]", encoding="utf-8")
    return manifest


@pytest.mark.parametrize("how", ["truncated", "pairs_not_a_mapping", "not_an_object"])
def test_unreadable_pair_manifest_is_discarded_but_named(tmp_path: Path, how: str) -> None:
    """Present-but-unreadable is not the same as absent: ``done`` is empty either way, but
    only the former sets ``manifest_error`` (the plan/run report it as PIPELINE-017)."""
    node_dir = tmp_path / "unwrap" / "h"
    pc = incremental.PairCache(incremental.pairs_dir(node_dir), "unwrap")
    p = pc.path_for("a_b")
    np.savez(p, unw=np.zeros((2, 2), np.float32))
    pc.store("a_b", "id1", p)
    pc.save()
    assert incremental.PairCache.load(node_dir).manifest_error is None
    _corrupt_pair_manifest(node_dir, how)
    loaded = incremental.PairCache.load(node_dir, "unwrap")
    assert loaded.done == {} and loaded.manifest_error, how
    assert loaded.manifest_error == ("InvalidManifest" if how != "truncated" else "JSONDecodeError")
    # a missing manifest stays silent (the normal first incremental run)
    empty = incremental.PairCache.load(tmp_path / "nowhere", "unwrap")
    assert empty.done == {} and empty.manifest_error is None
    partial = incremental.PartialCache(
        node_dir=node_dir, record=None, done={}, expected=["a_b"], manifest_error="JSONDecodeError"
    )
    assert not partial.usable
    assert partial.to_extra() == {
        "status": "recompute",
        "expected": 1,
        "done": 0,
        "cached": 0,
        "new": 1,
        "manifest": "unreadable",
        "manifest_error": "JSONDecodeError",
    }
    finding = incremental.manifest_finding("unwrap", "JSONDecodeError", partial.counts)
    assert finding.rule_id == "PIPELINE-017" and finding.severity == "INFO"
    assert finding.params == {"stage": "unwrap", "error": "JSONDecodeError", "n_pairs": 1}


def test_plan_and_run_report_an_unreadable_pair_manifest(tmp_path: Path, cache_dir: Path) -> None:
    """End to end: truncate ``pairs/manifest.json`` of the unwrap node, then plan/run with one
    more date. Both say why every pair is recomputed (PIPELINE-017, ``manifest:
    unreadable``); the run rebuilds the manifest so the next increment reuses again."""
    from wintersar.pipeline import api

    cfg = write_fake_config(tmp_path)
    six = {"interferogram": {"n_dates": 6, "shape": [16, 16]}}
    seven = {"interferogram": {"n_dates": 7, "shape": [16, 16]}}
    first = api.run(cfg, param_overrides=six, until="unwrap", incremental=True)
    assert first.ok
    unw_hash = next(r.node_hash for r in first.records if r.stage == "unwrap")
    node_dir = cache.stage_dir(cfg.workdir, "unwrap", unw_hash)
    _corrupt_pair_manifest(node_dir, "truncated")

    plan = api.plan(cfg, param_overrides=seven, incremental=True)
    by = {s.stage: s for s in plan.stages}
    assert by["unwrap"].extra["incremental"] == {
        "status": "recompute",
        "expected": 18,
        "done": 0,
        "cached": 0,
        "new": 18,
        "manifest": "unreadable",
        "manifest_error": "JSONDecodeError",
    }
    assert by["multilook"].extra["incremental"]["status"] == "partial"  # untouched upstream
    infos = {f.scope: f for f in plan.findings if f.rule_id == "PIPELINE-017"}
    assert list(infos) == ["unwrap"] and infos["unwrap"].severity == "INFO"
    assert infos["unwrap"].params == {"stage": "unwrap", "error": "JSONDecodeError", "n_pairs": 18}
    assert not any(f.rule_id == "PIPELINE-015" and f.scope == "unwrap" for f in plan.findings)

    result = api.run(cfg, param_overrides=seven, until="unwrap", incremental=True)
    assert result.ok, result.findings
    rec = next(r for r in result.records if r.stage == "unwrap")
    info = rec.extra["incremental"]
    assert info["manifest"] == "unreadable" and info["manifest_error"] == "JSONDecodeError"
    assert info["reused"] == 0 and info["computed"] == 18 and info["supported"]
    assert [f.scope for f in result.findings if f.rule_id == "PIPELINE-017"] == ["unwrap"]
    # the manifest is rebuilt: a third run with one more date reuses the 18 pairs
    eight = {"interferogram": {"n_dates": 8, "shape": [16, 16]}}
    third = api.run(cfg, param_overrides=eight, until="unwrap", incremental=True)
    assert third.ok
    rec3 = next(r for r in third.records if r.stage == "unwrap")
    assert rec3.extra["incremental"]["reused"] == 18 and rec3.extra["incremental"]["computed"] == 4
    assert "manifest" not in rec3.extra["incremental"]
    assert not any(f.rule_id == "PIPELINE-017" for f in third.findings)


def test_entry_without_file_hash_is_never_reused_and_discard_forgets_a_hit(
    tmp_path: Path,
) -> None:
    """``intact()`` verifies something or says no; ``discard()`` turns an unloadable hit back
    into a miss so the engine recomputes and re-stores the pair."""
    node_dir = tmp_path / "unwrap" / "h"
    pc = incremental.PairCache(incremental.pairs_dir(node_dir), "unwrap")
    p = pc.path_for("a_b")
    np.savez(p, unw=np.zeros((2, 2), np.float32))
    entry = pc.store("a_b", "id1", p)
    assert entry.intact()
    unverifiable = incremental.PairEntry("a_b", "id1", str(p), file_hash=None)
    assert not unverifiable.intact()
    assert not incremental.PairEntry("a_b", "id1", str(tmp_path / "gone.npz"), "x").intact()
    pc.save()
    manifest = incremental.pairs_dir(node_dir) / incremental.PAIRS_MANIFEST
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["pairs"]["a_b"]["file_hash"] = None
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    engine = incremental.PairCache.from_params(
        {
            incremental.PAIRS_DIR_KEY: str(incremental.pairs_dir(node_dir)),
            incremental.PAIRS_DONE_KEY: incremental.PairCache.load(node_dir).done_entries(),
        },
        "unwrap",
    )
    assert engine is not None and engine.lookup("a_b", "id1") is None  # cannot be verified
    # a verified hit that turns out unloadable: discard -> recompute -> store
    fresh = incremental.PairCache.from_params(
        {
            incremental.PAIRS_DIR_KEY: str(incremental.pairs_dir(node_dir)),
            incremental.PAIRS_DONE_KEY: {"a_b": entry.to_dict()},
        },
        "unwrap",
    )
    assert fresh is not None and fresh.lookup("a_b", "id1") == p and fresh.reused == ["a_b"]
    fresh.discard("a_b")
    assert fresh.reused == [] and fresh.entry("a_b") is None
    fresh.store("a_b", "id1", p)
    assert fresh.summary() == {"pairs_reused": [], "pairs_computed": ["a_b"]}
    fresh.discard("nope")  # unknown keys are ignored


def test_only_pair_stages_take_part_in_the_sub_cache_contract(
    tmp_path: Path, cache_dir: Path
) -> None:
    """fetch/coregister follow the hash rule but have no per-pair results: they are never
    asked for sub-caching, never get a partial cache and never a PIPELINE-016."""
    assert incremental.PAIR_STAGES == ("interferogram", "multilook", "unwrap")
    assert all(incremental.is_incremental_stage(s) for s in incremental.PAIR_STAGES)
    assert incremental.is_incremental_stage("fetch") and not incremental.is_pair_stage("fetch")
    cfg = write_fake_config(tmp_path)
    dag = Dag(cfg, incremental=True)
    dag.build({"interferogram": {"n_dates": 6, "shape": [16, 16]}})
    for stage in ("fetch", "coregister"):
        node = dag.node(stage)
        assert node.incremental and not node.pair_stage
        assert node.pairs_expected is None
        # even a stray pairs/ manifest in its directory is ignored
        node_dir = cache.stage_dir(cfg.workdir, stage, node.node_hash or "")
        pc = incremental.PairCache(incremental.pairs_dir(node_dir), stage)
        p = pc.path_for("20240101_20240113")
        np.savez(p, x=np.zeros(2))
        pc.store("20240101_20240113", "id", p)
        pc.save()
        assert dag.partial_cache(node) is None
    assert dag.node("interferogram").pair_stage and dag.node("unwrap").pair_stage
