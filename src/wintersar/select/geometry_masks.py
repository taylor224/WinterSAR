"""Layover / shadow / foreshortening masks from a DEM (plan §5.1.5, R-04, SEL-12).

Side-looking radar distorts slopes in the *range* direction. Which slopes are distorted
depends on where the sensor is, i.e. on the orbit direction (ascending vs descending), so
this module always evaluates a site for both directions and recommends the one with the
smaller distorted fraction over the AOI (plan §12.1 "레이오버·foreshortening·셰도우").

Conventions (ADR-0017)
----------------------
* Angles are degrees. Azimuths are measured **clockwise from north** (0 = N, 90 = E).
* ``heading_deg`` is the platform track azimuth (along-track direction), clockwise from
  north; e.g. ≈ -12° for a Sentinel-1 ascending pass and ≈ 192° (≡ -168°) for a descending
  pass at mid-latitudes.
  # source: https://raw.githubusercontent.com/insarlab/MintPy/main/src/mintpy/utils/utils0.py
  #   ("head_angle - the azimuth angle of the SAR platform's orbit (along-track direction)
  #   measured from the north, with clockwise as positive"; example values -12 / -168)
* Sentinel-1 is **right-looking**, so the look azimuth (sensor → ground, across track) is
  ``heading + 90`` and the azimuth from the ground *toward the sensor* is ``heading + 270``.
  # source: https://sentinel.esa.int/web/sentinel/missions/sentinel-1/instrument-payload
  #   ("right-looking active phased array antenna")
* ``aspect`` is the **downslope** direction (the direction a slope faces), clockwise from
  north — the GIS convention (gdaldem / ee.Terrain.aspect).
* ``incidence_deg`` is the angle between the line of sight and the local vertical on flat
  ground (IW: ≈ 29°-46°).
* DEM arrays are north-up: row index increases southward when ``dy_m > 0`` (GeoTIFF
  convention). Pass a negative ``dy_m`` for south-up arrays.

Local incidence angle
---------------------
With slope ``s``, aspect ``a`` (downslope), incidence ``θ`` and ``φ_sen`` the azimuth from the
ground toward the sensor, the upward surface normal is
``n = (sin s sin a, sin s cos a, cos s)`` (ENU) and the unit vector toward the sensor is
``u = (sin θ sin φ_sen, sin θ cos φ_sen, cos θ)``. Hence

    cos θ_loc = n · u = cos s · cos θ + sin s · sin θ · cos(φ_sen - a)

This is the classical local incidence angle of Ulander (1996, IEEE TGRS 34(5):1115-1122,
doi:10.1109/36.536527) and is exactly what ISCE2 computes in ``topozero.f90``
(``costheta = (enu(1)*alpha + enu(2)*beta - enu(3)) / sqrt(1 + alpha² + beta²)``, the dot
product of the LOS with the surface normal ``(∂z/∂E, ∂z/∂N, -1)``).
# source: https://raw.githubusercontent.com/isce-framework/isce2/main/components/zerodop/topozero/src/topozero.f90
**Sign caveat**: written with the *look* azimuth ``φ_look = φ_sen - 180°`` the last term flips
sign (``- sin s sin θ cos(φ_look - a)``); always use the sensor azimuth with the ``+`` form.

Layover / shadow / foreshortening
---------------------------------
Decompose the slope into its component along the range direction (positive = facing the
sensor): ``alpha_r = atan(tan s · cos(φ_sen - a))``. Then (Kropatsch & Strobl 1990, IEEE TGRS
28(1):98-107, doi:10.1109/36.45752; Vollrath, Mullissa & Reiche 2020, Remote Sens. 12(11):1867,
doi:10.3390/rs12111867)

* **layover**: ``alpha_r > θ`` — the slope facing the radar is steeper than the incidence angle,
  so slant range *decreases* uphill and the range order reverses (summit imaged before foot);
* **shadow**: ``alpha_r < -(90° - θ)`` — the slope facing away is steeper than the grazing
  angle, no energy reaches it;
* **foreshortening index** ``F = clip(1 - sin θ_loc / sin θ, 0, 1)``: ground length ``L``
  maps to slant-range length ``L sin θ_loc`` (flat ground: ``L sin θ``), so ``F`` is the
  fractional compression relative to flat ground; ``F = 1`` in layover, ``0`` on flat or
  stretched (facing-away) slopes.
# source: https://raw.githubusercontent.com/ESA-PhiLab/radiometric-slope-correction/master/javascript/slope_correction_module.js
#   (alpha_rRad = atan(tan(alpha_s) * cos(phi_r)); valid = alpha_r < theta_i (layover) and
#    alpha_r > -(90° - theta_i) (shadow); phi_r = phi_i - phi_s with phi_i toward near range)

These are the *active* (per-pixel) criteria. Passive layover/shadow (areas hidden behind a
neighbouring slope) needs a ray-cast along range; ISCE2's ``shadowMask`` includes it — see
ADR-0019 for the comparison plan.

Backend (PERF-10, ADR-0095/0097)
--------------------------------
The per-pixel math (:func:`slope_aspect`, :func:`local_incidence`, :func:`range_slope`,
:func:`compute_geometry_masks`) is written against the ``xp`` array module and runs on CuPy
when the backend policy selects it (``gpu=`` argument, ``WINTERSAR_GPU``, stage context,
auto-detect) and on numpy otherwise; every public function returns numpy arrays. On the CPU
the operations are the same numpy calls in the same order as before the port, so the golden
file (``tests/regression/golden/geometry``) is unchanged.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import numpy.typing as npt

from wintersar.compute.xp import GpuRequest, asarray, resolve_backend, to_numpy
from wintersar.i18n import t
from wintersar.io.schemas import Finding
from wintersar.util.masking import mask_text

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

ASCENDING = "ASCENDING"
DESCENDING = "DESCENDING"
FLIGHT_DIRECTIONS: tuple[str, str] = (ASCENDING, DESCENDING)

# Typical Sentinel-1 track azimuths at mid-latitudes (ADR-0017). Derived from the 98.18°
# inclination (sun-synchronous, near-polar) plus Earth rotation; the ascending ground track
# runs NNW (≈ -12°) and the descending track SSW (≈ 192° ≡ -168°). Override per site from
# product metadata when available — asf_search burst metadata carries no heading field
# (verified: no 'heading' key in asf_search 14.0.0).
# source: https://sentiwiki.copernicus.eu/web/s1-mission  (inclination 98.18°, 693 km)
# source: https://raw.githubusercontent.com/insarlab/MintPy/main/src/mintpy/utils/utils0.py
#   (near-polar example: head_angle = -12 ascending, -168 descending)
S1_HEADING_ASC_DEG: float = -12.0
S1_HEADING_DESC_DEG: float = 192.0

# ISCE2 topozero shadow/layover mask encoding, reused for our ls_map raster (ADR-0019).
# source: https://raw.githubusercontent.com/isce-framework/isce2/main/components/zerodop/topozero/src/topozero.f90
#   (mask(pixel) = 1 layover; omask(pixel) + 2 shadow; combined by addition → 3)
LS_NONE = 0
LS_LAYOVER = 1
LS_SHADOW = 2
LS_BOTH = 3
LS_NODATA = 255

BAND_NAMES: tuple[str, str, str, str] = (
    "layover",
    "shadow",
    "foreshortening",
    "local_incidence_deg",
)

_TAG_PREFIX = "WINTERSAR_"


@dataclass
class GeometryMaskResult:
    """Masks and per-pixel geometry for one orbit direction.

    ``layover``/``shadow`` are boolean, ``foreshortening`` is in [0, 1] (1 = fully
    compressed, i.e. layover), ``local_incidence_deg`` is in [0, 180] (> 90 means the surface
    faces away from the radar). NaN where the DEM is NaN.
    """

    layover: BoolArray
    shadow: BoolArray
    foreshortening: FloatArray
    local_incidence_deg: FloatArray
    flight_direction: str
    heading_deg: float
    incidence_deg: float | FloatArray
    stats: dict[str, float] = field(default_factory=dict)
    transform: Any | None = None
    crs: Any | None = None

    @property
    def shape(self) -> tuple[int, ...]:
        return self.layover.shape

    @property
    def distorted_fraction(self) -> float:
        return float(self.stats.get("layover_fraction", 0.0)) + float(
            self.stats.get("shadow_fraction", 0.0)
        )

    def ls_map(self) -> npt.NDArray[np.uint8]:
        """ISCE2-style byte mask: 0 none, 1 layover, 2 shadow, 3 both, 255 nodata."""
        out = np.zeros(self.layover.shape, dtype=np.uint8)
        out[self.layover] |= LS_LAYOVER
        out[self.shadow] |= LS_SHADOW
        out[~np.isfinite(self.local_incidence_deg)] = LS_NODATA
        return out


# ---------------------------------------------------------------------------- angles


def look_azimuth(heading_deg: float, right_looking: bool = True) -> float:
    """Azimuth of the look direction (sensor → ground, across track), clockwise from north.

    Right-looking (Sentinel-1): ``heading + 90``; left-looking: ``heading - 90``. Result in
    [0, 360).
    """
    offset = 90.0 if right_looking else -90.0
    return float((heading_deg + offset) % 360.0)


def sensor_azimuth(heading_deg: float, right_looking: bool = True) -> float:
    """Azimuth from a ground point *toward* the sensor: ``look_azimuth + 180`` in [0, 360)."""
    return float((look_azimuth(heading_deg, right_looking) + 180.0) % 360.0)


def _check_dem(z: Any, dx_m: float, dy_m: float) -> None:
    if z.ndim != 2:
        raise ValueError(t("select_geometry.error.dem_ndim", ndim=z.ndim))
    if dx_m == 0 or dy_m == 0 or not (math.isfinite(dx_m) and math.isfinite(dy_m)):
        raise ValueError(t("select_geometry.error.invalid_spacing", dx_m=dx_m, dy_m=dy_m))
    if z.shape[0] < 2 or z.shape[1] < 2:
        raise ValueError(t("select_geometry.error.dem_too_small", shape=tuple(z.shape)))


def _slope_aspect_xp(z: Any, dx_m: float, dy_m: float, xp: ModuleType) -> tuple[Any, Any]:
    """:func:`slope_aspect` on an ``xp`` array (already float64, validated)."""
    # source: https://docs.cupy.dev/en/stable/reference/generated/cupy.gradient.html
    #   (central differences inside, one-sided at the boundaries; ``axis`` keyword)
    dz_dcol = xp.gradient(z, axis=1) / abs(dx_m)
    if dx_m < 0:
        dz_dcol = -dz_dcol
    dz_drow = xp.gradient(z, axis=0)
    # rows increase southward for dy_m > 0 → d/dnorth = -d/drow
    dz_dnorth = -dz_drow / dy_m
    slope = xp.degrees(xp.arctan(xp.hypot(dz_dcol, dz_dnorth)))
    # downslope vector = -(dz/dE, dz/dN); azimuth clockwise from north = atan2(E, N)
    aspect = xp.degrees(xp.arctan2(-dz_dcol, -dz_dnorth)) % 360.0
    aspect = xp.where(slope == 0.0, 0.0, aspect)
    # a nodata pixel must not inherit a finite gradient from its two finite neighbours
    invalid = ~xp.isfinite(z) | ~xp.isfinite(slope)
    slope = xp.where(invalid, np.nan, slope)
    aspect = xp.where(invalid, np.nan, aspect)
    return slope, aspect


def slope_aspect(
    dem: npt.ArrayLike, dx_m: float, dy_m: float, *, gpu: GpuRequest = None
) -> tuple[FloatArray, FloatArray]:
    """Slope (deg, 0-90) and aspect (deg, downslope direction clockwise from north).

    ``dx_m``/``dy_m`` are the pixel spacings in metres. ``dy_m > 0`` means north-up (row index
    increases southward); ``dy_m < 0`` means rows increase northward. Central differences
    (``np.gradient``) inside, one-sided at the edges. Flat pixels get aspect 0; NaN in the DEM
    propagates to the neighbouring pixels. ``gpu`` selects the backend (module docstring).
    """
    xp = resolve_backend(gpu).xp
    z = asarray(dem, xp, dtype=np.float64)
    _check_dem(z, dx_m, dy_m)
    slope, aspect = _slope_aspect_xp(z, dx_m, dy_m, xp)
    return _f64(slope), _f64(aspect)


def _f64(a: Any) -> FloatArray:
    return np.asarray(to_numpy(a), dtype=np.float64)


def _local_incidence_xp(
    slope_deg: Any, aspect_deg: Any, sensor_azimuth_deg: float, incidence_deg: Any, xp: ModuleType
) -> Any:
    s = xp.radians(asarray(slope_deg, xp, dtype=np.float64))
    a = xp.radians(asarray(aspect_deg, xp, dtype=np.float64))
    th = xp.radians(asarray(incidence_deg, xp, dtype=np.float64))
    phi = math.radians(sensor_azimuth_deg)
    cos_loc = xp.cos(s) * xp.cos(th) + xp.sin(s) * xp.sin(th) * xp.cos(phi - a)
    return xp.degrees(xp.arccos(xp.clip(cos_loc, -1.0, 1.0)))


def local_incidence(
    slope_deg: npt.ArrayLike,
    aspect_deg: npt.ArrayLike,
    sensor_azimuth_deg: float,
    incidence_deg: float | npt.ArrayLike,
    *,
    gpu: GpuRequest = None,
) -> FloatArray:
    """Local incidence angle θ_loc (deg, 0-180) from the exact normal · LOS dot product.

    ``cos θ_loc = cos s cos θ + sin s sin θ cos(φ_sen - a)`` with ``φ_sen`` the azimuth from
    the ground toward the sensor (see module docstring for the derivation and references).
    """
    xp = resolve_backend(gpu).xp
    return _f64(_local_incidence_xp(slope_deg, aspect_deg, sensor_azimuth_deg, incidence_deg, xp))


def _range_slope_xp(
    slope_deg: Any, aspect_deg: Any, sensor_azimuth_deg: float, xp: ModuleType
) -> Any:
    s = xp.radians(asarray(slope_deg, xp, dtype=np.float64))
    a = xp.radians(asarray(aspect_deg, xp, dtype=np.float64))
    phi = math.radians(sensor_azimuth_deg)
    return xp.degrees(xp.arctan(xp.tan(s) * xp.cos(phi - a)))


def range_slope(
    slope_deg: npt.ArrayLike,
    aspect_deg: npt.ArrayLike,
    sensor_azimuth_deg: float,
    *,
    gpu: GpuRequest = None,
) -> FloatArray:
    """Slope component along the range direction (deg; positive = facing the sensor).

    ``alpha_r = atan(tan s · cos(φ_sen - a))``.
    """
    xp = resolve_backend(gpu).xp
    return _f64(_range_slope_xp(slope_deg, aspect_deg, sensor_azimuth_deg, xp))


# ---------------------------------------------------------------------------- masks


def _validate_direction(flight_direction: str) -> str:
    fd = flight_direction.upper()
    if fd not in FLIGHT_DIRECTIONS:
        raise ValueError(
            t(
                "select_geometry.error.invalid_direction",
                value=flight_direction,
                allowed=", ".join(FLIGHT_DIRECTIONS),
            )
        )
    return fd


def _validate_incidence(incidence_deg: float | npt.ArrayLike, shape: tuple[int, ...]) -> Any:
    if np.isscalar(incidence_deg):
        val = float(incidence_deg)  # type: ignore[arg-type]
        if not (0.0 < val < 90.0):
            raise ValueError(t("select_geometry.error.invalid_incidence", value=val))
        return val
    arr = np.asarray(incidence_deg, dtype=np.float64)
    if arr.shape != shape:
        raise ValueError(
            t("select_geometry.error.shape_mismatch", what="incidence_deg", a=arr.shape, b=shape)
        )
    finite = arr[np.isfinite(arr)]
    if finite.size and (finite.min() <= 0.0 or finite.max() >= 90.0):
        raise ValueError(
            t(
                "select_geometry.error.invalid_incidence",
                value=f"[{finite.min():.2f}, {finite.max():.2f}]",
            )
        )
    return arr


def compute_geometry_masks(
    dem: npt.ArrayLike,
    dx_m: float,
    dy_m: float,
    heading_deg: float,
    incidence_deg: float | npt.ArrayLike,
    flight_direction: str,
    aoi_mask: npt.ArrayLike | None = None,
    transform: Any | None = None,
    crs: Any | None = None,
    right_looking: bool = True,
    *,
    gpu: GpuRequest = None,
) -> GeometryMaskResult:
    """Layover, shadow, foreshortening and local incidence for one orbit direction.

    Parameters
    ----------
    dem
        2-D elevation array (metres), north-up unless ``dy_m < 0``. NaN = nodata.
    dx_m, dy_m
        Pixel spacing in metres (see :func:`slope_aspect`).
    heading_deg
        Platform heading, clockwise from north (ADR-0017).
    incidence_deg
        Scalar or per-pixel incidence angle (deg, 0 < θ < 90).
    flight_direction
        ``"ASCENDING"`` or ``"DESCENDING"`` — a label only; the geometry comes from
        ``heading_deg``.
    aoi_mask
        Optional boolean array (True inside the AOI) used for the statistics.
    transform, crs
        Optional georeferencing carried into :func:`write_mask_geotiff`.
    gpu
        Backend request (``None`` = ``WINTERSAR_GPU`` / stage context / auto-detect). The
        whole per-pixel chain runs on one backend; the result holds numpy arrays.
    """
    fd = _validate_direction(flight_direction)
    xp = resolve_backend(gpu).xp
    z_host = np.asarray(dem, dtype=np.float64)
    z = asarray(z_host, xp)
    _check_dem(z, dx_m, dy_m)
    slope, aspect = _slope_aspect_xp(z, dx_m, dy_m, xp)
    theta = _validate_incidence(incidence_deg, z_host.shape)
    phi_sen = sensor_azimuth(heading_deg, right_looking)

    theta_loc = _local_incidence_xp(slope, aspect, phi_sen, theta, xp)
    alpha_r = _range_slope_xp(slope, aspect, phi_sen, xp)
    valid = xp.isfinite(theta_loc) & xp.isfinite(alpha_r)

    theta_arr = xp.broadcast_to(asarray(theta, xp, dtype=np.float64), z.shape)
    layover = valid & (alpha_r > theta_arr)
    shadow = valid & (alpha_r < theta_arr - 90.0)

    with np.errstate(invalid="ignore"):
        sin_loc = xp.sin(xp.radians(xp.clip(theta_loc, 0.0, 90.0)))
        fore = 1.0 - sin_loc / xp.sin(xp.radians(theta_arr))
    fore = xp.clip(fore, 0.0, 1.0)
    fore = xp.where(layover, 1.0, fore)
    fore = xp.where(valid, fore, np.nan)

    layover_np = np.asarray(to_numpy(layover), dtype=bool)
    shadow_np = np.asarray(to_numpy(shadow), dtype=bool)
    fore_np = _f64(fore)
    theta_loc_np = _f64(theta_loc)
    valid_np = np.asarray(to_numpy(valid), dtype=bool)

    if aoi_mask is None:
        aoi = valid_np
    else:
        aoi_arr = np.asarray(aoi_mask, dtype=bool)
        if aoi_arr.shape != z_host.shape:
            raise ValueError(
                t(
                    "select_geometry.error.shape_mismatch",
                    what="aoi_mask",
                    a=aoi_arr.shape,
                    b=z_host.shape,
                )
            )
        aoi = aoi_arr & valid_np

    stats = _stats(layover_np, shadow_np, fore_np, aoi)
    return GeometryMaskResult(
        layover=layover_np,
        shadow=shadow_np,
        foreshortening=fore_np,
        local_incidence_deg=theta_loc_np,
        flight_direction=fd,
        heading_deg=float(heading_deg),
        incidence_deg=theta,
        stats=stats,
        transform=transform,
        crs=crs,
    )


def _stats(
    layover: BoolArray, shadow: BoolArray, fore: FloatArray, aoi: BoolArray
) -> dict[str, float]:
    n = int(aoi.sum())
    if n == 0:
        return {
            "layover_fraction": float("nan"),
            "shadow_fraction": float("nan"),
            "foreshortening_mean": float("nan"),
            "aoi_pixels": 0.0,
        }
    return {
        "layover_fraction": float(layover[aoi].sum() / n),
        "shadow_fraction": float(shadow[aoi].sum() / n),
        "foreshortening_mean": float(np.nanmean(fore[aoi])),
        "aoi_pixels": float(n),
    }


def masks_for_both_directions(
    dem: npt.ArrayLike,
    dx: float,
    dy: float,
    incidence_deg: float | npt.ArrayLike,
    heading_asc: float = S1_HEADING_ASC_DEG,
    heading_desc: float = S1_HEADING_DESC_DEG,
    aoi_mask: npt.ArrayLike | None = None,
    transform: Any | None = None,
    crs: Any | None = None,
    *,
    gpu: GpuRequest = None,
) -> dict[str, GeometryMaskResult]:
    """Masks for ascending and descending passes (defaults: typical Sentinel-1 headings)."""
    return {
        ASCENDING: compute_geometry_masks(
            dem, dx, dy, heading_asc, incidence_deg, ASCENDING, aoi_mask, transform, crs, gpu=gpu
        ),
        DESCENDING: compute_geometry_masks(
            dem, dx, dy, heading_desc, incidence_deg, DESCENDING, aoi_mask, transform, crs, gpu=gpu
        ),
    }


def recommend_direction(results: Mapping[str, GeometryMaskResult]) -> str:
    """Direction with the smallest layover + shadow fraction over the AOI.

    Ties are broken by the mean foreshortening index, then alphabetically (ASCENDING first)
    so the answer is deterministic.
    """
    if not results:
        raise ValueError(t("select_geometry.error.empty_results"))

    def key(item: tuple[str, GeometryMaskResult]) -> tuple[float, float, str]:
        name, r = item
        frac = r.distorted_fraction
        fore = float(r.stats.get("foreshortening_mean", 0.0))
        return (
            frac if math.isfinite(frac) else float("inf"),
            fore if math.isfinite(fore) else float("inf"),
            name,
        )

    return min(results.items(), key=key)[0]


def sel12_finding(
    result: GeometryMaskResult,
    threshold: float = 0.10,
    mask_path: Path | str | None = None,
    recommended: str | None = None,
) -> Finding:
    """SEL-12 finding: WARN when layover + shadow exceed ``threshold`` of the AOI, else INFO."""
    frac = result.distorted_fraction
    lay = float(result.stats.get("layover_fraction", 0.0))
    sha = float(result.stats.get("shadow_fraction", 0.0))
    inc = result.incidence_deg
    inc_val = float(np.nanmean(inc)) if isinstance(inc, np.ndarray) else float(inc)
    params: dict[str, Any] = {
        "layover_pct": 100.0 * lay,
        "shadow_pct": 100.0 * sha,
        "threshold_pct": 100.0 * threshold,
        "flight_direction": result.flight_direction,
        "heading_deg": result.heading_deg,
        "incidence_deg": inc_val,
        "mask_path": mask_text(str(mask_path)) if mask_path is not None else "-",
        "recommended": recommended or result.flight_direction,
    }
    evidence: dict[str, Any] = {**result.stats, "threshold": threshold}
    if math.isfinite(frac) and frac > threshold:
        return Finding(
            rule_id="SEL-12",
            severity="WARN",
            message_key="select_geometry.SEL-12.cause",
            fix_key="select_geometry.SEL-12.fix",
            params=params,
            evidence=evidence,
            refs=["docs/concepts/geometry.md", "ADR-0017"],
            scope=result.flight_direction,
        )
    return Finding(
        rule_id="SEL-12",
        severity="INFO",
        message_key="select_geometry.SEL-12.ok",
        params=params,
        evidence=evidence,
        refs=["docs/concepts/geometry.md"],
        scope=result.flight_direction,
    )


# ---------------------------------------------------------------------------- raster IO


def pixel_spacing_m(transform: Any, crs: Any, shape: tuple[int, int]) -> tuple[float, float]:
    """Metric pixel spacing ``(dx_m, dy_m)`` for an affine transform.

    Geographic CRS: geodesic length of one pixel step at the raster centre (pyproj WGS84).
    Projected CRS: transform scale x the CRS linear-unit factor. ``dy_m`` is positive for
    north-up rasters (``transform.e < 0``) and negative otherwise. Rotated rasters are
    rejected.
    """
    if transform.b != 0 or transform.d != 0:
        raise ValueError(t("select_geometry.error.rotated_raster"))
    north_up = transform.e < 0
    sign = 1.0 if north_up else -1.0
    if crs is None or crs.is_geographic:
        from pyproj import Geod  # source: pyproj 3.7.2 Geod.inv(lons1, lats1, lons2, lats2)

        nrows, ncols = shape
        cx, cy = transform * (ncols / 2.0, nrows / 2.0)
        geod = Geod(ellps="WGS84")
        _, _, dx = geod.inv(cx, cy, cx + abs(transform.a), cy)
        _, _, dy = geod.inv(cx, cy, cx, cy + abs(transform.e))
        return float(dx), sign * float(dy)
    factor = 1.0
    units = getattr(crs, "linear_units", None)
    if units and units.lower() not in ("metre", "meter", "m"):
        lu = getattr(crs, "linear_units_factor", None)
        if lu and len(lu) == 2 and lu[1]:
            factor = float(lu[1])
        else:
            raise ValueError(t("select_geometry.error.unknown_units", units=units))
    return abs(float(transform.a)) * factor, sign * abs(float(transform.e)) * factor


def write_mask_geotiff(
    result: GeometryMaskResult,
    path: Path | str,
    transform: Any | None = None,
    crs: Any | None = None,
    write_ls_map: bool = True,
) -> Path:
    """Write a 4-band float32 GeoTIFF (layover, shadow, foreshortening, local incidence).

    NaN marks nodata. Heading, incidence, direction and stats go into dataset tags so that
    :func:`read_mask_geotiff` can round-trip the result. With ``write_ls_map`` a second
    single-band uint8 ``<stem>_ls_map.tif`` is written with the ISCE2 encoding (0/1/2/3, 255).
    """
    import rasterio  # source: .venv/lib/python3.11/site-packages/rasterio/__init__.py (open)

    out = Path(path)
    tr = transform if transform is not None else result.transform
    cr = crs if crs is not None else result.crs
    if tr is None:
        raise ValueError(t("select_geometry.error.no_transform"))
    out.parent.mkdir(parents=True, exist_ok=True)
    h, w = result.layover.shape
    bands = np.stack(
        [
            result.layover.astype(np.float32),
            result.shadow.astype(np.float32),
            result.foreshortening.astype(np.float32),
            result.local_incidence_deg.astype(np.float32),
        ]
    )
    invalid = ~np.isfinite(result.local_incidence_deg)
    bands[:, invalid] = np.nan
    profile: dict[str, Any] = {
        "driver": "GTiff",
        "height": h,
        "width": w,
        "count": 4,
        "dtype": "float32",
        "crs": cr,
        "transform": tr,
        "nodata": float("nan"),
        "compress": "deflate",
        "tiled": False,
    }
    inc = result.incidence_deg
    tags = {
        f"{_TAG_PREFIX}HEADING_DEG": repr(float(result.heading_deg)),
        f"{_TAG_PREFIX}FLIGHT_DIRECTION": result.flight_direction,
        f"{_TAG_PREFIX}INCIDENCE_DEG": "map" if isinstance(inc, np.ndarray) else repr(float(inc)),
        f"{_TAG_PREFIX}STATS": json.dumps(result.stats, sort_keys=True),
        f"{_TAG_PREFIX}BANDS": ",".join(BAND_NAMES),
    }
    with rasterio.open(out, "w", **profile) as ds:
        ds.write(bands)
        for i, name in enumerate(BAND_NAMES, start=1):
            ds.set_band_description(i, name)
        ds.update_tags(**tags)
    if write_ls_map:
        ls_path = out.with_name(out.stem + "_ls_map.tif")
        ls_profile = {
            **profile,
            "count": 1,
            "dtype": "uint8",
            "nodata": LS_NODATA,
        }
        with rasterio.open(ls_path, "w", **ls_profile) as ds:
            ds.write(result.ls_map(), 1)
            ds.set_band_description(1, "ls_map")
            ds.update_tags(
                **tags,
                **{f"{_TAG_PREFIX}LS_ENCODING": "0=none,1=layover,2=shadow,3=both,255=nodata"},
            )
    return out


def read_mask_geotiff(path: Path | str) -> GeometryMaskResult:
    """Inverse of :func:`write_mask_geotiff` (incidence map is not stored; scalar only)."""
    import rasterio

    with rasterio.open(Path(path)) as ds:
        data = ds.read().astype(np.float64)
        tags = ds.tags()
        transform = ds.transform
        crs = ds.crs
    if data.shape[0] < 4:
        raise ValueError(t("select_geometry.error.bad_mask_file", path=mask_text(str(path))))
    theta_loc = data[3]
    valid = np.isfinite(theta_loc)
    inc_tag = tags.get(f"{_TAG_PREFIX}INCIDENCE_DEG", "nan")
    incidence: float | FloatArray = float("nan") if inc_tag == "map" else float(inc_tag)
    stats_raw = tags.get(f"{_TAG_PREFIX}STATS")
    stats: dict[str, float] = (
        {k: float(v) for k, v in json.loads(stats_raw).items()} if stats_raw else {}
    )
    return GeometryMaskResult(
        layover=valid & (data[0] > 0.5),
        shadow=valid & (data[1] > 0.5),
        foreshortening=data[2],
        local_incidence_deg=theta_loc,
        flight_direction=tags.get(f"{_TAG_PREFIX}FLIGHT_DIRECTION", ""),
        heading_deg=float(tags.get(f"{_TAG_PREFIX}HEADING_DEG", "nan")),
        incidence_deg=incidence,
        stats=stats,
        transform=transform,
        crs=crs,
    )


def _geom_to_raster_crs(geom: Any, aoi_crs: str, raster_crs: Any) -> Any:
    """Reproject a shapely geometry from ``aoi_crs`` to the raster CRS (no-op when equal)."""
    if raster_crs is None:
        return geom
    from pyproj import CRS as PyprojCRS  # noqa: N811
    from pyproj import Transformer
    from shapely.ops import transform as shapely_transform

    # source: pyproj 3.7.2 Transformer.from_crs(crs_from, crs_to, always_xy=...)
    # source: shapely 2.1.2 shapely.ops.transform(func, geom)
    src = PyprojCRS.from_user_input(aoi_crs)
    dst = PyprojCRS.from_user_input(raster_crs.to_wkt())
    if src == dst:
        return geom
    tf = Transformer.from_crs(src, dst, always_xy=True)
    return shapely_transform(tf.transform, geom)


def compute_from_dem_file(
    dem_path: Path | str,
    aoi_wkt: str,
    heading_deg: float,
    incidence_deg: float,
    flight_direction: str,
    pad_pixels: int = 2,
    band: int = 1,
    right_looking: bool = True,
    aoi_crs: str = "EPSG:4326",
) -> GeometryMaskResult:
    """Read the AOI window of a DEM GeoTIFF and compute the masks for one direction.

    ``aoi_wkt`` is expressed in ``aoi_crs`` (default lon/lat WGS84) and is reprojected to the
    raster CRS when they differ. The window is the AOI bounding box padded by ``pad_pixels``
    (so that edge gradients are central differences), clipped to the raster. Metric spacing
    comes from :func:`pixel_spacing_m`. The AOI mask (pixel centres inside the polygon)
    drives the stats.
    """
    import rasterio
    from rasterio import features, windows
    from shapely import wkt as shapely_wkt
    from shapely.geometry import mapping

    # source: .venv/lib/python3.11/site-packages/rasterio/windows.py (from_bounds, intersection)
    # source: .venv/lib/python3.11/site-packages/rasterio/features.py (geometry_mask)
    geom = shapely_wkt.loads(aoi_wkt)
    p = Path(dem_path)
    with rasterio.open(p) as ds:
        geom = _geom_to_raster_crs(geom, aoi_crs, ds.crs)
        left, bottom, right, top = geom.bounds
        try:
            win = windows.from_bounds(left, bottom, right, top, transform=ds.transform)
            win = win.round_offsets().round_lengths()
            win = windows.Window(
                win.col_off - pad_pixels,
                win.row_off - pad_pixels,
                win.width + 2 * pad_pixels,
                win.height + 2 * pad_pixels,
            )
            win = windows.intersection(win, windows.Window(0, 0, ds.width, ds.height))
        except (windows.WindowError, ValueError) as exc:
            raise ValueError(
                t(
                    "select_geometry.error.aoi_outside_dem",
                    dem=mask_text(str(p)),
                    bounds=str(tuple(ds.bounds)),
                )
            ) from exc
        if win.width < 2 or win.height < 2:
            raise ValueError(
                t(
                    "select_geometry.error.aoi_outside_dem",
                    dem=mask_text(str(p)),
                    bounds=str(tuple(ds.bounds)),
                )
            )
        raw = ds.read(band, window=win, masked=True)
        dem = np.ma.filled(raw.astype(np.float64), np.nan)
        wtransform = ds.window_transform(win)
        crs = ds.crs
    dx_m, dy_m = pixel_spacing_m(wtransform, crs, dem.shape)
    aoi_mask = features.geometry_mask(
        [mapping(geom)], out_shape=dem.shape, transform=wtransform, invert=True
    )
    return compute_geometry_masks(
        dem,
        dx_m,
        dy_m,
        heading_deg,
        incidence_deg,
        flight_direction,
        aoi_mask=aoi_mask,
        transform=wtransform,
        crs=crs,
        right_looking=right_looking,
    )


__all__ = [
    "ASCENDING",
    "BAND_NAMES",
    "DESCENDING",
    "FLIGHT_DIRECTIONS",
    "LS_BOTH",
    "LS_LAYOVER",
    "LS_NODATA",
    "LS_NONE",
    "LS_SHADOW",
    "S1_HEADING_ASC_DEG",
    "S1_HEADING_DESC_DEG",
    "GeometryMaskResult",
    "compute_from_dem_file",
    "compute_geometry_masks",
    "local_incidence",
    "look_azimuth",
    "masks_for_both_directions",
    "pixel_spacing_m",
    "range_slope",
    "read_mask_geotiff",
    "recommend_direction",
    "sel12_finding",
    "sensor_azimuth",
    "slope_aspect",
    "write_mask_geotiff",
]
