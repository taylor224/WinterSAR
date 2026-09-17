"""research/synth.py ground-truth generator (Phase 0 minimal)."""

from __future__ import annotations

import numpy as np
import pytest

from wintersar.research import synth


def test_interferogram_wrapped_in_range_and_truth_consistent(rng):
    ig = synth.make_interferogram((64, 64), rng, atmosphere_std_rad=0.5, looks=4)
    assert ig.wrapped.min() >= -np.pi and ig.wrapped.max() <= np.pi
    # with high coherence, wrapped(noisy) ≈ wrap(true)
    err = np.angle(np.exp(1j * (ig.wrapped - ig.unw_true)))
    assert np.abs(err).mean() < 0.6
    assert synth.unwrap_error_fraction(ig.unw_true, ig.unw_true) == 0.0


def test_stack_is_loop_closure_consistent(rng):
    st = synth.make_stack(n_dates=5, shape=(16, 16), rng=rng, atmosphere_std_rad=0.4)
    d = st.dates
    k = lambda a, b: f"{a:%Y%m%d}_{b:%Y%m%d}"  # noqa: E731
    if k(d[0], d[1]) in st.igrams and k(d[1], d[2]) in st.igrams and k(d[0], d[2]) in st.igrams:
        closure = (
            st.igrams[k(d[0], d[1])].unw_true
            + st.igrams[k(d[1], d[2])].unw_true
            - st.igrams[k(d[0], d[2])].unw_true
        )
        np.testing.assert_allclose(closure, 0.0, atol=1e-6)
    assert st.displacement_m[0].max() == 0.0
    assert st.velocity_m_per_yr.min() < 0


def test_sbas_pairs_thresholds():
    from datetime import date

    dates = [date(2024, 1, 1) + __import__("datetime").timedelta(days=12 * i) for i in range(5)]
    pairs = synth.sbas_pairs(dates, max_temporal_days=24)
    assert all(p.temporal_baseline_days <= 24 for p in pairs)
    assert len(pairs) == 4 + 3
    bp = {d: float(i * 100) for i, d in enumerate(dates)}
    pairs2 = synth.sbas_pairs(dates, 48, bp, max_perp_m=150)
    assert all(abs(p.perp_baseline_m or 0) <= 150 for p in pairs2)


def test_water_mask_and_atmosphere_stats(rng):
    m = synth.water_mask((10, 20), 0.25)
    assert m[:, :5].all() and not m[:, 5:].any()
    a = synth.turbulent_atmosphere((64, 64), rng, std_rad=2.0)
    assert abs(a.std() - 2.0) < 1e-6 and abs(a.mean()) < 1e-9


def test_phase_noise_crlb_is_a_lower_bound_and_exact_model_matches_the_true_pdf(rng):
    """Finding: ``_phase_noise`` used the Cramér-Rao bound for every ``looks``, which is up
    to ~2.2x below the true single-look phase std (Lee et al. 1994). The default is a domain
    checkpoint (rule 11.10) so it stays; ``noise_model="exact"`` draws the real pdf."""
    # analytic single-look std of the exact interferometric phase pdf, by quadrature
    from scipy.integrate import quad

    def pdf(phi: float, g: float) -> float:
        b = g * np.cos(phi)
        return float(
            (1 - g**2)
            / (2 * np.pi)
            / (1 - b**2)
            * (1 + b * (np.pi / 2 + np.arcsin(b)) / np.sqrt(1 - b**2))
        )

    for gamma in (0.8, 0.95):
        coh = np.full((200, 200), gamma)
        exact_std = np.sqrt(quad(lambda p, g=gamma: p * p * pdf(p, g), -np.pi, np.pi)[0])
        crlb_std = np.sqrt((1 - gamma**2) / (2 * gamma**2))
        assert crlb_std < 0.75 * exact_std  # the bound is far from the truth at looks=1

        drawn_exact = synth._phase_noise(coh, rng, 1, "exact")
        assert abs(np.sqrt(np.mean(drawn_exact**2)) - exact_std) < 0.05 * exact_std
        drawn_crlb = synth._phase_noise(coh, rng, 1, "crlb")
        assert abs(np.sqrt(np.mean(drawn_crlb**2)) - crlb_std) < 0.05 * crlb_std
        # many looks: the bound is tight, both models agree within 10 %
        many = synth._phase_noise(coh, rng, 16, "exact")
        assert abs(np.std(many) / np.sqrt((1 - gamma**2) / (32 * gamma**2)) - 1.0) < 0.1

    with pytest.raises(ValueError, match="noise model"):
        synth._phase_noise(np.full((4, 4), 0.8), rng, 1, "bogus")


def test_noise_model_reaches_interferograms_and_stacks_and_default_is_unchanged():
    ig_crlb = synth.make_interferogram(
        (64, 64), np.random.default_rng(3), atmosphere_std_rad=0.0, coherence_base=0.9
    )
    ig_exact = synth.make_interferogram(
        (64, 64),
        np.random.default_rng(3),
        atmosphere_std_rad=0.0,
        coherence_base=0.9,
        noise_model="exact",
    )
    # compare on the high-coherence pixels, where the wrapped residual is not saturated
    hi = ig_crlb.coherence > 0.88
    assert hi.sum() > 500
    err = lambda ig: np.std(synth.wrap(ig.wrapped - ig.unw_true)[hi])  # noqa: E731
    assert err(ig_exact) > 1.5 * err(ig_crlb)
    assert ig_crlb.meta["noise_model"] == "crlb" and ig_exact.meta["noise_model"] == "exact"
    st = synth.make_stack(n_dates=4, shape=(16, 16), rng=np.random.default_rng(1))
    assert all(i.meta["noise_model"] == "crlb" for i in st.igrams.values())


def test_phase_per_m_los_sign_matches_adr_0040():
    """ADR-0040 / io.timeseries / HyP3: LOS positive = towards the satellite (range
    decrease) and the phase of that motion is negative."""
    assert synth.PHASE_PER_M_LOS < 0
    towards_satellite_m = 0.01
    ig = synth.make_interferogram(
        (8, 8),
        np.random.default_rng(0),
        displacement_m=np.full((8, 8), towards_satellite_m),
        atmosphere_std_rad=0.0,
    )
    expected = -4.0 * np.pi / synth.WAVELENGTH_S1_M * towards_satellite_m
    np.testing.assert_allclose(ig.unw_true, expected)
    assert expected < 0
