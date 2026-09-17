"""InSAR vs ground-truth comparison metrics (plan §5.6, R-10).

Per site: the InSAR value is the mean (or median) of the pixels within ``radius_m`` of the
site (haversine distance on the lat/lon grids), the ground truth is projected to LOS with
:func:`wintersar.validate.ground_truth.to_los`, the two series are aligned in time (nearest
InSAR date within ``max_gap_days``, or linear interpolation) and **both are referenced to the
first common date**. Metrics: RMSE, bias (InSAR - GT), Pearson correlation, least-squares
velocities and their difference. Overall RMSE/bias pool every aligned sample.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray

from wintersar.io.schemas import Finding, GroundTruthRecord
from wintersar.io.timeseries import TimeSeries
from wintersar.validate.ground_truth import LosSample, make_finding, to_los

FloatArray = NDArray[np.float64]
Aggregate = Literal["mean", "median"]
Align = Literal["nearest", "interp"]

EARTH_RADIUS_M: float = 6_371_008.8  # IUGG mean radius
DEFAULT_RADIUS_M: float = 100.0
DEFAULT_MAX_GAP_DAYS: int = 6  # half the Sentinel-1 12-day repeat


def haversine_m(lat1: ArrayLike, lon1: ArrayLike, lat2: ArrayLike, lon2: ArrayLike) -> FloatArray:
    """Great-circle distance in metres (inputs in degrees, broadcastable)."""
    p1 = np.deg2rad(np.asarray(lat1, dtype=np.float64))
    p2 = np.deg2rad(np.asarray(lat2, dtype=np.float64))
    dphi = p2 - p1
    dlam = np.deg2rad(np.asarray(lon2, dtype=np.float64) - np.asarray(lon1, dtype=np.float64))
    a = np.sin(dphi / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlam / 2.0) ** 2
    return np.asarray(
        2.0 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0))), dtype=np.float64
    )


def pixels_within_radius(
    ts: TimeSeries, lat: float, lon: float, radius_m: float
) -> tuple[NDArray[np.intp], NDArray[np.intp]]:
    """``(rows, cols)`` of pixels within ``radius_m`` of ``(lat, lon)``; at least the nearest
    pixel is returned when the radius is smaller than the pixel spacing."""
    d = haversine_m(ts.lat2d(), ts.lon2d(), lat, lon)
    inside = d <= radius_m
    if not inside.any():
        r, c = ts.nearest_pixel(lat, lon)
        inside[r, c] = True
    rows, cols = np.nonzero(inside)
    return rows, cols


def site_series(
    ts: TimeSeries,
    lat: float,
    lon: float,
    radius_m: float = DEFAULT_RADIUS_M,
    method: Aggregate = "mean",
) -> tuple[FloatArray, int, float]:
    """InSAR displacement series at a site: aggregate over pixels within the radius.

    Returns ``(series (n_dates,), n_pixels_used, distance_to_nearest_pixel_m)``. Pixels with
    NaN on a date are ignored on that date only.
    """
    rows, cols = pixels_within_radius(ts, lat, lon, radius_m)
    block = ts.displacement_m[:, rows, cols].astype(np.float64)
    finite = np.isfinite(block).any(axis=0)
    block = block[:, finite]
    n = int(block.shape[1])
    if n == 0:
        return np.full(ts.n_dates, np.nan), 0, float("nan")
    with np.errstate(invalid="ignore"):
        agg = np.nanmedian(block, axis=1) if method == "median" else np.nanmean(block, axis=1)
    r, c = ts.nearest_pixel(lat, lon)
    d_near = float(haversine_m(float(ts.lat2d()[r, c]), float(ts.lon2d()[r, c]), lat, lon))
    return np.asarray(agg, dtype=np.float64), n, d_near


def align_dates(
    ts_dates: Sequence[date],
    gt_dates: Sequence[date],
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
    mode: Align = "nearest",
) -> list[tuple[int, float]]:
    """For each GT date, ``(gt_index, insar_position)`` where ``insar_position`` is a fractional
    index into ``ts_dates`` (integer for ``nearest``); GT dates outside the tolerance /
    outside the InSAR span are dropped."""
    t = np.asarray([(d - ts_dates[0]).days for d in ts_dates], dtype=np.float64)
    out: list[tuple[int, float]] = []
    for gi, gd in enumerate(gt_dates):
        x = float((gd - ts_dates[0]).days)
        if mode == "nearest":
            j = int(np.argmin(np.abs(t - x)))
            if abs(t[j] - x) <= max_gap_days:
                out.append((gi, float(j)))
        elif t[0] <= x <= t[-1]:
            k = int(np.searchsorted(t, x, side="right") - 1)
            k = min(max(k, 0), len(t) - 2) if len(t) > 1 else 0
            frac = 0.0 if len(t) == 1 or t[k + 1] == t[k] else (x - t[k]) / (t[k + 1] - t[k])
            out.append((gi, k + frac))
    return out


def sample_series(series: FloatArray, positions: Sequence[float]) -> FloatArray:
    """Evaluate ``series`` at fractional indices (linear interpolation between neighbours)."""
    out = np.empty(len(positions), dtype=np.float64)
    for i, p in enumerate(positions):
        k = int(np.floor(p))
        frac = p - k
        if frac == 0.0 or k + 1 >= len(series):
            out[i] = series[k]
        else:
            out[i] = (1.0 - frac) * series[k] + frac * series[k + 1]
    return out


def fit_velocity(days: ArrayLike, values: ArrayLike) -> float:
    """Least-squares slope in metres per year (NaN when fewer than two finite samples)."""
    x = np.asarray(days, dtype=np.float64) / 365.25
    y = np.asarray(values, dtype=np.float64)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 2 or np.ptp(x[ok]) == 0:
        return float("nan")
    slope, _ = np.polyfit(x[ok], y[ok], 1)
    return float(slope)


def pearson(a: ArrayLike, b: ArrayLike) -> float:
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3 or np.std(x[ok]) == 0 or np.std(y[ok]) == 0:
        return float("nan")
    return float(np.corrcoef(x[ok], y[ok])[0, 1])


@dataclass
class SiteComparison:
    site_id: str
    method: str
    lat: float
    lon: float
    n: int
    rmse_m: float
    bias_m: float
    corr: float
    insar_velocity_m_per_yr: float
    gt_velocity_m_per_yr: float
    velocity_diff_m_per_yr: float
    n_pixels: int
    distance_to_pixel_m: float
    reference_date: date | None
    dates: list[date] = field(default_factory=list)
    insar_m: list[float] = field(default_factory=list)
    gt_los_m: list[float] = field(default_factory=list)
    incidence_deg: float | None = None
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "method": self.method,
            "lat": self.lat,
            "lon": self.lon,
            "n": self.n,
            "rmse_m": self.rmse_m,
            "bias_m": self.bias_m,
            "corr": self.corr,
            "insar_velocity_m_per_yr": self.insar_velocity_m_per_yr,
            "gt_velocity_m_per_yr": self.gt_velocity_m_per_yr,
            "velocity_diff_m_per_yr": self.velocity_diff_m_per_yr,
            "n_pixels": self.n_pixels,
            "distance_to_pixel_m": self.distance_to_pixel_m,
            "reference_date": self.reference_date.isoformat() if self.reference_date else None,
            "dates": [d.isoformat() for d in self.dates],
            "insar_m": list(self.insar_m),
            "gt_los_m": list(self.gt_los_m),
            "incidence_deg": self.incidence_deg,
            "findings": [f.model_dump(mode="json") for f in self.findings],
        }


@dataclass
class ComparisonResult:
    per_site: list[SiteComparison]
    rmse_m: float
    bias_m: float
    n_sites: int
    n_points: int
    radius_m: float
    method: str
    align: str
    max_gap_days: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def sites_compared(self) -> list[SiteComparison]:
        return [s for s in self.per_site if s.n > 0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rmse_m": self.rmse_m,
            "bias_m": self.bias_m,
            "n_sites": self.n_sites,
            "n_points": self.n_points,
            "radius_m": self.radius_m,
            "method": self.method,
            "align": self.align,
            "max_gap_days": self.max_gap_days,
            "per_site": [s.to_dict() for s in self.per_site],
            "findings": [f.model_dump(mode="json") for f in self.findings],
        }


def _finding(rule_id: str, severity: str, site_id: str, **params: Any) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message_key=f"validate.{rule_id}.cause",
        fix_key=f"validate.{rule_id}.fix",
        params={"site_id": site_id, **params},
        evidence={"site_id": site_id, **params},
        scope=site_id,
    )


def _empty_site(
    site_id: str, method: str, lat: float, lon: float, n_pixels: int, d_near: float, f: Finding
) -> SiteComparison:
    nan = float("nan")
    return SiteComparison(
        site_id=site_id,
        method=method,
        lat=lat,
        lon=lon,
        n=0,
        rmse_m=nan,
        bias_m=nan,
        corr=nan,
        insar_velocity_m_per_yr=nan,
        gt_velocity_m_per_yr=nan,
        velocity_diff_m_per_yr=nan,
        n_pixels=n_pixels,
        distance_to_pixel_m=d_near,
        reference_date=None,
        findings=[f],
    )


def compare(
    ts: TimeSeries,
    gt: Sequence[GroundTruthRecord],
    radius_m: float = DEFAULT_RADIUS_M,
    method: Aggregate = "mean",
    *,
    align: Align = "nearest",
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
    max_site_distance_m: float | None = None,
    heading_deg: float | None = None,
    incidence_deg: float | None = None,
) -> ComparisonResult:
    """Compare the InSAR time series with ground truth (levelling and/or GNSS) per site.

    ``max_site_distance_m`` (default ``2 * radius_m``) rejects sites whose nearest pixel is
    farther away than that, i.e. sites outside the grid (VAL-009).
    """
    samples = to_los(gt, ts, heading_deg=heading_deg, incidence_deg=incidence_deg)
    findings: list[Finding] = []
    n_gnss = sum(1 for s in samples if s.method == "gnss")
    if n_gnss and heading_deg is None and ts.attrs.get("synthetic_heading"):
        # The heading was filled from the configured orbit direction, not measured: the
        # mid-latitude default is 1-3 deg off (docs/open-questions.md #13) and the whole
        # east/north projection hangs on it.
        findings.append(
            make_finding("VAL-017", "WARN", heading_deg=ts.heading_deg, n_samples=n_gnss)
        )
    by_site: dict[str, list[LosSample]] = {}
    for s in samples:
        by_site.setdefault(s.site_id, []).append(s)
    limit = max_site_distance_m if max_site_distance_m is not None else 2.0 * radius_m
    per_site: list[SiteComparison] = []
    all_ins: list[float] = []
    all_gt: list[float] = []
    for site_id, ss in by_site.items():
        ss.sort(key=lambda s: s.date)
        lat = float(np.mean([s.lat for s in ss]))
        lon = float(np.mean([s.lon for s in ss]))
        series, n_pix, d_near = site_series(ts, lat, lon, radius_m, method)
        if d_near > limit or n_pix == 0:
            f = _finding("VAL-009", "WARN", site_id, distance_m=round(d_near, 1), limit_m=limit)
            findings.append(f)
            per_site.append(_empty_site(site_id, ss[0].method, lat, lon, n_pix, d_near, f))
            continue
        gt_dates = [s.date for s in ss]
        pairs = align_dates(ts.dates, gt_dates, max_gap_days, align)
        # Drop the samples the InSAR series cannot supply. The test must be on the *sampled*
        # value, not on ``series[floor(pos)]``: in 'interp' mode a NaN epoch next to the
        # bracketing one poisons the interpolation and would turn the site (and the pooled)
        # RMSE into NaN.
        sampled = sample_series(series, [pos for _, pos in pairs])
        keep = np.isfinite(sampled)
        gt_idx = [gi for (gi, _), ok in zip(pairs, keep.tolist(), strict=True) if ok]
        if len(gt_idx) < 2:
            f = _finding("VAL-010", "WARN", site_id, n=len(gt_idx), max_gap_days=max_gap_days)
            findings.append(f)
            per_site.append(_empty_site(site_id, ss[0].method, lat, lon, n_pix, d_near, f))
            continue
        ins = sampled[keep]
        gtv = np.asarray([ss[gi].los_m for gi in gt_idx], dtype=np.float64)
        dates = [ss[gi].date for gi in gt_idx]
        # reference both to the first common date
        ins = ins - ins[0]
        gtv = gtv - gtv[0]
        diff = ins - gtv
        days = np.asarray([(d - dates[0]).days for d in dates], dtype=np.float64)
        v_ins = fit_velocity(days, ins)
        v_gt = fit_velocity(days, gtv)
        per_site.append(
            SiteComparison(
                site_id=site_id,
                method=ss[0].method,
                lat=lat,
                lon=lon,
                n=len(gt_idx),
                rmse_m=float(np.sqrt(np.mean(diff**2))),
                bias_m=float(np.mean(diff)),
                corr=pearson(ins, gtv),
                insar_velocity_m_per_yr=v_ins,
                gt_velocity_m_per_yr=v_gt,
                velocity_diff_m_per_yr=v_ins - v_gt,
                n_pixels=n_pix,
                distance_to_pixel_m=d_near,
                reference_date=dates[0],
                dates=dates,
                insar_m=[float(x) for x in ins],
                gt_los_m=[float(x) for x in gtv],
                incidence_deg=ss[0].incidence_deg,
            )
        )
        all_ins.extend(float(x) for x in ins[1:])  # the reference sample is 0 by construction
        all_gt.extend(float(x) for x in gtv[1:])
    if all_ins:
        d = np.asarray(all_ins) - np.asarray(all_gt)
        rmse = float(np.sqrt(np.mean(d**2)))
        bias = float(np.mean(d))
    else:
        rmse = bias = float("nan")
    return ComparisonResult(
        per_site=per_site,
        rmse_m=rmse,
        bias_m=bias,
        n_sites=sum(1 for s in per_site if s.n > 0),
        n_points=len(all_ins),
        radius_m=radius_m,
        method=method,
        align=align,
        max_gap_days=max_gap_days,
        findings=findings,
    )


__all__ = [
    "DEFAULT_MAX_GAP_DAYS",
    "DEFAULT_RADIUS_M",
    "ComparisonResult",
    "SiteComparison",
    "align_dates",
    "compare",
    "fit_velocity",
    "haversine_m",
    "pearson",
    "pixels_within_radius",
    "sample_series",
    "site_series",
]
