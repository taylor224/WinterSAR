"""Cloud-Optimized GeoTIFF export of results (PERF-08 "결과는 COG(오버뷰 포함)", ADR-0051).

Facts (verified 2026-09-16):

* GDAL COG driver — source: https://gdal.org/en/stable/drivers/raster/cog.html — creation
  options ``BLOCKSIZE`` (default 512, multiple of 16), ``COMPRESS`` (NONE/LZW/JPEG/DEFLATE/
  ZSTD/WEBP/LERC/LERC_DEFLATE/LERC_ZSTD/LZMA/JXL, default LZW), ``LEVEL`` (DEFLATE default 6,
  ZSTD default 9), ``PREDICTOR`` (YES/NO/STANDARD/FLOATING_POINT, default NO), ``OVERVIEWS``
  (AUTO/IGNORE_EXISTING/FORCE_USE_EXISTING/NONE, default AUTO), ``OVERVIEW_RESAMPLING`` /
  ``RESAMPLING`` (NEAREST/AVERAGE/BILINEAR/CUBIC/…), ``BIGTIFF`` (IF_NEEDED), ``NUM_THREADS``.
  Requires GDAL >= 3.1; ``Create()`` only since GDAL 3.13, so on the installed GDAL 3.10.3
  rasterio goes through its ``IndirectRasterUpdater`` (temporary in-memory dataset +
  ``GDALCreateCopy``, source: https://rasterio.readthedocs.io/en/stable/topics/writing.html).
  Verified empirically in this venv: ``rasterio.open(path, "w", driver="COG", …)`` and
  ``rasterio.shutil.copy(tmp, path, driver="COG", **opts)`` both produce
  ``IMAGE_STRUCTURE.LAYOUT == "COG"`` with overviews; overviews are only generated when the
  raster is larger than ``BLOCKSIZE`` (AUTO), so tiny rasters legitimately have none.
* Fallback when the COG driver is missing: ``GTiff`` with ``TILED=YES`` + ``build_overviews``
  (source: https://rasterio.readthedocs.io/en/stable/topics/overviews.html).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from wintersar.io.timeseries import TimeSeries

Compress = Literal["DEFLATE", "ZSTD", "LZW", "NONE"]
COMPRESSORS: tuple[str, ...] = ("DEFLATE", "ZSTD", "LZW", "NONE")
DEFAULT_BLOCKSIZE = 512  # GDAL COG default
MIN_BLOCKSIZE = 128  # GDAL COG driver warns for BLOCKSIZE < 128 (observed on GDAL 3.10.3)
DEFAULT_OVERVIEW_RESAMPLING = "AVERAGE"  # continuous fields (velocity/displacement/coherence)
MASK_OVERVIEW_RESAMPLING = "NEAREST"  # categorical (mask/conncomp)
MASK_NODATA = 255


class CogError(RuntimeError):
    """The written file is not a valid tiled GeoTIFF / COG."""


def cog_driver_available() -> bool:
    """``True`` when the installed GDAL exposes the ``COG`` driver."""
    import rasterio

    with rasterio.Env() as env:
        return "COG" in env.drivers()


def _predictor(dtype: np.dtype[Any], compress: str, predictor: str) -> str | None:
    if compress == "NONE":
        return None
    if predictor != "auto":
        return predictor
    # PREDICTOR=FLOATING_POINT (3) for floats, STANDARD (2) for integers — GDAL COG doc
    return "FLOATING_POINT" if np.issubdtype(dtype, np.floating) else "STANDARD"


# rasterio/GDAL has no bool or float16 band type; both cast losslessly to a type it has.
# source: .venv/lib/python3.11/site-packages/rasterio/dtypes.py::check_dtype / dtype_ranges
CAST_DTYPES: dict[str, str] = {"bool": "uint8", "float16": "float32"}


def _writable_array(array: NDArray[Any]) -> NDArray[Any]:
    """``(bands, ny, nx)`` array with a dtype GDAL can write; raises ``ValueError`` otherwise."""
    from rasterio.dtypes import check_dtype, dtype_ranges

    arr = np.asarray(array)
    if arr.ndim == 2:
        arr = arr[None, :, :]
    if arr.ndim != 3:
        msg = f"array must be 2-D or 3-D, got shape {arr.shape}"
        raise ValueError(msg)
    if min(arr.shape) == 0:
        msg = f"array must not be empty, got shape {arr.shape} (bands, ny, nx)"
        raise ValueError(msg)
    cast = CAST_DTYPES.get(arr.dtype.name)
    if cast is not None:
        arr = arr.astype(cast)
    if not check_dtype(arr.dtype):
        msg = (
            f"dtype {arr.dtype.name} cannot be written as a GeoTIFF band; "
            f"cast it first (GDAL types: {sorted(dtype_ranges)})"
        )
        raise ValueError(msg)
    return arr


def write_cog(
    array: NDArray[Any],
    transform: Any,
    crs: Any,
    path: Path | str,
    *,
    nodata: float | int | None = np.nan,
    overviews: bool = True,
    compress: str = "DEFLATE",
    blocksize: int = DEFAULT_BLOCKSIZE,
    predictor: str = "auto",
    level: int | None = None,
    overview_resampling: str = DEFAULT_OVERVIEW_RESAMPLING,
    tags: dict[str, str] | None = None,
    band_descriptions: list[str] | None = None,
) -> Path:
    """Write ``array`` (``(ny, nx)`` or ``(bands, ny, nx)``) as a COG and verify it.

    ``transform`` is an ``affine.Affine`` (or 6-tuple) mapping pixel → CRS coordinates,
    ``crs`` anything ``rasterio.crs.CRS.from_user_input`` accepts (e.g. ``"EPSG:4326"``).
    ``compress`` ∈ :data:`COMPRESSORS`; ``blocksize`` must be a multiple of 16. NaN ``nodata``
    is only valid for float arrays, any other ``nodata`` must fit the array dtype. ``bool`` and
    ``float16`` arrays are cast losslessly (uint8 / float32); every other dtype GDAL does not
    know, and an empty extent, raise ``ValueError`` here instead of a raw rasterio/GDAL error.
    Falls back to tiled GTiff + overviews when the GDAL build has no COG driver. Returns
    ``path`` after :func:`check_cog` passed.
    """
    import rasterio
    from affine import Affine
    from rasterio.crs import CRS

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    arr = _writable_array(array)
    compress = compress.upper()
    if compress not in COMPRESSORS:
        msg = f"compress must be one of {COMPRESSORS}, got {compress!r}"
        raise ValueError(msg)
    if blocksize % 16 != 0 or blocksize < MIN_BLOCKSIZE:
        msg = f"blocksize must be a multiple of 16 and >= {MIN_BLOCKSIZE}, got {blocksize}"
        raise ValueError(msg)
    nan_nodata = nodata is not None and isinstance(nodata, float) and np.isnan(nodata)
    if nan_nodata and not np.issubdtype(arr.dtype, np.floating):
        msg = f"NaN nodata is only valid for float arrays (dtype {arr.dtype})"
        raise ValueError(msg)
    if nodata is not None and not nan_nodata:
        # source: .venv/lib/python3.11/site-packages/rasterio/dtypes.py::in_dtype_range —
        # GDAL silently drops a nodata value the band dtype cannot hold (uint8 + 300 -> None)
        from rasterio.dtypes import in_dtype_range

        if not in_dtype_range(nodata, arr.dtype):
            msg = f"nodata {nodata!r} is outside the range of dtype {arr.dtype.name}"
            raise ValueError(msg)
    tr = transform if isinstance(transform, Affine) else Affine(*tuple(transform)[:6])
    crs_obj = CRS.from_user_input(crs)
    pred = _predictor(arr.dtype, compress, predictor)
    profile: dict[str, Any] = {
        "width": int(arr.shape[2]),
        "height": int(arr.shape[1]),
        "count": int(arr.shape[0]),
        "dtype": arr.dtype.name,
        "crs": crs_obj,
        "transform": tr,
        "nodata": nodata,
    }
    if cog_driver_available():
        opts: dict[str, Any] = {
            "driver": "COG",
            "COMPRESS": compress,
            "BLOCKSIZE": blocksize,
            "OVERVIEWS": "AUTO" if overviews else "NONE",
            "OVERVIEW_RESAMPLING": overview_resampling,
            "BIGTIFF": "IF_NEEDED",
        }
        if pred is not None:
            opts["PREDICTOR"] = pred
        if level is not None and compress in ("DEFLATE", "ZSTD"):
            opts["LEVEL"] = int(level)
        with rasterio.open(p, "w", **profile, **opts) as dst:
            dst.write(arr)
            _annotate(dst, tags, band_descriptions)
    else:  # pragma: no cover - depends on the GDAL build
        opts = {
            "driver": "GTiff",
            "tiled": True,
            "blockxsize": blocksize,
            "blockysize": blocksize,
            "compress": compress if compress != "NONE" else None,
            "BIGTIFF": "IF_NEEDED",
        }
        if pred is not None:
            opts["predictor"] = 3 if pred == "FLOATING_POINT" else 2
        with rasterio.open(
            p, "w", **profile, **{k: v for k, v in opts.items() if v is not None}
        ) as dst:
            dst.write(arr)
            _annotate(dst, tags, band_descriptions)
        if overviews:
            from rasterio.enums import Resampling

            factors = _overview_factors(arr.shape[1], arr.shape[2], blocksize)
            if factors:
                with rasterio.open(p, "r+") as dst:
                    dst.build_overviews(factors, Resampling[overview_resampling.lower()])
                    dst.update_tags(ns="rio_overview", resampling=overview_resampling.lower())
    check_cog(p, expect_overviews=overviews)
    return p


def _annotate(dst: Any, tags: dict[str, str] | None, band_descriptions: list[str] | None) -> None:
    if tags:
        dst.update_tags(**tags)
    if band_descriptions:
        for i, d in enumerate(band_descriptions, start=1):
            dst.set_band_description(i, d)


def _overview_factors(ny: int, nx: int, blocksize: int) -> list[int]:
    """Power-of-two factors down to (and including) the first level that fits in one block —
    the COG driver's AUTO behaviour observed on GDAL 3.10.3 (300x400 @128 -> [2, 4], @256 -> [2])."""
    factors: list[int] = []
    f = 2
    while max(ny, nx) / (f / 2) > blocksize:
        factors.append(f)
        f *= 2
    return factors


@dataclass(frozen=True)
class CogInfo:
    path: Path
    layout_cog: bool  # IMAGE_STRUCTURE.LAYOUT == 'COG'
    tiled: bool
    block_shape: tuple[int, int]
    overviews: list[int]
    compression: str | None
    nodata: float | None
    dtype: str
    shape: tuple[int, int]
    count: int
    crs: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "layout_cog": self.layout_cog,
            "tiled": self.tiled,
            "block_shape": list(self.block_shape),
            "overviews": list(self.overviews),
            "compression": self.compression,
            "nodata": None if self.nodata is None or np.isnan(self.nodata) else self.nodata,
            "dtype": self.dtype,
            "shape": list(self.shape),
            "count": self.count,
            "crs": self.crs,
        }


def read_cog_info(path: Path | str) -> CogInfo:
    import rasterio

    with rasterio.open(path) as src:
        tags = src.tags(ns="IMAGE_STRUCTURE")
        comp = src.compression
        return CogInfo(
            path=Path(path),
            layout_cog=tags.get("LAYOUT") == "COG",
            tiled=bool(src.profile.get("tiled", False)),
            block_shape=tuple(int(x) for x in src.block_shapes[0]),  # type: ignore[arg-type]
            overviews=[int(f) for f in src.overviews(1)],
            compression=None if comp is None else str(comp.value).lower(),
            nodata=None if src.nodata is None else float(src.nodata),
            dtype=str(src.dtypes[0]),
            shape=(int(src.height), int(src.width)),
            count=int(src.count),
            crs=None if src.crs is None else src.crs.to_string(),
        )


def check_cog(path: Path | str, expect_overviews: bool = True) -> CogInfo:
    """Read back and verify: tiled, and overviews present when the raster exceeds one block."""
    info = read_cog_info(path)
    if not info.tiled:
        msg = f"{path}: not tiled"
        raise CogError(msg)
    if expect_overviews and max(info.shape) > max(info.block_shape) and not info.overviews:
        msg = (
            f"{path}: raster {info.shape} exceeds one block {info.block_shape} but has no overviews"
        )
        raise CogError(msg)
    return info


# ============================================================================ TimeSeries exports
def timeseries_transform(ts: TimeSeries) -> tuple[Any, NDArray[np.bool_]]:
    """Affine transform of the regular pixel-centre lat/lon grid of ``ts`` (EPSG:4326).

    Returns ``(transform, flip_rows)``: when latitude increases with the row index the export
    flips rows so the file is north-up. Irregular grids raise ``ValueError`` (geocode first).
    """
    from affine import Affine

    lat = np.asarray(ts.lat, dtype=np.float64)
    lon = np.asarray(ts.lon, dtype=np.float64)
    lat1 = lat[:, 0] if lat.ndim == 2 else lat
    lon1 = lon[0, :] if lon.ndim == 2 else lon
    ny, nx = ts.shape
    if lat1.shape[0] != ny or lon1.shape[0] != nx:
        msg = f"lat/lon vectors {lat1.shape}/{lon1.shape} do not match grid {ts.shape}"
        raise ValueError(msg)
    dy = float(lat1[1] - lat1[0]) if ny > 1 else -1e-3
    dx = float(lon1[1] - lon1[0]) if nx > 1 else 1e-3
    if ny > 1 and not np.allclose(np.diff(lat1), dy, rtol=1e-3, atol=abs(dy) * 1e-3):
        msg = "latitude grid is not regular; export needs a geocoded (regular) grid"
        raise ValueError(msg)
    if nx > 1 and not np.allclose(np.diff(lon1), dx, rtol=1e-3, atol=abs(dx) * 1e-3):
        msg = "longitude grid is not regular; export needs a geocoded (regular) grid"
        raise ValueError(msg)
    if lat.ndim == 2 and not np.allclose(lat, lat1[:, None], atol=abs(dy) * 1e-3):
        msg = "2-D latitude varies along columns; export needs a north-up regular grid"
        raise ValueError(msg)
    flip = dy > 0
    top_lat = float(lat1[-1]) if flip else float(lat1[0])
    step_y = -abs(dy)
    # pixel centre -> upper-left corner of the first pixel
    tr = Affine(dx, 0.0, float(lon1[0]) - dx / 2.0, 0.0, step_y, top_lat - step_y / 2.0)
    return tr, np.asarray(flip, dtype=bool)


def _oriented(arr: NDArray[Any], flip: bool) -> NDArray[Any]:
    return np.ascontiguousarray(arr[::-1, ...]) if flip else np.ascontiguousarray(arr)


MIN_VELOCITY_SAMPLES = 2  # a slope needs two finite epochs (same rule as validate.metrics)


def fit_velocity(ts: TimeSeries) -> NDArray[np.float32]:
    """Per-pixel least-squares slope of displacement vs. time (m/yr) — a plain linear fit,
    used only when the engine did not export a velocity map (not an SBAS inversion).

    The fit is per pixel over that pixel's *finite* epochs only: a pixel with fewer than
    :data:`MIN_VELOCITY_SAMPLES` finite samples (masked / no-data) stays NaN instead of
    collapsing to 0 m/yr, which an exported COG would show as stable ground
    (same NaN convention as :func:`wintersar.validate.metrics.fit_velocity`).
    """
    years = np.asarray(ts.years(), dtype=np.float64)
    d = np.asarray(ts.displacement_m, dtype=np.float64)
    valid = np.isfinite(d)
    n = valid.sum(axis=0)
    yrs = np.broadcast_to(years[:, None, None], d.shape)
    with np.errstate(invalid="ignore", divide="ignore"):
        safe_n = np.where(n > 0, n, 1).astype(np.float64)
        t_mean = np.where(valid, yrs, 0.0).sum(axis=0) / safe_n
        d_mean = np.where(valid, d, 0.0).sum(axis=0) / safe_n
        tc = np.where(valid, yrs - t_mean[None], 0.0)
        dc = np.where(valid, d - d_mean[None], 0.0)
        denom = (tc * tc).sum(axis=0)
        slope = np.where(
            denom > 0.0, (tc * dc).sum(axis=0) / np.where(denom > 0.0, denom, 1.0), np.nan
        )
    slope = np.where(n >= MIN_VELOCITY_SAMPLES, slope, np.nan)
    return np.asarray(slope, dtype=np.float32)


def export_velocity_cog(
    ts: TimeSeries, path: Path | str, crs: str = "EPSG:4326", **kw: Any
) -> Path:
    """``velocity_m_per_yr`` (or a linear fit) as a float32 COG; unit tag ``m/year``."""
    tr, flip = timeseries_transform(ts)
    vel = ts.velocity_m_per_yr if ts.velocity_m_per_yr is not None else fit_velocity(ts)
    tags = {
        "UNIT": "m/year",
        "SIGN": "positive = towards satellite (LOS)",
        "START": ts.dates[0].isoformat(),
        "END": ts.dates[-1].isoformat(),
    }
    return write_cog(
        _oriented(np.asarray(vel, dtype=np.float32), bool(flip)),
        tr,
        crs,
        path,
        tags=tags,
        band_descriptions=["LOS velocity (m/year)"],
        **kw,
    )


def export_displacement_cog(
    ts: TimeSeries,
    path: Path | str,
    at: date | int | None = None,
    crs: str = "EPSG:4326",
    **kw: Any,
) -> Path:
    """Cumulative LOS displacement at one date (default: last) as a float32 COG."""
    tr, flip = timeseries_transform(ts)
    if at is None:
        i = ts.n_dates - 1
    elif isinstance(at, int):
        i = at
    else:
        i = ts.dates.index(at)
    arr = np.asarray(ts.displacement_m[i], dtype=np.float32)
    tags = {
        "UNIT": "m",
        "SIGN": "positive = towards satellite (LOS)",
        "DATE": ts.dates[i].isoformat(),
        "REF_DATE": str(ts.attrs.get("REF_DATE", ts.dates[0].isoformat())),
    }
    return write_cog(
        _oriented(arr, bool(flip)),
        tr,
        crs,
        path,
        tags=tags,
        band_descriptions=[f"cumulative LOS displacement {ts.dates[i].isoformat()} (m)"],
        **kw,
    )


def export_displacement_stack_cog(
    ts: TimeSeries, path: Path | str, crs: str = "EPSG:4326", **kw: Any
) -> Path:
    """All epochs as bands of one COG (band i = ``dates[i]``), for QGIS temporal display."""
    tr, flip = timeseries_transform(ts)
    arr = np.asarray(ts.displacement_m, dtype=np.float32)
    arr = np.ascontiguousarray(arr[:, ::-1, :]) if flip else arr
    return write_cog(
        arr,
        tr,
        crs,
        path,
        tags={"UNIT": "m", "DATES": ",".join(d.isoformat() for d in ts.dates)},
        band_descriptions=[d.isoformat() for d in ts.dates],
        **kw,
    )


def export_coherence_cog(
    ts: TimeSeries, path: Path | str, crs: str = "EPSG:4326", **kw: Any
) -> Path:
    if ts.coherence is None:
        msg = "TimeSeries has no coherence map"
        raise ValueError(msg)
    tr, flip = timeseries_transform(ts)
    return write_cog(
        _oriented(np.asarray(ts.coherence, dtype=np.float32), bool(flip)),
        tr,
        crs,
        path,
        tags={"UNIT": "1"},
        band_descriptions=["temporal/mean coherence"],
        **kw,
    )


def export_mask_cog(
    mask: NDArray[Any],
    ts: TimeSeries,
    path: Path | str,
    crs: str = "EPSG:4326",
    **kw: Any,
) -> Path:
    """Boolean/uint8 mask (``1`` = masked out) as a uint8 COG with nodata 255 and NEAREST
    overviews (categorical data must not be averaged)."""
    tr, flip = timeseries_transform(ts)
    m = np.asarray(mask)
    if m.shape != ts.shape:
        msg = f"mask shape {m.shape} != grid {ts.shape}"
        raise ValueError(msg)
    arr = np.asarray(m, dtype=np.uint8) if m.dtype != np.bool_ else m.astype(np.uint8)
    kw.setdefault("overview_resampling", MASK_OVERVIEW_RESAMPLING)
    kw.setdefault("nodata", MASK_NODATA)
    return write_cog(
        _oriented(arr, bool(flip)), tr, crs, path, band_descriptions=["mask (1 = masked)"], **kw
    )


def export_conncomp_cog(
    ts: TimeSeries, path: Path | str, crs: str = "EPSG:4326", **kw: Any
) -> Path:
    if ts.conncomp is None:
        msg = "TimeSeries has no connected-component map"
        raise ValueError(msg)
    tr, flip = timeseries_transform(ts)
    kw.setdefault("overview_resampling", MASK_OVERVIEW_RESAMPLING)
    kw.setdefault("nodata", MASK_NODATA)
    return write_cog(
        _oriented(np.asarray(ts.conncomp, dtype=np.uint8), bool(flip)),
        tr,
        crs,
        path,
        band_descriptions=["connected component"],
        **kw,
    )


def export_timeseries_cogs(
    ts: TimeSeries,
    out_dir: Path | str,
    *,
    mask: NDArray[Any] | None = None,
    stack: bool = False,
    crs: str = "EPSG:4326",
    **kw: Any,
) -> dict[str, Path]:
    """Everything the QGIS plugin loads: ``velocity.tif``, ``displacement_<last>.tif``,
    ``coherence.tif`` (when present), ``mask.tif`` (when given), optional ``displacement_stack.tif``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {
        "velocity": export_velocity_cog(ts, out / "velocity.tif", crs=crs, **kw),
        "displacement": export_displacement_cog(
            ts, out / f"displacement_{ts.dates[-1]:%Y%m%d}.tif", crs=crs, **kw
        ),
    }
    if ts.coherence is not None:
        written["coherence"] = export_coherence_cog(ts, out / "coherence.tif", crs=crs, **kw)
    if ts.conncomp is not None:
        written["conncomp"] = export_conncomp_cog(ts, out / "conncomp.tif", crs=crs, **kw)
    if mask is not None:
        written["mask"] = export_mask_cog(mask, ts, out / "mask.tif", crs=crs, **kw)
    if stack:
        written["displacement_stack"] = export_displacement_stack_cog(
            ts, out / "displacement_stack.tif", crs=crs, **kw
        )
    return written
