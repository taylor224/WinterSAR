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


def test_fake_engine_pairs_are_pure_functions_of_the_pair_key(tmp_path: Path) -> None:
    """PERF-06 (ADR-0080): a 6-date stack is bitwise contained in the 7-date stack, so an
    incremental rebuild from cached pairs equals a full recompute."""
    from wintersar.engines.fake import fake_dates, fake_pairs

    eng = get_engine("fake")
    base = {"shape": (16, 16), "seed": 4, "water_fraction": 0.1}
    stacks = {}
    for n in (6, 7):
        out = tmp_path / str(n)
        arts = eng.run(
            "interferogram", Artifacts(), {**base, "n_dates": n, "_out_dir": str(out)}, out / "logs"
        )
        stacks[n] = np.load(arts["igrams"].path)
        assert list(stacks[n]["dates"]) == [d.isoformat() for d in fake_dates({"n_dates": n})]
        assert list(stacks[n]["pairs"]) == [p.key for p in fake_pairs({"n_dates": n})]
    six, seven = stacks[6], stacks[7]
    index7 = {str(k): i for i, k in enumerate(seven["pairs"])}
    for i, key in enumerate(six["pairs"]):
        j = index7[str(key)]
        for name in ("wrapped", "coherence", "mask", "unw_true"):
            np.testing.assert_array_equal(six[name][i], seven[name][j], err_msg=f"{key}:{name}")
    np.testing.assert_array_equal(six["displacement_true"], seven["displacement_true"][:6])
    np.testing.assert_array_equal(six["velocity_true"], seven["velocity_true"])
    # loop closure of the truth still holds (per-date atmosphere, per-pair noise)
    d = fake_dates({"n_dates": 7})
    k = lambda a, b: f"{a:%Y%m%d}_{b:%Y%m%d}"  # noqa: E731
    closure = (
        seven["unw_true"][index7[k(d[0], d[1])]]
        + seven["unw_true"][index7[k(d[1], d[2])]]
        - seven["unw_true"][index7[k(d[0], d[2])]]
    )
    np.testing.assert_allclose(closure, 0.0, atol=1e-4)
    # the 7th date's pairs are the only new ones
    new = set(index7) - {str(k_) for k_ in six["pairs"]}
    assert len(new) == 4 and all(key.endswith(d[6].strftime("%Y%m%d")) for key in new)
