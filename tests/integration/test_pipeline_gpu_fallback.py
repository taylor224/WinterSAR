"""PERF-10 CPU fallback is reported, not silent (ADR-0095): ``compute.gpu: true`` on a machine
without CuPy/CUDA runs on numpy and carries one ``ENV-005`` (WARN) finding in ``plan`` and
``run``; the operator override ``WINTERSAR_GPU=0`` silences it (the GPU was switched off on
purpose)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.pipeline._support import write_fake_config
from wintersar.compute import xp as xpmod
from wintersar.pipeline import api
from wintersar.pipeline.executor import gpu_findings, machine_budget

SMALL = {"interferogram": {"n_dates": 4, "shape": [16, 16]}}


@pytest.fixture(autouse=True)
def _no_cupy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WINTERSAR_GPU", raising=False)
    xpmod.reset_backend_cache()
    monkeypatch.setattr(xpmod, "cupy_available", lambda: False)
    monkeypatch.setattr(xpmod, "cupy_installed", lambda: False)
    yield
    xpmod.reset_backend_cache()


def _env005(findings) -> list:
    return [f for f in findings if f.rule_id == "ENV-005"]


def test_gpu_true_without_cupy_is_reported_once_in_plan_and_run(
    tmp_path: Path, cache_dir: Path
) -> None:
    cfg = write_fake_config(tmp_path, compute={"cores": 2, "memory_gb": 4, "gpu": True})
    assert machine_budget(cfg).gpu is True  # the config forces the request
    plan = api.plan(cfg, param_overrides=SMALL)
    warn = _env005(plan.findings)
    assert len(warn) == 1 and warn[0].severity == "WARN" and warn[0].scope == "compute"
    assert warn[0].evidence["backend"] == "numpy" and warn[0].evidence["requested_by"] == "stage"

    result = api.run(cfg, param_overrides=SMALL)
    assert result.ok, result.findings
    assert len(_env005(result.findings)) == 1  # plan + executor, deduplicated
    summary = result.to_dict()
    assert [f["rule_id"] for f in summary["findings"]].count("ENV-005") == 1
    # every stage still received the request (and the executor's stage context)
    # -- a fully cached second run computes nothing on the GPU: no finding
    again = api.run(cfg, param_overrides=SMALL)
    assert again.ok and again.ran == [] and _env005(again.findings) == []
    assert _env005(api.plan(cfg, param_overrides=SMALL).findings) == []


def test_no_request_or_operator_override_gives_no_finding(
    tmp_path: Path, cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auto = write_fake_config(tmp_path, compute={"cores": 2, "memory_gb": 4})
    assert _env005(api.plan(auto, param_overrides=SMALL).findings) == []
    assert gpu_findings(machine_budget(auto)) == []
    (tmp_path / "f").mkdir()
    forced = write_fake_config(tmp_path / "f", compute={"cores": 2, "memory_gb": 4, "gpu": True})
    assert gpu_findings(machine_budget(forced))  # requested and unavailable
    monkeypatch.setenv("WINTERSAR_GPU", "0")
    assert gpu_findings(machine_budget(forced)) == []  # switched off for this invocation
    assert _env005(api.plan(forced, param_overrides=SMALL).findings) == []
