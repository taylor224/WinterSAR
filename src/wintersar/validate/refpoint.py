"""Reference-point recommendation and re-referencing (plan §5.6, R-09, ADR-0041).

Score per candidate pixel (plan §5.6)::

    s = w1·coh + w2·[conncomp == AOI majority] + w3·(1 - |h - h_AOI| / range)
        + w4·(1 - d / d_max) + w5·(1 - min(|v| / v_scale, 1))

Each component is in [0, 1]; weights are renormalised over the components that are actually
available (no DEM → no elevation term) so the score stays in [0, 1]. The default weights are
a **domain checkpoint** (rule 11.10, ADR-0041) and must not change before the researcher
confirms them.

MintPy's automatic choice is reproduced for comparison. Verified behaviour
(``select_max_coherence_yx``): the method is *named* ``maxCoherence`` but the code picks a
**random** pixel among those with average spatial coherence ≥ ``minCoherence`` (default
0.85); the ``argmax`` line is commented out.
# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/reference_point.py
#   def select_max_coherence_yx(coh_file, mask=None, min_coh=0.85):
#       coh_mask = coh >= min_coh ... y, x = random_select_reference_yx(coh_mask)
#       #y, x = np.unravel_index(np.argmax(coh), coh.shape)
# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/defaults/smallbaselineApp.cfg
#   mintpy.reference.minCoherence = auto  #[0.0-1.0], auto for 0.85
#   mintpy.reference.maskFile     = auto  #auto for maskConnComp.h5
#   reference_point_attribute(): REF_Y, REF_X (+ REF_LAT, REF_LON when geocoded)
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wintersar.io.timeseries import TimeSeries
from wintersar.validate.metrics import haversine_m

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]

# Domain checkpoint (ADR-0041): initial weights, sum = 1. Coherence and connectivity dominate
# because a reference inside a different connected component is unusable regardless of the
# other terms; elevation and velocity priors refine; distance is a weak tie-breaker.
DEFAULT_WEIGHTS: dict[str, float] = {
    "coherence": 0.35,
    "conncomp": 0.25,
    "elevation": 0.15,
    "distance": 0.10,
    "velocity": 0.15,
}
COMPONENTS: tuple[str, ...] = tuple(DEFAULT_WEIGHTS)
MINTPY_MIN_COHERENCE: float = 0.85  # smallbaselineApp.cfg default (source above)


@dataclass
class RefPointCandidate:
    row: int
    col: int
    lat: float
    lon: float
    score: float
    components: dict[str, float] = field(default_factory=dict)
    coherence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "col": self.col,
            "lat": self.lat,
            "lon": self.lon,
            "score": self.score,
            "coherence": self.coherence,
            "components": dict(self.components),
        }


def _mean_coherence(ts: TimeSeries, coherence: NDArray[Any] | None) -> FloatArray | None:
    coh = coherence if coherence is not None else ts.coherence
    if coh is None:
        return None
    arr = np.asarray(coh, dtype=np.float64)
    if arr.ndim == 3:
        arr = np.nanmean(arr, axis=0)
    if arr.shape != ts.shape:
        msg = f"coherence shape {arr.shape} != time series shape {ts.shape}"
        raise ValueError(msg)
    return np.asarray(arr, dtype=np.float64)


def aoi_majority_conncomp(conncomp: NDArray[Any], aoi_mask: BoolArray) -> int:
    """Most frequent non-zero connected-component label inside the AOI (0 if none)."""
    vals = np.asarray(conncomp)[aoi_mask]
    vals = vals[(vals > 0) & np.isfinite(vals.astype(np.float64))]
    if vals.size == 0:
        return 0
    labels, counts = np.unique(vals.astype(np.int64), return_counts=True)
    return int(labels[int(np.argmax(counts))])


def valid_mask(ts: TimeSeries, coherence: FloatArray | None, min_coherence: float) -> BoolArray:
    """Pixels with finite displacement on every date (and coherence ≥ ``min_coherence``)."""
    valid = np.all(np.isfinite(ts.displacement_m), axis=0)
    if coherence is not None:
        valid &= np.isfinite(coherence) & (coherence >= min_coherence)
    return np.asarray(valid, dtype=bool)


def score_components(
    ts: TimeSeries,
    aoi_mask: BoolArray | None = None,
    *,
    coherence: NDArray[Any] | None = None,
    conncomp: NDArray[Any] | None = None,
    dem: NDArray[Any] | None = None,
    velocity: NDArray[Any] | None = None,
    min_coherence: float = 0.0,
) -> tuple[dict[str, FloatArray], BoolArray, BoolArray]:
    """Per-pixel component maps in [0, 1] (``NaN`` where undefined), the valid mask and the AOI mask."""
    ny, nx = ts.shape
    coh = _mean_coherence(ts, coherence)
    valid = valid_mask(ts, coh, min_coherence)
    aoi = np.asarray(aoi_mask, dtype=bool) if aoi_mask is not None else valid.copy()
    if aoi.shape != ts.shape:
        msg = f"aoi_mask shape {aoi.shape} != time series shape {ts.shape}"
        raise ValueError(msg)
    if not aoi.any():
        aoi = valid.copy()
    comps: dict[str, FloatArray] = {}
    if coh is not None:
        comps["coherence"] = np.clip(coh, 0.0, 1.0)
    cc = conncomp if conncomp is not None else ts.conncomp
    if cc is not None:
        major = aoi_majority_conncomp(np.asarray(cc), aoi)
        if major > 0:
            comps["conncomp"] = (np.asarray(cc) == major).astype(np.float64)
            valid &= np.asarray(cc) > 0
    h = dem if dem is not None else ts.dem_m
    if h is not None:
        hh = np.asarray(h, dtype=np.float64)
        ref_h = float(np.nanmedian(hh[aoi])) if np.isfinite(hh[aoi]).any() else float("nan")
        dev = np.abs(hh - ref_h)
        scale = float(np.nanmax(dev[valid])) if np.isfinite(dev[valid]).any() else 0.0
        comps["elevation"] = (
            np.clip(1.0 - dev / scale, 0.0, 1.0) if scale > 0 else np.ones((ny, nx))
        )
    lat2, lon2 = ts.lat2d(), ts.lon2d()
    c_lat = float(np.nanmean(lat2[aoi]))
    c_lon = float(np.nanmean(lon2[aoi]))
    dist = haversine_m(lat2, lon2, c_lat, c_lon)
    d_max = float(np.nanmax(dist[valid])) if valid.any() else 0.0
    comps["distance"] = np.clip(1.0 - dist / d_max, 0.0, 1.0) if d_max > 0 else np.ones((ny, nx))
    v = velocity if velocity is not None else ts.velocity_m_per_yr
    if v is not None:
        av = np.abs(np.asarray(v, dtype=np.float64))
        finite = av[valid & np.isfinite(av)]
        v_scale = float(np.percentile(finite, 95)) if finite.size else 0.0
        if v_scale <= 0 and finite.size:
            v_scale = float(finite.max())
        comps["velocity"] = (
            np.clip(1.0 - av / v_scale, 0.0, 1.0) if v_scale > 0 else np.ones((ny, nx))
        )
    return comps, valid, aoi


def normalise_weights(
    weights: Mapping[str, float] | None, available: list[str]
) -> dict[str, float]:
    """Keep only ``available`` components and rescale so the weights sum to 1."""
    w = {k: float(v) for k, v in (weights or DEFAULT_WEIGHTS).items() if k in available and v > 0}
    unknown = set(weights or {}) - set(COMPONENTS)
    if unknown:
        msg = f"unknown refpoint weight(s): {sorted(unknown)}; known: {COMPONENTS}"
        raise ValueError(msg)
    total = sum(w.values())
    if total <= 0:
        msg = "no usable refpoint component (all weights zero or components missing)"
        raise ValueError(msg)
    return {k: v / total for k, v in w.items()}


def score_map(
    ts: TimeSeries,
    aoi_mask: BoolArray | None = None,
    weights: Mapping[str, float] | None = None,
    **kw: Any,
) -> tuple[FloatArray, dict[str, FloatArray], dict[str, float]]:
    """Weighted score map (``NaN`` outside valid pixels), component maps and used weights."""
    comps, valid, _ = score_components(ts, aoi_mask, **kw)
    w = normalise_weights(weights, list(comps))
    score = np.zeros(ts.shape, dtype=np.float64)
    for name, wt in w.items():
        score += wt * np.nan_to_num(comps[name], nan=0.0)
    score[~valid] = np.nan
    return score, comps, w


def recommend(
    ts: TimeSeries,
    aoi_mask: BoolArray | None = None,
    weights: Mapping[str, float] | None = None,
    top_k: int = 5,
    coherence: NDArray[Any] | None = None,
    conncomp: NDArray[Any] | None = None,
    dem: NDArray[Any] | None = None,
    velocity: NDArray[Any] | None = None,
    *,
    min_coherence: float = 0.0,
    min_separation_px: int = 5,
) -> list[RefPointCandidate]:
    """Top-``k`` reference-point candidates by the weighted score (ADR-0041).

    Candidates closer than ``min_separation_px`` (Chebyshev distance) to a better one are
    suppressed so the list spans distinct locations rather than one bright neighbourhood.
    """
    score, comps, _ = score_map(
        ts,
        aoi_mask,
        weights,
        coherence=coherence,
        conncomp=conncomp,
        dem=dem,
        velocity=velocity,
        min_coherence=min_coherence,
    )
    flat = np.where(np.isfinite(score), score, -np.inf).ravel()
    order = np.argsort(-flat, kind="stable")
    lat2, lon2 = ts.lat2d(), ts.lon2d()
    coh = comps.get("coherence")
    out: list[RefPointCandidate] = []
    taken: list[tuple[int, int]] = []
    _ny, nx = ts.shape
    for idx in order:
        if not np.isfinite(flat[idx]) or len(out) >= top_k:
            break
        r, c = divmod(int(idx), nx)
        if any(max(abs(r - tr), abs(c - tc)) < min_separation_px for tr, tc in taken):
            continue
        taken.append((r, c))
        out.append(
            RefPointCandidate(
                row=r,
                col=c,
                lat=float(lat2[r, c]),
                lon=float(lon2[r, c]),
                score=float(flat[idx]),
                components={k: float(v[r, c]) for k, v in comps.items()},
                coherence=None if coh is None else float(coh[r, c]),
            )
        )
    return out


def compare_with_mintpy_auto(
    ts: TimeSeries,
    coherence: NDArray[Any] | None = None,
    threshold: float = MINTPY_MIN_COHERENCE,
    mask: NDArray[Any] | None = None,
    candidates: list[RefPointCandidate] | None = None,
    top_k: int = 5,
    **kw: Any,
) -> dict[str, Any]:
    """Compare our ranking with MintPy's automatic rule (random pixel with coherence ≥ threshold).

    Because MintPy's pick is random, the comparison reports the *candidate set* (size,
    fraction of the scene), the deterministic ``argmax`` variant that MintPy keeps commented
    out, and whether our candidates fall inside MintPy's set.
    """
    coh = _mean_coherence(ts, coherence)
    if coh is None:
        msg = "compare_with_mintpy_auto needs a coherence map"
        raise ValueError(msg)
    cohm = coh.copy()
    if mask is not None:
        cohm[np.asarray(mask) == 0] = 0.0
    cohm[~np.isfinite(cohm)] = 0.0
    in_set = cohm >= threshold
    n_set = int(in_set.sum())
    ours = candidates if candidates is not None else recommend(ts, coherence=coh, top_k=top_k, **kw)
    lat2, lon2 = ts.lat2d(), ts.lon2d()
    argmax_flat = int(np.argmax(cohm))
    ar, ac = divmod(argmax_flat, ts.shape[1])
    rows = [
        {**c.to_dict(), "in_mintpy_set": bool(in_set[c.row, c.col]), "rank": i + 1}
        for i, c in enumerate(ours)
    ]
    return {
        "rule": "mintpy.reference_point.select_max_coherence_yx: random pixel with coherence >= minCoherence",
        "threshold": float(threshold),
        "n_candidates_mintpy": n_set,
        "fraction_of_scene": float(n_set / cohm.size) if cohm.size else 0.0,
        "argmax": {
            "row": int(ar),
            "col": int(ac),
            "lat": float(lat2[ar, ac]),
            "lon": float(lon2[ar, ac]),
            "coherence": float(cohm[ar, ac]),
        },
        "ours": rows,
        "top1_in_mintpy_set": bool(rows[0]["in_mintpy_set"]) if rows else False,
        "n_top_in_mintpy_set": int(sum(1 for r in rows if r["in_mintpy_set"])),
        "argmax_is_our_top1": bool(rows and rows[0]["row"] == ar and rows[0]["col"] == ac),
    }


def apply_reference(ts: TimeSeries, row: int, col: int) -> TimeSeries:
    """Re-reference the time series (and velocity) to pixel ``(row, col)``.

    The pixel's displacement series is subtracted from every pixel so the reference is zero
    on all dates (MintPy ``reference_point`` semantics; attributes ``REF_Y``/``REF_X``/
    ``REF_LAT``/``REF_LON`` are set like ``reference_point_attribute``).
    """
    ny, nx = ts.shape
    if not (0 <= row < ny and 0 <= col < nx):
        msg = f"reference pixel ({row}, {col}) outside shape {ts.shape}"
        raise IndexError(msg)
    ref_series = ts.displacement_m[:, row, col]
    if not np.all(np.isfinite(ref_series)):
        msg = f"reference pixel ({row}, {col}) has NaN displacement"
        raise ValueError(msg)
    disp = ts.displacement_m - ref_series[:, None, None]
    vel = None
    if ts.velocity_m_per_yr is not None:
        vel = ts.velocity_m_per_yr - ts.velocity_m_per_yr[row, col]
    lat = float(ts.lat2d()[row, col])
    lon = float(ts.lon2d()[row, col])
    attrs = {
        **ts.attrs,
        "REF_Y": str(row),
        "REF_X": str(col),
        "REF_LAT": str(lat),
        "REF_LON": str(lon),
    }
    return TimeSeries(
        dates=list(ts.dates),
        displacement_m=disp,
        lat=ts.lat,
        lon=ts.lon,
        incidence_deg=ts.incidence_deg,
        heading_deg=ts.heading_deg,
        coherence=ts.coherence,
        velocity_m_per_yr=vel,
        reference_latlon=(lat, lon),
        dem_m=ts.dem_m,
        conncomp=ts.conncomp,
        attrs=attrs,
    )


def aoi_mask_from_geojson(path: Path | str, ts: TimeSeries) -> BoolArray:
    """Rasterise a GeoJSON (Feature/FeatureCollection/Geometry, lon/lat) onto the grid.

    # source: .venv/lib/python3.11/site-packages/shapely/predicates.py::contains_xy(geom, x, y)
    # source: .venv/lib/python3.11/site-packages/shapely/geometry/geo.py::shape(context)
    """
    import shapely
    from shapely.geometry import shape

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    geoms: list[Any] = []
    if data.get("type") == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in data.get("features", []) if f.get("geometry")]
    elif data.get("type") == "Feature":
        geoms = [shape(data["geometry"])]
    else:
        geoms = [shape(data)]
    if not geoms:
        return np.zeros(ts.shape, dtype=bool)
    geom = shapely.union_all(geoms) if len(geoms) > 1 else geoms[0]
    lat2, lon2 = ts.lat2d(), ts.lon2d()
    inside = shapely.contains_xy(geom, lon2.ravel(), lat2.ravel())
    return np.asarray(inside, dtype=bool).reshape(ts.shape)


__all__ = [
    "COMPONENTS",
    "DEFAULT_WEIGHTS",
    "MINTPY_MIN_COHERENCE",
    "RefPointCandidate",
    "aoi_majority_conncomp",
    "aoi_mask_from_geojson",
    "apply_reference",
    "compare_with_mintpy_auto",
    "normalise_weights",
    "recommend",
    "score_components",
    "score_map",
    "valid_mask",
]
