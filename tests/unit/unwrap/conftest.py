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


# --------------------------------------------------------------- ISCE2 flat-binary input
# Minimal ``merged/interferograms`` tree as topsStack leaves it (ADR-0052, R-06): one
# directory per pair with the filtered interferogram and its coherence.
# source: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/FilterAndCoherence.py
#   runFilter -> filt_fine.int (IntImage, CFLOAT), estCoherence -> filt_fine.cor
#   (``phsigImage.dataType='FLOAT'``, ``phsigImage.bands = 1``)
# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/utils/readfile.py read_isce_xml
#   (lower-case property names in the ``.xml`` sidecar)
ISCE_WRAPPED_FILE = "filt_fine.int"
ISCE_COHERENCE_FILE = "filt_fine.cor"


def isce_sidecar_xml(
    width: int, length: int, data_type: str, scheme: str, file_name: str, bands: int = 1
) -> str:
    return (
        "<imageFile>\n"
        f'    <property name="byte_order"><value>l</value></property>\n'
        f'    <property name="data_type"><value>{data_type}</value></property>\n'
        f'    <property name="file_name"><value>{file_name}</value></property>\n'
        f'    <property name="length"><value>{length}</value></property>\n'
        f'    <property name="number_bands"><value>{bands}</value></property>\n'
        f'    <property name="scheme"><value>{scheme}</value></property>\n'
        f'    <property name="width"><value>{width}</value></property>\n'
        "</imageFile>\n"
    )


def write_isce_igram_dir(root: Path, stack: IgramStack) -> Path:
    """Write ``stack`` as an ISCE2 topsStack ``merged/interferograms`` directory."""
    ny, nx = stack.shape
    for i, pair in enumerate(stack.pairs):
        d = root / pair
        d.mkdir(parents=True, exist_ok=True)
        igram = (stack.coherence[i] * np.exp(1j * stack.wrapped[i])).astype("<c8")
        igram.tofile(d / ISCE_WRAPPED_FILE)
        (d / f"{ISCE_WRAPPED_FILE}.xml").write_text(
            isce_sidecar_xml(nx, ny, "CFLOAT", "BIP", ISCE_WRAPPED_FILE), encoding="utf-8"
        )
        np.asarray(stack.coherence[i], dtype="<f4").tofile(d / ISCE_COHERENCE_FILE)
        (d / f"{ISCE_COHERENCE_FILE}.xml").write_text(
            isce_sidecar_xml(nx, ny, "FLOAT", "BIL", ISCE_COHERENCE_FILE), encoding="utf-8"
        )
    return root
