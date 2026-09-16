"""Fixtures for bench tests: the synthetic S site and an injected stage-by-stage fake runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wintersar.bench.profiler import Measurement, StageProfiler
from wintersar.bench.runner import RunOutcome
from wintersar.bench.sites import ENGINE_STAGES, Site, load_site
from wintersar.engines.fake import FakeEngine
from wintersar.io.schemas import Artifacts

SITES_DIR = Path(__file__).resolve().parents[3] / "benchmarks" / "sites"


@pytest.fixture
def sites_dir() -> Path:
    return SITES_DIR


@pytest.fixture
def synthetic_site() -> Site:
    return load_site(SITES_DIR / "S_synthetic.yaml")


def injected_fake_runner(site: Site, workdir: Path, repeat: int) -> RunOutcome:
    """Test double: FakeEngine called directly, one StageProfiler per stage, tiny params."""
    eng = FakeEngine()
    run_dir = Path(workdir) / f"r{repeat}"
    inputs = Artifacts()
    stages: dict[str, Measurement] = {}
    artifacts: dict[str, Path] = {}
    params_all: dict[str, dict[str, Any]] = {
        "interferogram": {"n_dates": 5, "shape": [24, 24], "seed": repeat, "water_fraction": 0.1},
        "unwrap": {"coherence_threshold": 0.3},
    }
    with StageProfiler(watch_dir=run_dir, interval_s=0.01) as total:
        for stage in site.stages:
            if stage not in ENGINE_STAGES:
                continue
            params = dict(params_all.get(stage, {}))
            params["_out_dir"] = str(run_dir / stage)
            with StageProfiler(watch_dir=run_dir, interval_s=0.01) as prof:
                produced = eng.run(stage, inputs, params, run_dir / stage / "logs")
            stages[stage] = prof.result
            inputs = inputs.merged(produced)
            artifacts.update({n: a.path for n, a in produced.items.items()})
    return RunOutcome(stages=stages, artifacts=artifacts, total=total.result)


@pytest.fixture
def fake_runner():
    return injected_fake_runner


def failing_runner(site: Site, workdir: Path, repeat: int) -> RunOutcome:
    return RunOutcome(stages={}, ok=False, error="boom", failed_stage="unwrap")
