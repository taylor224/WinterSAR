"""io.cog: COG write/read-back, overviews, exports (ADR-0051)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine

from tests.unit.io.conftest import make_timeseries
from wintersar.io import cog
from wintersar.io.timeseries import TimeSeries

TR = Affine(0.001, 0.0, 126.9, 0.0, -0.001, 37.6)


def test_cog_driver_available_in_this_gdal() -> None:
    assert cog.cog_driver_available()


def test_write_cog_roundtrip_with_overviews(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    arr = rng.random((300, 400)).astype(np.float32)
    arr[0, 0] = np.nan
    p = cog.write_cog(arr, TR, "EPSG:4326", tmp_path / "a.tif", blocksize=128, compress="DEFLATE")
    info = cog.read_cog_info(p)
    assert info.layout_cog and info.tiled and info.block_shape == (128, 128)
    assert info.overviews and info.overviews[0] == 2
    assert info.compression == "deflate" and info.dtype == "float32" and info.crs == "EPSG:4326"
    with rasterio.open(p) as src:
        back = src.read(1)
        assert src.transform == TR and np.isnan(src.nodata)
        assert src.tags(ns="IMAGE_STRUCTURE")["PREDICTOR"] == "3"
    np.testing.assert_array_equal(np.isnan(back), np.isnan(arr))
    np.testing.assert_allclose(back[1:], arr[1:])
    assert info.to_dict()["nodata"] is None


def test_write_cog_zstd_uint8_mask_and_tags(tmp_path: Path) -> None:
    m = (np.arange(200 * 300).reshape(200, 300) % 7 == 0).astype(np.uint8)
    p = cog.write_cog(
        m,
        TR,
        "EPSG:4326",
        tmp_path / "m.tif",
        nodata=255,
        compress="ZSTD",
        blocksize=128,
        overview_resampling="NEAREST",
        tags={"UNIT": "1"},
        band_descriptions=["mask"],
    )
    info = cog.check_cog(p)
    assert info.compression == "zstd" and info.nodata == 255 and info.overviews
    with rasterio.open(p) as src:
        assert src.tags()["UNIT"] == "1" and src.descriptions == ("mask",)
        assert src.tags(ns="IMAGE_STRUCTURE")["PREDICTOR"] == "2"
        np.testing.assert_array_equal(src.read(1), m)


def test_write_cog_multiband_and_no_overviews(tmp_path: Path) -> None:
    arr = np.random.default_rng(1).random((3, 150, 160)).astype(np.float32)
    p = cog.write_cog(
        arr, TR, "EPSG:4326", tmp_path / "s.tif", blocksize=128, overviews=False, compress="NONE"
    )
    info = cog.read_cog_info(p)
    assert info.count == 3 and info.overviews == [] and info.layout_cog
    with rasterio.open(p) as src:
        np.testing.assert_allclose(src.read(), arr)


def test_small_raster_passes_check_without_overviews(tmp_path: Path) -> None:
    arr = np.zeros((30, 40), np.float32)
    p = cog.write_cog(arr, TR, "EPSG:4326", tmp_path / "t.tif")
    info = cog.check_cog(p)
    assert info.overviews == [] and info.block_shape == (512, 512)


def test_write_cog_rejects_bad_arguments(tmp_path: Path) -> None:
    arr = np.zeros((10, 10), np.float32)
    with pytest.raises(ValueError, match="blocksize"):
        cog.write_cog(arr, TR, "EPSG:4326", tmp_path / "x.tif", blocksize=64)
    with pytest.raises(ValueError, match="compress"):
        cog.write_cog(arr, TR, "EPSG:4326", tmp_path / "x.tif", compress="FOO")
    with pytest.raises(ValueError, match="NaN nodata"):
        cog.write_cog(arr.astype(np.uint8), TR, "EPSG:4326", tmp_path / "x.tif")
    with pytest.raises(ValueError, match="2-D or 3-D"):
        cog.write_cog(np.zeros((2, 2, 2, 2), np.float32), TR, "EPSG:4326", tmp_path / "x.tif")


def test_write_cog_rejects_unwritable_dtype_extent_and_nodata(tmp_path: Path) -> None:
    """Dtypes/extents GDAL cannot take, and a nodata the band cannot hold, are caught here
    instead of surfacing as a raw rasterio TypeError / CPLE_AppDefinedError."""
    with pytest.raises(ValueError, match="empty"):
        cog.write_cog(np.zeros((0, 0), np.float32), TR, "EPSG:4326", tmp_path / "e.tif")
    with pytest.raises(ValueError, match="GeoTIFF band"):
        cog.write_cog(
            np.zeros((8, 8), "datetime64[D]"), TR, "EPSG:4326", tmp_path / "c.tif", nodata=None
        )
    with pytest.raises(ValueError, match="outside the range"):
        cog.write_cog(np.zeros((8, 8), np.uint8), TR, "EPSG:4326", tmp_path / "n.tif", nodata=300)
    # bool / float16 are cast losslessly instead of raising
    b = cog.write_cog(
        np.eye(8, dtype=bool), TR, "EPSG:4326", tmp_path / "b.tif", nodata=None, blocksize=128
    )
    h = cog.write_cog(
        np.eye(8, dtype=np.float16), TR, "EPSG:4326", tmp_path / "h.tif", blocksize=128
    )
    with rasterio.open(b) as src:
        assert src.dtypes[0] == "uint8" and src.read(1)[0, 0] == 1
    with rasterio.open(h) as src:
        assert src.dtypes[0] == "float32" and np.isnan(src.nodata)


def test_overview_factors_rule() -> None:
    assert cog._overview_factors(300, 400, 128) == [2, 4]
    assert cog._overview_factors(300, 400, 256) == [2]
    assert cog._overview_factors(100, 100, 512) == []


def test_timeseries_transform_pixel_centres() -> None:
    ts = make_timeseries(shape=(30, 40))
    tr, flip = cog.timeseries_transform(ts)
    assert not flip
    # the transform maps the pixel *centre* (0.5, 0.5) back to lon[0], lat[0]
    x, y = tr * (0.5, 0.5)
    assert x == pytest.approx(float(ts.lon[0])) and y == pytest.approx(float(ts.lat[0]))
    x1, y1 = tr * (39.5, 29.5)
    assert x1 == pytest.approx(float(ts.lon[-1])) and y1 == pytest.approx(float(ts.lat[-1]))
    assert tr.e < 0


def test_irregular_grid_is_rejected() -> None:
    ts = make_timeseries(shape=(10, 12))
    lat = ts.lat.copy()
    lat[3] += 0.01
    bad = TimeSeries(dates=ts.dates, displacement_m=ts.displacement_m, lat=lat, lon=ts.lon)
    with pytest.raises(ValueError, match="regular"):
        cog.timeseries_transform(bad)


def test_export_velocity_displacement_coherence(tmp_path: Path) -> None:
    ts = make_timeseries(shape=(200, 260))
    v = cog.export_velocity_cog(ts, tmp_path / "vel.tif", blocksize=128)
    d = cog.export_displacement_cog(ts, tmp_path / "disp.tif", blocksize=128)
    d2 = cog.export_displacement_cog(
        ts, tmp_path / "disp2.tif", at=date(2024, 1, 13), blocksize=128
    )
    c = cog.export_coherence_cog(ts, tmp_path / "coh.tif", blocksize=128)
    with rasterio.open(v) as src:
        np.testing.assert_allclose(src.read(1), ts.velocity_m_per_yr)
        assert src.tags()["UNIT"] == "m/year" and "towards satellite" in src.tags()["SIGN"]
        assert src.overviews(1) and src.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
    with rasterio.open(d) as src:
        np.testing.assert_allclose(src.read(1), ts.displacement_m[-1])
        assert src.tags()["DATE"] == "2024-02-18" and src.tags()["REF_DATE"] == "20240101"
    with rasterio.open(d2) as src:
        np.testing.assert_allclose(src.read(1), ts.displacement_m[1])
    with rasterio.open(c) as src:
        np.testing.assert_allclose(src.read(1), ts.coherence)


def test_export_flips_south_up_grid_to_north_up(tmp_path: Path) -> None:
    ts = make_timeseries(shape=(150, 160), north_up=False)
    assert ts.lat[1] > ts.lat[0]
    p = cog.export_displacement_cog(ts, tmp_path / "d.tif", blocksize=128)
    with rasterio.open(p) as src:
        assert src.transform.e < 0
        np.testing.assert_allclose(src.read(1), ts.displacement_m[-1][::-1])
        _x, y = src.transform * (0.5, 0.5)
        assert y == pytest.approx(float(ts.lat[-1]))


def test_fit_velocity_recovers_linear_trend() -> None:
    ts = make_timeseries(shape=(20, 24), n_dates=8)
    fit = cog.fit_velocity(ts)
    assert fit.shape == (20, 24)
    np.testing.assert_allclose(fit, ts.velocity_m_per_yr, atol=5e-3)


def test_fit_velocity_keeps_nodata_pixels_nan(tmp_path: Path) -> None:
    """A pixel without enough finite epochs must stay NaN: 0.0 would be exported as valid
    'stable ground' (nodata is NaN, so a zero never triggers it)."""
    ts = make_timeseries(shape=(8, 8), n_dates=6)
    disp = np.asarray(ts.displacement_m).copy()
    disp[:, 1, 1] = np.nan  # fully masked pixel
    disp[1:, 2, 2] = np.nan  # a single valid epoch
    disp[4:, 3, 3] = np.nan  # four valid epochs -> still a fit
    ts.displacement_m = disp
    fit = cog.fit_velocity(ts)
    assert np.isnan(fit[1, 1]) and np.isnan(fit[2, 2])
    assert np.isfinite(fit[3, 3])
    assert fit[3, 3] == pytest.approx(float(ts.velocity_m_per_yr[3, 3]), abs=5e-3)
    assert np.isfinite(fit[0, 0])

    ts.velocity_m_per_yr = None
    p = cog.export_velocity_cog(ts, tmp_path / "vel.tif", blocksize=128)
    with rasterio.open(p) as src:
        band = src.read(1)
        assert np.isnan(src.nodata)
        assert np.isnan(band[1, 1]) and np.isnan(band[2, 2])

    all_nan = make_timeseries(shape=(4, 4), n_dates=4)
    all_nan.displacement_m = np.full_like(np.asarray(all_nan.displacement_m), np.nan)
    assert np.isnan(cog.fit_velocity(all_nan)).all()


def test_export_all_and_mask(tmp_path: Path) -> None:
    ts = make_timeseries(shape=(140, 150))
    mask = np.zeros((140, 150), bool)
    mask[:, :10] = True
    out = cog.export_timeseries_cogs(ts, tmp_path / "out", mask=mask, stack=True, blocksize=128)
    assert set(out) == {
        "velocity",
        "displacement",
        "coherence",
        "conncomp",
        "mask",
        "displacement_stack",
    }
    for p in out.values():
        assert cog.read_cog_info(p).layout_cog
    with rasterio.open(out["mask"]) as src:
        assert src.nodata == 255 and src.dtypes[0] == "uint8"
        np.testing.assert_array_equal(src.read(1), mask.astype(np.uint8))
        assert src.tags(ns="rio_overview").get("resampling", "nearest") in ("nearest", "")
    with rasterio.open(out["displacement_stack"]) as src:
        assert src.count == ts.n_dates and src.descriptions[0] == "2024-01-01"
    no_coh = TimeSeries(dates=ts.dates, displacement_m=ts.displacement_m, lat=ts.lat, lon=ts.lon)
    with pytest.raises(ValueError, match="coherence"):
        cog.export_coherence_cog(no_coh, tmp_path / "x.tif")
    with pytest.raises(ValueError, match="mask shape"):
        cog.export_mask_cog(np.zeros((3, 3), bool), ts, tmp_path / "x.tif")
