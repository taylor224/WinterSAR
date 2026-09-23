"""PERF-06 incremental update mode end-to-end on the fake path (ADR-0080).

Adding one date (``n_dates`` 6 -> 7) must compute only the interferogram pairs that touch
the new date, unwrap only those, re-run the time series and leave everything about the old
pairs to the cache. A parameter change keeps the PERF-03 semantics (every pair again).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.pipeline._support import EngineSpy, spy_fake_engine, write_fake_config
from wintersar.engines.fake import FakeEngine, fake_pairs
from wintersar.pipeline import api, cache, incremental

SHAPE = [16, 16]


def _overrides(n_dates: int, **unwrap: Any) -> dict[str, dict[str, Any]]:
    ov: dict[str, dict[str, Any]] = {"interferogram": {"n_dates": n_dates, "shape": SHAPE}}
    if unwrap:
        ov["unwrap"] = dict(unwrap)
    return ov


class PairSpy:
    """Records the pair keys the fake engine actually synthesised / unwrapped."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.synth: list[str] = []
        self.unwrapped: list[str] = []
        real_synth = FakeEngine.synth_pair
        real_unwrap = FakeEngine.unwrap_pair
        spy = self

        def synth_pair(self_: FakeEngine, spec: Any, pair: Any) -> Any:
            spy.synth.append(pair.key)
            return real_synth(self_, spec, pair)

        def unwrap_pair(
            self_: FakeEngine, coherence: Any, mask: Any, truth: Any, thr: float
        ) -> Any:
            spy.unwrapped.append("?")  # the key is not a parameter: count calls
            return real_unwrap(self_, coherence, mask, truth, thr)

        monkeypatch.setattr(FakeEngine, "synth_pair", synth_pair)
        monkeypatch.setattr(FakeEngine, "unwrap_pair", unwrap_pair)

    def clear(self) -> None:
        self.synth.clear()
        self.unwrapped.clear()


@pytest.fixture
def cfg(tmp_path: Path, cache_dir: Path):
    return write_fake_config(tmp_path)


@pytest.fixture
def engine_spy(monkeypatch: pytest.MonkeyPatch) -> EngineSpy:
    return spy_fake_engine(monkeypatch)


@pytest.fixture
def pair_spy(monkeypatch: pytest.MonkeyPatch) -> PairSpy:
    return PairSpy(monkeypatch)


def _by_stage(result: api.RunResult) -> dict[str, Any]:
    return {r.stage: r for r in result.records}


def test_adding_one_date_recomputes_only_its_pairs(cfg, engine_spy, pair_spy) -> None:
    six = fake_pairs({"n_dates": 6})
    seven = fake_pairs({"n_dates": 7})
    new_pairs = sorted({p.key for p in seven} - {p.key for p in six})
    assert len(six) == 14 and len(seven) == 18 and len(new_pairs) == 4
    new_date = "20240313"  # 2024-01-01 + 6 * 12 days
    assert all(k.endswith(new_date) for k in new_pairs)

    # --- run 1: six dates, incremental mode seeds the per-pair caches
    first = api.run(cfg, param_overrides=_overrides(6), incremental=True)
    assert first.ok, first.findings
    assert sorted(pair_spy.synth) == sorted(p.key for p in six)
    assert len(pair_spy.unwrapped) == 14
    by = _by_stage(first)
    for stage in ("interferogram", "multilook", "unwrap"):
        info = by[stage].extra["incremental"]
        assert info["supported"] and info["reused"] == 0 and info["computed"] == 14, stage
        assert by[stage].extra["pairs"] == [p.key for p in six]
        node_dir = cache.stage_dir(cfg.workdir, stage, by[stage].node_hash)
        assert (incremental.pairs_dir(node_dir) / incremental.PAIRS_MANIFEST).exists()
    assert "incremental" not in by["timeseries"].extra  # not an incremental stage
    hashes6 = {r.stage: r.node_hash for r in first.records}

    # --- plan for seven dates: 14 cached / 4 new on every incremental stage
    plan = api.plan(cfg, param_overrides=_overrides(7), incremental=True)
    pby = {s.stage: s for s in plan.stages}
    for stage in ("interferogram", "multilook", "unwrap"):
        assert pby[stage].extra["incremental"] == {
            "status": "partial",
            "expected": 18,
            "done": 14,
            "cached": 14,
            "new": 4,
        }, stage
        assert pby[stage].node_hash == hashes6[stage]  # the node hash did not move
    assert pby["fetch"].extra.get("cache_hit") and pby["coregister"].extra.get("cache_hit")
    counts = {f.scope: f.params for f in plan.findings if f.rule_id == "PIPELINE-015"}
    assert counts == {
        s: {"stage": s, "n_cached": 14, "n_new": 4}
        for s in ("interferogram", "multilook", "unwrap")
    }

    # --- run 2: seven dates
    engine_spy.clear()
    pair_spy.clear()
    second = api.run(cfg, param_overrides=_overrides(7), incremental=True)
    assert second.ok, second.findings
    assert engine_spy.stages == [
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    ]
    assert sorted(pair_spy.synth) == new_pairs  # only the pairs touching the new date
    assert len(pair_spy.unwrapped) == 4
    by2 = _by_stage(second)
    assert [r.stage for r in second.cached] == ["fetch", "coregister"]
    for stage in ("interferogram", "multilook", "unwrap"):
        assert by2[stage].node_hash == hashes6[stage]
        info = by2[stage].extra["incremental"]
        assert info["reused"] == 14 and info["computed"] == 4, (stage, info)
        assert sorted(info["computed_pairs"]) == new_pairs
        assert by2[stage].extra["pairs"] == [p.key for p in seven]
        assert not by2[stage].extra.get("cache_hit")
    assert by2["timeseries"].node_hash != hashes6["timeseries"]  # re-inverted on new inputs
    assert second.partial and [r.stage for r in second.partial] == [
        "interferogram",
        "multilook",
        "unwrap",
    ]
    # per stage, never summed over stages (every pair stage handles the same 18 pairs)
    summary = second.pair_summary()
    assert summary["by_stage"] == {
        s: {"reused": 14, "computed": 4} for s in ("interferogram", "multilook", "unwrap")
    }
    assert (summary["reused"], summary["computed"], summary["stage"]) == (14, 4, "interferogram")
    assert second.to_dict()["pairs"] == summary
    ids = [f.rule_id for f in second.findings]
    assert "PIPELINE-016" not in ids  # the fake engine supports the contract
    # fetch/coregister are hash-rule stages only: never asked for sub-caching, never
    # reported as "not supporting" it, no incremental block on their records
    for r in [*first.records, *second.records]:
        if r.stage in ("fetch", "coregister"):
            assert "incremental" not in r.extra, (r.stage, r.extra)
            assert not [f for f in r.findings if f.rule_id == "PIPELINE-016"]
    assert "PIPELINE-016" not in [f.rule_id for f in first.findings]
    # the engine received the executor's private keys
    ig_params = next(p for s, p in engine_spy.calls if s == "interferogram")
    assert (
        incremental.PAIRS_DIR_KEY in ig_params and len(ig_params[incremental.PAIRS_DONE_KEY]) == 14
    )

    # --- outputs: seven dates everywhere, old pairs bitwise identical to run 1
    with np.load(second.artifacts["timeseries"].path) as ts:
        assert len(ts["dates"]) == 7 and ts["displacement_m"].shape[0] == 7
        assert str(ts["dates"][-1]) == "2024-03-13"
    assert second.artifacts["velocity"].meta["n_dates"] == 7
    assert second.artifacts["velocity"].meta["end"] == "2024-03-13"
    vel = np.load(second.artifacts["velocity"].path)
    assert vel.shape == tuple(SHAPE) and vel.min() < 0
    # run 1's unw.npz was overwritten in place (same node dir): the artifact of run 1 now
    # holds the seven-date stack, whose pair set contains every six-date pair
    assert first.artifacts["unw"].path == second.artifacts["unw"].path
    with np.load(second.artifacts["unw"].path) as b:
        assert list(b["pairs"]) == [p.key for p in seven]
        assert {p.key for p in six} < {str(k) for k in b["pairs"]}
        assert b["unw"].shape[0] == 18 and b["conncomp"].dtype == np.uint8

    # --- run 3: nothing to do
    engine_spy.clear()
    pair_spy.clear()
    third = api.run(cfg, param_overrides=_overrides(7), incremental=True)
    assert (
        third.ok and engine_spy.stages == [] and pair_spy.synth == [] and pair_spy.unwrapped == []
    )
    assert [r.stage for r in third.cached] == [
        "fetch",
        "coregister",
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    ]
    assert api.plan(cfg, param_overrides=_overrides(7), incremental=True).to_run == []


def test_incremental_result_equals_a_full_recompute(cfg, pair_spy) -> None:
    """The per-pair synthesis is pure, so 6 -> 7 incrementally equals 7 from scratch."""
    api.run(cfg, param_overrides=_overrides(6), incremental=True, until="unwrap")
    inc = api.run(cfg, param_overrides=_overrides(7), incremental=True, until="unwrap")
    assert inc.ok
    fresh_dir = Path(cfg.workdir).parent / "fresh"
    fresh_dir.mkdir()
    fresh_cfg = write_fake_config(fresh_dir)
    full = api.run(fresh_cfg, param_overrides=_overrides(7), until="unwrap")
    assert full.ok
    for name in ("igrams", "unw"):
        with np.load(inc.artifacts[name].path) as a, np.load(full.artifacts[name].path) as b:
            assert list(a["pairs"]) == list(b["pairs"])
            for key in a.files:
                np.testing.assert_array_equal(a[key], b[key], err_msg=f"{name}:{key}")


def test_parameter_change_still_recomputes_every_pair(cfg, engine_spy, pair_spy) -> None:
    """PERF-03 semantics survive: ``unwrap.coherence_threshold`` is a parameter, so the
    unwrap node moves to a new hash and every pair is unwrapped again; upstream is reused."""
    assert api.run(cfg, param_overrides=_overrides(6), incremental=True).ok
    engine_spy.clear()
    pair_spy.clear()
    changed = api.run(cfg, param_overrides=_overrides(6, coherence_threshold=0.6), incremental=True)
    assert changed.ok
    assert engine_spy.stages == ["unwrap", "timeseries", "corrections", "geocode"]
    assert pair_spy.synth == [] and len(pair_spy.unwrapped) == 14
    unw = _by_stage(changed)["unwrap"]
    assert unw.extra["incremental"]["reused"] == 0 and unw.extra["incremental"]["computed"] == 14
    assert len(cache.list_records(cfg.workdir, "unwrap")) == 2  # both variants coexist
    # ... and the config-side parameter (nested under ``unwrap``) behaves the same
    engine_spy.clear()
    pair_spy.clear()
    cfg.unwrap.coherence_threshold = 0.5
    again = api.run(cfg, param_overrides=_overrides(6), incremental=True)
    assert again.ok and engine_spy.stages == ["unwrap", "timeseries", "corrections", "geocode"]
    assert len(pair_spy.unwrapped) == 14


def test_default_mode_recomputes_the_whole_stage_in_the_same_directory(
    cfg, engine_spy, pair_spy
) -> None:
    """Without ``incremental=True`` a date addition is a full recompute (node dir reused)."""
    first = api.run(cfg, param_overrides=_overrides(5))
    assert first.ok
    assert "incremental" not in _by_stage(first)["interferogram"].extra
    engine_spy.clear()
    pair_spy.clear()
    second = api.run(cfg, param_overrides=_overrides(6))
    assert second.ok
    assert engine_spy.stages == [
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    ]
    assert len(pair_spy.synth) == 14 and len(pair_spy.unwrapped) == 14  # everything again
    by1, by2 = _by_stage(first), _by_stage(second)
    assert by1["interferogram"].node_hash == by2["interferogram"].node_hash
    assert "incremental" not in by2["interferogram"].extra
    assert by2["interferogram"].extra["pairs"] == [p.key for p in fake_pairs({"n_dates": 6})]
    # no per-pair cache was written in default mode
    node_dir = cache.stage_dir(cfg.workdir, "interferogram", by2["interferogram"].node_hash)
    assert not incremental.pairs_dir(node_dir).exists()


def test_first_incremental_run_after_a_default_run_seeds_the_pair_cache(
    cfg, engine_spy, pair_spy
) -> None:
    assert api.run(cfg, param_overrides=_overrides(5)).ok  # default mode: no pairs/
    plan = api.plan(cfg, param_overrides=_overrides(6), incremental=True)
    ig = next(s for s in plan.stages if s.stage == "interferogram")
    assert ig.extra.get("stale") and "incremental" not in ig.extra  # nothing to reuse yet
    engine_spy.clear()
    pair_spy.clear()
    result = api.run(cfg, param_overrides=_overrides(6), incremental=True)
    assert result.ok and len(pair_spy.synth) == 14
    info = _by_stage(result)["interferogram"].extra["incremental"]
    assert info["supported"] and info["reused"] == 0 and info["computed"] == 14
    engine_spy.clear()
    pair_spy.clear()
    assert api.run(cfg, param_overrides=_overrides(7), incremental=True).ok
    assert len(pair_spy.synth) == 4  # now the cache pays off


def test_engine_without_per_pair_support_is_reported(cfg, engine_spy, monkeypatch) -> None:
    """An engine that ignores ``_pairs_done`` recomputes everything: PIPELINE-016 (INFO)."""
    assert api.run(cfg, param_overrides=_overrides(5), incremental=True).ok
    original = FakeEngine._stage_interferogram

    def no_pairs(self, inputs, params, out):
        arts = original(self, inputs, params, out)
        art = arts["igrams"]
        meta = {k: v for k, v in art.meta.items() if k not in ("pairs_reused", "pairs_computed")}
        return arts.add(art.model_copy(update={"meta": meta}))

    monkeypatch.setattr(FakeEngine, "_stage_interferogram", no_pairs)
    result = api.run(cfg, param_overrides=_overrides(6), incremental=True)
    assert result.ok
    ig = _by_stage(result)["interferogram"]
    assert ig.extra["incremental"]["supported"] is False
    info = next(f for f in result.findings if f.rule_id == "PIPELINE-016")
    assert info.severity == "INFO" and info.scope == "interferogram"
    assert info.params == {"stage": "interferogram", "engine": "fake"}


def test_tampered_stack_artifact_still_reuses_intact_pairs(cfg, engine_spy, pair_spy) -> None:
    """The assembled ``unw.npz`` is damaged: the node is stale, but its per-pair files are
    verified individually and reused; only the assembly is redone."""
    first = api.run(cfg, param_overrides=_overrides(5), incremental=True)
    unw = _by_stage(first)["unwrap"]
    p = Path(unw.outputs["unw"])
    p.write_bytes(p.read_bytes() + b"\0")
    engine_spy.clear()
    pair_spy.clear()
    result = api.run(cfg, param_overrides=_overrides(5), incremental=True)
    assert result.ok and engine_spy.stages == ["unwrap", "timeseries", "corrections", "geocode"]
    assert pair_spy.unwrapped == []
    assert _by_stage(result)["unwrap"].extra["incremental"]["reused"] == 10  # 5 dates -> 10 pairs


def _corrupt_cached_pair(node_dir: Path, key: str, how: str) -> None:
    """Damage one cached pair file so that the manifest still believes it is intact."""
    import json

    manifest = incremental.pairs_dir(node_dir) / incremental.PAIRS_MANIFEST
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    entry = raw["pairs"][key]
    f = incremental.pairs_dir(node_dir) / entry["path"]
    if how == "garbage":
        # a manifest without a file hash cannot be verified at all
        entry["file_hash"] = None
        f.write_bytes(b"garbage" * 100)
        manifest.write_text(json.dumps(raw), encoding="utf-8")
    else:
        # mid-file corruption the fast hash (size, mtime, head/tail) does not see on a large
        # file; this small one gets the damaged bytes' hash recorded so the entry verifies
        from wintersar.util.hashing import hash_path

        data = bytearray(f.read_bytes())
        mid = len(data) // 2
        data[mid : mid + 64] = b"\xff" * 64
        f.write_bytes(bytes(data))
        entry["file_hash"] = hash_path(f, fast=True)
        manifest.write_text(json.dumps(raw), encoding="utf-8")
        assert incremental.PairEntry.from_dict(key, entry, incremental.pairs_dir(node_dir)).intact()


@pytest.mark.parametrize("how", ["garbage", "mid-file"])
def test_an_unloadable_cached_pair_is_recomputed_not_a_stage_failure(
    cfg, engine_spy, pair_spy, how: str
) -> None:
    """The fast hash does not see every corruption. A hit that cannot be loaded is a cache
    miss for that one pair: the stage recomputes it, re-stores it and succeeds."""
    first = api.run(cfg, param_overrides=_overrides(6), incremental=True, until="interferogram")
    assert first.ok
    ig = _by_stage(first)["interferogram"]
    node_dir = cache.stage_dir(cfg.workdir, "interferogram", ig.node_hash)
    victim = fake_pairs({"n_dates": 6})[3].key
    _corrupt_cached_pair(node_dir, victim, how)
    pair_spy.clear()
    second = api.run(cfg, param_overrides=_overrides(7), incremental=True, until="interferogram")
    assert second.ok, second.findings
    new_pairs = sorted(
        {p.key for p in fake_pairs({"n_dates": 7})} - {p.key for p in fake_pairs({"n_dates": 6})}
    )
    assert sorted(pair_spy.synth) == sorted([*new_pairs, victim])
    info = _by_stage(second)["interferogram"].extra["incremental"]
    assert info["reused"] == 13 and info["computed"] == 5 and victim in info["computed_pairs"]
    # the repaired pair is bitwise what a clean synthesis gives and is reusable again
    with np.load(second.artifacts["igrams"].path) as z:
        idx = list(z["pairs"]).index(victim)
        expected = FakeEngine().synth_pair(
            __import__("wintersar.engines.fake", fromlist=["StackSpec"]).StackSpec.from_params(
                {"n_dates": 7, "shape": SHAPE}
            ),
            fake_pairs({"n_dates": 7})[idx],
        )
        np.testing.assert_array_equal(z["wrapped"][idx], expected["wrapped"])
    pair_spy.clear()
    third = api.run(cfg, param_overrides=_overrides(7), incremental=True, until="interferogram")
    assert third.ok and pair_spy.synth == []


def test_cache_hits_do_not_replay_run_event_findings(cfg, engine_spy, monkeypatch) -> None:
    """PIPELINE-016 describes what happened while the stage ran; a later cache hit of the
    same manifest must not claim "recomputed in full" next to "cached"."""
    assert api.run(cfg, param_overrides=_overrides(5), incremental=True).ok
    original = FakeEngine._stage_interferogram

    def no_pairs(self, inputs, params, out):
        arts = original(self, inputs, params, out)
        art = arts["igrams"]
        meta = {k: v for k, v in art.meta.items() if k not in ("pairs_reused", "pairs_computed")}
        return arts.add(art.model_copy(update={"meta": meta}))

    monkeypatch.setattr(FakeEngine, "_stage_interferogram", no_pairs)
    ran = api.run(cfg, param_overrides=_overrides(6), incremental=True)
    assert ran.ok and any(f.rule_id == "PIPELINE-016" for f in ran.findings)
    stored = cache.load_record(
        cache.stage_dir(cfg.workdir, "interferogram", _by_stage(ran)["interferogram"].node_hash)
    )
    assert stored is not None and any(f.rule_id == "PIPELINE-016" for f in stored.findings)
    hit = api.run(cfg, param_overrides=_overrides(6), incremental=True)
    assert hit.ok and hit.ran == []
    assert not any(f.rule_id == "PIPELINE-016" for f in hit.findings)
    assert not any(f.rule_id == "PIPELINE-016" for r in hit.records for f in r.findings)
    plan = api.plan(cfg, param_overrides=_overrides(6), incremental=True)
    assert not any(f.rule_id == "PIPELINE-016" for s in plan.stages for f in s.findings)
