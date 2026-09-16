"""Mask combination and NaN semantics (plan §5.4: masked pixels -> NaN, conncomp 0)."""

from __future__ import annotations

import numpy as np
import pytest

from wintersar.unwrap.masks import apply_mask, combine_masks, mask_stats, masked_conncomp


def test_combine_masks_threshold_nan_and_layers():
    coh = np.array([[0.9, 0.2], [np.nan, 0.5]], dtype=np.float32)
    m = combine_masks(coh, 0.3)
    assert m.tolist() == [[False, True], [True, False]]
    water = np.array([[True, False], [False, False]])
    layover = np.array([[False, False], [False, True]])
    m2 = combine_masks(coh, 0.3, water=water, layover=layover)
    assert m2.tolist() == [[True, True], [True, True]]
    extra = np.zeros((2, 2), dtype=bool)
    assert combine_masks(coh, 0.0, extra=extra).tolist() == [[False, False], [True, False]]


def test_combine_masks_shape_mismatch():
    with pytest.raises(ValueError):
        combine_masks(np.ones((2, 2)), 0.3, water=np.zeros((3, 3), dtype=bool))


def test_apply_mask_sets_nan_and_conncomp_zero():
    unw = np.arange(6, dtype=np.float32).reshape(2, 3)
    mask = np.array([[True, False, False], [False, False, True]])
    out = apply_mask(unw, mask)
    assert out.dtype == np.float32
    assert np.isnan(out[0, 0]) and np.isnan(out[1, 2])
    assert out[0, 1] == 1.0 and not np.isnan(unw).any()  # input untouched
    cc = masked_conncomp(np.ones((2, 3), dtype=np.uint8), mask)
    assert cc[0, 0] == 0 and cc[1, 2] == 0 and cc[0, 1] == 1
    cc_float = masked_conncomp(np.ones((2, 3)), mask)
    assert cc_float.dtype.kind in "ui"
    with pytest.raises(ValueError):
        apply_mask(unw, np.zeros((3, 3), dtype=bool))


def test_mask_stats_node_reduction():
    mask = np.zeros((10, 10), dtype=bool)
    mask[:3] = True
    s = mask_stats(mask)
    assert s == {"n_pixels": 100, "n_masked": 30, "n_nodes": 70, "masked_fraction": 0.3}
    assert mask_stats(np.zeros((0, 0), dtype=bool))["masked_fraction"] == 0.0
