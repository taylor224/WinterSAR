"""Fixtures for the research module tests (R-07, R-15)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from wintersar.research import synth
from wintersar.research.repr_phase import truth_lowres_phase
from wintersar.research.stitching import save_tiles_npz


@pytest.fixture
def clean_igram() -> synth.SynthIgram:
    """Nearly noise-free interferogram (64 looks, high coherence) for method-accuracy tests."""
    rng = np.random.default_rng(7)
    return synth.make_interferogram(
        (48, 48), rng, atmosphere_std_rad=0.3, coherence_base=0.95, looks=64
    )


@pytest.fixture
def slc_stack() -> synth.SynthSlcStack:
    """10-date SLC stack with a known exponential coherence model (64 looks per 8x8 block)."""
    rng = np.random.default_rng(11)
    return synth.make_slc_stack(10, (64, 64), rng, tau_dates=6.0, coherence_floor=0.3)


@pytest.fixture
def tiled_truth() -> synth.TiledTruth:
    rng = np.random.default_rng(5)
    return synth.make_tiled_truth(
        (96, 96), 3, 3, 12, rng, noise_std_rad=0.2, atmosphere_std_rad=0.8, looks=16
    )


@pytest.fixture
def tiles_npz(tmp_path: Path, tiled_truth: synth.TiledTruth) -> Path:
    tt = tiled_truth
    return save_tiles_npz(
        tmp_path / "tiles.npz",
        tt.tiles,
        tt.shape,
        coh=tt.coherence,
        lowres_ref=truth_lowres_phase(tt.unw_true, 3),
        unw_true=tt.unw_true,
        mask=tt.mask,
        offsets_cycles=tt.offsets_cycles,
    )
