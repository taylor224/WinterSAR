"""Phase 2 DoD for the DAG cache (PERF-03, R-05, R-11): fake engine, synthetic data, no
network. "파라미터 1개 변경 시 변경 단계 하류만 재실행됨을 테스트로 확인".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.pipeline._support import SMALL, EngineSpy, spy_fake_engine, write_fake_config
from wintersar.engines.fake import FakeEngine
from wintersar.i18n import t
from wintersar.io.schemas import Artifacts, Finding, Resources
from wintersar.pipeline import api, cache
from wintersar.pipeline import executor as execmod
from wintersar.pipeline.dag import Dag
from wintersar.pipeline.executor import Executor, PipelineError
from wintersar.pipeline.stages import STAGE_ORDER
from wintersar.util.sysinfo import MachineSpec

ENGINE_STAGES = [
    "fetch",
    "coregister",
    "interferogram",
    "multilook",
    "unwrap",
    "timeseries",
    "corrections",
    "geocode",
]


@pytest.fixture
def cfg(tmp_path: Path, cache_dir: Path):
    return write_fake_config(tmp_path)


@pytest.fixture
def small() -> dict[str, dict[str, Any]]:
    return {k: dict(v) for k, v in SMALL.items()}


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> EngineSpy:
    return spy_fake_engine(monkeypatch)


def _manifest(cfg, rec) -> dict[str, Any]:
    p = cache.stage_dir(cfg.workdir, rec.stage, rec.node_hash) / cache.MANIFEST_NAME
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- full run


def test_full_run_produces_velocity_and_manifests(cfg, small, spy) -> None:
    result = api.run(cfg, param_overrides=small)
    assert result.ok, result.findings
    assert spy.stages == ENGINE_STAGES
    assert [r.stage for r in result.records] == STAGE_ORDER
    assert {r.stage for r in result.skipped} == {"search", "precheck", "validate"}
    vel = np.load(result.artifacts["velocity"].path)
    assert vel.shape == (24, 24)
    for rec in result.ran:
        m = _manifest(cfg, rec)
        assert m["status"] == "ok" and m["engine"] == "fake"
        assert m["node_hash"] == rec.node_hash and m["engine_version"]
        assert set(m["outputs"]) == set(m["extra"]["artifacts"])
        assert all(Path(p).exists() for p in m["outputs"].values())
        assert not any(k.startswith("_") for k in m["params"])
        assert m["resources"]["wall_time_s"] is not None
        assert (
            cache.stage_dir(cfg.workdir, rec.stage, rec.node_hash) / "logs" / f"{rec.stage}.log"
        ).exists()
    # downstream manifests reference upstream artifact hashes
    unw = next(r for r in result.records if r.stage == "unwrap")
    ml = next(r for r in result.records if r.stage == "multilook")
    assert unw.inputs["igrams"] == ml.extra["artifacts"]["igrams"]["sha256"]
    assert result.run_id and (Path(cfg.workdir) / "runs" / f"{result.run_id}.json").exists()


def test_second_run_is_fully_cached(cfg, small, spy) -> None:
    api.run(cfg, param_overrides=small)
    spy.clear()
    result = api.run(cfg, param_overrides=small)
    assert result.ok and spy.stages == []
    assert [r.stage for r in result.cached] == ENGINE_STAGES
    assert result.ran == []
    assert "velocity" in result.artifacts


# ---------------------------------------------------------------------- PERF-03 DoD


def test_param_change_reruns_only_changed_stage_and_downstream(cfg, small, spy) -> None:
    first = api.run(cfg, param_overrides=small)
    assert first.ok
    cfg.unwrap.coherence_threshold = 0.5  # one parameter of one stage
    spy.clear()
    result = api.run(cfg, param_overrides=small)
    assert result.ok
    assert spy.stages == ["unwrap", "timeseries", "corrections", "geocode"]
    assert [r.stage for r in result.cached] == ["fetch", "coregister", "interferogram", "multilook"]
    assert [r.stage for r in result.ran] == ["unwrap", "timeseries", "corrections", "geocode"]
    old = {r.stage: r.node_hash for r in first.records}
    new = {r.stage: r.node_hash for r in result.records}
    for s in ["fetch", "coregister", "interferogram", "multilook"]:
        assert old[s] == new[s]
    for s in ["unwrap", "timeseries", "corrections", "geocode"]:
        assert old[s] != new[s]
    # both unwrap results coexist in the cache
    assert len(cache.list_records(cfg.workdir, "unwrap")) == 2
    # switching back is free
    cfg.unwrap.coherence_threshold = 0.3
    spy.clear()
    assert api.run(cfg, param_overrides=small).ok and spy.stages == []


def test_override_change_reruns_downstream_of_interferogram(cfg, small, spy) -> None:
    api.run(cfg, param_overrides=small)
    spy.clear()
    small["interferogram"]["seed"] = 7
    result = api.run(cfg, param_overrides=small)
    assert result.ok
    assert spy.stages == [
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    ]


# ---------------------------------------------------------------------- force / until / from


def test_force_reruns_stage_and_downstream(cfg, small, spy) -> None:
    api.run(cfg, param_overrides=small)
    spy.clear()
    result = api.run(cfg, param_overrides=small, force=["interferogram"])
    assert result.ok
    assert spy.stages == [
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    ]
    assert [r.stage for r in result.cached] == ["fetch", "coregister"]
    assert all(_manifest(cfg, r)["extra"]["forced"] for r in result.ran)
    spy.clear()
    assert api.run(cfg, param_overrides=small).ok and spy.stages == []


def test_until_stops_after_stage(cfg, small, spy) -> None:
    result = api.run(cfg, param_overrides=small, until="unwrap")
    assert result.ok
    assert spy.stages == ["fetch", "coregister", "interferogram", "multilook", "unwrap"]
    assert [r.stage for r in result.records][-1] == "unwrap"
    assert "unw" in result.artifacts and "timeseries" not in result.artifacts
    spy.clear()
    result = api.run(cfg, param_overrides=small)  # continue: only the rest runs
    assert result.ok and spy.stages == ["timeseries", "corrections", "geocode"]


def test_from_stage_uses_cache(cfg, small, spy) -> None:
    api.run(cfg, param_overrides=small)
    spy.clear()
    cfg.timeseries.deramp = "quadratic"
    result = api.run(cfg, param_overrides=small, from_stage="timeseries")
    assert result.ok
    assert spy.stages == ["timeseries", "corrections", "geocode"]
    assert [r.stage for r in result.cached] == [
        "fetch",
        "coregister",
        "interferogram",
        "multilook",
        "unwrap",
    ]


def test_from_stage_falls_back_to_latest_when_upstream_config_changed(cfg, small, spy) -> None:
    api.run(cfg, param_overrides=small)
    spy.clear()
    cfg.unwrap.coherence_threshold = 0.9  # upstream of --from changed; do not recompute it
    result = api.run(cfg, param_overrides=small, from_stage="timeseries")
    assert result.ok
    unwrap = next(r for r in result.records if r.stage == "unwrap")
    assert unwrap.extra.get("fallback") and unwrap.extra.get("cache_hit")
    assert "PIPELINE-003" in {f.rule_id for f in result.findings}
    # the reused unw artifact is unchanged, so timeseries and below are still cache hits
    assert spy.stages == []
    cfg.timeseries.deramp = "quadratic"
    result = api.run(cfg, param_overrides=small, from_stage="timeseries")
    assert result.ok and spy.stages == ["timeseries", "corrections", "geocode"]
    assert next(r for r in result.records if r.stage == "unwrap").extra.get("fallback")


def test_from_stage_without_cache_fails_fast(cfg, small, spy) -> None:
    result = api.run(cfg, param_overrides=small, from_stage="timeseries")
    assert not result.ok and spy.stages == []
    assert result.failed_stage == "fetch"
    assert "PIPELINE-002" in {f.rule_id for f in result.findings}


# ---------------------------------------------------------------------- failure


def test_failure_injection_writes_failed_manifest_and_findings(cfg, small, spy) -> None:
    result = api.run(cfg, param_overrides={**small, "unwrap": {"fail_stage": "unwrap"}})
    assert not result.ok and result.failed_stage == "unwrap"
    assert spy.stages == ["fetch", "coregister", "interferogram", "multilook", "unwrap"]
    failed = next(r for r in result.records if r.status == "failed")
    assert failed.stage == "unwrap"
    assert [r.stage for r in result.records][-1] == "unwrap"  # nothing after the failure
    m = _manifest(cfg, failed)
    assert m["status"] == "failed" and m["finished_at"]
    assert "FakeEngineFailureError" in m["extra"]["error"]
    ids = [f.rule_id for f in failed.findings]
    assert ids[0] == "PIPELINE-001"
    generic = failed.findings[0]
    assert generic.severity == "FAIL" and generic.scope == "unwrap"
    assert "injected" in generic.evidence["log_excerpt"]["unwrap.log"]
    assert generic.params["engine"] == "fake"
    assert "PIPELINE-001" in {f.rule_id for f in result.findings}
    # a failed manifest is never a cache hit; the next run retries the stage
    spy.clear()
    again = api.run(cfg, param_overrides={**small, "unwrap": {"fail_stage": "unwrap"}})
    assert not again.ok and spy.stages == ["unwrap"]


def test_diagnose_hook_findings_and_retry_hint_are_attached(cfg, small, monkeypatch) -> None:
    kb = Finding(
        rule_id="KB-TEST-001",
        severity="FAIL",
        message_key="x.cause",
        evidence={"retry_hint": {"tiles": [2, 2]}},
    )
    seen: list[tuple[Path, str | None]] = []

    def fake_diagnose(log_dir: Path, engine: str | None) -> list[Finding]:
        seen.append((log_dir, engine))
        return [kb]

    real = execmod.load_entrypoint

    def load(module: str, function: str):
        if (module, function) == ("wintersar.diagnose.api", "diagnose_logs"):
            return fake_diagnose
        return real(module, function)

    monkeypatch.setattr(execmod, "load_entrypoint", load)
    result = api.run(cfg, param_overrides={**small, "unwrap": {"fail_stage": "unwrap"}})
    assert not result.ok
    assert seen and seen[0][1] == "fake" and seen[0][0].name == "logs"
    failed = next(r for r in result.records if r.status == "failed")
    ids = [f.rule_id for f in failed.findings]
    assert ids == ["PIPELINE-001", "KB-TEST-001", "PIPELINE-008"]
    assert failed.extra["retry_hint"] == {"tiles": [2, 2]}
    assert _manifest(cfg, failed)["extra"]["retry_hint"] == {"tiles": [2, 2]}


def test_exception_retry_hint_and_broken_diagnose_do_not_mask_failure(
    cfg, small, monkeypatch
) -> None:
    class Boom(RuntimeError):
        retry_hint = "ntiles=2x2"

    original = FakeEngine.run

    def run(self, stage, inputs, params, log_dir):
        if stage == "unwrap":
            raise Boom("tile failure")
        return original(self, stage, inputs, params, log_dir)

    monkeypatch.setattr(FakeEngine, "run", run)

    def broken(log_dir: Path, engine: str | None) -> list[Finding]:
        raise ValueError("diagnose exploded")

    real = execmod.load_entrypoint
    monkeypatch.setattr(
        execmod,
        "load_entrypoint",
        lambda m, f: broken if f == "diagnose_logs" else real(m, f),
    )
    result = api.run(cfg, param_overrides=small)
    assert not result.ok and result.failed_stage == "unwrap"
    failed = next(r for r in result.records if r.status == "failed")
    assert failed.extra["retry_hint"] == "ntiles=2x2"
    assert "diagnose exploded" in failed.extra["diagnose_error"]
    assert [f.rule_id for f in failed.findings] == ["PIPELINE-001", "PIPELINE-008"]


def test_missing_declared_output_is_pipeline_009(cfg, small, monkeypatch) -> None:
    from wintersar.io.schemas import Artifact

    original = FakeEngine.run

    def run(self, stage, inputs, params, log_dir):
        if stage == "geocode":
            log_dir.mkdir(parents=True, exist_ok=True)
            return Artifacts().add(
                Artifact(name="velocity", path=Path(params["_out_dir"]) / "no.npy")
            )
        return original(self, stage, inputs, params, log_dir)

    monkeypatch.setattr(FakeEngine, "run", run)
    result = api.run(cfg, param_overrides=small)
    assert not result.ok and result.failed_stage == "geocode"
    ids = {f.rule_id for f in result.findings}
    assert {"PIPELINE-001", "PIPELINE-009"} <= ids


# ---------------------------------------------------------------------- plan / dry run


def test_plan_reports_cached_vs_to_run(cfg, small, spy) -> None:
    p = api.plan(cfg, param_overrides=small)
    assert len(p.to_run) == 8 and p.cached == []
    assert [s.status for s in p.stages] == ["skipped"] * 2 + ["pending"] * 8 + ["skipped"]
    # fetch..unwrap are identified from their producers' node hashes (ADR-0080); the
    # time-series stages need the *content* of unw/igrams and stay provisional
    assert not any(s.extra.get("provisional") for s in p.stages[2:7])
    assert all(s.extra.get("provisional") for s in p.stages[7:10])  # inputs unresolved
    api.run(cfg, param_overrides=small, until="unwrap")
    p = api.plan(cfg, param_overrides=small)
    assert len(p.cached) == 5 and len(p.to_run) == 3
    assert [s.stage for s in p.stages if s.extra.get("cache_hit")] == [
        "fetch",
        "coregister",
        "interferogram",
        "multilook",
        "unwrap",
    ]
    assert not p.stages[7].extra.get("provisional")  # timeseries resolved from cached unw
    assert "PIPELINE-010" in {f.rule_id for f in p.findings}
    assert not any(f.is_fail for f in p.findings)
    spy.clear()
    dry = api.run(cfg, param_overrides=small, dry_run=True)
    assert dry.ok and dry.dry_run and spy.stages == []
    assert len(dry.plan.cached) == 5


def test_plan_reports_missing_engine(cfg, small, monkeypatch) -> None:
    monkeypatch.setattr(FakeEngine, "detect_version", lambda self: None)
    p = api.plan(cfg, param_overrides=small)
    assert any(f.rule_id == "ENV-001" for f in p.findings)
    result = api.run(cfg, param_overrides=small)
    assert not result.ok and result.ran == []  # fail fast, nothing executed


# ---------------------------------------------------------------------- invalidation


def test_engine_version_change_invalidates_cache(cfg, small, spy, monkeypatch) -> None:
    api.run(cfg, param_overrides=small)
    spy.clear()
    monkeypatch.setattr(FakeEngine, "detect_version", lambda self: "9.9.9")
    result = api.run(cfg, param_overrides=small)
    assert result.ok and spy.stages == ENGINE_STAGES
    assert all(r.engine_version == "9.9.9" for r in result.ran)


def test_modified_output_invalidates_cache(cfg, small, spy) -> None:
    first = api.run(cfg, param_overrides=small)
    unw = next(r for r in first.records if r.stage == "unwrap")
    p = Path(unw.outputs["unw"])
    p.write_bytes(p.read_bytes() + b"\0")
    spy.clear()
    result = api.run(cfg, param_overrides=small)
    assert result.ok and spy.stages == ["unwrap", "timeseries", "corrections", "geocode"]


def test_cache_gc_keeps_latest(cfg, small, spy) -> None:
    api.run(cfg, param_overrides=small)
    for thr in (0.4, 0.5):
        cfg.unwrap.coherence_threshold = thr
        api.run(cfg, param_overrides=small)
    assert len(cache.list_records(cfg.workdir, "unwrap")) == 3
    report = cache.gc(cfg.workdir, keep_latest=1)
    assert [e.stage for e in report.kept].count("unwrap") == 1
    assert len(report.removed) == 8  # 2 x (unwrap, timeseries, corrections, geocode)
    spy.clear()
    assert api.run(cfg, param_overrides=small).ok and spy.stages == []  # latest still cached


# ---------------------------------------------------------------------- executor contract


def test_engine_receives_private_params_and_budget(cfg, small, spy) -> None:
    result = api.run(cfg, param_overrides=small, until="fetch")
    assert result.ok
    _, params = spy.calls[0]
    for key in ("_cores", "_memory_gb", "_gpu", "_out_dir", "_workdir", "_cache_dir", "_log_dir"):
        assert key in params, key
    assert params["_cores"] == 2 and params["_memory_gb"] <= 4.0
    assert Path(params["_out_dir"]).is_relative_to(cfg.workdir)
    assert Path(params["_cache_dir"]) == cfg.cache_dir
    fetch = next(r for r in result.records if r.stage == "fetch")
    assert not any(k.startswith("_") for k in fetch.params)


def test_memory_budget_warning_and_reservation(cfg, small) -> None:
    dag = Dag(cfg)
    dag.build(small, until="fetch")
    machine = MachineSpec(cores=2, memory_gb=1.0, gpu=False, gpu_name=None, python="3", os="t")
    ex = Executor(cfg, dag, machine=machine, estimates={"fetch": Resources(peak_rss_gb=8.0)})
    records, _arts, _findings = ex.run()
    fetch = records[2]
    assert fetch.status == "ok"
    assert "PIPELINE-006" in {f.rule_id for f in fetch.findings}
    assert ex.budget.reserved_gb == 0.0  # released after the stage
    assert ex.budget.reserve("x", 0.5) == 0.5 and ex.budget.available_gb == 0.5
    assert ex.budget.reserve("y", None) == 0.5 and ex.budget.available_gb == 0.0
    ex.budget.release("x")
    ex.budget.release("y")


def test_executor_raises_pipeline_error_with_records(cfg, small) -> None:
    dag = Dag(cfg)
    dag.build({**small, "unwrap": {"fail_stage": "unwrap"}})
    with pytest.raises(PipelineError) as ei:
        Executor(cfg, dag).run()
    err = ei.value
    assert err.failed_stage == "unwrap"
    assert [r.stage for r in err.records][-1] == "unwrap"
    assert "igrams" in err.artifacts
    assert any(f.rule_id == "PIPELINE-001" for f in err.findings)


# ---------------------------------------------------------------------- concurrency / output


def test_a_second_run_never_touches_a_node_directory_in_use(cfg, small, spy) -> None:
    """ADR-0032 + node lock: two runs on one workdir resolve the same node hash.

    Without the lock the second ``prepare_node_dir(clean=True)`` deletes the first run's
    in-flight ``out/`` and both manifests race, so the first run hashes a half-written file
    and fails downstream with a misleading error.
    """
    dag = Dag(cfg)
    dag.build(small, until="fetch")
    node_dir = cache.stage_dir(cfg.workdir, "fetch", dag.node("fetch").node_hash or "")
    out, _ = cache.prepare_node_dir(node_dir)
    inflight = out / "slc_manifest.json"
    inflight.write_text("half written", encoding="utf-8")  # the other run is still writing
    with cache.node_lock(node_dir):
        result = api.run(cfg, param_overrides=small, until="fetch")
    assert not result.ok and result.failed_stage == "fetch"
    assert spy.stages == []  # the engine never ran
    assert inflight.read_text(encoding="utf-8") == "half written"  # nothing was cleaned
    busy = next(f for f in result.findings if f.rule_id == "PIPELINE-013")
    assert busy.severity == "FAIL" and busy.scope == "fetch"
    record = next(r for r in result.records if r.stage == "fetch")
    assert record.status == "failed" and record.extra["busy"] is True
    # once the other run releases the directory the stage executes normally
    result = api.run(cfg, param_overrides=small, until="fetch")
    assert result.ok and spy.stages == ["fetch"]


def test_dag_findings_are_reported_once_per_run(cfg, small, spy) -> None:
    """``run`` merges the plan's findings with the executor's; both walk the same DAG."""
    result = api.run(cfg, param_overrides=small)
    assert result.ok
    ids = [f.rule_id for f in result.findings]
    assert ids.count("PIPELINE-010") == 1, ids
    keys = [
        (f.rule_id, f.severity, f.scope, f.message_key, tuple(sorted(f.params.items())))
        for f in result.findings
    ]
    assert len(keys) == len(set(keys)), keys
    plan_ids = [f.rule_id for f in api.plan(cfg, param_overrides=small).findings]
    assert plan_ids.count("PIPELINE-010") == 1  # the plan alone never repeated it


def test_fully_cached_plan_reports_zero_cost(cfg, small, spy) -> None:
    """Nothing left to run means no additional cost — not 'unknown' (plan §4.5)."""
    fresh = api.plan(cfg, param_overrides=small)
    assert fresh.resources.wall_time_s is not None  # a fresh plan estimates the work
    api.run(cfg, param_overrides=small)
    cached = api.plan(cfg, param_overrides=small)
    assert cached.to_run == []
    assert cached.resources.wall_time_s == 0.0
    assert cached.resources.peak_rss_gb == 0.0
    assert cached.resources.disk_gb == 0.0
    assert cached.resources.network_gb == 0.0


def test_failure_fix_text_never_suggests_an_unknown_diagnose_engine(cfg, small) -> None:
    """``wintersar diagnose --engine fake`` exits 2, so the fix text must not propose it."""
    result = api.run(cfg, param_overrides={**small, "unwrap": {"fail_stage": "unwrap"}})
    generic = next(f for f in result.findings if f.rule_id == "PIPELINE-001")
    assert generic.fix_key == "pipeline.PIPELINE-001.fix_auto"
    assert generic.params["kb_engine"] == ""
    for lang in ("ko", "en"):
        fix = t(generic.fix_key, lang, **generic.params)
        assert "--engine" not in fix, fix
        assert "wintersar diagnose" in fix
