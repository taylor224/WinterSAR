"""fringe_density on the xp backend (PERF-10, ADR-0097): bit-identical CPU behaviour, float
result, degrade path."""

from __future__ import annotations

import numpy as np
import pytest

from wintersar.compute import xp as xpmod
from wintersar.research import synth
from wintersar.unwrap.scheduler import fringe_density


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WINTERSAR_GPU", raising=False)
    xpmod.reset_backend_cache()
    yield
    xpmod.reset_backend_cache()


def _reference(wrapped, mask=None) -> float:
    w = np.asarray(wrapped)
    phase = np.angle(w).astype(np.float64) if np.iscomplexobj(w) else w.astype(np.float64)
    valid = np.isfinite(phase)
    if mask is not None:
        valid &= ~np.asarray(mask, dtype=bool)
    z = np.exp(1j * np.where(valid, phase, 0.0))
    dy = np.angle(z[1:, :] * np.conj(z[:-1, :]))
    dx = np.angle(z[:, 1:] * np.conj(z[:, :-1]))
    vy = valid[1:, :] & valid[:-1, :]
    vx = valid[:, 1:] & valid[:, :-1]
    vals = np.concatenate([np.abs(dy[vy]), np.abs(dx[vx])])
    return float("nan") if vals.size == 0 else float(np.mean(vals) / np.pi)


def test_fringe_density_is_bit_identical_to_the_numpy_reference(rng) -> None:
    ig = synth.make_interferogram(
        (60, 70), rng, atmosphere_std_rad=0.6, coherence_base=0.5, looks=2, water_fraction=0.2
    )
    wrapped = ig.wrapped.astype(np.float32)
    wrapped[5, 5] = np.nan
    out = fringe_density(wrapped, ig.mask, gpu=False)
    assert isinstance(out, float) and out == _reference(wrapped, ig.mask)
    assert fringe_density(ig.complex, gpu=False) == _reference(ig.complex)
    assert np.isnan(fringe_density(wrapped, np.ones_like(ig.mask), gpu=False))


def test_fringe_density_gpu_request_without_cupy_degrades(monkeypatch, rng) -> None:
    monkeypatch.setattr(xpmod, "cupy_available", lambda: False)
    w = rng.uniform(-np.pi, np.pi, (16, 20))
    ref = fringe_density(w, gpu=False)
    monkeypatch.setenv("WINTERSAR_GPU", "1")
    assert fringe_density(w) == ref and fringe_density(w, gpu=True) == ref
    assert xpmod.resolve_backend().findings[0].rule_id == "ENV-005"
    monkeypatch.delenv("WINTERSAR_GPU")
    with xpmod.stage_gpu(True):
        assert fringe_density(w) == ref


def test_fringe_density_validation_unchanged() -> None:
    with pytest.raises(ValueError, match="2-D"):
        fringe_density(np.zeros(10), gpu=False)
    with pytest.raises(ValueError, match="mask shape"):
        fringe_density(np.zeros((4, 4)), np.zeros((3, 3), dtype=bool), gpu=False)
