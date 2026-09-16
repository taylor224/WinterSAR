"""Phase 0: fake engine runs every stage on synthetic data (no network)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wintersar.engines.base import get_engine, list_engines
from wintersar.io.schemas import Artifacts


def test_fake_engine_registered_and_available() -> None:
    assert "fake" in list_engines()
    eng = get_engine("fake")
    assert eng.check_install() == []


def test_fake_engine_end_to_end(tmp_path: Path) -> None:
    eng = get_engine("fake")
    params = {"n_dates": 5, "shape": (32, 32), "seed": 1, "coherence_threshold": 0.3}
    arts = Artifacts()
    for stage in (
        "fetch",
        "coregister",
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    ):
        out = tmp_path / stage
        p = {**params, "_out_dir": str(out)}
        arts = arts.merged(eng.run(stage, arts, p, out / "logs"))
        assert (out / "logs" / f"{stage}.log").exists()
    ig = np.load(arts["igrams"].path)
    assert ig["wrapped"].shape[1:] == (32, 32)
    assert ig["wrapped"].shape[0] == len(ig["pairs"]) > 0
    unw = np.load(arts["unw"].path)["unw"]
    assert np.isnan(unw).any()  # coherence mask applied
    vel = np.load(arts["velocity"].path)
    assert vel.shape == (32, 32)
    assert vel.min() < 0  # subsidence bowl


def test_fake_engine_failure_injection(tmp_path: Path) -> None:
    import pytest

    from wintersar.engines.fake import FakeEngineFailureError

    eng = get_engine("fake")
    with pytest.raises(FakeEngineFailureError):
        eng.run(
            "unwrap",
            Artifacts(),
            {"fail_stage": "unwrap", "_out_dir": str(tmp_path)},
            tmp_path / "logs",
        )
    assert "injected" in (tmp_path / "logs" / "unwrap.log").read_text()
