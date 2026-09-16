"""io.formats: ISCE .xml/.vrt, HyP3 product dirs, MintPy h5 and .npz time series (ADR-0052)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import h5py
import numpy as np
import pytest

from tests.unit.io.conftest import (
    HYP3_NAMES,
    isce_xml,
    make_timeseries,
    write_hyp3_product,
    write_tif,
)
from wintersar.io import formats
from wintersar.io.formats import (
    Hyp3FormatError,
    IsceFormatError,
    hyp3_pair_key,
    load_timeseries,
    mintpy_latlon_grid,
    normalize_heading_deg,
    parse_hyp3_metadata_txt,
    parse_isce_xml,
    read_hyp3_product_dir,
    read_isce_raster,
    read_timeseries_h5,
    synthetic_latlon_grid,
    write_timeseries_h5,
    write_timeseries_npz,
)

# ---------------------------------------------------------------------- ISCE


def test_parse_isce_xml_properties_and_coordinates(tmp_path: Path) -> None:
    p = tmp_path / "test.unw.xml"
    p.write_text(isce_xml(7, 5, "FLOAT", "BIL", bands=2), encoding="utf-8")
    meta = parse_isce_xml(p)
    assert meta["WIDTH"] == 7 and meta["LENGTH"] == 5 and meta["NUMBER_BANDS"] == 2
    assert meta["DATA_TYPE"] == "FLOAT" and meta["SCHEME"] == "BIL" and meta["BYTE_ORDER"] == "l"
    assert meta["FILE_NAME"] == "test.unw" and meta["IMAGE_TYPE"] == "unw"
    assert meta["X_FIRST"] == pytest.approx(126.9) and meta["X_STEP"] == pytest.approx(0.000833)
    assert meta["Y_FIRST"] == pytest.approx(37.6) and meta["Y_STEP"] == pytest.approx(-0.000833)
    assert meta["X_UNIT"] == "degrees" and meta["Y_UNIT"] == "degrees"


def test_parse_isce_xml_radar_geometry_has_no_degree_unit(tmp_path: Path) -> None:
    p = tmp_path / "a.int.xml"
    p.write_text(isce_xml(4, 3, "CFLOAT", "BIP", geocoded=False), encoding="utf-8")
    meta = parse_isce_xml(p)
    assert "X_UNIT" not in meta and meta["X_STEP"] == 1.0


@pytest.mark.parametrize("scheme", ["BIL", "BIP", "BSQ"])
def test_read_isce_two_band_float_all_schemes(tmp_path: Path, scheme: str) -> None:
    ny, nx = 5, 7
    amp = np.arange(ny * nx, dtype=np.float32).reshape(ny, nx)
    ph = -amp / 3.0
    (tmp_path / "test.unw.xml").write_text(isce_xml(nx, ny, "FLOAT", scheme, bands=2))
    if scheme == "BIL":
        raw = np.stack([amp, ph], axis=1)  # (line, band, pixel)
    elif scheme == "BIP":
        raw = np.stack([amp, ph], axis=2)  # (line, pixel, band)
    else:
        raw = np.stack([amp, ph], axis=0)  # (band, line, pixel)
    raw.astype("<f4").tofile(tmp_path / "test.unw")
    arr, meta = read_isce_raster(tmp_path / "test.unw")
    assert arr.shape == (2, ny, nx) and meta["reader"] == "isce_xml"
    np.testing.assert_array_equal(arr[0], amp)
    np.testing.assert_array_equal(arr[1], ph)
    band1, _ = read_isce_raster(tmp_path / "test.unw.xml", band=1)
    np.testing.assert_array_equal(band1, ph)


def test_read_isce_cfloat_and_big_endian_short(tmp_path: Path) -> None:
    cx = (np.arange(12, dtype=np.float32).reshape(3, 4) * (1 + 2j)).astype(np.complex64)
    (tmp_path / "a.int.xml").write_text(isce_xml(4, 3, "CFLOAT", "BIP", file_name="a.int"))
    cx.tofile(tmp_path / "a.int")
    arr, _meta = read_isce_raster(tmp_path / "a.int")
    assert arr.dtype == np.complex64 and arr.shape == (3, 4)
    np.testing.assert_array_equal(arr, cx)
    sh = np.arange(12, dtype=">i2").reshape(3, 4)
    (tmp_path / "b.msk.xml").write_text(
        isce_xml(4, 3, "SHORT", "BSQ", byte_order="b", file_name="b.msk")
    )
    sh.tofile(tmp_path / "b.msk")
    arr2, meta2 = read_isce_raster(tmp_path / "b.msk")
    assert meta2["BYTE_ORDER"] == "b"
    np.testing.assert_array_equal(arr2.astype(np.int64), np.arange(12).reshape(3, 4))


def test_read_isce_byte_is_unsigned(tmp_path: Path) -> None:
    # ISCE Image.py TO_NUMPY says 'i1' but its VRT (Byte) and MintPy read uint8 (ADR-0052)
    m = np.array([[0, 1], [2, 255]], dtype=np.uint8)
    (tmp_path / "m.msk.xml").write_text(isce_xml(2, 2, "BYTE", "BIL", file_name="m.msk"))
    m.tofile(tmp_path / "m.msk")
    arr, _ = read_isce_raster(tmp_path / "m.msk")
    assert arr.dtype == np.uint8 and int(arr[1, 1]) == 255


def test_read_isce_size_mismatch_and_bad_values(tmp_path: Path) -> None:
    (tmp_path / "t.unw.xml").write_text(isce_xml(7, 5, "FLOAT", "BIL", file_name="t.unw"))
    np.zeros(10, np.float32).tofile(tmp_path / "t.unw")
    with pytest.raises(IsceFormatError, match="bytes"):
        read_isce_raster(tmp_path / "t.unw")
    (tmp_path / "u.unw.xml").write_text(isce_xml(7, 5, "WEIRD", "BIL", file_name="u.unw"))
    with pytest.raises(IsceFormatError, match="DATA_TYPE"):
        parse_isce_xml(tmp_path / "u.unw.xml")
    (tmp_path / "v.unw.xml").write_text(isce_xml(7, 5, "FLOAT", "XYZ", file_name="v.unw"))
    with pytest.raises(IsceFormatError, match="SCHEME"):
        parse_isce_xml(tmp_path / "v.unw.xml")
    with pytest.raises(FileNotFoundError):
        read_isce_raster(tmp_path / "missing.unw")


def test_read_isce_vrt_fallback(tmp_path: Path) -> None:
    """Without .xml the reader opens the ISCE-rendered VRT (VRTRawRasterBand).
    source: https://gdal.org/en/stable/drivers/raster/vrt.html (VRTRawRasterBand elements)."""
    ny, nx = 4, 6
    data = np.arange(ny * nx, dtype="<f4").reshape(ny, nx)
    data.tofile(tmp_path / "a.r4")
    (tmp_path / "a.r4.vrt").write_text(
        f'<VRTDataset rasterXSize="{nx}" rasterYSize="{ny}">\n'
        '  <VRTRasterBand dataType="Float32" band="1" subClass="VRTRawRasterBand">\n'
        '    <SourceFilename relativeToVRT="1">a.r4</SourceFilename>\n'
        "    <ByteOrder>LSB</ByteOrder>\n"
        "    <ImageOffset>0</ImageOffset>\n"
        "    <PixelOffset>4</PixelOffset>\n"
        f"    <LineOffset>{4 * nx}</LineOffset>\n"
        "  </VRTRasterBand>\n"
        "</VRTDataset>\n",
        encoding="utf-8",
    )
    arr, meta = read_isce_raster(tmp_path / "a.r4")
    assert meta["reader"] == "vrt" and meta["WIDTH"] == nx and meta["LENGTH"] == ny
    np.testing.assert_array_equal(arr, data)


# ---------------------------------------------------------------------- HyP3


def test_hyp3_pair_key_and_metadata_txt(tmp_path: Path) -> None:
    assert hyp3_pair_key(HYP3_NAMES[0]) == "20240101_20240113"
    assert hyp3_pair_key("S1_136231_IW2_20240113_20240101_VV_INT80_ABCD") == "20240101_20240113"
    with pytest.raises(Hyp3FormatError):
        hyp3_pair_key("no_dates_here.tif")
    p = tmp_path / "x.txt"
    p.write_text("Heading: -12.3\nAzimuth looks: 4\nno colon line\nUTC time: 1:2\n")
    meta = parse_hyp3_metadata_txt(p)
    assert meta == {"Heading": "-12.3", "Azimuthlooks": "4", "UTCtime": "1"}
    assert normalize_heading_deg(192.0) == pytest.approx(-168.0)
    assert normalize_heading_deg(-12.3) == pytest.approx(-12.3)


def test_read_hyp3_product_dir_stack(hyp3_root: Path) -> None:
    st = read_hyp3_product_dir(hyp3_root)
    assert st.pairs == ["20240101_20240113", "20240113_20240125"]
    assert st.dates == [date(2024, 1, 1), date(2024, 1, 13), date(2024, 1, 25)]
    assert st.shape == (12, 16) and st.n_pairs == 2
    assert st.unw is not None and st.conncomp is not None and st.mask is not None
    # nodata (0) and conncomp == 0 become NaN; water column masked
    assert np.isnan(st.unw[0, 0, 0]) and np.isnan(st.unw[0, 1, 1])
    assert st.mask[0][:, 0].all() and not st.mask[0][2, 5]
    assert st.mask[0][0, 0] and st.mask[0][1, 1]
    # wrapped derived from unw when no _wrapped_phase
    finite = np.isfinite(st.unw[1])
    np.testing.assert_allclose(
        st.wrapped[1][finite], np.angle(np.exp(1j * st.unw[1][finite])), atol=1e-6
    )
    assert st.attrs["epsg"] == 32652 and len(st.attrs["transform"]) == 6
    assert st.attrs["heading_deg"] == pytest.approx(-12.3)
    # lv_theta 50 deg elevation -> 40 deg incidence; lv_phi 192 deg from East -> 102 deg from North CCW
    assert float(st.attrs["incidence_deg"][0, 0]) == pytest.approx(40.0, abs=1e-3)
    assert float(st.attrs["azimuth_deg"][0, 0]) == pytest.approx(102.0, abs=1e-3)
    assert float(st.attrs["dem_m"][3, 3]) == 55.0
    assert st.attrs["hyp3_meta"]["20240101_20240113"]["Unwrappingtype"] == "snaphu_mcf"
    assert st.attrs["hyp3_meta"]["20240101_20240113"]["Heading"] == "-12.3"


def test_read_hyp3_single_product_dir_and_wrapped_file(tmp_path: Path) -> None:
    pd = write_hyp3_product(tmp_path, HYP3_NAMES[0], with_wrapped=True, with_conncomp=False)
    st = read_hyp3_product_dir(pd)
    assert st.n_pairs == 1 and st.conncomp is None
    assert np.nanmax(np.abs(st.wrapped)) <= np.pi + 1e-6


def test_read_hyp3_prefers_clipped_files(tmp_path: Path) -> None:
    pd = write_hyp3_product(tmp_path, HYP3_NAMES[0], shape=(12, 16))
    write_hyp3_product(tmp_path, HYP3_NAMES[0], shape=(10, 10), clip=True)
    st = read_hyp3_product_dir(pd)
    assert st.shape == (10, 10)


def test_read_hyp3_shape_mismatch_and_missing(tmp_path: Path) -> None:
    root = tmp_path / "h"
    write_hyp3_product(root, HYP3_NAMES[0], shape=(12, 16))
    write_hyp3_product(root, HYP3_NAMES[1], shape=(12, 15))
    with pytest.raises(Hyp3FormatError, match="common extent"):
        read_hyp3_product_dir(root)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(Hyp3FormatError, match="no HyP3 product"):
        read_hyp3_product_dir(empty)
    only = tmp_path / "only_unw" / HYP3_NAMES[0]
    only.mkdir(parents=True)
    write_tif(only / f"{HYP3_NAMES[0]}_unw_phase.tif", np.zeros((4, 4), np.float32))
    with pytest.raises(Hyp3FormatError, match="_corr"):
        read_hyp3_product_dir(only)


# ---------------------------------------------------------------------- time series


def test_synthetic_latlon_grid_is_regular_north_up() -> None:
    lat, lon = synthetic_latlon_grid((10, 20))
    assert lat.shape == (10,) and lon.shape == (20,)
    assert np.all(np.diff(lat) < 0) and np.all(np.diff(lon) > 0)
    assert lat.mean() == pytest.approx(37.55) and lon.mean() == pytest.approx(126.95)
    # 80 m pixel -> ~7.2e-4 deg of latitude
    assert abs(lat[1] - lat[0]) == pytest.approx(80.0 / 111320.0)


def test_load_timeseries_npz_fake_engine_format(tmp_path: Path) -> None:
    ts0 = make_timeseries()
    p = tmp_path / "timeseries.npz"
    np.savez_compressed(
        p,
        dates=np.array([d.isoformat() for d in ts0.dates]),
        displacement_m=ts0.displacement_m,
        velocity_m_per_yr=ts0.velocity_m_per_yr,
    )
    ts = load_timeseries(p)
    assert ts.n_dates == 5 and ts.shape == (30, 40)
    assert ts.attrs["latlon_synthetic"] is True and ts.lat.shape == (30,) and ts.lon.shape == (40,)
    assert ts.velocity_m_per_yr is not None and ts.lat2d().shape == (30, 40)
    assert ts.years()[-1] == pytest.approx(48 / 365.25)


def test_timeseries_npz_roundtrip(tmp_path: Path) -> None:
    ts0 = make_timeseries()
    p = write_timeseries_npz(ts0, tmp_path / "ts.npz")
    ts = load_timeseries(p)
    assert ts.attrs["latlon_synthetic"] is False and ts.attrs["REF_DATE"] == "20240101"
    assert ts.dates == ts0.dates and ts.heading_deg == -12.0 and ts.incidence_deg == 39.0
    assert ts.reference_latlon == (37.55, 126.95)
    np.testing.assert_allclose(ts.displacement_m, ts0.displacement_m)
    np.testing.assert_allclose(ts.lat, ts0.lat)
    assert ts.coherence is not None and ts.conncomp is not None


def test_load_timeseries_bad_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        load_timeseries(tmp_path / "x.csv")
    p = tmp_path / "bad.npz"
    np.savez(p, foo=np.zeros(3))
    with pytest.raises(ValueError, match="displacement_m"):
        load_timeseries(p)
    q = tmp_path / "bad.h5"
    with h5py.File(q, "w") as f:
        f.create_dataset("velocity", data=np.zeros((2, 2)))
    with pytest.raises(ValueError, match="timeseries"):
        load_timeseries(q)


def test_mintpy_latlon_grid_pixel_centre_convention() -> None:
    # source: mintpy/utils/utils0.py get_lat_lon: Y_FIRST + Y_STEP * (row + 0.5)
    attrs = {"X_FIRST": "126.9", "X_STEP": "0.01", "Y_FIRST": "37.6", "Y_STEP": "-0.01"}
    lat, lon = mintpy_latlon_grid(attrs, 3, 4)
    np.testing.assert_allclose(lon, [126.905, 126.915, 126.925, 126.935])
    np.testing.assert_allclose(lat, [37.595, 37.585, 37.575])


def test_mintpy_h5_roundtrip_with_documented_attrs_and_geometry(tmp_path: Path) -> None:
    ts0 = make_timeseries()
    p = write_timeseries_h5(
        ts0, tmp_path / "timeseries.h5", extra_attrs={"WAVELENGTH": "0.05546576"}
    )
    with h5py.File(p, "r") as f:
        # datasets/attrs as in MintPy objects/stack.py + api/attributes (ADR-0052)
        assert f["timeseries"].dtype == np.float32 and f["timeseries"].shape == (5, 30, 40)
        assert f["date"][0] == b"20240101"
        for k in (
            "X_FIRST",
            "Y_FIRST",
            "X_STEP",
            "Y_STEP",
            "LENGTH",
            "WIDTH",
            "REF_LAT",
            "REF_LON",
            "HEADING",
            "FILE_TYPE",
            "UNIT",
            "REF_DATE",
        ):
            assert k in f.attrs, k
        assert f.attrs["FILE_TYPE"] == "timeseries" and f.attrs["UNIT"] == "m"
        assert int(f.attrs["LENGTH"]) == 30 and int(f.attrs["WIDTH"]) == 40
    with h5py.File(tmp_path / "geometryGeo.h5", "w") as g:
        g.create_dataset("incidenceAngle", data=np.full((30, 40), 39.5, np.float32))
        g.create_dataset("height", data=np.full((30, 40), 50.0, np.float32))
    with h5py.File(tmp_path / "temporalCoherence.h5", "w") as g:
        g.create_dataset("temporalCoherence", data=np.full((30, 40), 0.9, np.float32))
    with h5py.File(tmp_path / "velocity.h5", "w") as g:
        g.create_dataset("velocity", data=ts0.velocity_m_per_yr)
    ts = load_timeseries(p)
    assert ts.attrs["format"] == "mintpy_h5" and ts.dates == ts0.dates
    np.testing.assert_allclose(ts.displacement_m, ts0.displacement_m)
    np.testing.assert_allclose(ts.lat, ts0.lat, atol=1e-9)
    np.testing.assert_allclose(ts.lon, ts0.lon, atol=1e-9)
    assert ts.heading_deg == -12.0 and ts.reference_latlon == (37.55, 126.95)
    assert float(ts.incidence2d()[0, 0]) == pytest.approx(39.5)
    assert ts.dem_m is not None and float(ts.dem_m[0, 0]) == 50.0
    assert ts.coherence is not None and float(ts.coherence[0, 0]) == pytest.approx(0.9)
    assert ts.velocity_m_per_yr is not None and ts.attrs["WAVELENGTH"] == "0.05546576"
    assert ts.attrs["geometry_path"].endswith("geometryGeo.h5")


def test_mintpy_h5_radar_coded_needs_geometry(tmp_path: Path) -> None:
    ts0 = make_timeseries(shape=(6, 8))
    p = tmp_path / "timeseries.h5"
    with h5py.File(p, "w") as f:
        f.create_dataset("timeseries", data=ts0.displacement_m)
        f.create_dataset("date", data=np.array([f"{d:%Y%m%d}".encode() for d in ts0.dates]))
        f.attrs["FILE_TYPE"] = "timeseries"
    with pytest.raises(ValueError, match="radar-coded"):
        read_timeseries_h5(p)
    g = tmp_path / "geometryRadar.h5"
    with h5py.File(g, "w") as f:
        f.create_dataset("latitude", data=ts0.lat2d())
        f.create_dataset("longitude", data=ts0.lon2d())
    ts = read_timeseries_h5(p, geometry_path=g)
    assert ts.lat.shape == (6, 8) and ts.heading_deg is None


def test_mintpy_h5_utm_attrs_are_transformed_to_latlon(tmp_path: Path) -> None:
    # X_UNIT=meters + EPSG -> pyproj to EPSG:4326 (ADR-0052)
    p = tmp_path / "timeseries.h5"
    with h5py.File(p, "w") as f:
        f.create_dataset("timeseries", data=np.zeros((2, 3, 4), np.float32))
        f.create_dataset("date", data=np.array([b"20240101", b"20240113"]))
        for k, v in {
            "X_FIRST": "300000",
            "Y_FIRST": "4160000",
            "X_STEP": "80",
            "Y_STEP": "-80",
            "X_UNIT": "meters",
            "Y_UNIT": "meters",
            "EPSG": "32652",
            "FILE_TYPE": "timeseries",
        }.items():
            f.attrs[k] = v
    ts = load_timeseries(p)
    assert ts.lat.shape == (3, 4) and 37.0 < float(ts.lat[0, 0]) < 38.0
    assert 126.0 < float(ts.lon[0, 0]) < 128.0


def test_h5_reader_matches_engines_reader_when_available(tmp_path: Path) -> None:
    """The io reader is the canonical one; engines.mintpy's copy must agree on the basics."""
    ts0 = make_timeseries(shape=(5, 6))
    p = write_timeseries_h5(ts0, tmp_path / "timeseries.h5")
    a = formats.read_timeseries_h5(p)
    try:
        from wintersar.engines.mintpy import read_timeseries_h5 as engines_reader
    except ImportError:  # pragma: no cover - adapter optional
        pytest.skip("engines.mintpy reader not available")
    b = engines_reader(p)
    assert a.dates == b.dates
    np.testing.assert_allclose(a.lat, b.lat)
    np.testing.assert_allclose(a.displacement_m, b.displacement_m)
