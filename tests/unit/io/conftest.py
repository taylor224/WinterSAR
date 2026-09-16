"""Fixtures for the io format tests (ISCE sidecars, HyP3 GeoTIFF products, small stacks)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from wintersar.io.formats import synthetic_latlon_grid
from wintersar.io.igrams import IgramStack
from wintersar.io.timeseries import TimeSeries
from wintersar.research import synth


def isce_xml(
    width: int,
    length: int,
    data_type: str = "FLOAT",
    scheme: str = "BIL",
    bands: int = 1,
    byte_order: str = "l",
    file_name: str = "test.unw",
    geocoded: bool = True,
) -> str:
    """ISCE2 image sidecar as MintPy's ``read_isce_xml`` expects it (lower-case property
    names, ``component name="coordinate1|2"`` with ``startingvalue``/``delta``/``size``).
    source: https://github.com/isce-framework/isce2/blob/main/components/isceobj/Image/Image.py
    (parameter_list WIDTH/LENGTH/DATA_TYPE/SCHEME/NUMBER_BANDS/BYTE_ORDER/FILE_NAME, coordinates)
    and https://github.com/insarlab/MintPy/blob/main/src/mintpy/utils/readfile.py read_isce_xml."""
    if geocoded:
        c1 = ("126.9", "0.000833")
        c2 = ("37.6", "-0.000833")
    else:
        c1 = ("0", "1")
        c2 = ("0", "1")

    def coord(name: str, start: str, delta: str, size: int, doc: str) -> str:
        return f"""    <component name="{name}">
        <factorymodule>isceobj.Image</factorymodule>
        <factoryname>createCoordinate</factoryname>
        <doc>{doc}</doc>
        <property name="delta"><value>{delta}</value><doc>Coordinate quantization.</doc></property>
        <property name="endingvalue"><value>0</value><doc>Ending value of the coordinate.</doc></property>
        <property name="family"><value>imagecoordinate</value><doc>Instance family name</doc></property>
        <property name="name"><value>imagecoordinate_name</value><doc>Instance name</doc></property>
        <property name="size"><value>{size}</value><doc>Coordinate size.</doc></property>
        <property name="startingvalue"><value>{start}</value><doc>Starting value of the coordinate.</doc></property>
    </component>
"""

    return (
        "<imageFile>\n"
        '    <property name="ISCE_VERSION"><value>Release: 2.6.3, svn-, 20230418.</value></property>\n'
        '    <property name="access_mode"><value>read</value><doc>Image access mode.</doc></property>\n'
        f'    <property name="byte_order"><value>{byte_order}</value><doc>Endianness of the image.</doc></property>\n'
        + coord("coordinate1", c1[0], c1[1], width, "First coordinate of a 2D image (width).")
        + coord("coordinate2", c2[0], c2[1], length, "Second coordinate of a 2D image (length).")
        + f'    <property name="data_type"><value>{data_type}</value><doc>Image data type.</doc></property>\n'
        f'    <property name="extra_file_name"><value>{file_name}.vrt</value><doc>For example name of vrt metadata.</doc></property>\n'
        '    <property name="family"><value>image</value><doc>Instance family name</doc></property>\n'
        f'    <property name="file_name"><value>{file_name}</value><doc>Name of the image file.</doc></property>\n'
        '    <property name="image_type"><value>unw</value><doc>Image type used for displaying.</doc></property>\n'
        f'    <property name="length"><value>{length}</value><doc>Image length</doc></property>\n'
        f'    <property name="number_bands"><value>{bands}</value><doc>Number of image bands</doc></property>\n'
        f'    <property name="scheme"><value>{scheme}</value><doc>Interleaving scheme of the image.</doc></property>\n'
        f'    <property name="width"><value>{width}</value><doc>Image width</doc></property>\n'
        '    <property name="xmax"><value>0</value></property>\n'
        '    <property name="xmin"><value>0</value></property>\n'
        "</imageFile>\n"
    )


@pytest.fixture
def isce_xml_factory() -> Callable[..., str]:
    return isce_xml


def write_tif(path: Path, arr: np.ndarray, nodata: float | None = None, epsg: int = 32652) -> Path:
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=arr.shape[1],
        height=arr.shape[0],
        count=1,
        dtype=arr.dtype.name,
        crs=f"EPSG:{epsg}",
        transform=from_origin(300000.0, 4160000.0, 80.0, 80.0),
        nodata=nodata,
    ) as dst:
        dst.write(arr, 1)
    return path


def write_hyp3_product(
    root: Path,
    name: str,
    shape: tuple[int, int] = (12, 16),
    seed: int = 0,
    clip: bool = False,
    with_conncomp: bool = True,
    with_water: bool = True,
    with_wrapped: bool = False,
    heading: float = -12.3,
) -> Path:
    """One HyP3 Burst InSAR product directory with the documented file suffixes
    (source: https://hyp3-docs.asf.alaska.edu/guides/burst_insar_product_guide/)."""
    pd = root / name
    pd.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    infix = "_clip" if clip else ""
    unw = rng.normal(0, 1, shape).astype(np.float32)
    unw[0, 0] = 0.0  # nodata pixel
    write_tif(pd / f"{name}_unw_phase{infix}.tif", unw, nodata=0.0)
    write_tif(pd / f"{name}_corr{infix}.tif", np.full(shape, 0.7, np.float32))
    if with_conncomp:
        cc = np.ones(shape, np.uint8)
        cc[1, 1] = 0
        write_tif(pd / f"{name}_conncomp{infix}.tif", cc)
    if with_water:
        wm = np.ones(shape, np.uint8)
        wm[:, 0] = 0  # first column is water (1 = land, 0 = water)
        write_tif(pd / f"{name}_water_mask{infix}.tif", wm)
    if with_wrapped:
        write_tif(
            pd / f"{name}_wrapped_phase{infix}.tif", np.angle(np.exp(1j * unw)).astype(np.float32)
        )
    write_tif(pd / f"{name}_lv_theta{infix}.tif", np.full(shape, np.deg2rad(50.0), np.float32))
    write_tif(pd / f"{name}_lv_phi{infix}.tif", np.full(shape, np.deg2rad(192.0), np.float32))
    write_tif(pd / f"{name}_dem{infix}.tif", np.full(shape, 55.0, np.float32))
    (pd / f"{name}.txt").write_text(
        "Reference Granule: S1_136231_IW2_20240101T092000_VV_ABCD-BURST\n"
        f"Heading: {heading}\nUTC time: 33600.0\nAzimuth looks: 4\nRange looks: 20\n"
        "Earth radius at nadir: 6371000\nSpacecraft height: 693000\nSlant range near: 800000\n"
        "Baseline: 45.6\nUnwrapping type: snaphu_mcf\n",
        encoding="utf-8",
    )
    (pd / f"{name}.README.md.txt").write_text("readme", encoding="utf-8")
    return pd


HYP3_NAMES = (
    "S1_136231_IW2_20240101_20240113_VV_INT80_ABCD",
    "S1_136231_IW2_20240113_20240125_VV_INT80_EF01",
)


@pytest.fixture
def hyp3_root(tmp_path: Path) -> Path:
    root = tmp_path / "hyp3"
    for i, name in enumerate(HYP3_NAMES):
        write_hyp3_product(root, name, seed=i)
    return root


def make_stack(n_dates: int = 5, shape: tuple[int, int] = (40, 48), seed: int = 0) -> IgramStack:
    rng = np.random.default_rng(seed)
    st = synth.make_stack(n_dates=n_dates, shape=shape, rng=rng, water_fraction=0.1)
    keys = [p.key for p in st.pairs]
    return IgramStack(
        wrapped=np.stack([st.igrams[k].wrapped for k in keys]).astype(np.float32),
        coherence=np.stack([st.igrams[k].coherence for k in keys]).astype(np.float32),
        pairs=keys,
        dates=st.dates,
        mask=np.stack([st.igrams[k].mask for k in keys]),
        unw=np.stack([st.igrams[k].unw_true for k in keys]).astype(np.float32),
        conncomp=np.ones((len(keys), *shape), dtype=np.uint8),
        truth={"velocity_true": st.velocity_m_per_yr.astype(np.float32)},
        attrs={"note": "synthetic"},
    )


@pytest.fixture
def small_stack() -> IgramStack:
    return make_stack()


def make_timeseries(
    shape: tuple[int, int] = (30, 40), n_dates: int = 5, seed: int = 0, north_up: bool = True
) -> TimeSeries:
    rng = np.random.default_rng(seed)
    lat, lon = synthetic_latlon_grid(shape)
    if not north_up:
        lat = lat[::-1].copy()
    dates = [date(2024, 1, 1) + timedelta(days=12 * i) for i in range(n_dates)]
    years = np.array([(d - dates[0]).days / 365.25 for d in dates])
    vel = synth.deformation_field(shape, "gaussian", -0.03)
    disp = (vel[None] * years[:, None, None] + rng.normal(0, 1e-4, (n_dates, *shape))).astype(
        np.float32
    )
    return TimeSeries(
        dates=dates,
        displacement_m=disp,
        lat=lat,
        lon=lon,
        incidence_deg=39.0,
        heading_deg=-12.0,
        coherence=rng.random(shape).astype(np.float32),
        velocity_m_per_yr=vel.astype(np.float32),
        reference_latlon=(37.55, 126.95),
        conncomp=np.ones(shape, np.uint8),
        attrs={"REF_DATE": "20240101"},
    )


@pytest.fixture
def small_ts() -> TimeSeries:
    return make_timeseries()
