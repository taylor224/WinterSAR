"""combine_masks on the xp backend (PERF-10, ADR-0097): CPU results are unchanged, the
result is always numpy, and a GPU request without CuPy degrades with ENV-005."""

from __future__ import annotations

import numpy as np
import pytest

from wintersar.compute import xp as xpmod
from wintersar.unwrap.masks import combine_masks


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WINTERSAR_GPU", raising=False)
    xpmod.reset_backend_cache()
    yield
    xpmod.reset_backend_cache()


def _reference(coh, threshold, water=None, layover=None, extra=None):
    c = np.asarray(coh, dtype=np.float32)
    mask = ~np.isfinite(c)
    if threshold > 0.0:
        mask |= c < np.float32(threshold)
    for m in (water, layover, extra):
        if m is not None:
            mask |= np.asarray(m, dtype=bool)
    return mask


def test_combine_masks_matches_pure_numpy_reference(rng) -> None:
    coh = rng.random((40, 50)).astype(np.float32)
    coh[rng.random((40, 50)) < 0.05] = np.nan
    water = rng.random((40, 50)) < 0.2
    layover = rng.random((40, 50)) < 0.1
    for thr in (0.0, 0.3, 0.7):
        out = combine_masks(coh, thr, water=water, layover=layover, gpu=False)
        assert isinstance(out, np.ndarray) and out.dtype == bool
        np.testing.assert_array_equal(out, _reference(coh, thr, water, layover))
    # float64 coherence is compared in float32 exactly as before
    c64 = rng.random((8, 8))
    np.testing.assert_array_equal(combine_masks(c64, 0.5), _reference(c64, 0.5))


def test_combine_masks_gpu_request_without_cupy_degrades(monkeypatch, rng) -> None:
    monkeypatch.setattr(xpmod, "cupy_available", lambda: False)
    coh = rng.random((12, 9)).astype(np.float32)
    ref = combine_masks(coh, 0.5, gpu=False)
    monkeypatch.setenv("WINTERSAR_GPU", "1")
    np.testing.assert_array_equal(combine_masks(coh, 0.5), ref)
    np.testing.assert_array_equal(combine_masks(coh, 0.5, gpu=True), ref)
    assert xpmod.resolve_backend().findings[0].rule_id == "ENV-005"
    monkeypatch.delenv("WINTERSAR_GPU")
    with xpmod.stage_gpu(True):
        np.testing.assert_array_equal(combine_masks(coh, 0.5), ref)


def test_combine_masks_shape_mismatch_still_a_value_error() -> None:
    with pytest.raises(ValueError, match="water"):
        combine_masks(np.ones((2, 2)), 0.3, water=np.zeros((3, 3), dtype=bool), gpu=False)
