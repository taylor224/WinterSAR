"""Unit tests for select/geometry_masks (R-04, SEL-12): conventions, masks, stats."""

from __future__ import annotations

import numpy as np
import pytest

from tests.unit.select_geometry.synthetic import plane_dem, ridge_dem
from wintersar import i18n
from wintersar.select import geometry_masks as gm

ASC, DESC = gm.ASCENDING, gm.DESCENDING

# ------------------------------------------------------------------ azimuth conventions


def test_look_azimuth_right_looking_is_heading_plus_90() -> None:
    assert gm.look_azimuth(-12.0) == pytest.approx(78.0)
    assert gm.look_azimuth(192.0) == pytest.approx(282.0)
    assert gm.look_azimuth(-12.0, right_looking=False) == pytest.approx(258.0)
    assert 0.0 <= gm.look_azimuth(350.0) < 360.0


def test_sensor_azimuth_is_opposite_of_look_azimuth() -> None:
    assert gm.sensor_azimuth(-12.0) == pytest.approx(258.0)
    assert gm.sensor_azimuth(192.0) == pytest.approx(102.0)
    for h in (-12.0, 192.0, 0.0, 45.0):
        assert (gm.sensor_azimuth(h) - gm.look_azimuth(h)) % 360.0 == pytest.approx(180.0)


# ------------------------------------------------------------------ slope / aspect


@pytest.mark.parametrize(
    ("rises_toward", "expected_aspect"),
    [("east", 270.0), ("west", 90.0), ("north", 180.0), ("south", 0.0)],
)
def test_slope_aspect_plane(rises_toward: str, expected_aspect: float) -> None:
    dem = plane_dem(20, 24, 30.0, 20.0, rises_toward)
    slope, aspect = gm.slope_aspect(dem, 30.0, 30.0)
    inner = (slice(1, -1), slice(1, -1))
    np.testing.assert_allclose(slope[inner], 20.0, atol=1e-9)
    np.testing.assert_allclose(aspect[inner], expected_aspect, atol=1e-9)


def test_slope_aspect_south_up_flips_north_south() -> None:
    dem = plane_dem(20, 24, 30.0, 20.0, "north")  # north-up: row 0 highest
    _, aspect_south_up = gm.slope_aspect(dem, 30.0, -30.0)  # tell it rows increase northward
    np.testing.assert_allclose(aspect_south_up[1:-1, 1:-1], 0.0, atol=1e-9)


def test_slope_aspect_anisotropic_spacing() -> None:
    # z = 0.5 * east_m, sampled with dx=10 m, dy=40 m: slope must not depend on dy
    ncols, nrows = 30, 12
    dem = np.tile(0.5 * np.arange(ncols) * 10.0, (nrows, 1))
    slope, aspect = gm.slope_aspect(dem, 10.0, 40.0)
    np.testing.assert_allclose(slope[:, 1:-1], np.degrees(np.arctan(0.5)), atol=1e-9)
    np.testing.assert_allclose(aspect[:, 1:-1], 270.0)


def test_slope_aspect_flat_and_nan() -> None:
    dem = np.full((10, 10), 50.0)
    dem[4, 4] = np.nan
    slope, aspect = gm.slope_aspect(dem, 30.0, 30.0)
    assert np.all(slope[np.isfinite(slope)] == 0.0)
    assert np.all(aspect[np.isfinite(aspect)] == 0.0)
    assert np.isnan(slope[4, 4]) and np.isnan(aspect[4, 4])
    assert np.isnan(slope[3, 4]) and np.isnan(slope[4, 5])  # neighbours via central diff


def test_slope_aspect_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        gm.slope_aspect(np.zeros((3, 3, 3)), 30.0, 30.0)
    with pytest.raises(ValueError):
        gm.slope_aspect(np.zeros((3, 3)), 0.0, 30.0)
    with pytest.raises(ValueError):
        gm.slope_aspect(np.zeros((1, 3)), 30.0, 30.0)


# ------------------------------------------------------------------ local incidence


def test_local_incidence_facing_away_and_perpendicular(incidence: float) -> None:
    s = 10.0
    phi_sen = gm.sensor_azimuth(-12.0)
    facing = gm.local_incidence(s, phi_sen, phi_sen, incidence)  # downslope toward sensor
    away = gm.local_incidence(s, (phi_sen + 180.0) % 360.0, phi_sen, incidence)
    perp = gm.local_incidence(s, (phi_sen + 90.0) % 360.0, phi_sen, incidence)
    assert facing == pytest.approx(incidence - s, abs=1e-9)
    assert away == pytest.approx(incidence + s, abs=1e-9)
    expected = np.degrees(np.arccos(np.cos(np.radians(s)) * np.cos(np.radians(incidence))))
    assert perp == pytest.approx(expected, abs=1e-9)


def test_local_incidence_flat_equals_incidence(incidence: float) -> None:
    for a in (0.0, 90.0, 213.0):
        assert gm.local_incidence(0.0, a, 258.0, incidence) == pytest.approx(incidence)


def test_range_slope_sign_and_magnitude() -> None:
    phi_sen = 258.0
    assert gm.range_slope(30.0, phi_sen, phi_sen) == pytest.approx(30.0)
    assert gm.range_slope(30.0, (phi_sen + 180) % 360, phi_sen) == pytest.approx(-30.0)
    assert gm.range_slope(30.0, (phi_sen + 90) % 360, phi_sen) == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------------------ masks: flat / thresholds


def test_flat_dem_has_no_distortion(flat: np.ndarray, incidence: float, dx: float) -> None:
    res = gm.masks_for_both_directions(flat, dx, dx, incidence)
    for r in res.values():
        assert not r.layover.any()
        assert not r.shadow.any()
        np.testing.assert_allclose(r.foreshortening, 0.0, atol=1e-12)
        np.testing.assert_allclose(r.local_incidence_deg, incidence, atol=1e-9)
        assert r.stats["layover_fraction"] == 0.0
        assert r.stats["shadow_fraction"] == 0.0
        assert r.stats["foreshortening_mean"] == pytest.approx(0.0, abs=1e-12)
        assert r.stats["aoi_pixels"] == flat.size


@pytest.mark.parametrize(("slope_deg", "expected"), [(38.9, False), (39.1, True)])
def test_layover_threshold_is_incidence_angle(slope_deg: float, expected: bool) -> None:
    # heading 0 → look east → sensor azimuth 270; a plane rising to the east faces west = sensor
    dem = plane_dem(20, 40, 30.0, slope_deg, "east")
    r = gm.compute_geometry_masks(dem, 30.0, 30.0, 0.0, 39.0, ASC)
    assert bool(r.layover[2:-2, 2:-2].all()) is expected
    assert not r.shadow.any()
    if expected:
        np.testing.assert_allclose(r.foreshortening[2:-2, 2:-2], 1.0)


@pytest.mark.parametrize(("slope_deg", "expected"), [(50.9, False), (51.1, True)])
def test_shadow_threshold_is_90_minus_incidence(slope_deg: float, expected: bool) -> None:
    # plane rising to the west faces east → away from a sensor at azimuth 270
    dem = plane_dem(20, 40, 30.0, slope_deg, "west")
    r = gm.compute_geometry_masks(dem, 30.0, 30.0, 0.0, 39.0, ASC)
    assert bool(r.shadow[2:-2, 2:-2].all()) is expected
    assert not r.layover.any()
    # local incidence crosses 90° exactly at the shadow limit
    assert bool(r.local_incidence_deg[5, 5] > 90.0) is expected
    np.testing.assert_allclose(r.foreshortening[2:-2, 2:-2], 0.0)


# ------------------------------------------------------------------ ridge: direction flip


def test_ridge_layover_flank_flips_between_directions(
    ridge: np.ndarray, incidence: float, dx: float
) -> None:
    """Ascending (look ENE) → west flank in layover; descending (look WNW) → east flank."""
    res = gm.masks_for_both_directions(ridge, dx, dx, incidence)
    asc, desc = res[ASC], res[DESC]
    ncols = ridge.shape[1]
    centre = (ncols - 1) / 2.0
    cols = np.arange(ncols)

    asc_lay = cols[asc.layover.any(axis=0)]
    asc_sha = cols[asc.shadow.any(axis=0)]
    desc_lay = cols[desc.layover.any(axis=0)]
    desc_sha = cols[desc.shadow.any(axis=0)]
    assert asc_lay.size and asc_sha.size and desc_lay.size and desc_sha.size
    assert asc_lay.max() < centre, "ascending layover must sit on the west (radar-facing) flank"
    assert asc_sha.min() > centre, "ascending shadow must sit on the east (facing-away) flank"
    assert desc_lay.min() > centre, "descending layover must sit on the east flank"
    assert desc_sha.max() < centre, "descending shadow must sit on the west flank"

    # symmetric ridge + mirrored geometry → exact mirror images
    assert np.array_equal(asc.layover, desc.layover[:, ::-1])
    assert np.array_equal(asc.shadow, desc.shadow[:, ::-1])
    np.testing.assert_allclose(asc.foreshortening, desc.foreshortening[:, ::-1], atol=1e-12)
    np.testing.assert_allclose(
        asc.local_incidence_deg, desc.local_incidence_deg[:, ::-1], atol=1e-9
    )
    # layover and shadow never coincide with the per-pixel criteria
    assert not (asc.layover & asc.shadow).any()


def test_ridge_foreshortening_in_unit_interval_and_only_on_facing_flank(
    ridge: np.ndarray, incidence: float, dx: float
) -> None:
    r = gm.compute_geometry_masks(ridge, dx, dx, gm.S1_HEADING_ASC_DEG, incidence, ASC)
    f = r.foreshortening
    assert np.isfinite(f).all()
    assert f.min() >= 0.0 and f.max() <= 1.0
    centre = (ridge.shape[1] - 1) / 2.0
    west = np.arange(ridge.shape[1]) < centre
    assert f[:, west].max() == 1.0  # layover pixels are fully compressed
    assert f[:, ~west].max() == 0.0  # facing-away flank is stretched, not compressed
    assert f[r.layover].min() == 1.0
    # compression increases with slope on the facing flank up to the layover limit
    col_profile = f[10, :]
    first_layover_col = int(np.argmax(r.layover[10, :]))
    facing_cols = np.where(
        west & (col_profile < 1.0) & (col_profile > 0) & (np.arange(f.shape[1]) < first_layover_col)
    )[0]
    assert facing_cols.size > 2
    assert np.all(np.diff(col_profile[facing_cols]) >= -1e-12)


def test_layover_pixels_have_small_local_incidence(
    ridge: np.ndarray, incidence: float, dx: float
) -> None:
    r = gm.compute_geometry_masks(ridge, dx, dx, gm.S1_HEADING_ASC_DEG, incidence, ASC)
    assert r.local_incidence_deg[r.layover].max() < incidence
    assert r.local_incidence_deg[r.shadow].min() > 90.0


# ------------------------------------------------------------------ incidence as a map, NaN


def test_incidence_map_matches_scalar(ridge: np.ndarray, incidence: float, dx: float) -> None:
    inc_map = np.full(ridge.shape, incidence)
    a = gm.compute_geometry_masks(ridge, dx, dx, -12.0, incidence, ASC)
    b = gm.compute_geometry_masks(ridge, dx, dx, -12.0, inc_map, ASC)
    assert np.array_equal(a.layover, b.layover)
    assert np.array_equal(a.shadow, b.shadow)
    np.testing.assert_allclose(a.foreshortening, b.foreshortening)
    assert isinstance(b.incidence_deg, np.ndarray)


def test_nan_dem_pixels_are_excluded(ridge: np.ndarray, incidence: float, dx: float) -> None:
    dem = ridge.copy()
    dem[:5, :] = np.nan
    r = gm.compute_geometry_masks(dem, dx, dx, -12.0, incidence, ASC)
    assert not r.layover[:5].any() and not r.shadow[:5].any()
    assert np.isnan(r.foreshortening[:5]).all()
    assert np.isnan(r.local_incidence_deg[:5]).all()
    assert r.stats["aoi_pixels"] < dem.size
    ls = r.ls_map()
    assert (ls[:5] == gm.LS_NODATA).all()
    assert set(np.unique(ls[6:])) <= {gm.LS_NONE, gm.LS_LAYOVER, gm.LS_SHADOW, gm.LS_BOTH}


# ------------------------------------------------------------------ validation


def test_validation_errors_use_i18n(flat: np.ndarray) -> None:
    with pytest.raises(ValueError) as e1:
        gm.compute_geometry_masks(flat, 30.0, 30.0, -12.0, 39.0, "SIDEWAYS")
    assert "SIDEWAYS" in str(e1.value)
    with pytest.raises(ValueError):
        gm.compute_geometry_masks(flat, 30.0, 30.0, -12.0, 95.0, ASC)
    with pytest.raises(ValueError):
        gm.compute_geometry_masks(flat, 30.0, 30.0, -12.0, np.full((3, 3), 39.0), ASC)
    with pytest.raises(ValueError):
        gm.compute_geometry_masks(flat, 30.0, 30.0, -12.0, 39.0, ASC, aoi_mask=np.ones((2, 2)))
    with pytest.raises(ValueError):
        gm.recommend_direction({})


def test_direction_label_is_case_insensitive(flat: np.ndarray) -> None:
    r = gm.compute_geometry_masks(flat, 30.0, 30.0, -12.0, 39.0, "ascending")
    assert r.flight_direction == ASC


# ------------------------------------------------------------------ stats / recommendation


def test_stats_respect_aoi_mask(ridge: np.ndarray, incidence: float, dx: float) -> None:
    ncols = ridge.shape[1]
    aoi = np.zeros(ridge.shape, dtype=bool)
    aoi[:, : ncols // 2] = True  # west half only
    r = gm.compute_geometry_masks(ridge, dx, dx, -12.0, incidence, ASC, aoi_mask=aoi)
    assert r.stats["aoi_pixels"] == aoi.sum()
    assert r.stats["layover_fraction"] > 0.0
    assert r.stats["shadow_fraction"] == 0.0  # shadow is on the east half
    expected = r.layover[aoi].sum() / aoi.sum()
    assert r.stats["layover_fraction"] == pytest.approx(expected)
    empty = gm.compute_geometry_masks(
        ridge, dx, dx, -12.0, incidence, ASC, aoi_mask=np.zeros(ridge.shape, bool)
    )
    assert empty.stats["aoi_pixels"] == 0 and np.isnan(empty.stats["layover_fraction"])


def test_recommend_direction_prefers_less_distortion(
    asym_ridge: np.ndarray, incidence: float, dx: float
) -> None:
    """Steep west flank: ascending puts it in layover, descending only its steepest part in shadow."""
    res = gm.masks_for_both_directions(asym_ridge, dx, dx, incidence)
    assert res[ASC].distorted_fraction > res[DESC].distorted_fraction
    assert gm.recommend_direction(res) == DESC
    # mirror the terrain → the other direction wins
    res_m = gm.masks_for_both_directions(asym_ridge[:, ::-1], dx, dx, incidence)
    assert gm.recommend_direction(res_m) == ASC


def test_recommend_direction_tie_is_deterministic(
    flat: np.ndarray, incidence: float, dx: float
) -> None:
    res = gm.masks_for_both_directions(flat, dx, dx, incidence)
    assert gm.recommend_direction(res) == ASC
    assert gm.recommend_direction(dict(reversed(list(res.items())))) == ASC


def test_masks_for_both_directions_defaults_are_s1_headings(
    flat: np.ndarray, incidence: float, dx: float
) -> None:
    res = gm.masks_for_both_directions(flat, dx, dx, incidence)
    assert res[ASC].heading_deg == gm.S1_HEADING_ASC_DEG == -12.0
    assert res[DESC].heading_deg == gm.S1_HEADING_DESC_DEG == 192.0
    custom = gm.masks_for_both_directions(flat, dx, dx, incidence, heading_asc=-10.0)
    assert custom[ASC].heading_deg == -10.0


# ------------------------------------------------------------------ SEL-12 finding


def test_sel12_finding_warn_and_info(ridge: np.ndarray, incidence: float, dx: float) -> None:
    r = gm.compute_geometry_masks(ridge, dx, dx, -12.0, incidence, ASC)
    warn = gm.sel12_finding(r, threshold=0.05, mask_path="/tmp/x.tif", recommended=DESC)
    assert warn.rule_id == "SEL-12" and warn.severity == "WARN"
    assert warn.fix_key == "select_geometry.SEL-12.fix"
    text_ko = i18n.t(warn.message_key, "ko", **warn.params)
    text_en = i18n.t(warn.message_key, "en", **warn.params)
    assert "레이오버" in text_ko and "layover" in text_en
    assert "{" not in text_ko and "{" not in text_en
    assert DESC in i18n.t(warn.fix_key, "en", **warn.params)
    info = gm.sel12_finding(r, threshold=0.5)
    assert info.severity == "INFO" and info.message_key == "select_geometry.SEL-12.ok"
    assert "{" not in i18n.t(info.message_key, "ko", **info.params)


def test_ls_map_encoding(ridge: np.ndarray, incidence: float, dx: float) -> None:
    r = gm.compute_geometry_masks(ridge, dx, dx, -12.0, incidence, ASC)
    ls = r.ls_map()
    assert ls.dtype == np.uint8
    assert np.array_equal(ls == gm.LS_LAYOVER, r.layover)
    assert np.array_equal(ls == gm.LS_SHADOW, r.shadow)
    assert not (ls == gm.LS_NODATA).any()


def test_ridge_dem_helper_is_steep_enough(ridge: np.ndarray, dx: float) -> None:
    slope, _ = gm.slope_aspect(ridge, dx, dx)
    assert slope.max() > 51.0  # needed for the shadow branch of the ridge tests
    assert ridge_dem(sigma_px=20.0).shape == ridge.shape
