"""Fixtures for the validate module: a synthetic time series with known truth (R-09..R-11).

``timeseries_from_stack`` is also used by ``scripts``-style fixture generation (the ground-truth
CSVs under ``tests/fixtures/ground_truth`` are derived from exactly this stack).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from wintersar.io.igrams import IgramStack
from wintersar.io.timeseries import TimeSeries
from wintersar.research.synth import SynthStack, make_stack
from wintersar.validate.api import synthetic_latlon_grid
from wintersar.validate.los import S1_HEADING_DESC_DEG, S1_INCIDENCE_MID_DEG

# Deterministic synthetic scene shared by every validate test and by the CSV fixtures.
SYNTH_N_DATES = 10
SYNTH_SHAPE = (32, 32)
SYNTH_SEED = 0
SYNTH_VELOCITY_PEAK = -0.10  # m/yr, subsidence bowl at the scene centre
SYNTH_LAT0 = 37.60
SYNTH_LON0 = 126.90
SYNTH_PIXEL_M = 40.0
SYNTH_HEADING = S1_HEADING_DESC_DEG
SYNTH_INCIDENCE = S1_INCIDENCE_MID_DEG

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ground_truth"


def make_synth_stack(seed: int = SYNTH_SEED) -> SynthStack:
    return make_stack(
        n_dates=SYNTH_N_DATES,
        shape=SYNTH_SHAPE,
        rng=np.random.default_rng(seed),
        velocity_peak_m_per_yr=SYNTH_VELOCITY_PEAK,
        atmosphere_std_rad=0.3,
    )


def timeseries_from_stack(
    stack: SynthStack,
    *,
    lat0: float = SYNTH_LAT0,
    lon0: float = SYNTH_LON0,
    pixel_m: float = SYNTH_PIXEL_M,
    heading_deg: float = SYNTH_HEADING,
    incidence_deg: float = SYNTH_INCIDENCE,
    noise_m: float = 0.0,
    seed: int = 1,
) -> TimeSeries:
    """TimeSeries whose displacement is the synthetic truth (+ optional white noise)."""
    lat, lon = synthetic_latlon_grid(stack.shape, lat0, lon0, pixel_m)
    disp = stack.displacement_m.astype(np.float64).copy()
    if noise_m > 0:
        disp += np.random.default_rng(seed).normal(0.0, noise_m, disp.shape)
    coh = np.mean([ig.coherence for ig in stack.igrams.values()], axis=0)
    ny, nx = stack.shape
    conncomp = np.ones((ny, nx), dtype=np.int16)
    conncomp[:, : nx // 5] = 2  # a second component along the left edge
    dem = 50.0 + 2.0 * np.arange(ny, dtype=np.float64)[:, None] + 0.0 * np.arange(nx)[None, :]
    return TimeSeries(
        dates=list(stack.dates),
        displacement_m=disp,
        lat=lat,
        lon=lon,
        incidence_deg=incidence_deg,
        heading_deg=heading_deg,
        coherence=np.asarray(coh, dtype=np.float64),
        velocity_m_per_yr=stack.velocity_m_per_yr.astype(np.float64),
        dem_m=dem,
        conncomp=conncomp,
        attrs={"source": "synthetic", "synthetic_geometry": True},
    )


def igram_stack_from_synth(stack: SynthStack, with_unw: bool = True) -> IgramStack:
    keys = [p.key for p in stack.pairs]
    return IgramStack(
        wrapped=np.stack([stack.igrams[k].wrapped for k in keys]).astype(np.float32),
        coherence=np.stack([stack.igrams[k].coherence for k in keys]).astype(np.float32),
        pairs=keys,
        dates=list(stack.dates),
        mask=np.stack([stack.igrams[k].mask for k in keys]),
        unw=np.stack([stack.igrams[k].unw_true for k in keys]).astype(np.float32)
        if with_unw
        else None,
    )


@pytest.fixture(scope="session")
def synth_stack() -> SynthStack:
    return make_synth_stack()


@pytest.fixture
def synth_ts(synth_stack: SynthStack) -> TimeSeries:
    return timeseries_from_stack(synth_stack)


@pytest.fixture
def igram_stack(synth_stack: SynthStack) -> IgramStack:
    return igram_stack_from_synth(synth_stack)


@pytest.fixture
def leveling_csv() -> Path:
    return FIXTURES / "leveling_synth.csv"


@pytest.fixture
def gnss_csv() -> Path:
    return FIXTURES / "gnss_synth.csv"


@pytest.fixture
def ts_npz(tmp_path: Path, synth_stack: SynthStack) -> Path:
    """Fake-engine style ``timeseries.npz`` (no lat/lon: the loader synthesises the grid)."""
    p = tmp_path / "timeseries.npz"
    np.savez_compressed(
        p,
        dates=np.array([d.isoformat() for d in synth_stack.dates]),
        displacement_m=synth_stack.displacement_m.astype(np.float32),
        velocity_m_per_yr=synth_stack.velocity_m_per_yr.astype(np.float32),
        lat0=SYNTH_LAT0,
        lon0=SYNTH_LON0,
        pixel_m=SYNTH_PIXEL_M,
        heading_deg=SYNTH_HEADING,
    )
    return p


@pytest.fixture
def ts_npz_bare(tmp_path: Path, synth_stack: SynthStack) -> Path:
    """Exactly what ``engines/fake.py::_stage_timeseries`` writes: no lat/lon, no geometry.

    # source: src/wintersar/engines/fake.py::FakeEngine._stage_timeseries
    #   np.savez_compressed(p, dates=…, displacement_m=…, velocity_m_per_yr=…)
    """
    p = tmp_path / "bare_timeseries.npz"
    np.savez_compressed(
        p,
        dates=np.array([d.isoformat() for d in synth_stack.dates]),
        displacement_m=synth_stack.displacement_m.astype(np.float32),
        velocity_m_per_yr=synth_stack.velocity_m_per_yr.astype(np.float32),
    )
    return p
