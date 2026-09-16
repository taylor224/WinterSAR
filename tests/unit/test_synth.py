"""research/synth.py ground-truth generator (Phase 0 minimal)."""

from __future__ import annotations

import numpy as np

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
