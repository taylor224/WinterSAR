"""Loop closure (plan §5.6, §12.1, ADR-0042): consistent synthetic stack -> ~0; an injected
2π error in one interferogram ranks that interferogram first with integer residual ±1."""

from __future__ import annotations

import json

import numpy as np

from wintersar.io.igrams import IgramStack
from wintersar.validate.closure import (
    ClosureResult,
    closure_coherence,
    closure_phase,
    design_matrix,
    triplets,
    wrap,
    write_closure_maps,
    write_dashboard_json,
)


def test_triplets_and_design_matrix_follow_mintpy_convention():
    pairs = ["20240101_20240113", "20240113_20240125", "20240101_20240125", "20240125_20240206"]
    tri = triplets(pairs)
    assert tri == [(0, 1, 2)]  # (ij, jk, ik)
    c = design_matrix(pairs)
    assert c.shape == (1, 4)
    np.testing.assert_array_equal(c[0], [1, 1, -1, 0])  # row[idx12]=1, row[idx23]=1, row[idx13]=-1
    assert triplets(["20240101_20240113", "20240113_20240125"]) == []


def test_synthetic_stack_closure_is_zero(igram_stack: IgramStack):
    res = closure_phase(igram_stack, use_unw=True)
    assert res.mode == "unw" and res.n_triplets > 0
    assert res.per_pixel_rms.shape == igram_stack.shape
    assert np.nanmax(res.per_pixel_rms) < 1e-3  # float32 round-off only
    assert res.per_triplet_nonzero_fraction is not None
    assert float(res.per_triplet_nonzero_fraction.max()) == 0.0
    assert res.suspicious == []
    assert all(v == 0.0 for v in res.per_igram_score.values())
    assert res.n_valid_pixels == igram_stack.shape[0] * igram_stack.shape[1]
    # noise-free wrapped phase also closes (mod 2π)
    clean = IgramStack(
        wrapped=wrap(np.asarray(igram_stack.unw, dtype=np.float64)),
        coherence=igram_stack.coherence,
        pairs=igram_stack.pairs,
        dates=igram_stack.dates,
    )
    resw = closure_phase(clean, use_unw=False)
    assert resw.mode == "wrapped" and np.nanmax(resw.per_pixel_rms) < 1e-3
    assert resw.suspicious == []
    cc = closure_coherence(clean)
    assert np.nanmin(cc) > 0.999


def test_wrapped_mode_on_noisy_phase_has_finite_scores(igram_stack: IgramStack):
    res = closure_phase(igram_stack, use_unw=False)
    assert res.mode == "wrapped"
    assert np.isfinite(res.per_triplet_rms).all() and res.per_triplet_rms.max() <= np.pi * 2
    assert set(res.per_igram_score) == set(igram_stack.pairs)
    assert "wrapped_min_rms" in res.thresholds


def test_injected_2pi_error_ranks_first(igram_stack: IgramStack, tmp_path):
    bad = 3
    unw = np.asarray(igram_stack.unw, dtype=np.float32).copy()
    unw[bad, 8:20, 8:20] += 2.0 * np.pi
    stack = IgramStack(
        wrapped=igram_stack.wrapped,
        coherence=igram_stack.coherence,
        pairs=igram_stack.pairs,
        dates=igram_stack.dates,
        mask=igram_stack.mask,
        unw=unw,
    )
    res = closure_phase(stack, use_unw=True, keep_maps=True)
    bad_key = stack.pairs[bad]
    assert res.ranked_igrams()[0][0] == bad_key
    assert res.suspicious == [bad_key]  # greedy peeling does not drag neighbours along
    assert res.integer_multiples is not None and res.per_pixel_nonzero is not None
    involved = [i for i, tr in enumerate(res.triplets) if bad in tr]
    assert involved
    for i in involved:
        region = res.integer_multiples[i, 8:20, 8:20]
        assert set(np.unique(region).tolist()) <= {-1, 1} and (region != 0).all()
        outside = res.integer_multiples[i].copy()
        outside[8:20, 8:20] = 0
        assert not outside.any()
    assert np.nanmin(res.per_pixel_nonzero[8:20, 8:20]) >= 1
    assert np.nanmax(res.per_pixel_nonzero[:8, :8]) == 0
    # per-triplet nonzero fraction ≈ region share (144 / 1024) for the involved triplets
    frac = res.per_triplet_nonzero_fraction
    assert all(abs(frac[i] - 144 / 1024) < 1e-6 for i in involved)
    # dashboard export
    dash = res.to_dashboard()
    json.dumps(dash)
    assert dash["igrams"][0]["pair"] == bad_key and dash["igrams"][0]["suspicious"]
    assert dash["summary"]["pixels_with_any_nonzero_fraction"] > 0
    p = write_dashboard_json(res, tmp_path / "dash.json")
    assert json.loads(p.read_text(encoding="utf-8"))["suspicious"] == [bad_key]
    maps = write_closure_maps(res, tmp_path / "maps.npz")
    with np.load(maps) as z:
        assert z["integer_multiples"].shape == (res.n_triplets, *stack.shape)
        assert z["closure"].shape[0] == res.n_triplets


def test_mask_and_nan_pixels_are_excluded(igram_stack: IgramStack):
    mask = np.zeros(igram_stack.shape, dtype=bool)
    mask[0, 0] = True
    unw = np.asarray(igram_stack.unw, dtype=np.float32).copy()
    unw[:, 1, 1] = np.nan
    stack = IgramStack(
        wrapped=igram_stack.wrapped,
        coherence=igram_stack.coherence,
        pairs=igram_stack.pairs,
        dates=igram_stack.dates,
        mask=mask,
        unw=unw,
    )
    res = closure_phase(stack)
    assert np.isnan(res.per_pixel_rms[0, 0]) and np.isnan(res.per_pixel_rms[1, 1])
    assert res.n_valid_pixels == igram_stack.shape[0] * igram_stack.shape[1] - 2


def test_no_triplets_gives_empty_result():
    stack = IgramStack(
        wrapped=np.zeros((2, 4, 4), dtype=np.float32),
        coherence=np.ones((2, 4, 4), dtype=np.float32),
        pairs=["20240101_20240113", "20240113_20240125"],
        dates=[],
        unw=np.zeros((2, 4, 4), dtype=np.float32),
    )
    res = closure_phase(stack)
    assert isinstance(res, ClosureResult) and res.n_triplets == 0
    assert np.isnan(res.per_pixel_rms).all() and res.suspicious == []
    assert res.to_dashboard()["histogram_per_pixel_rms"]["counts"] == []
