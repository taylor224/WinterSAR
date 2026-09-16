"""Fixtures for the unwrap scheduler tests (R-06, PERF-04)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from wintersar.io.igrams import IgramStack, save_igram_stack
from wintersar.research import synth
from wintersar.util.sysinfo import MachineSpec


def make_machine(cores: int = 10, memory_gb: float = 64.0) -> MachineSpec:
    """A *budgeted* machine spec (memory_gb is the budget, no 0.8 factor applied)."""
    return MachineSpec(
        cores=cores, memory_gb=memory_gb, gpu=False, gpu_name=None, python="3.11", os="test"
    )


def make_synth_stack(
    n_dates: int = 5,
    shape: tuple[int, int] = (48, 48),
    seed: int = 0,
    water_fraction: float = 0.0,
    with_truth: bool = True,
) -> IgramStack:
    rng = np.random.default_rng(seed)
    st = synth.make_stack(
        n_dates=n_dates,
        shape=shape,
        rng=rng,
        atmosphere_std_rad=0.3,
        coherence_base=0.8,
        looks=4,
        water_fraction=water_fraction,
    )
    keys = [p.key for p in st.pairs]
    wrapped = np.stack([st.igrams[k].wrapped for k in keys]).astype(np.float32)
    coh = np.stack([st.igrams[k].coherence for k in keys]).astype(np.float32)
    mask = np.stack([st.igrams[k].mask for k in keys])
    truth = (
        {"unw_true": np.stack([st.igrams[k].unw_true for k in keys]).astype(np.float32)}
        if with_truth
        else {}
    )
    return IgramStack(
        wrapped=wrapped, coherence=coh, pairs=keys, dates=st.dates, mask=mask, truth=truth
    )


@pytest.fixture
def machine_64gb_10c() -> MachineSpec:
    return make_machine(10, 64.0)


@pytest.fixture
def synth_stack() -> IgramStack:
    return make_synth_stack()


@pytest.fixture
def synth_stack_npz(tmp_path: Path, synth_stack: IgramStack) -> Path:
    return save_igram_stack(synth_stack, tmp_path / "igrams.npz")
