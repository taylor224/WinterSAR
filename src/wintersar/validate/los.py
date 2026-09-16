"""ENU → LOS projection with the MintPy sign/heading convention (plan §5.6, R-10, ADR-0040).

Every formula below is copied (not re-derived) from MintPy ``utils0.py`` so that the
values produced here agree with MintPy ``asc_desc2horz_vert`` / ``view.py`` products.
MintPy is **not** imported (rule 11.2); the source was read and the lines are cited.

# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/utils/utils0.py
#   heading2azimuth_angle(head_angle, look_direction='right'):
#       az_angle = (head_angle - 90) * -1            # right-looking
#       az_angle -= np.round(az_angle / 360.) * 360.  # wrap to [-180, 180]
#   enu2los(v_e, v_n, v_u, inc_angle, head_angle=None, az_angle=None):
#       v_los = (  v_e * sin(inc) * sin(az) * -1
#                + v_n * sin(inc) * cos(az)
#                + v_u * cos(inc))
#       "v_los - displacement in LOS direction, motion toward satellite as positive"
#   get_unit_vector4component_of_interest(..., comp='enu2los'):
#       unit_vec = [-sin(inc) sin(az), sin(inc) cos(az), cos(inc)]

Angle definitions (MintPy docstrings, same file):

* ``head_angle`` — azimuth of the platform along-track direction, measured from north,
  **clockwise positive** (Sentinel-1 near-polar: ≈ -12° ascending, ≈ -168° ≡ 192° descending).
* ``az_angle`` — azimuth of the LOS vector *from the ground to the satellite*, measured from
  north, **anti-clockwise positive** (ISCE-2 ``los.rdr`` band 2 convention).
* ``inc_angle`` — incidence angle from vertical.

Derived signs (fixed by ``tests/unit/validate/test_los.py``; domain checkpoint, ADR-0040):

* pure uplift  → LOS = +cos(inc) for both ascending and descending (towards satellite);
* pure eastward motion → LOS < 0 on ascending (heading -12°, az 102°, sin(az) > 0) and
  LOS > 0 on descending (heading 192°, az -102°, sin(az) < 0).
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
LookDirection = Literal["right", "left"]

# Sentinel-1 mid-latitude defaults; identical to wintersar.select.geometry_masks (ADR-0017) and
# to the MintPy docstring examples (-12 ascending, -168 ≡ 192 descending). Site-specific values
# differ by 1-3° (docs/open-questions.md #13).
S1_HEADING_ASC_DEG: float = -12.0
S1_HEADING_DESC_DEG: float = 192.0
# Sentinel-1 IW incidence range is ≈ 29-46°; 39° is the swath centre used when a time series
# carries no incidence layer (synthetic / fake-engine data only).
S1_INCIDENCE_MID_DEG: float = 39.0


def wrap_deg(angle_deg: ArrayLike) -> FloatArray:
    """Wrap degrees into [-180, 180] exactly like MintPy (``a -= round(a/360)*360``)."""
    a = np.asarray(angle_deg, dtype=np.float64)
    return np.asarray(a - np.round(a / 360.0) * 360.0, dtype=np.float64)


def heading_to_azimuth(
    heading_deg: ArrayLike, look_direction: LookDirection = "right"
) -> FloatArray:
    """Platform heading (clockwise from north) → LOS azimuth ``az_angle`` (anti-clockwise from
    north, ground → satellite). MintPy ``heading2azimuth_angle``."""
    h = np.asarray(heading_deg, dtype=np.float64)
    az = (h - 90.0) * -1.0 if look_direction == "right" else (h + 90.0) * -1.0
    return wrap_deg(az)


def azimuth_to_heading(az_deg: ArrayLike, look_direction: LookDirection = "right") -> FloatArray:
    """Inverse of :func:`heading_to_azimuth` (MintPy ``azimuth2heading_angle``)."""
    a = np.asarray(az_deg, dtype=np.float64)
    h = (a - 90.0) * -1.0 if look_direction == "right" else (a + 90.0) * -1.0
    return wrap_deg(h)


def los_unit_vector(
    incidence_deg: ArrayLike, heading_deg: ArrayLike, look_direction: LookDirection = "right"
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Unit vector ``(e, n, u)`` such that ``los = e*v_e + n*v_n + u*v_u`` (positive = towards
    the satellite). MintPy ``get_unit_vector4component_of_interest(comp='enu2los')``."""
    inc = np.deg2rad(np.asarray(incidence_deg, dtype=np.float64))
    az = np.deg2rad(heading_to_azimuth(heading_deg, look_direction))
    e = np.asarray(-np.sin(inc) * np.sin(az), dtype=np.float64)
    n = np.asarray(np.sin(inc) * np.cos(az), dtype=np.float64)
    u = np.asarray(np.cos(inc) + 0.0 * az, dtype=np.float64)
    return e, n, u


def enu_to_los(
    e: ArrayLike,
    n: ArrayLike,
    u: ArrayLike,
    incidence_deg: ArrayLike,
    heading_deg: ArrayLike,
    look_direction: LookDirection = "right",
) -> FloatArray:
    """Project ENU displacement (metres, east/north/up positive) onto the radar line of sight.

    Returns LOS displacement with **positive = towards the satellite** (range decrease), the
    convention of MintPy ``enu2los`` and of :class:`wintersar.io.timeseries.TimeSeries`.
    """
    ue, un, uu = los_unit_vector(incidence_deg, heading_deg, look_direction)
    v = (
        np.asarray(e, dtype=np.float64) * ue
        + np.asarray(n, dtype=np.float64) * un
        + np.asarray(u, dtype=np.float64) * uu
    )
    return np.asarray(v, dtype=np.float64)


def los_to_vertical(los: ArrayLike, incidence_deg: ArrayLike) -> FloatArray:
    """LOS → vertical assuming **purely vertical** motion: ``up = los / cos(inc)``.

    Positive = uplift. Horizontal motion is ignored (MintPy ``comp='u2los'`` inverse); use
    ascending + descending decomposition when horizontal motion matters.
    """
    inc = np.deg2rad(np.asarray(incidence_deg, dtype=np.float64))
    return np.asarray(np.asarray(los, dtype=np.float64) / np.cos(inc), dtype=np.float64)


def vertical_to_los(up: ArrayLike, incidence_deg: ArrayLike) -> FloatArray:
    """Vertical → LOS for purely vertical motion: ``los = up * cos(inc)`` (MintPy ``u2los``)."""
    inc = np.deg2rad(np.asarray(incidence_deg, dtype=np.float64))
    return np.asarray(np.asarray(up, dtype=np.float64) * np.cos(inc), dtype=np.float64)


def default_heading(flight_direction: str | None) -> float:
    """Mid-latitude Sentinel-1 heading for ``ASCENDING``/``asc`` or ``DESCENDING``/``desc``."""
    if flight_direction and flight_direction.lower().startswith("asc"):
        return S1_HEADING_ASC_DEG
    return S1_HEADING_DESC_DEG


__all__ = [
    "S1_HEADING_ASC_DEG",
    "S1_HEADING_DESC_DEG",
    "S1_INCIDENCE_MID_DEG",
    "azimuth_to_heading",
    "default_heading",
    "enu_to_los",
    "heading_to_azimuth",
    "los_to_vertical",
    "los_unit_vector",
    "vertical_to_los",
    "wrap_deg",
]
