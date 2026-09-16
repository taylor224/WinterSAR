"""Engine output formats → shared containers (plan §3.3: *all* format conversion lives here).

* :func:`read_isce_raster` — ISCE2 flat binary + ``.xml`` sidecar (BIL/BIP/BSQ, any band
  count) with a ``.vrt``/rasterio fallback.
* :func:`read_hyp3_product_dir` — HyP3 Sentinel-1 Burst InSAR GeoTIFF products
  (``*_unw_phase.tif`` …) → :class:`~wintersar.io.igrams.IgramStack`.
* :func:`load_timeseries` — fake-engine ``timeseries.npz`` or MintPy ``timeseries.h5``
  (read with ``h5py`` only, rule 11.2: MintPy is GPL and is never imported) →
  :class:`~wintersar.io.timeseries.TimeSeries`.
* :func:`write_timeseries_npz` — the ``.npz`` interchange used by tests, bench and QGIS.

Verified format facts and their sources are recorded in ADR-0052; every constant below
carries a ``# source:`` comment (rule 11.3).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wintersar.io.igrams import IgramStack
from wintersar.io.timeseries import TimeSeries

# ============================================================================ ISCE2
# source: https://github.com/isce-framework/isce2/blob/main/components/isceobj/Image/Image.py
#   TO_NUMPY = {'BYTE':'i1','SHORT':'i2','INT':'i4','LONG':'i'+sizeLong,'FLOAT':'f4',
#               'DOUBLE':'f8','CFLOAT':'c8','CDOUBLE':'c16'}; renderVRT typeMap BYTE->Byte
#   (GDAL Byte = unsigned). MintPy mintpy/utils/readfile.py DATA_TYPE_ISCE2NUMPY maps
#   'byte' -> 'uint8', which is what the mask products (0/1/2/3) need; we follow the
#   VRT/MintPy reading (unsigned) — see ADR-0052.
ISCE_DATA_TYPES: dict[str, str] = {
    "BYTE": "u1",
    "SHORT": "i2",
    "INT": "i4",
    "LONG": "i8",
    "FLOAT": "f4",
    "DOUBLE": "f8",
    "CFLOAT": "c8",
    "CDOUBLE": "c16",
}
# source: isce2 Image.py ENDIAN = {'l','L','<','little','Little' -> 'l'; 'b','B','>','big' -> 'b'}
ISCE_BYTE_ORDER: dict[str, str] = {
    "l": "<",
    "little": "<",
    "<": "<",
    "b": ">",
    "big": ">",
    ">": ">",
}
# source: isce2 Image.py — self.scheme.upper() in ('BIL', 'BIP', 'BSQ')
ISCE_SCHEMES: tuple[str, ...] = ("BIL", "BIP", "BSQ")


class IsceFormatError(ValueError):
    """Malformed / inconsistent ISCE ``.xml`` sidecar or flat-binary file."""


def _sidecar_xml(path: Path) -> Path | None:
    """``file.int`` → ``file.int.xml`` (ISCE naming); accept ``file.int.xml`` itself."""
    if path.suffix == ".xml" and path.exists():
        return path
    cand = path.with_name(path.name + ".xml")
    return cand if cand.exists() else None


def _sidecar_vrt(path: Path) -> Path | None:
    if path.suffix == ".vrt" and path.exists():
        return path
    cand = path.with_name(path.name + ".vrt")
    return cand if cand.exists() else None


def parse_isce_xml(xml_path: Path) -> dict[str, Any]:
    """Parse an ISCE2 image ``.xml`` sidecar into a flat metadata dict.

    Property names are matched case-insensitively (ISCE renders them in lower case, e.g.
    ``<property name="width">``; MintPy's ``read_isce_xml`` lower-cases them too — source:
    https://github.com/insarlab/MintPy/blob/main/src/mintpy/utils/readfile.py). Coordinates
    come from ``<component name="coordinate1|coordinate2">`` (``startingvalue``, ``delta``,
    ``size``) and are exported MintPy-style as ``X_FIRST/X_STEP/Y_FIRST/Y_STEP``.
    """
    root = ET.parse(xml_path).getroot()
    if root.tag.lower() != "imagefile":
        msg = f"{xml_path}: root element is <{root.tag}>, expected <imageFile>"
        raise IsceFormatError(msg)
    props: dict[str, str] = {}
    for child in root.findall("property"):
        name = (child.get("name") or "").strip().lower()
        value = child.findtext("value")
        if name and value is not None:
            props[name] = value.strip()
    coords: dict[str, dict[str, str]] = {}
    for comp in root.findall("component"):
        cname = (comp.get("name") or "").strip().lower()
        if cname in ("coordinate1", "coordinate2"):
            coords[cname] = {
                (p.get("name") or "").strip().lower(): (p.findtext("value") or "").strip()
                for p in comp.findall("property")
            }
    try:
        width = int(props["width"])
        length = int(props["length"])
    except KeyError as e:
        msg = f"{xml_path}: missing required property {e.args[0]!r} (need width, length)"
        raise IsceFormatError(msg) from e
    data_type = props.get("data_type", "FLOAT").upper()
    if data_type not in ISCE_DATA_TYPES:
        msg = f"{xml_path}: unknown DATA_TYPE {data_type!r}; known: {sorted(ISCE_DATA_TYPES)}"
        raise IsceFormatError(msg)
    scheme = props.get("scheme", "BIP").upper()
    if scheme not in ISCE_SCHEMES:
        msg = f"{xml_path}: unknown SCHEME {scheme!r}; known: {ISCE_SCHEMES}"
        raise IsceFormatError(msg)
    byte_order = props.get("byte_order", "l").strip()
    if byte_order not in ISCE_BYTE_ORDER:
        msg = f"{xml_path}: unknown BYTE_ORDER {byte_order!r}; known: {sorted(ISCE_BYTE_ORDER)}"
        raise IsceFormatError(msg)
    meta: dict[str, Any] = {
        "WIDTH": width,
        "LENGTH": length,
        "DATA_TYPE": data_type,
        "SCHEME": scheme,
        "NUMBER_BANDS": int(props.get("number_bands", 1)),
        "BYTE_ORDER": byte_order,
        "FILE_NAME": props.get("file_name"),
        "IMAGE_TYPE": props.get("image_type"),
        "ACCESS_MODE": props.get("access_mode"),
        "ISCE_VERSION": props.get("isce_version"),
        "properties": props,
    }
    # source: MintPy readfile.read_isce_xml — coordinate1 -> X, coordinate2 -> Y; units are
    # degrees when 1e-7 < |step| < 1 (geocoded ``.geo`` files), else pixels (radar geometry)
    for cname, axis in (("coordinate1", "X"), ("coordinate2", "Y")):
        c = coords.get(cname)
        if not c:
            continue
        try:
            first = float(c.get("startingvalue", "nan"))
            step = float(c.get("delta", "nan"))
        except ValueError:
            continue
        meta[f"{axis}_FIRST"] = first
        meta[f"{axis}_STEP"] = step
        if 1e-7 < abs(step) < 1.0:
            meta[f"{axis}_UNIT"] = "degrees"
    return meta


def isce_numpy_dtype(meta: Mapping[str, Any]) -> np.dtype[Any]:
    base = ISCE_DATA_TYPES[str(meta["DATA_TYPE"]).upper()]
    order = ISCE_BYTE_ORDER[str(meta.get("BYTE_ORDER", "l"))]
    return np.dtype(order + base)


def read_isce_raster(
    path: Path | str, band: int | None = None
) -> tuple[NDArray[Any], dict[str, Any]]:
    """Read an ISCE2 flat-binary raster (+ ``.xml`` sidecar) or, failing that, its ``.vrt``.

    Returns ``(array, meta)``: ``(ny, nx)`` for single-band files or when ``band`` (0-based)
    is given, else ``(n_bands, ny, nx)``. ``meta`` holds the parsed sidecar (``WIDTH``,
    ``LENGTH``, ``DATA_TYPE``, ``SCHEME``, ``NUMBER_BANDS``, ``BYTE_ORDER`` …) plus
    ``reader`` (``"isce_xml"`` | ``"vrt"``). Data are read into memory (``np.fromfile``),
    not memory-mapped, so the returned array is safe to modify.
    """
    p = Path(path)
    xml = _sidecar_xml(p)
    if xml is not None:
        data_path = p if p.suffix != ".xml" else p.with_suffix("")
        meta = parse_isce_xml(xml)
        if not data_path.exists() and meta.get("FILE_NAME"):
            alt = xml.parent / str(meta["FILE_NAME"])
            if alt.exists():
                data_path = alt
        if not data_path.exists():
            msg = f"ISCE data file not found for sidecar {xml}"
            raise FileNotFoundError(msg)
        arr = _read_isce_binary(data_path, meta)
        meta["reader"] = "isce_xml"
        meta["source"] = str(data_path)
        return _select_band(arr, band), meta
    vrt = _sidecar_vrt(p)
    if vrt is not None:
        import rasterio

        with rasterio.open(vrt) as src:
            arr = src.read()
            meta = {
                "WIDTH": int(src.width),
                "LENGTH": int(src.height),
                "NUMBER_BANDS": int(src.count),
                "DATA_TYPE": str(src.dtypes[0]),
                "SCHEME": None,
                "BYTE_ORDER": None,
                "reader": "vrt",
                "source": str(vrt),
                "transform": tuple(src.transform)[:6],
                "crs": None if src.crs is None else src.crs.to_string(),
                "nodata": src.nodata,
            }
        return _select_band(arr, band), meta
    msg = f"no ISCE .xml or .vrt sidecar found for {p}"
    raise FileNotFoundError(msg)


def _read_isce_binary(data_path: Path, meta: Mapping[str, Any]) -> NDArray[Any]:
    dtype = isce_numpy_dtype(meta)
    width, length, bands = int(meta["WIDTH"]), int(meta["LENGTH"]), int(meta["NUMBER_BANDS"])
    expected = width * length * bands * dtype.itemsize
    size = data_path.stat().st_size
    if size < expected:
        msg = (
            f"{data_path}: file has {size} bytes but the sidecar implies {expected} "
            f"({bands} band(s) x {length} x {width} x {dtype.itemsize} B)"
        )
        raise IsceFormatError(msg)
    raw = np.fromfile(data_path, dtype=dtype, count=width * length * bands)
    scheme = str(meta["SCHEME"]).upper()
    # source: isce2 Image.py / MintPy readfile.read_binary — BSQ: (band, line, pixel),
    # BIL: (line, band, pixel), BIP: (line, pixel, band)
    if scheme == "BSQ":
        arr = raw.reshape(bands, length, width)
    elif scheme == "BIL":
        arr = raw.reshape(length, bands, width).transpose(1, 0, 2)
    else:  # BIP
        arr = raw.reshape(length, width, bands).transpose(2, 0, 1)
    return np.ascontiguousarray(arr)


def _select_band(arr: NDArray[Any], band: int | None) -> NDArray[Any]:
    if arr.ndim == 2:
        return arr
    if band is not None:
        return np.asarray(arr[band])
    return np.asarray(arr[0]) if arr.shape[0] == 1 else arr


# ============================================================================ HyP3
# source: https://hyp3-docs.asf.alaska.edu/guides/burst_insar_product_guide/ "Product files":
#   _unw_phase.tif  float32 radians ("negative values = motion toward sensor")
#   _wrapped_phase.tif float32 (-π, π]      _corr.tif float32 0..1
#   _conncomp.tif uint8                     _water_mask.tif uint8 (1 = land, 0 = water)
#   _lv_theta.tif float32 rad (look-vector elevation angle, -π/2..π/2)
#   _lv_phi.tif float32 rad (look-vector orientation, from East increasing toward North)
#   _dem.tif float32 m (geoid corrected)    _amp.tif float32 (multi-burst only)
#   <name>.txt processing parameters; projection UTM; 20x4 = 80 m, 10x2 = 40 m, 5x1 = 20 m
HYP3_SUFFIXES: dict[str, str] = {
    "unw": "_unw_phase",
    "wrapped": "_wrapped_phase",
    "corr": "_corr",
    "conncomp": "_conncomp",
    "water_mask": "_water_mask",
    "lv_theta": "_lv_theta",
    "lv_phi": "_lv_phi",
    "dem": "_dem",
    "amp": "_amp",
}
HYP3_REQUIRED: tuple[str, ...] = ("unw", "corr")
# source: MintPy docs dir_structure.md (HyP3 example ``*unw_phase_clip.tif``): products that
# were clipped to a common extent carry a ``_clip`` infix before ``.tif``.
HYP3_CLIP_INFIX = "_clip"
# source: MintPy prep_hyp3.py — dates are the two 8-digit fields of the product name
_HYP3_DATES_RE = re.compile(r"_(\d{8})_(\d{8})_")
# source: MintPy prep_hyp3.py HEADING = float(hyp3_meta['Heading']) % 360. - 360. and
#   ORBIT_DIRECTION = 'ASCENDING' if abs(HEADING) < 90 else 'DESCENDING'
HYP3_PIXEL_SPACING_M: dict[str, float] = {"20x4": 80.0, "10x2": 40.0, "5x1": 20.0}


class Hyp3FormatError(ValueError):
    """Missing / inconsistent HyP3 product files."""


def find_hyp3_file(product_dir: Path, key: str) -> Path | None:
    """``<product_dir>/*<suffix>[_clip].tif`` for one of :data:`HYP3_SUFFIXES` (clip preferred)."""
    suffix = HYP3_SUFFIXES[key]
    clipped = sorted(product_dir.glob(f"*{suffix}{HYP3_CLIP_INFIX}.tif"))
    if clipped:
        return clipped[0]
    plain = sorted(product_dir.glob(f"*{suffix}.tif"))
    return plain[0] if plain else None


def parse_hyp3_metadata_txt(path: Path) -> dict[str, str]:
    """``<product>.txt`` → ``{key_without_spaces: value}`` exactly like MintPy ``prep_hyp3``.

    source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/prep_hyp3.py
    (``key, value = line.strip().replace(' ', '').split(':')[:2]``).
    """
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.strip().replace(" ", "").split(":")[:2]
        if key:
            out[key] = value
    return out


def hyp3_pair_key(product_name: str) -> str:
    m = _HYP3_DATES_RE.search(product_name)
    if not m:
        msg = f"cannot find the two YYYYMMDD dates in HyP3 product name {product_name!r}"
        raise Hyp3FormatError(msg)
    d1, d2 = m.group(1), m.group(2)
    if d2 < d1:
        d1, d2 = d2, d1
    return f"{d1}_{d2}"


def _hyp3_product_dirs(root: Path) -> list[Path]:
    if find_hyp3_file(root, "unw") is not None:
        return [root]
    dirs = [d for d in sorted(root.iterdir()) if d.is_dir() and find_hyp3_file(d, "unw")]
    if not dirs:
        msg = f"{root}: no HyP3 product (*{HYP3_SUFFIXES['unw']}.tif) found here or one level down"
        raise Hyp3FormatError(msg)
    return dirs


def _read_tif(path: Path) -> tuple[NDArray[Any], dict[str, Any]]:
    import rasterio

    with rasterio.open(path) as src:
        arr = src.read(1)
        info = {
            "nodata": src.nodata,
            "transform": tuple(src.transform)[:6],
            "crs": None if src.crs is None else src.crs.to_string(),
            "epsg": None if src.crs is None else src.crs.to_epsg(),
            "shape": (int(src.height), int(src.width)),
        }
    return arr, info


def normalize_heading_deg(heading: float) -> float:
    """Heading into (-180, 180] (ascending S1 about -12 deg, descending about -168 deg / 192 deg)."""
    return float(((heading + 180.0) % 360.0) - 180.0)


def read_hyp3_product_dir(
    path: Path | str, *, with_geometry: bool = True, with_wrapped: bool = True
) -> IgramStack:
    """Read one HyP3 Burst InSAR product directory, or a directory of them, into an
    :class:`IgramStack`.

    * ``wrapped``: ``_wrapped_phase`` when present, else ``angle(exp(i·unw))``.
    * ``unw``: unwrapped phase in radians, HyP3 sign (negative = towards the satellite —
      the same sign as :data:`wintersar.research.synth.PHASE_PER_M_LOS`, so no flip).
      ``nodata`` and ``conncomp == 0`` pixels become NaN.
    * ``mask``: ``True`` where water (``_water_mask == 0``) or ``unw`` is not finite.
    * ``attrs``: ``crs``/``epsg``/``transform`` of the first product, ``hyp3_meta`` (parsed
      ``.txt`` per pair), ``heading_deg`` (normalised), and with ``with_geometry`` the
      ``incidence_deg = 90 - deg(lv_theta)`` (elevation → incidence), ``azimuth_deg``
      (``deg(lv_phi) - 90``: from-East-CCW → from-North-CCW, MintPy ``azimuthAngle``
      convention, source: mintpy/utils/utils0.py ``azimuth2heading_angle`` docstring) and
      ``dem_m`` of the first product.

    All products must share one raster shape; otherwise :class:`Hyp3FormatError` names the
    offending pair (clip to a common extent first — HYP3-009 in the HyP3 adapter).
    """
    root = Path(path)
    dirs = _hyp3_product_dirs(root)
    wrapped_l: list[NDArray[np.float32]] = []
    unw_l: list[NDArray[np.float32]] = []
    coh_l: list[NDArray[np.float32]] = []
    mask_l: list[NDArray[np.bool_]] = []
    cc_l: list[NDArray[np.uint8]] = []
    pairs: list[str] = []
    attrs: dict[str, Any] = {"source": str(root), "hyp3_meta": {}, "product_dirs": []}
    shape: tuple[int, int] | None = None
    have_cc = True
    for d in dirs:
        unw_path = find_hyp3_file(d, "unw")
        corr_path = find_hyp3_file(d, "corr")
        if unw_path is None or corr_path is None:
            msg = f"{d}: HyP3 product needs {[HYP3_SUFFIXES[k] for k in HYP3_REQUIRED]}"
            raise Hyp3FormatError(msg)
        key = hyp3_pair_key(unw_path.name)
        unw, info = _read_tif(unw_path)
        unw = np.asarray(unw, dtype=np.float32)
        if shape is None:
            shape = info["shape"]
            attrs.update({k: info[k] for k in ("crs", "epsg", "transform")})
        elif info["shape"] != shape:
            msg = (
                f"HyP3 product {key} has shape {info['shape']} but the first product has "
                f"{shape}; clip all products to a common extent before loading"
            )
            raise Hyp3FormatError(msg)
        if info["nodata"] is not None and np.isfinite(info["nodata"]):
            unw[unw == np.float32(info["nodata"])] = np.nan
        coh, _ = _read_tif(corr_path)
        coh = np.asarray(coh, dtype=np.float32)
        cc_path = find_hyp3_file(d, "conncomp")
        if cc_path is not None:
            cc, _ = _read_tif(cc_path)
            cc = np.asarray(cc, dtype=np.uint8)
            unw[cc == 0] = np.nan
            cc_l.append(cc)
        else:
            have_cc = False
        wm_path = find_hyp3_file(d, "water_mask")
        mask = ~np.isfinite(unw)
        if wm_path is not None:
            wm, _ = _read_tif(wm_path)
            mask |= np.asarray(wm) == 0  # 1 = land, 0 = water
        wr_path = find_hyp3_file(d, "wrapped") if with_wrapped else None
        if wr_path is not None:
            wr, _ = _read_tif(wr_path)
            wrapped = np.asarray(wr, dtype=np.float32)
        else:
            wrapped = np.asarray(np.angle(np.exp(1j * np.nan_to_num(unw))), dtype=np.float32)
        txts = [p for p in d.glob("*.txt") if not p.name.endswith(".README.md.txt")]
        if txts:
            attrs["hyp3_meta"][key] = parse_hyp3_metadata_txt(txts[0])
        pairs.append(key)
        wrapped_l.append(wrapped)
        unw_l.append(unw)
        coh_l.append(coh)
        mask_l.append(mask)
        attrs["product_dirs"].append(str(d))
    if with_geometry:
        # source: MintPy objects/stackDict.py geometryDict.write2hdf5 (HyP3/Gamma angles):
        #   theta: data[data == 0] = nan; incidenceAngle = 90 - theta*180/pi
        #   phi:   data[data == 0] = nan; azimuthAngle = wrap(phi*180/pi - 90, [-180, 180])
        first = dirs[0]
        th = find_hyp3_file(first, "lv_theta")
        if th is not None:
            theta = np.asarray(_read_tif(th)[0], dtype=np.float64)
            theta[theta == 0] = np.nan
            attrs["incidence_deg"] = np.asarray(90.0 - np.degrees(theta), dtype=np.float32)
        ph = find_hyp3_file(first, "lv_phi")
        if ph is not None:
            phi = np.asarray(_read_tif(ph)[0], dtype=np.float64)
            phi[phi == 0] = np.nan
            az = np.degrees(phi) - 90.0
            attrs["azimuth_deg"] = np.asarray(((az + 180.0) % 360.0) - 180.0, dtype=np.float32)
        dem = find_hyp3_file(first, "dem")
        if dem is not None:
            dem_arr, _ = _read_tif(dem)
            attrs["dem_m"] = np.asarray(dem_arr, dtype=np.float32)
    headings = [
        float(m["Heading"])
        for m in attrs["hyp3_meta"].values()
        if "Heading" in m and _is_float(m["Heading"])
    ]
    if headings:
        attrs["heading_deg"] = normalize_heading_deg(float(np.mean(headings)))
    dates = sorted({date.fromisoformat(f"{k[:4]}-{k[4:6]}-{k[6:8]}") for k in pairs}) + sorted(
        {date.fromisoformat(f"{k[9:13]}-{k[13:15]}-{k[15:17]}") for k in pairs}
    )
    return IgramStack(
        wrapped=np.stack(wrapped_l),
        coherence=np.stack(coh_l),
        pairs=pairs,
        dates=sorted(set(dates)),
        mask=np.stack(mask_l),
        unw=np.stack(unw_l),
        conncomp=np.stack(cc_l) if have_cc and cc_l else None,
        attrs=attrs,
    )


def _is_float(s: str) -> bool:
    try:
        float(s)
    except ValueError:
        return False
    return True


# ============================================================================ time series
# Synthetic grid for .npz stacks without coordinates: pixel spacing of the HyP3 default
# 20x4 looks (80 m, source: burst_insar_product_guide) around the tests' Seoul AOI centre.
SYNTHETIC_CENTER_LATLON: tuple[float, float] = (37.55, 126.95)
SYNTHETIC_PIXEL_M: float = 80.0
_M_PER_DEG_LAT = 111_320.0  # metres per degree of latitude (spherical Earth approximation)


def synthetic_latlon_grid(
    shape: tuple[int, int],
    center: tuple[float, float] = SYNTHETIC_CENTER_LATLON,
    pixel_m: float = SYNTHETIC_PIXEL_M,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Regular north-up lat (ny,) / lon (nx,) vectors for a stack without coordinates."""
    ny, nx = shape
    lat0, lon0 = center
    dlat = pixel_m / _M_PER_DEG_LAT
    dlon = pixel_m / (_M_PER_DEG_LAT * np.cos(np.deg2rad(lat0)))
    lat = lat0 + dlat * ((ny - 1) / 2.0 - np.arange(ny, dtype=np.float64))
    lon = lon0 + dlon * (np.arange(nx, dtype=np.float64) - (nx - 1) / 2.0)
    return lat, lon


def _parse_iso_or_yyyymmdd(s: str) -> date:
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return date.fromisoformat(s[:10])


def load_timeseries(path: Path | str, **kwargs: Any) -> TimeSeries:
    """Dispatch on suffix: ``.npz`` (fake engine / :func:`write_timeseries_npz`) or ``.h5``
    (MintPy ``timeseries*.h5``; keyword ``geometry_path`` etc. see :func:`read_timeseries_h5`)."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".npz":
        return read_timeseries_npz(p, **kwargs)
    if suffix in (".h5", ".hdf5", ".he5"):
        return read_timeseries_h5(p, **kwargs)
    msg = f"unsupported time-series format {p.suffix!r} (expected .npz or .h5)"
    raise ValueError(msg)


def read_timeseries_npz(
    path: Path | str, center: tuple[float, float] = SYNTHETIC_CENTER_LATLON
) -> TimeSeries:
    """``timeseries.npz``: ``dates`` (ISO), ``displacement_m`` (n, ny, nx), optional
    ``velocity_m_per_yr``, ``lat``/``lon`` (1-D or 2-D), ``incidence_deg``, ``heading_deg``,
    ``coherence``, ``dem_m``, ``conncomp``, ``attrs`` (JSON string). A missing lat/lon grid is
    replaced by :func:`synthetic_latlon_grid` and flagged ``attrs['latlon_synthetic']``."""
    p = Path(path)
    with np.load(p, allow_pickle=False) as z:
        files = set(z.files)
        if "displacement_m" not in files or "dates" not in files:
            msg = f"{p}: needs 'dates' and 'displacement_m' arrays (found {sorted(files)})"
            raise ValueError(msg)
        disp = np.asarray(z["displacement_m"], dtype=np.float32)
        dates = [_parse_iso_or_yyyymmdd(str(d)) for d in z["dates"]]
        opt = {k: z[k] for k in z.files if k not in ("displacement_m", "dates", "attrs")}
        attrs_raw = str(z["attrs"]) if "attrs" in files else ""
    if disp.ndim != 3 or disp.shape[0] != len(dates):
        msg = f"{p}: displacement_m shape {disp.shape} does not match {len(dates)} dates"
        raise ValueError(msg)
    shape = (int(disp.shape[1]), int(disp.shape[2]))
    attrs: dict[str, Any] = {"source": str(p), "format": "npz"}
    if attrs_raw:
        try:
            attrs.update(json.loads(attrs_raw))
        except json.JSONDecodeError:
            attrs["attrs_raw"] = attrs_raw
    if "lat" in opt and "lon" in opt:
        lat, lon = (
            np.asarray(opt["lat"], dtype=np.float64),
            np.asarray(opt["lon"], dtype=np.float64),
        )
        attrs["latlon_synthetic"] = False
    else:
        lat, lon = synthetic_latlon_grid(shape, center)
        attrs["latlon_synthetic"] = True
        attrs["pixel_m"] = SYNTHETIC_PIXEL_M
    inc: NDArray[np.floating] | float | None = None
    if "incidence_deg" in opt:
        a = np.asarray(opt["incidence_deg"], dtype=np.float32)
        inc = float(a) if a.ndim == 0 else a
    heading = float(opt["heading_deg"]) if "heading_deg" in opt else None
    ref = None
    if "reference_latlon" in opt:
        r = np.asarray(opt["reference_latlon"], dtype=np.float64).ravel()
        ref = (float(r[0]), float(r[1]))
    return TimeSeries(
        dates=dates,
        displacement_m=disp,
        lat=lat,
        lon=lon,
        incidence_deg=inc,
        heading_deg=heading,
        coherence=_f32(opt.get("coherence")),
        velocity_m_per_yr=_f32(opt.get("velocity_m_per_yr")),
        reference_latlon=ref,
        dem_m=_f32(opt.get("dem_m")),
        conncomp=None if "conncomp" not in opt else np.asarray(opt["conncomp"]),
        attrs=attrs,
    )


def _f32(a: Any) -> NDArray[np.float32] | None:
    return None if a is None else np.asarray(a, dtype=np.float32)


def write_timeseries_npz(ts: TimeSeries, path: Path | str) -> Path:
    """Inverse of :func:`read_timeseries_npz` (compressed; attrs as a JSON string)."""
    p = Path(path)
    arrays: dict[str, Any] = {
        "dates": np.array([d.isoformat() for d in ts.dates]),
        "displacement_m": np.asarray(ts.displacement_m, dtype=np.float32),
        "lat": np.asarray(ts.lat, dtype=np.float64),
        "lon": np.asarray(ts.lon, dtype=np.float64),
    }
    if ts.velocity_m_per_yr is not None:
        arrays["velocity_m_per_yr"] = np.asarray(ts.velocity_m_per_yr, dtype=np.float32)
    if ts.incidence_deg is not None:
        arrays["incidence_deg"] = np.asarray(ts.incidence_deg, dtype=np.float32)
    if ts.heading_deg is not None:
        arrays["heading_deg"] = np.float64(ts.heading_deg)
    if ts.coherence is not None:
        arrays["coherence"] = np.asarray(ts.coherence, dtype=np.float32)
    if ts.dem_m is not None:
        arrays["dem_m"] = np.asarray(ts.dem_m, dtype=np.float32)
    if ts.conncomp is not None:
        arrays["conncomp"] = np.asarray(ts.conncomp)
    if ts.reference_latlon is not None:
        arrays["reference_latlon"] = np.asarray(ts.reference_latlon, dtype=np.float64)
    plain = {k: v for k, v in ts.attrs.items() if _jsonable(v)}
    arrays["attrs"] = np.array(json.dumps(plain, ensure_ascii=False, default=str))
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(p, **arrays)
    return p


def _jsonable(v: Any) -> bool:
    try:
        json.dumps(v, default=str)
    except (TypeError, ValueError):
        return False
    return not isinstance(v, np.ndarray)


# ---------------------------------------------------------------- MintPy HDF5 (h5py only)
# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/objects/stack.py
#   timeseries.write2hdf5: datasets 'timeseries' float32 (numDate, length, width) [m],
#   'date' bytes 'YYYYMMDD' (numDate,), 'bperp' float32 (numDate,); attribute FILE_TYPE.
#   GEOMETRY_DSET_NAMES: height, latitude, longitude, rangeCoord, azimuthCoord,
#   incidenceAngle, azimuthAngle, slantRangeDistance, shadowMask, waterMask, commonMask, bperp
#   DSET_UNIT_DICT: timeseries 'm', velocity 'm/year', temporalCoherence '1',
#   incidenceAngle 'degree', azimuthAngle 'degree', height 'm'
# source: https://mintpy.readthedocs.io/en/latest/api/attributes/
#   X_FIRST/Y_FIRST (upper-left corner of the first pixel, degrees or metres), X_STEP/Y_STEP,
#   X_UNIT/Y_UNIT ('degrees' | 'meters'), LENGTH (rows), WIDTH (columns), REF_LAT/REF_LON,
#   REF_X/REF_Y, REF_DATE, HEADING ("measured from the north with clock-wise as positive"),
#   ORBIT_DIRECTION, WAVELENGTH, EPSG (geocoded only), UTM_ZONE, FILE_TYPE, UNIT, NO_DATA_VALUE
# source: mintpy/utils/utils0.py get_lat_lon — pixel centre = Y_FIRST + Y_STEP * (row + 0.5);
#   azimuth2heading_angle docstring: azimuthAngle is "measured from the north in
#   anti-clockwise as positive", heading (right-looking) = -(azimuth - 90)
MINTPY_TS_DATASET = "timeseries"
MINTPY_DATE_DATASET = "date"
MINTPY_BPERP_DATASET = "bperp"
MINTPY_GEOMETRY_DATASETS: tuple[str, ...] = (
    "height",
    "latitude",
    "longitude",
    "incidenceAngle",
    "azimuthAngle",
    "slantRangeDistance",
    "shadowMask",
    "waterMask",
)
MINTPY_GEO_ATTRS: tuple[str, ...] = ("X_FIRST", "Y_FIRST", "X_STEP", "Y_STEP")


def _decode(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, np.ndarray):
        return " ".join(_decode(x) for x in v.tolist())
    return str(v)


def read_h5_attrs(path: Path | str) -> dict[str, str]:
    """Root attributes of a MintPy HDF5 file as ``{str: str}`` (MintPy stores strings)."""
    import h5py

    with h5py.File(path, "r") as f:
        return {str(k): _decode(v) for k, v in f.attrs.items()}


def _h5_dataset(path: Path | None, name: str) -> NDArray[Any] | None:
    if path is None or not Path(path).exists():
        return None
    import h5py

    with h5py.File(path, "r") as f:
        if name not in f:
            return None
        return np.asarray(f[name][:])


def mintpy_latlon_grid(
    attrs: Mapping[str, str], ny: int, nx: int
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Pixel-centre lat/lon from ``X_FIRST/Y_FIRST/X_STEP/Y_STEP`` (+ ``EPSG`` for UTM)."""
    y0, dy = float(attrs["Y_FIRST"]), float(attrs["Y_STEP"])
    x0, dx = float(attrs["X_FIRST"]), float(attrs["X_STEP"])
    ys = y0 + dy * (np.arange(ny, dtype=np.float64) + 0.5)
    xs = x0 + dx * (np.arange(nx, dtype=np.float64) + 0.5)
    unit = str(attrs.get("Y_UNIT", attrs.get("X_UNIT", "degrees"))).lower()
    epsg = attrs.get("EPSG")
    if unit.startswith("deg") or not epsg or str(epsg) == "4326":
        return ys, xs
    from pyproj import Transformer

    tr = Transformer.from_crs(f"EPSG:{int(float(epsg))}", "EPSG:4326", always_xy=True)
    xx, yy = np.meshgrid(xs, ys)
    lon, lat = tr.transform(xx, yy)
    return np.asarray(lat, dtype=np.float64), np.asarray(lon, dtype=np.float64)


def _first_existing(*cands: Path) -> Path | None:
    return next((c for c in cands if c.exists()), None)


def read_timeseries_h5(
    path: Path | str,
    *,
    geometry_path: Path | None = None,
    velocity_path: Path | None = None,
    temporal_coherence_path: Path | None = None,
    mask_path: Path | None = None,
) -> TimeSeries:
    """MintPy ``timeseries*.h5`` (+ optional sidecars) → :class:`TimeSeries`.

    Geocoded files carry ``X_FIRST/Y_FIRST/X_STEP/Y_STEP`` (pixel-centre grid); radar-coded
    files need ``geometryRadar.h5`` with ``latitude``/``longitude``. Sidecars default to the
    siblings ``geometryGeo.h5`` / ``inputs/geometryGeo.h5`` (``incidenceAngle`` [degree],
    ``height`` [m]), ``velocity.h5`` (``velocity`` [m/year]), ``temporalCoherence.h5``
    (``temporalCoherence``) and ``maskTempCoh.h5`` (``mask``). ``HEADING`` (degrees, clockwise
    from north) → ``heading_deg``; ``REF_LAT/REF_LON`` → ``reference_latlon``.
    """
    import h5py

    p = Path(path)
    with h5py.File(p, "r") as f:
        if MINTPY_TS_DATASET not in f or MINTPY_DATE_DATASET not in f:
            msg = f"{p}: not a MintPy timeseries file (needs datasets 'timeseries' and 'date')"
            raise ValueError(msg)
        attrs = {str(k): _decode(v) for k, v in f.attrs.items()}
        data = np.asarray(f[MINTPY_TS_DATASET][:], dtype=np.float32)
        dates = [_parse_iso_or_yyyymmdd(_decode(d)) for d in f[MINTPY_DATE_DATASET][:]]
        bperp = np.asarray(f[MINTPY_BPERP_DATASET][:]) if MINTPY_BPERP_DATASET in f else None
    if data.ndim != 3 or data.shape[0] != len(dates):
        msg = f"{p}: timeseries shape {data.shape} does not match {len(dates)} dates"
        raise ValueError(msg)
    ny, nx = int(data.shape[1]), int(data.shape[2])
    wd = p.parent
    prefix = "geo_" if p.name.startswith("geo_") else ""
    if geometry_path is None:
        geometry_path = _first_existing(
            wd / f"{prefix}geometryGeo.h5",
            wd / f"{prefix}geometryRadar.h5",
            wd / "inputs" / "geometryGeo.h5",
            wd / "inputs" / "geometryRadar.h5",
            wd.parent / "inputs" / "geometryGeo.h5",
            wd.parent / "inputs" / "geometryRadar.h5",
        )
    if all(k in attrs for k in MINTPY_GEO_ATTRS):
        lat, lon = mintpy_latlon_grid(attrs, ny, nx)
    else:
        la = _h5_dataset(geometry_path, "latitude")
        lo = _h5_dataset(geometry_path, "longitude")
        if la is None or lo is None:
            msg = (
                f"{p}: radar-coded file (no X_FIRST/Y_FIRST) and no geometry file with "
                "'latitude'/'longitude' datasets; pass geometry_path=geometryRadar.h5"
            )
            raise ValueError(msg)
        lat, lon = np.asarray(la, dtype=np.float64), np.asarray(lo, dtype=np.float64)
    inc = _h5_dataset(geometry_path, "incidenceAngle")
    dem = _h5_dataset(geometry_path, "height")
    vel = _h5_dataset(velocity_path or wd / f"{prefix}velocity.h5", "velocity")
    coh = _h5_dataset(
        temporal_coherence_path or wd / f"{prefix}temporalCoherence.h5", "temporalCoherence"
    )
    mask = _h5_dataset(mask_path or wd / f"{prefix}maskTempCoh.h5", "mask")
    heading = float(attrs["HEADING"]) if "HEADING" in attrs else None
    ref: tuple[float, float] | None = None
    if "REF_LAT" in attrs and "REF_LON" in attrs:
        ref = (float(attrs["REF_LAT"]), float(attrs["REF_LON"]))
    extra: dict[str, Any] = {
        **attrs,
        "source": str(p),
        "format": "mintpy_h5",
        "REF_DATE": attrs.get("REF_DATE", f"{dates[0]:%Y%m%d}"),
        "geometry_path": None if geometry_path is None else str(geometry_path),
    }
    if bperp is not None:
        extra["bperp"] = bperp.astype(np.float32)
    if mask is not None:
        extra["mask"] = mask.astype(bool)
    return TimeSeries(
        dates=dates,
        displacement_m=data,
        lat=lat,
        lon=lon,
        incidence_deg=_f32(inc),
        heading_deg=heading,
        coherence=_f32(coh),
        velocity_m_per_yr=_f32(vel),
        reference_latlon=ref,
        dem_m=_f32(dem),
        attrs=extra,
    )


def write_timeseries_h5(
    ts: TimeSeries,
    path: Path | str,
    *,
    extra_attrs: Mapping[str, Any] | None = None,
) -> Path:
    """Write a MintPy-layout ``timeseries.h5`` (h5py) — used by tests/fixtures and to hand
    fake-engine results to tools that read MintPy files. Requires a regular lat/lon grid."""
    import h5py

    p = Path(path)
    lat, lon = np.asarray(ts.lat, dtype=np.float64), np.asarray(ts.lon, dtype=np.float64)
    if lat.ndim == 2:
        lat = lat[:, 0]
    if lon.ndim == 2:
        lon = lon[0, :]
    ny, nx = ts.shape
    dy = float(lat[1] - lat[0]) if ny > 1 else -1e-3
    dx = float(lon[1] - lon[0]) if nx > 1 else 1e-3
    attrs: dict[str, Any] = {
        "FILE_TYPE": "timeseries",
        "UNIT": "m",
        "LENGTH": str(ny),
        "WIDTH": str(nx),
        "X_FIRST": repr(float(lon[0] - dx / 2.0)),
        "Y_FIRST": repr(float(lat[0] - dy / 2.0)),
        "X_STEP": repr(dx),
        "Y_STEP": repr(dy),
        "X_UNIT": "degrees",
        "Y_UNIT": "degrees",
        "EPSG": "4326",
        "REF_DATE": f"{ts.dates[0]:%Y%m%d}",
    }
    if ts.heading_deg is not None:
        attrs["HEADING"] = repr(float(ts.heading_deg))
    if ts.reference_latlon is not None:
        attrs["REF_LAT"] = repr(float(ts.reference_latlon[0]))
        attrs["REF_LON"] = repr(float(ts.reference_latlon[1]))
    for k, v in (extra_attrs or {}).items():
        attrs[str(k)] = str(v)
    p.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(p, "w") as f:
        f.create_dataset(MINTPY_TS_DATASET, data=np.asarray(ts.displacement_m, dtype=np.float32))
        f.create_dataset(
            MINTPY_DATE_DATASET,
            data=np.array([f"{d:%Y%m%d}".encode() for d in ts.dates], dtype="S8"),
        )
        for k, v in attrs.items():
            f.attrs[k] = str(v)
    return p
