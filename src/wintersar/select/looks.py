"""Automatic multilook selection (R-03, SEL-09, plan §5.1.4, ADR-0015).

Sentinel-1 IW SLC pixels are strongly asymmetric: the nominal *pixel spacing* is
2.3 m (slant range) x 14.1 m (azimuth) with 1x1 looks
(# source: https://sentiwiki.copernicus.eu/web/s1-products, "Level-1 SLC Product
Characteristics", Table 6). This is by design, not a defect ("the image looks squeezed
along azimuth" is *always* the case before multilooking). Ground-range spacing is
``slant / sin(theta)`` (~ 3.7 m at theta = 39°), so roughly 4:1 range:azimuth looks give a
square-ish pixel.

:func:`compute_looks` picks integer ``(rg_looks, az_looks)`` so that the multilooked
pixel is as close as possible to a target size while the aspect ratio stays bounded.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from wintersar.io.schemas import BurstRecord

# Nominal IW SLC pixel spacing used only as a documented fallback when the metadata is
# missing. # source: https://sentiwiki.copernicus.eu/web/s1-products (Table 6: IW 2.3 x 14.1 m)
IW_NOMINAL_RANGE_SPACING_M = 2.3
IW_NOMINAL_AZIMUTH_SPACING_M = 14.1
# Mid-swath incidence used as fallback; IW swath spans roughly 30-46 deg
# (# source: https://sentiwiki.copernicus.eu/web/s1-products, IW beam incidence range).
IW_NOMINAL_INCIDENCE_DEG = 39.0

DEFAULT_TARGET_PIXEL_M = 40.0
DEFAULT_MAX_ASPECT = 1.2


@dataclass(frozen=True)
class LooksResult:
    """Chosen multilook factors and the resulting pixel geometry.

    ``aspect_ratio`` is ``max(pixel_rg_m, pixel_az_m) / min(...)`` (always >= 1) so that
    "<= 1.2" reads the same regardless of which axis is longer.
    """

    rg_looks: int
    az_looks: int
    ground_range_spacing_m: float
    azimuth_spacing_m: float
    pixel_rg_m: float
    pixel_az_m: float
    aspect_ratio: float
    target_m: float

    @property
    def pixel_m(self) -> float:
        """Equivalent square pixel side (geometric mean of the two spacings)."""
        return math.sqrt(self.pixel_rg_m * self.pixel_az_m)

    @property
    def looks(self) -> tuple[int, int]:
        return (self.rg_looks, self.az_looks)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pixel_m"] = self.pixel_m
        return d


def ground_range_spacing(range_pixel_spacing_m: float, incidence_deg: float) -> float:
    """Slant-range pixel spacing projected to the ground: ``slant / sin(theta)``."""
    if not 0.0 < incidence_deg < 90.0:
        msg = f"incidence_deg must be in (0, 90), got {incidence_deg}"
        raise ValueError(msg)
    if range_pixel_spacing_m <= 0:
        msg = f"range_pixel_spacing_m must be > 0, got {range_pixel_spacing_m}"
        raise ValueError(msg)
    return range_pixel_spacing_m / math.sin(math.radians(incidence_deg))


def _pixel_size(rg_m: float, az_m: float) -> float:
    return math.sqrt(rg_m * az_m)


def _aspect(rg_m: float, az_m: float) -> float:
    return max(rg_m, az_m) / min(rg_m, az_m)


def compute_looks(
    range_pixel_spacing_m: float,
    azimuth_pixel_spacing_m: float,
    incidence_deg: float,
    target_pixel_m: float = DEFAULT_TARGET_PIXEL_M,
    max_aspect: float = DEFAULT_MAX_ASPECT,
) -> LooksResult:
    """Choose ``(rg_looks, az_looks) >= 1`` for a near-square pixel of ``target_pixel_m``.

    Algorithm (ADR-0015):

    1. ``ground_rg = range_pixel_spacing_m / sin(incidence_deg)``.
    2. Enumerate ``rg in 1..N``, ``az in 1..M`` (bounded by ~2x the target on each axis).
    3. Keep combinations whose aspect ratio ``max/min <= max_aspect``.
    4. Minimise ``|sqrt(rg_m * az_m) - target_pixel_m|`` (the equal-area square side);
       ties are broken by fewer total looks (higher resolution), then by smaller aspect.
    5. If no combination satisfies the aspect bound (only possible for unusual inputs),
       fall back to the combination with the smallest aspect ratio.

    Raises ``ValueError`` on non-positive spacings/target or ``max_aspect < 1``.
    """
    if azimuth_pixel_spacing_m <= 0:
        msg = f"azimuth_pixel_spacing_m must be > 0, got {azimuth_pixel_spacing_m}"
        raise ValueError(msg)
    if target_pixel_m <= 0:
        msg = f"target_pixel_m must be > 0, got {target_pixel_m}"
        raise ValueError(msg)
    if max_aspect < 1.0:
        msg = f"max_aspect must be >= 1, got {max_aspect}"
        raise ValueError(msg)
    ground_rg = ground_range_spacing(range_pixel_spacing_m, incidence_deg)
    az = azimuth_pixel_spacing_m

    max_rg = max(1, math.ceil(2.0 * target_pixel_m * max_aspect / ground_rg) + 1)
    max_az = max(1, math.ceil(2.0 * target_pixel_m * max_aspect / az) + 1)

    best: tuple[float, int, float, int, int] | None = None
    fallback: tuple[float, int, int, int] | None = None
    for n_az in range(1, max_az + 1):
        p_az = az * n_az
        for n_rg in range(1, max_rg + 1):
            p_rg = ground_rg * n_rg
            aspect = _aspect(p_rg, p_az)
            key_fb = (aspect, n_rg * n_az, n_rg, n_az)
            if fallback is None or key_fb < fallback:
                fallback = key_fb
            if aspect > max_aspect:
                continue
            key = (abs(_pixel_size(p_rg, p_az) - target_pixel_m), n_rg * n_az, aspect, n_rg, n_az)
            if best is None or key < best:
                best = key
    if best is not None:
        _, _, aspect, n_rg, n_az = best
    else:  # pragma: no cover - defensive; the enumeration always contains a feasible pair
        assert fallback is not None
        aspect, _, n_rg, n_az = fallback
    p_rg = ground_rg * n_rg
    p_az = az * n_az
    return LooksResult(
        rg_looks=n_rg,
        az_looks=n_az,
        ground_range_spacing_m=ground_rg,
        azimuth_spacing_m=az,
        pixel_rg_m=p_rg,
        pixel_az_m=p_az,
        aspect_ratio=_aspect(p_rg, p_az),
        target_m=target_pixel_m,
    )


def describe_looks(
    rg_looks: int,
    az_looks: int,
    range_pixel_spacing_m: float,
    azimuth_pixel_spacing_m: float,
    incidence_deg: float,
    target_pixel_m: float = DEFAULT_TARGET_PIXEL_M,
) -> LooksResult:
    """Pixel geometry for *user-specified* looks (``engine.looks: [rg, az]``)."""
    if rg_looks < 1 or az_looks < 1:
        msg = f"looks must be >= 1, got ({rg_looks}, {az_looks})"
        raise ValueError(msg)
    if azimuth_pixel_spacing_m <= 0:
        msg = f"azimuth_pixel_spacing_m must be > 0, got {azimuth_pixel_spacing_m}"
        raise ValueError(msg)
    ground_rg = ground_range_spacing(range_pixel_spacing_m, incidence_deg)
    p_rg = ground_rg * rg_looks
    p_az = azimuth_pixel_spacing_m * az_looks
    return LooksResult(
        rg_looks=rg_looks,
        az_looks=az_looks,
        ground_range_spacing_m=ground_rg,
        azimuth_spacing_m=azimuth_pixel_spacing_m,
        pixel_rg_m=p_rg,
        pixel_az_m=p_az,
        aspect_ratio=_aspect(p_rg, p_az),
        target_m=target_pixel_m,
    )


@dataclass(frozen=True)
class SpacingStats:
    """Pixel-spacing statistics of a stack (input for SEL-09)."""

    rg_median: float
    az_median: float
    rg_min: float
    rg_max: float
    az_min: float
    az_max: float
    incidence_deg: float
    n_with_metadata: int
    n_records: int
    nominal: bool

    @property
    def rg_deviation(self) -> float:
        return (self.rg_max - self.rg_min) / self.rg_median if self.rg_median else 0.0

    @property
    def az_deviation(self) -> float:
        return (self.az_max - self.az_min) / self.az_median if self.az_median else 0.0

    @property
    def deviation(self) -> float:
        return max(self.rg_deviation, self.az_deviation)


def spacing_stats(records: Iterable[BurstRecord]) -> SpacingStats:
    """Median/min/max pixel spacing and mean centre incidence over ``records``.

    Records without spacing metadata are ignored; when *no* record carries spacing
    metadata the IW nominal values are used and ``nominal=True`` is set so SEL-09 can
    say so.
    """
    rg: list[float] = []
    az: list[float] = []
    inc: list[float] = []
    n = 0
    for r in records:
        n += 1
        if r.range_pixel_spacing_m and r.azimuth_pixel_spacing_m:
            rg.append(float(r.range_pixel_spacing_m))
            az.append(float(r.azimuth_pixel_spacing_m))
        near, far = r.incidence_near_deg, r.incidence_far_deg
        if near is not None and far is not None:
            inc.append((float(near) + float(far)) / 2.0)
        elif near is not None:
            inc.append(float(near))
        elif far is not None:
            inc.append(float(far))
    incidence = statistics.fmean(inc) if inc else IW_NOMINAL_INCIDENCE_DEG
    if not rg:
        return SpacingStats(
            rg_median=IW_NOMINAL_RANGE_SPACING_M,
            az_median=IW_NOMINAL_AZIMUTH_SPACING_M,
            rg_min=IW_NOMINAL_RANGE_SPACING_M,
            rg_max=IW_NOMINAL_RANGE_SPACING_M,
            az_min=IW_NOMINAL_AZIMUTH_SPACING_M,
            az_max=IW_NOMINAL_AZIMUTH_SPACING_M,
            incidence_deg=incidence,
            n_with_metadata=0,
            n_records=n,
            nominal=True,
        )
    return SpacingStats(
        rg_median=statistics.median(rg),
        az_median=statistics.median(az),
        rg_min=min(rg),
        rg_max=max(rg),
        az_min=min(az),
        az_max=max(az),
        incidence_deg=incidence,
        n_with_metadata=len(rg),
        n_records=n,
        nominal=False,
    )


def looks_for_records(
    records: Iterable[BurstRecord],
    target_pixel_m: float = DEFAULT_TARGET_PIXEL_M,
    max_aspect: float = DEFAULT_MAX_ASPECT,
    looks: tuple[int, int] | None = None,
) -> LooksResult:
    """Convenience: :func:`compute_looks` on the median spacing of a stack.

    ``looks`` (explicit ``engine.looks``) bypasses the search and only reports geometry.
    """
    st = spacing_stats(records)
    if looks is not None:
        return describe_looks(
            looks[0], looks[1], st.rg_median, st.az_median, st.incidence_deg, target_pixel_m
        )
    return compute_looks(st.rg_median, st.az_median, st.incidence_deg, target_pixel_m, max_aspect)
