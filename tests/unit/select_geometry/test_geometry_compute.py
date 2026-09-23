"""select/geometry_masks on the xp backend (PERF-10, ADR-0097): the CPU path is the same
numpy arithmetic as before the port (checked against a frozen pure-numpy reference), public
functions return numpy, and a GPU request without CuPy degrades with ENV-005."""

from __future__ import annotations

import numpy as np
import pytest

from tests.unit.select_geometry.synthetic import ridge_dem
from wintersar.compute import xp as xpmod
from wintersar.select import geometry_masks as gm


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WINTERSAR_GPU", raising=False)
    xpmod.reset_backend_cache()
    yield
    xpmod.reset_backend_cache()


def _legacy_slope_aspect(dem, dx_m, dy_m):
    z = np.asarray(dem, dtype=np.float64)
    dz_dcol = np.gradient(z, axis=1) / abs(dx_m)
    if dx_m < 0:
        dz_dcol = -dz_dcol
    dz_drow = np.gradient(z, axis=0)
    dz_dnorth = -dz_drow / dy_m
    slope = np.degrees(np.arctan(np.hypot(dz_dcol, dz_dnorth)))
    aspect = np.degrees(np.arctan2(-dz_dcol, -dz_dnorth)) % 360.0
    aspect = np.where(slope == 0.0, 0.0, aspect)
    invalid = ~np.isfinite(z) | ~np.isfinite(slope)
    return np.where(invalid, np.nan, slope), np.where(invalid, np.nan, aspect)


def _legacy_local_incidence(slope_deg, aspect_deg, sensor_azimuth_deg, incidence_deg):
    s = np.radians(np.asarray(slope_deg, dtype=np.float64))
    a = np.radians(np.asarray(aspect_deg, dtype=np.float64))
    th = np.radians(np.asarray(incidence_deg, dtype=np.float64))
    phi = np.radians(sensor_azimuth_deg)
    cos_loc = np.cos(s) * np.cos(th) + np.sin(s) * np.sin(th) * np.cos(phi - a)
    return np.degrees(np.arccos(np.clip(cos_loc, -1.0, 1.0)))


@pytest.fixture
def dem() -> np.ndarray:
    d = ridge_dem(40, 56, 30.0, 350.0, 4.0, sigma_px_east=12.0)
    d[7:9, 30:33] = np.nan
    return d


def test_slope_aspect_and_angles_are_bit_identical_to_numpy(dem) -> None:
    slope, aspect = gm.slope_aspect(dem, 30.0, -30.0, gpu=False)
    ref_s, ref_a = _legacy_slope_aspect(dem, 30.0, -30.0)
    assert isinstance(slope, np.ndarray) and slope.dtype == np.float64
    np.testing.assert_array_equal(slope, ref_s)
    np.testing.assert_array_equal(aspect, ref_a)
    phi = gm.sensor_azimuth(192.0)
    np.testing.assert_array_equal(
        gm.local_incidence(slope, aspect, phi, 39.0, gpu=False),
        _legacy_local_incidence(slope, aspect, phi, 39.0),
    )
    # per-pixel incidence map and scalar inputs keep working
    inc = np.full(dem.shape, 41.0)
    np.testing.assert_array_equal(
        gm.local_incidence(slope, aspect, phi, inc, gpu=False),
        _legacy_local_incidence(slope, aspect, phi, inc),
    )
    assert gm.local_incidence(0.0, 0.0, phi, 39.0, gpu=False) == pytest.approx(39.0)
    rs = gm.range_slope(slope, aspect, phi, gpu=False)
    s, a = np.radians(slope), np.radians(aspect)
    np.testing.assert_array_equal(
        rs, np.degrees(np.arctan(np.tan(s) * np.cos(np.radians(phi) - a)))
    )


def test_compute_geometry_masks_returns_numpy_and_matches_per_step_functions(dem) -> None:
    r = gm.compute_geometry_masks(dem, 30.0, 30.0, -12.0, 39.0, "ascending", gpu=False)
    for arr in (r.layover, r.shadow, r.foreshortening, r.local_incidence_deg):
        assert isinstance(arr, np.ndarray)
    assert r.layover.dtype == bool and r.foreshortening.dtype == np.float64
    slope, aspect = gm.slope_aspect(dem, 30.0, 30.0)
    phi = gm.sensor_azimuth(-12.0)
    theta_loc = gm.local_incidence(slope, aspect, phi, 39.0)
    alpha_r = gm.range_slope(slope, aspect, phi)
    np.testing.assert_array_equal(r.local_incidence_deg, theta_loc)
    valid = np.isfinite(theta_loc) & np.isfinite(alpha_r)
    np.testing.assert_array_equal(r.layover, valid & (alpha_r > 39.0))
    np.testing.assert_array_equal(r.shadow, valid & (alpha_r < 39.0 - 90.0))
    assert np.isnan(r.foreshortening[7, 31]) and np.isnan(r.local_incidence_deg[7, 31])
    assert r.layover.any() and r.stats["aoi_pixels"] == int(valid.sum())


def test_geometry_gpu_request_without_cupy_degrades(dem, monkeypatch) -> None:
    monkeypatch.setattr(xpmod, "cupy_available", lambda: False)
    ref = gm.masks_for_both_directions(dem, 30.0, 30.0, 39.0, gpu=False)
    monkeypatch.setenv("WINTERSAR_GPU", "1")
    out = gm.masks_for_both_directions(dem, 30.0, 30.0, 39.0, gpu=True)
    for d in (gm.ASCENDING, gm.DESCENDING):
        np.testing.assert_array_equal(out[d].layover, ref[d].layover)
        np.testing.assert_array_equal(out[d].foreshortening, ref[d].foreshortening)
        assert out[d].stats == ref[d].stats
    assert xpmod.resolve_backend().findings[0].rule_id == "ENV-005"
    monkeypatch.delenv("WINTERSAR_GPU")
    with xpmod.stage_gpu(True):
        s, _ = gm.slope_aspect(dem, 30.0, 30.0)
        np.testing.assert_array_equal(s, gm.slope_aspect(dem, 30.0, 30.0, gpu=False)[0])


def test_geometry_validation_errors_unchanged() -> None:
    with pytest.raises(ValueError):
        gm.slope_aspect(np.zeros((3, 3, 3)), 30.0, 30.0, gpu=False)
    with pytest.raises(ValueError):
        gm.slope_aspect(np.zeros((3, 3)), 0.0, 30.0, gpu=False)
    with pytest.raises(ValueError):
        gm.compute_geometry_masks(np.zeros((1, 3)), 30.0, 30.0, -12.0, 39.0, "ASCENDING")
