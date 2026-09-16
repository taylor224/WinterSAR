"""research/repr_phase.py — five representative-phase methods (R-07, ADR-0061/0062).

Accuracy checks: on a nearly noise-free interferogram every method's low-res phase is
close to the block mean of the true unwrapped phase; SHP prefers same-region neighbours;
EVD/EMI recover the injected linked phases of a stack with a known covariance.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import ks_2samp, ttest_ind

from wintersar.research import repr_phase as rp
from wintersar.research import synth
from wintersar.research.metrics import phase_rmse

F = 3


def _truth(ig: synth.SynthIgram, f: int = F) -> np.ndarray:
    return rp.truth_lowres_phase(ig.unw_true, f)


# ---------------------------------------------------------------- helpers
def test_multilook_matches_tophu_block_mean_and_crops():
    a = np.arange(7 * 8, dtype=float).reshape(7, 8)
    ml = rp.multilook(a, 3)
    assert ml.shape == (2, 2)
    np.testing.assert_allclose(ml[0, 0], a[:3, :3].mean())
    np.testing.assert_allclose(ml[1, 1], a[3:6, 3:6].mean())
    z = np.exp(1j * a)
    np.testing.assert_allclose(rp.multilook(z, 2)[0, 0], z[:2, :2].mean())
    assert rp.multilook(a, 1).shape == a.shape and rp.lowres_shape((7, 8), 3) == (2, 2)
    up = rp.upsample_nearest(ml, (7, 8))
    assert up.shape == (7, 8) and up[0, 0] == ml[0, 0] and up[6, 7] == ml[1, 1]
    with pytest.raises(rp.ResearchError):
        rp.multilook(a, 0)


@pytest.mark.parametrize(
    ("method", "kw", "tol"),
    [
        ("ml", {}, 0.1),
        ("coh_weighted", {"p": 1.0}, 0.1),
        ("coh_weighted", {"p": 0.0}, 0.1),
        ("filtered", {"alpha": 0.5, "window": 16, "overlap": 0.5}, 0.25),
        ("filtered", {"alpha": 0.8, "window": 8, "overlap": 0.0, "spectrum_smooth": 3}, 0.3),
    ],
)
def test_single_igram_methods_match_lowres_truth(clean_igram, method, kw, tol):
    est = rp.representative_phase(method, clean_igram.complex, clean_igram.coherence, F, **kw)
    assert est.shape == rp.lowres_shape(clean_igram.shape, F) and est.dtype == np.complex128
    assert phase_rmse(est, _truth(clean_igram)) < tol
    assert np.all(np.abs(est) <= 1.0 + 1e-9)


def test_ml_equals_tophu_style_block_mean_of_complex(clean_igram):
    z = clean_igram.complex
    est = rp.repr_ml(z, None, F)
    ref = z[: 48 - 48 % F, : 48 - 48 % F].reshape(16, F, 16, F).mean(axis=(1, 3))
    np.testing.assert_allclose(est, ref)


def test_coh_weighted_ignores_zero_coherence_pixels(clean_igram):
    z = clean_igram.complex.copy()
    coh = clean_igram.coherence.copy()
    # corrupt half of the first block with a wrong phase but zero coherence
    z[0:3, 0:1] = np.exp(1j * 3.0)
    coh[0:3, 0:1] = 0.0
    est = rp.repr_coh_weighted(z, coh, F, p=1.0)
    clean = np.exp(1j * clean_igram.unw_true[0:3, 1:3]).mean()
    assert abs(np.angle(est[0, 0]) - np.angle(clean)) < 0.05


def test_real_input_is_taken_as_wrapped_phase(clean_igram):
    a = rp.repr_ml(clean_igram.wrapped, None, F)
    b = rp.repr_ml(np.exp(1j * clean_igram.wrapped), None, F)
    np.testing.assert_allclose(a, b)


def test_goldstein_filter_reduces_noise_and_keeps_magnitude():
    rng = np.random.default_rng(2)
    ig = synth.make_interferogram(
        (64, 64), rng, atmosphere_std_rad=0.2, coherence_base=0.6, looks=1
    )
    z = ig.complex
    filt = rp.goldstein_filter(z, alpha=0.8, window=16, overlap=0.5)
    assert filt.shape == z.shape
    np.testing.assert_allclose(np.abs(filt), np.abs(z), atol=1e-9)
    err_raw = rp.wrap(np.angle(z) - ig.unw_true)
    err_filt = rp.wrap(np.angle(filt) - ig.unw_true)
    assert np.std(err_filt) < 0.8 * np.std(err_raw)
    with pytest.raises(rp.ResearchError):
        rp.goldstein_filter(z, window=15)


# ---------------------------------------------------------------- SHP
def test_ks_statistic_matches_scipy_and_critical_value():
    rng = np.random.default_rng(3)
    a = rng.rayleigh(1.0, (25, 4, 3))
    b = rng.rayleigh(1.3, (25, 4, 3))
    mine = rp.ks_statistic(a, b)
    ref = np.array(
        [[ks_2samp(a[:, i, j], b[:, i, j]).statistic for j in range(3)] for i in range(4)]
    )
    np.testing.assert_allclose(mine, ref, atol=1e-12)
    # asymptotic critical value: K_alpha * sqrt((n+m)/(nm)) with K_0.05 = 1.358 (scipy kstwobign)
    assert abs(rp.ks_critical_value(0.05, 30, 30) - 1.3581 * np.sqrt(60 / 900)) < 1e-3
    assert rp.ks_critical_value(0.01, 30, 30) > rp.ks_critical_value(0.05, 30, 30)
    with pytest.raises(rp.ResearchError):
        rp.ks_critical_value(1.5, 10, 10)


def test_t_test_accept_matches_scipy_welch():
    rng = np.random.default_rng(4)
    a = rng.rayleigh(1.0, (20, 30))
    b = rng.rayleigh(1.4, (20, 30))
    mine = rp.t_test_accept(a, b, 0.05)
    ref = ttest_ind(a, b, equal_var=False).pvalue > 0.05
    np.testing.assert_array_equal(mine, ref)


def test_shp_selects_more_neighbours_inside_region_than_across_boundary():
    rng = np.random.default_rng(8)
    amp, labels = synth.shp_amplitude_stack((32, 64), 30, rng, region_scales=(1.0, 4.0))
    f, w = 4, 7
    nb = rp.shp_neighbors(amp, f, window=w, alpha=0.05)
    assert nb.shape == (8, 16, w, w) and nb[:, :, w // 2, w // 2].all()
    fam = rp.shp_family_size(nb)
    # block centres cx = 4k + 2; the amplitude boundary is at x = 32
    cx = np.arange(16) * f + f // 2
    inside = (cx + w // 2 < 32) | (cx - w // 2 >= 32)
    straddle = ~inside
    interior_rows = slice(1, 7)  # avoid image-edge clipping
    n_inside = fam[interior_rows][:, inside].mean()
    n_straddle = fam[interior_rows][:, straddle].mean()
    assert n_inside > 0.85 * w * w  # ~95 % acceptance at alpha = 0.05
    assert n_straddle < 0.9 * n_inside  # 1-2 of 7 window columns lie across the boundary
    # selected neighbours of straddling blocks are (almost) all in the centre's own region
    lab_lo = labels[np.arange(8) * f + f // 2][:, cx]
    for bi in np.nonzero(straddle)[0][:2]:
        i = 3
        sel = nb[i, bi]
        ys = np.clip(np.arange(-3, 4) + (i * f + f // 2), 0, 31)
        xs = np.clip(np.arange(-3, 4) + cx[bi], 0, 63)
        same = labels[ys[:, None], xs[None, :]] == lab_lo[i, bi]
        assert (sel & ~same).sum() <= 0.1 * sel.sum()
    # the t-test variant works too and also prefers the homogeneous side
    nb_t = rp.shp_neighbors(amp, f, window=w, alpha=0.05, test="t")
    fam_t = rp.shp_family_size(nb_t)
    assert fam_t[interior_rows][:, inside].mean() > fam_t[interior_rows][:, straddle].mean()


def test_repr_shp_matches_truth_and_default_window():
    rng = np.random.default_rng(9)
    amp, _ = synth.shp_amplitude_stack((32, 32), 24, rng, region_scales=(1.0, 3.0))
    ig = synth.make_interferogram(
        (32, 32),
        rng,
        deformation_amplitude_m=-0.01,
        atmosphere_std_rad=0.2,
        coherence_base=0.95,
        looks=64,
    )
    truth = rp.truth_lowres_phase(ig.unw_true, 4)
    est = rp.representative_phase("shp", ig.complex, ig.coherence, 4, stack=amp)
    assert est.shape == (8, 8)
    assert phase_rmse(est, truth) < 0.2
    est_px = rp.representative_phase(
        "shp", ig.complex, ig.coherence, 4, stack=amp, mode="pixelwise"
    )
    assert est_px.shape == (8, 8) and phase_rmse(est_px, truth) < 0.2
    full = rp.shp_adaptive_multilook(ig.complex, amp, window=5)
    assert full.shape == (32, 32)
    assert rp.default_shp_window(3) == 3 and rp.default_shp_window(4) == 5
    assert rp.default_shp_window(8) == 9 and rp.default_shp_window(20) == 15
    with pytest.raises(rp.ResearchError):
        rp.representative_phase("shp", ig.complex, ig.coherence, 4, stack=amp, mode="x")
    with pytest.raises(rp.ResearchError):
        rp.shp_neighbors(amp, 4, window=17)
    with pytest.raises(rp.ResearchError):
        rp.representative_phase("shp", ig.complex, ig.coherence, 4)


# ---------------------------------------------------------------- phase linking
@pytest.mark.parametrize("method", ["evd", "emi"])
def test_phase_linking_recovers_injected_phases(slc_stack, method):
    f = 8
    phases, tc = rp.phase_link_stack(slc_stack.slc, f, method)
    assert phases.shape == (10, 8, 8) and tc.shape == (8, 8)
    assert np.all(phases[0] == 0.0)
    err = rp.wrap(phases - rp.multilook(slc_stack.phase_true, f))
    assert np.sqrt(np.mean(err**2)) < 0.2
    assert np.abs(err).max() < 0.8
    assert tc.mean() > 0.95


def test_phase_linking_on_exact_covariance_is_exact():
    n = 6
    gamma = synth.coherence_matrix_model(n, 3.0, 0.2)
    phi = np.array([0.0, 0.4, -1.0, 2.2, 3.0, -2.5])
    c = gamma * np.exp(1j * (phi[:, None] - phi[None, :]))
    for method in ("evd", "emi"):
        ph, tc = rp.link_phases(c[None, None], method)
        np.testing.assert_allclose(rp.wrap(ph[0, 0] - phi), 0.0, atol=1e-8)
        np.testing.assert_allclose(tc, 1.0, atol=1e-8)


def test_sequential_estimator_matches_full_stack_and_pair_phase(slc_stack):
    f = 8
    full, _ = rp.phase_link_stack(slc_stack.slc, f, "evd")
    seq, _ = rp.phase_link_stack(slc_stack.slc, f, "evd", ministack_size=4)
    truth = rp.multilook(slc_stack.phase_true, f)
    assert np.sqrt(np.mean(rp.wrap(seq - truth) ** 2)) < 0.25
    assert np.sqrt(np.mean(rp.wrap(seq - full) ** 2)) < 0.25
    est = rp.representative_phase("phase_link", None, None, f, stack=slc_stack.slc, pair=(0, 5))
    assert est.shape == (8, 8)
    assert phase_rmse(est, rp.multilook(slc_stack.pair_phase_true(0, 5), f)) < 0.25
    assert np.all(np.abs(est) <= 1.0 + 1e-9)
    with pytest.raises(rp.ResearchError, match=r"pair|쌍"):
        rp.representative_phase("phase_link", None, None, f, stack=slc_stack.slc)


def test_dispatcher_errors_are_diagnostics():
    with pytest.raises(rp.ResearchError) as ei:
        rp.representative_phase("nope", np.ones((4, 4)), None, 2)
    assert ei.value.rule_id == "RES-001" and ei.value.fix
    with pytest.raises(rp.ResearchError) as ei:
        rp.representative_phase("ml", np.ones((4, 4)), None, 0)
    assert ei.value.rule_id == "RES-006"
    assert set(rp.METHODS) == {"ml", "coh_weighted", "shp", "phase_link", "filtered"}
