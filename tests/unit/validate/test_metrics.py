"""InSAR vs ground-truth metrics on synthetic truth (R-10): RMSE at the noise level."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from wintersar.validate.ground_truth import load_csv
from wintersar.validate.metrics import (
    align_dates,
    compare,
    fit_velocity,
    haversine_m,
    pearson,
    pixels_within_radius,
    sample_series,
    site_series,
)

NOISE_M = 0.001  # noise injected by scripts that generated the fixtures (1 mm)


def test_haversine_scale():
    assert haversine_m(37.0, 127.0, 38.0, 127.0) == pytest.approx(111_195.0, rel=1e-3)
    d = haversine_m(np.array([37.0, 37.0]), np.array([127.0, 127.001]), 37.0, 127.0)
    assert d[0] == 0.0 and 85 < d[1] < 95  # 1e-3 deg lon at 37°N ≈ 88.8 m


def test_pixels_within_radius_and_site_series(synth_ts):
    lat, lon = float(synth_ts.lat2d()[16, 16]), float(synth_ts.lon2d()[16, 16])
    rows, cols = pixels_within_radius(synth_ts, lat, lon, 100.0)
    assert (16, 16) in set(zip(rows.tolist(), cols.tolist(), strict=True))
    assert 15 <= len(rows) <= 25  # ≈ π·(100/40)² ≈ 19.6 pixels
    rows1, _cols1 = pixels_within_radius(synth_ts, lat, lon, 1.0)
    assert len(rows1) == 1  # smaller than the pixel spacing -> nearest pixel only
    series, n, d_near = site_series(synth_ts, lat, lon, 100.0)
    assert series.shape == (synth_ts.n_dates,) and n == len(rows) and d_near < 1.0
    med, _, _ = site_series(synth_ts, lat, lon, 100.0, method="median")
    assert med.shape == series.shape


def test_align_and_sample():
    d0 = date(2024, 1, 1)
    ts_dates = [d0 + timedelta(days=12 * i) for i in range(4)]
    gt = [
        d0 + timedelta(days=2),
        d0 + timedelta(days=30),
        d0 + timedelta(days=100),
        d0 - timedelta(days=1),
    ]
    near = align_dates(ts_dates, gt, max_gap_days=6)
    assert near == [
        (0, 0.0),
        (1, 2.0),
        (3, 0.0),
    ]  # +30 d -> 36 d (gap 6); 100 d dropped; -1 d within 6
    near2 = align_dates(ts_dates, gt, max_gap_days=1)
    assert near2 == [(3, 0.0)]
    interp = align_dates(ts_dates, gt, mode="interp")
    assert [gi for gi, _ in interp] == [0, 1]
    assert interp[0][1] == pytest.approx(2 / 12) and interp[1][1] == pytest.approx(2.5)
    series = np.array([0.0, 12.0, 24.0, 36.0])
    np.testing.assert_allclose(
        sample_series(series, [0.0, 2 / 12, 2.5, 3.0]), [0.0, 2.0, 30.0, 36.0]
    )


def test_fit_velocity_and_pearson():
    days = np.array([0, 100, 200, 365.25])
    assert fit_velocity(days, days / 365.25 * 0.02) == pytest.approx(0.02)
    assert np.isnan(fit_velocity([0.0], [1.0]))
    assert pearson([0, 1, 2, 3], [0, 2, 4, 6]) == pytest.approx(1.0)
    assert np.isnan(pearson([1, 1, 1], [0, 1, 2]))


def test_compare_leveling_fixture_reaches_noise_level(synth_ts, leveling_csv):
    gt = load_csv(leveling_csv)
    res = compare(synth_ts, gt, radius_m=100.0)
    by = {s.site_id: s for s in res.per_site}
    assert set(by) == {"L01-center", "L02-edge", "L03-slope", "L99-outside"}
    assert res.n_sites == 3
    for sid in ("L01-center", "L02-edge", "L03-slope"):
        s = by[sid]
        assert s.n >= 5 and s.n_pixels > 10
        assert s.rmse_m < 3 * NOISE_M, (sid, s.rmse_m)
        assert abs(s.bias_m) < 2 * NOISE_M, (sid, s.bias_m)
        assert s.insar_m[0] == 0.0 and s.gt_los_m[0] == 0.0  # referenced to the first common date
        assert s.reference_date == s.dates[0]
        assert abs(s.velocity_diff_m_per_yr) < 0.01
    centre = by["L01-center"]
    assert centre.corr > 0.99 and centre.insar_velocity_m_per_yr < -0.05  # subsiding bowl centre
    assert abs(by["L02-edge"].insar_velocity_m_per_yr) < 0.01
    # nearest-date alignment: L01 dates are +2 d, L03 -3 d, both within 6 d
    assert centre.n == 10 and by["L03-slope"].n == 10
    outside = by["L99-outside"]
    assert outside.n == 0 and outside.findings[0].rule_id == "VAL-009"
    assert [f.rule_id for f in res.findings] == ["VAL-009"] and res.findings[
        0
    ].scope == "L99-outside"
    assert res.rmse_m < 3 * NOISE_M and abs(res.bias_m) < 2 * NOISE_M
    assert res.n_points == sum(s.n - 1 for s in res.sites_compared)
    d = res.to_dict()
    assert d["n_sites"] == 3 and len(d["per_site"]) == 4


@pytest.mark.parametrize("align", ["nearest", "interp"])
def test_compare_gnss_fixture(synth_ts, gnss_csv, align):
    gt = load_csv(gnss_csv)
    res = compare(synth_ts, gt, radius_m=100.0, align=align)
    assert res.n_sites == 2 and not res.findings
    for s in res.sites_compared:
        assert s.method == "gnss" and s.incidence_deg == pytest.approx(39.0)
        assert s.rmse_m < 3 * NOISE_M, (s.site_id, align, s.rmse_m)
        assert s.n >= 10  # 6-day epochs: every GT epoch is within ±6 d of an InSAR date
    if align == "interp":
        assert all(s.n == 19 for s in res.sites_compared)


def test_compare_too_few_epochs_and_heading_override(synth_ts, leveling_csv):
    gt = [r for r in load_csv(leveling_csv) if r.site_id == "L01-center"][:1]
    res = compare(synth_ts, gt)
    assert res.n_sites == 0 and res.findings[0].rule_id == "VAL-010"
    gt2 = [r for r in load_csv(leveling_csv) if r.site_id == "L01-center"]
    res2 = compare(synth_ts, gt2, incidence_deg=30.0)
    assert res2.sites_compared[0].incidence_deg == 30.0
    # projecting with the wrong incidence scales the GT series and must worsen the fit
    assert res2.rmse_m > compare(synth_ts, gt2).rmse_m
