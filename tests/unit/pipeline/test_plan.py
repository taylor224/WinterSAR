"""``plan`` resource estimation wiring (Engine.estimate + diagnose resource model)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.pipeline._support import write_fake_config
from wintersar.engines.fake import FakeEngine
from wintersar.io.schemas import Artifact, Artifacts, Plan, Resources
from wintersar.pipeline import plan as planmod
from wintersar.pipeline.dag import Dag
from wintersar.util.sysinfo import MachineSpec

MACHINE = MachineSpec(cores=4, memory_gb=16.0, gpu=False, gpu_name=None, python="3", os="t")


def test_stage_size_from_meta_then_params(tmp_path: Path, cache_dir: Path) -> None:
    dag = Dag(write_fake_config(tmp_path))
    dag.build({"interferogram": {"shape": [24, 32]}})
    node = dag.node("unwrap")
    assert planmod.stage_size(node, Artifacts()) == (0, 0)
    arts = Artifacts().add(
        Artifact(name="igrams", path=tmp_path / "x", meta={"n_pairs": 7, "shape": [10, 20]})
    )
    assert planmod.stage_size(node, arts) == (7, 200)
    assert planmod.stage_size(dag.node("interferogram"), Artifacts()) == (0, 24 * 32)


def test_model_params_maps_config_names(tmp_path: Path, cache_dir: Path) -> None:
    cfg = write_fake_config(
        tmp_path,
        unwrap={"tiles": {"rows": 2, "cols": 3}, "nproc_per_igram": 2, "memory_mb_per_mpixel": 120},
    )
    dag = Dag(cfg)
    dag.build()
    p = planmod.model_params(dag.node("unwrap"))
    assert p["ntiles"] == [2, 3] and p["nproc"] == 2 and p["memory_mb_per_mpixel"] == 120
    p = planmod.model_params(dag.node("interferogram"))
    assert p["pixel_m"] == 40.0 and p["looks"] == "auto"


def test_estimate_node_calls_engine_and_model(tmp_path: Path, cache_dir: Path, monkeypatch) -> None:
    dag = Dag(write_fake_config(tmp_path))
    dag.build({"interferogram": {"shape": [24, 24]}})
    node = dag.node("unwrap")
    monkeypatch.setattr(FakeEngine, "estimate", lambda self, plan: Resources(wall_time_s=5.0))
    seen: list[tuple] = []

    def model(stage, n_pairs, pixels, engine, machine, params=None) -> Resources:
        seen.append((stage, n_pairs, pixels, engine, machine, params))
        return Resources(wall_time_s=10.0, peak_rss_gb=2.5, notes={"model": "linear"})

    real = planmod.load_entrypoint
    monkeypatch.setattr(
        planmod,
        "load_entrypoint",
        lambda m, f: (
            model if (m, f) == ("wintersar.diagnose.resources", "estimate") else real(m, f)
        ),
    )
    arts = Artifacts().add(
        Artifact(name="igrams", path=tmp_path / "x", meta={"n_pairs": 9, "shape": [24, 24]})
    )
    res = planmod.estimate_node(dag, node, MACHINE, arts)
    assert res.wall_time_s == 15.0 and res.peak_rss_gb == 2.5
    assert seen[0][:4] == ("unwrap", 9, 576, "fake") and seen[0][4] is MACHINE
    assert "coherence_threshold" in seen[0][5]
    assert res.notes["model"] == {"model": "linear"}
    # python stages have no engine -> nothing estimated
    assert planmod.estimate_node(dag, dag.node("search"), MACHINE) == Resources()


def test_estimate_node_survives_broken_hooks(tmp_path: Path, cache_dir: Path, monkeypatch) -> None:
    dag = Dag(write_fake_config(tmp_path))
    dag.build()
    node = dag.node("unwrap")

    def boom(self: FakeEngine, plan: Plan) -> Resources:
        raise RuntimeError("no estimate")

    monkeypatch.setattr(FakeEngine, "estimate", boom)

    def bad_model(*args, **kwargs):
        raise TypeError("signature drift")

    real = planmod.load_entrypoint
    monkeypatch.setattr(
        planmod, "load_entrypoint", lambda m, f: bad_model if f == "estimate" else real(m, f)
    )
    res = planmod.estimate_node(dag, node, MACHINE)
    assert "estimate_error" in res.notes and "model_error" in res.notes
    assert res.wall_time_s is None


def test_real_resource_model_if_present(tmp_path: Path, cache_dir: Path) -> None:
    pytest.importorskip("wintersar.diagnose.resources")
    dag = Dag(write_fake_config(tmp_path))
    dag.build({"interferogram": {"shape": [24, 24]}})
    res = planmod.estimate_node(dag, dag.node("unwrap"), MACHINE)
    assert isinstance(res, Resources)
    assert "model_error" not in res.notes, res.notes


def test_build_plan_feeds_cached_artifacts_to_estimates(
    tmp_path: Path, cache_dir: Path, monkeypatch
) -> None:
    from wintersar.pipeline import api

    cfg = write_fake_config(tmp_path)
    small = {"interferogram": {"n_dates": 5, "shape": [24, 24]}}
    api.run(cfg, param_overrides=small, until="multilook")
    sizes: dict[str, tuple[int, int]] = {}

    def model(stage, n_pairs, pixels, engine, machine, params=None) -> Resources:
        sizes[stage] = (n_pairs, pixels)
        return Resources()

    real = planmod.load_entrypoint
    monkeypatch.setattr(
        planmod, "load_entrypoint", lambda m, f: model if f == "estimate" else real(m, f)
    )
    p = api.plan(cfg, param_overrides=small)
    assert sizes["unwrap"][1] == 576 and sizes["unwrap"][0] > 0  # from cached igrams meta
    assert set(planmod.plan_estimates(p)) == {"unwrap", "timeseries", "corrections", "geocode"}
