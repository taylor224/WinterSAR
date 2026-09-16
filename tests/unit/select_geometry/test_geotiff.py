"""GeoTIFF IO for geometry masks (R-04): write/re-read, window read from a DEM file."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from rasterio.crs import CRS
from rasterio.transform import from_origin

from tests.unit.select_geometry.synthetic import ridge_dem
from wintersar.select import geometry_masks as gm

ARCSEC = 1.0 / 3600.0
LON0, LAT0 = 127.0, 37.6  # upper-left corner (north-up)


def _wgs84_transform():
    return from_origin(LON0, LAT0, ARCSEC, ARCSEC)


def test_write_and_read_roundtrip(tmp_path: Path, ridge: np.ndarray, incidence: float) -> None:
    tr = _wgs84_transform()
    crs = CRS.from_epsg(4326)
    dem = ridge.copy()
    dem[0, :3] = np.nan
    r = gm.compute_geometry_masks(
        dem, 30.0, 30.0, -12.0, incidence, "ASCENDING", transform=tr, crs=crs
    )
    out = gm.write_mask_geotiff(r, tmp_path / "masks" / "asc.tif")
    assert out.exists()
    ls_path = out.with_name("asc_ls_map.tif")
    assert ls_path.exists()

    back = gm.read_mask_geotiff(out)
    assert np.array_equal(back.layover, r.layover)
    assert np.array_equal(back.shadow, r.shadow)
    np.testing.assert_allclose(back.foreshortening, r.foreshortening, rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(
        back.local_incidence_deg, r.local_incidence_deg, rtol=1e-6, atol=1e-4
    )
    assert back.flight_direction == "ASCENDING"
    assert back.heading_deg == -12.0
    assert back.incidence_deg == pytest.approx(incidence)
    assert back.stats == pytest.approx(r.stats, nan_ok=True)
    assert back.transform == tr
    assert back.crs == crs

    import rasterio

    with rasterio.open(out) as ds:
        assert ds.count == 4
        assert tuple(ds.descriptions) == gm.BAND_NAMES
        assert ds.dtypes[0] == "float32"
    with rasterio.open(ls_path) as ds:
        assert ds.count == 1 and ds.dtypes[0] == "uint8" and ds.nodata == gm.LS_NODATA
        ls = ds.read(1)
    assert np.array_equal(ls, r.ls_map())
    assert (ls[0, :3] == gm.LS_NODATA).all()


def test_write_requires_transform(ridge: np.ndarray, incidence: float, tmp_path: Path) -> None:
    r = gm.compute_geometry_masks(ridge, 30.0, 30.0, -12.0, incidence, "ASCENDING")
    with pytest.raises(ValueError):
        gm.write_mask_geotiff(r, tmp_path / "x.tif")
    out = gm.write_mask_geotiff(
        r, tmp_path / "y.tif", transform=_wgs84_transform(), crs="EPSG:4326", write_ls_map=False
    )
    assert out.exists() and not out.with_name("y_ls_map.tif").exists()


def test_pixel_spacing_geographic_and_projected() -> None:
    tr = _wgs84_transform()
    dx, dy = gm.pixel_spacing_m(tr, CRS.from_epsg(4326), (48, 64))
    # 1 arcsec at ~37.6°N: pyproj WGS84 geodesic (checked interactively: 24.5 m x 30.8 m)
    assert dx == pytest.approx(24.5, abs=0.3)
    assert dy == pytest.approx(30.8, abs=0.1)
    assert dy > 0  # north-up

    utm = from_origin(300000.0, 4165000.0, 30.0, 30.0)
    dx, dy = gm.pixel_spacing_m(utm, CRS.from_epsg(32652), (10, 10))
    assert (dx, dy) == (30.0, 30.0)

    south_up = from_origin(300000.0, 4165000.0, 30.0, -30.0)
    _, dy = gm.pixel_spacing_m(south_up, CRS.from_epsg(32652), (10, 10))
    assert dy == -30.0

    from affine import Affine

    with pytest.raises(ValueError):
        gm.pixel_spacing_m(Affine(30.0, 1.0, 0.0, 0.0, -30.0, 0.0), CRS.from_epsg(32652), (2, 2))


def test_compute_from_dem_file_wgs84(write_geotiff, incidence: float) -> None:
    dem = ridge_dem(nrows=120, ncols=160, dx_m=30.0)
    tr = _wgs84_transform()
    p = write_geotiff("dem.tif", dem, tr, CRS.from_epsg(4326))
    # AOI: a box well inside the raster (cols ~40..120, rows ~30..90)
    left, top = tr * (40, 30)
    right, bottom = tr * (120, 90)
    wkt = f"POLYGON(({left} {bottom},{right} {bottom},{right} {top},{left} {top},{left} {bottom}))"
    r = gm.compute_from_dem_file(p, wkt, -12.0, incidence, "ASCENDING")
    assert r.transform is not None and r.crs is not None
    # window = bbox + 2 px padding
    assert r.shape == (60 + 4, 80 + 4)
    assert r.stats["aoi_pixels"] == 60 * 80
    assert r.layover.any() and r.shadow.any()
    # layover on the west flank of the ridge (ascending)
    centre_col_in_window = (160 - 1) / 2.0 - (40 - 2)
    cols = np.arange(r.shape[1])
    assert cols[r.layover.any(axis=0)].max() < centre_col_in_window
    # metric spacing was estimated (not 1 arcsec == 1 m)
    lia = r.local_incidence_deg
    assert np.isfinite(lia).all()
    # direction flip on the same file
    d = gm.compute_from_dem_file(p, wkt, 192.0, incidence, "DESCENDING")
    assert cols[d.layover.any(axis=0)].min() > centre_col_in_window


def test_compute_from_dem_file_projected_crs_reprojects_aoi(
    write_geotiff, incidence: float
) -> None:
    from pyproj import Transformer

    dem = ridge_dem(nrows=100, ncols=100, dx_m=30.0)
    utm = CRS.from_epsg(32652)
    tr = from_origin(300000.0, 4165000.0, 30.0, 30.0)
    p = write_geotiff("dem_utm.tif", dem, tr, utm)
    # AOI given in lon/lat, covering the raster centre
    to_ll = Transformer.from_crs("EPSG:32652", "EPSG:4326", always_xy=True)
    x0, y0 = tr * (30, 30)
    x1, y1 = tr * (70, 70)
    lon0, lat0 = to_ll.transform(x0, y0)
    lon1, lat1 = to_ll.transform(x1, y1)
    wkt = f"POLYGON(({lon0} {lat1},{lon1} {lat1},{lon1} {lat0},{lon0} {lat0},{lon0} {lat1}))"
    r = gm.compute_from_dem_file(p, wkt, -12.0, incidence, "ASCENDING")
    assert r.stats["aoi_pixels"] > 0.8 * 40 * 40
    assert r.layover.any()


def test_compute_from_dem_file_nodata_and_outside(write_geotiff, incidence: float) -> None:
    dem = ridge_dem(nrows=60, ncols=80, dx_m=30.0)
    dem[:10, :] = -9999.0
    tr = _wgs84_transform()
    p = write_geotiff("dem_nd.tif", dem, tr, CRS.from_epsg(4326), nodata=-9999.0)
    left, top = tr * (0, 0)
    right, bottom = tr * (80, 60)
    wkt = f"POLYGON(({left} {bottom},{right} {bottom},{right} {top},{left} {top},{left} {bottom}))"
    r = gm.compute_from_dem_file(p, wkt, -12.0, incidence, "ASCENDING")
    assert np.isnan(r.local_incidence_deg[:9]).all()
    assert not r.layover[:9].any()
    assert r.stats["aoi_pixels"] < dem.size

    far = "POLYGON((10 10,11 10,11 11,10 11,10 10))"
    with pytest.raises(ValueError) as exc:
        gm.compute_from_dem_file(p, far, -12.0, incidence, "ASCENDING")
    assert "AOI" in str(exc.value)
