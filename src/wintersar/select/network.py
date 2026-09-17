"""Stack grouping, interferogram network and reference-date recommendation
(R-01, plan §5.1.2, ADR-0016).

* :func:`group_candidates` turns search records into :class:`StackCandidate` objects,
  one per ``(relative_orbit, flight_direction, polarization)`` group. Bursts that
  intersect the AOI on *every* date form ``burst_ids``; both burst-level alternatives
  (drop incomplete dates vs. drop missing bursts) are evaluated and recorded in
  ``notes["alternatives"]``.
* :func:`build_network` creates ``sbas`` / ``sequential`` / ``single_reference`` pair
  lists.
* :func:`recommend_reference` picks the date that is temporally central *and* has the
  smallest total |Bperp| to the others.

Perpendicular baselines are **not** computed here (``wintersar.select.baseline``); the
functions accept them as ``perp_by_date`` / ``Pair.perp_baseline_m`` when available.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from wintersar.io.schemas import BurstRecord, Pair, StackCandidate

if TYPE_CHECKING:
    from wintersar.pipeline.config import DataCfg, SelectionCfg

NetworkMethod = Literal["sbas", "sequential", "single_reference"]
OrbitDirection = Literal["asc", "desc", "auto"]

_DIRECTION_OF: dict[str, str] = {"asc": "ASCENDING", "desc": "DESCENDING"}


# --------------------------------------------------------------------------- filters


def orbit_direction_filter(
    records: Iterable[BurstRecord], orbit_direction: OrbitDirection | str
) -> list[BurstRecord]:
    """Keep records of the requested direction (``auto`` keeps both)."""
    recs = list(records)
    if orbit_direction == "auto":
        return recs
    wanted = _DIRECTION_OF.get(str(orbit_direction))
    if wanted is None:
        msg = f"orbit_direction must be asc|desc|auto, got {orbit_direction!r}"
        raise ValueError(msg)
    return [r for r in recs if r.flight_direction == wanted]


def has_polarization(record: BurstRecord, pol: str) -> bool:
    """``True`` when ``record.polarization`` contains ``pol`` (handles ``VV+VH``)."""
    parts = {p.strip().upper() for p in record.polarization.replace("/", "+").split("+")}
    return pol.upper() in parts


def polarization_filter(records: Iterable[BurstRecord], pol: str) -> list[BurstRecord]:
    """Records carrying ``pol``; if none does, return everything (SEL-05 will report)."""
    recs = list(records)
    matched = [r for r in recs if has_polarization(r, pol)]
    return matched or recs


# --------------------------------------------------------------------------- geometry


def _load_geom(wkt: str) -> Any:
    from shapely import wkt as shapely_wkt  # source: shapely/wkt.py `def loads(data)`

    geom = shapely_wkt.loads(wkt)
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def coverage_fraction(footprints_wkt: Iterable[str], aoi_wkt: str) -> float:
    """Area of (union of footprints & AOI) / area(AOI), clipped to [0, 1].

    Geometries are in geographic degrees; the ratio of two areas in the same small
    region is adequate for a coverage fraction (no reprojection needed).
    """
    from shapely.ops import unary_union  # source: shapely/ops.py `unary_union`

    aoi = _load_geom(aoi_wkt)
    if aoi.is_empty or aoi.area <= 0:
        return 0.0
    geoms = [_load_geom(w) for w in footprints_wkt]
    geoms = [g for g in geoms if not g.is_empty]
    if not geoms:
        return 0.0
    union = unary_union(geoms)
    frac = union.intersection(aoi).area / aoi.area
    return float(min(1.0, max(0.0, frac)))


def aoi_centroid_lat(aoi_wkt: str) -> float | None:
    try:
        g = _load_geom(aoi_wkt)
    except Exception:
        return None
    if g.is_empty:
        return None
    return float(g.centroid.y)


# --------------------------------------------------------------------------- grouping


def _group_key(r: BurstRecord) -> tuple[int, str, str, str]:
    return (r.relative_orbit, r.flight_direction, r.polarization, r.mode)


def _index_by_date(records: Iterable[BurstRecord]) -> dict[date, dict[str, BurstRecord]]:
    """date -> {full_burst_id -> record} (duplicates of one burst on one date collapse)."""
    by_date: dict[date, dict[str, BurstRecord]] = defaultdict(dict)
    for r in records:
        by_date[r.acquisition_date].setdefault(r.full_burst_id, r)
    return by_date


def _stats_for(
    burst_ids: set[str],
    dates: list[date],
    footprint_of: Mapping[str, str],
    aoi_wkt: str,
) -> float:
    if not burst_ids or not dates:
        return 0.0
    return coverage_fraction((footprint_of[b] for b in sorted(burst_ids)), aoi_wkt)


def group_candidates(
    records: Sequence[BurstRecord],
    aoi_wkt: str,
    selection: SelectionCfg,
    data: DataCfg,
) -> list[StackCandidate]:
    """Group records into stack candidates (plan §5.1.2).

    Steps per ``(relative_orbit, flight_direction, polarization, mode)`` group:

    1. keep bursts whose footprint intersects the AOI,
    2. ``common`` = bursts present on every date, ``union`` = bursts on any date,
    3. alternative A ("drop_bursts"): all dates, ``common`` bursts;
       alternative B ("drop_dates"): all ``union`` bursts, only dates that have them all,
    4. the primary candidate is A when its coverage >= ``selection.min_coverage``,
       otherwise whichever alternative covers more (ties -> more dates); both are kept
       in ``notes["alternatives"]`` so the user can decide (ADR-0016),
    5. pairs from :func:`build_network` (no baselines yet) and a temporal reference date.

    Records are pre-filtered by ``data.orbit_direction``, ``data.relative_orbit`` and
    ``data.polarization`` (the latter falls back to all polarizations when nothing
    matches so that SEL-05 can explain the problem).
    """
    recs = orbit_direction_filter(records, data.orbit_direction)
    if data.relative_orbit != "auto":
        recs = [r for r in recs if r.relative_orbit == int(data.relative_orbit)]
    recs = polarization_filter(recs, data.polarization)

    aoi = _load_geom(aoi_wkt)
    groups: dict[tuple[int, str, str, str], list[BurstRecord]] = defaultdict(list)
    footprint_of: dict[str, str] = {}
    intersect_cache: dict[str, bool] = {}
    for r in recs:
        hit = intersect_cache.get(r.full_burst_id)
        if hit is None:
            try:
                hit = bool(_load_geom(r.footprint_wkt).intersects(aoi))
            except Exception:
                hit = False
            intersect_cache[r.full_burst_id] = hit
        if not hit:
            continue
        footprint_of.setdefault(r.full_burst_id, r.footprint_wkt)
        groups[_group_key(r)].append(r)

    candidates: list[StackCandidate] = []
    for key in sorted(groups):
        rel_orbit, direction, pol, mode = key
        grp = groups[key]
        by_date = _index_by_date(grp)
        dates = sorted(by_date)
        sets = [set(by_date[d]) for d in dates]
        common: set[str] = set.intersection(*sets) if sets else set()
        union: set[str] = set.union(*sets) if sets else set()
        missing_bursts = union - common
        dates_missing = [d for d in dates if not union <= set(by_date[d])]
        dates_complete = [d for d in dates if union <= set(by_date[d])]

        cov_a = _stats_for(common, dates, footprint_of, aoi_wkt)
        cov_b = _stats_for(union, dates_complete, footprint_of, aoi_wkt)
        alt_a: dict[str, Any] = {
            "name": "drop_bursts",
            "coverage_of_aoi": cov_a,
            "n_dates": len(dates),
            "n_bursts": len(common),
            "n_dates_dropped": 0,
            "n_bursts_dropped": len(missing_bursts),
            "dates_dropped": [],
            "bursts_dropped": sorted(missing_bursts),
        }
        alt_b: dict[str, Any] = {
            "name": "drop_dates",
            "coverage_of_aoi": cov_b,
            "n_dates": len(dates_complete),
            "n_bursts": len(union),
            "n_dates_dropped": len(dates_missing),
            "n_bursts_dropped": 0,
            "dates_dropped": [d.isoformat() for d in dates_missing],
            "bursts_dropped": [],
        }
        if cov_a >= selection.min_coverage or (cov_a, len(dates)) >= (cov_b, len(dates_complete)):
            chosen, use_dates, use_bursts = alt_a, dates, common
        else:
            chosen, use_dates, use_bursts = alt_b, dates_complete, union

        pairs = build_network(
            use_dates,
            selection.network,
            selection.max_temporal_baseline_days,
            selection.max_perp_baseline_m,
            None,
            selection.sequential_connections,
        )
        ref = recommend_reference(use_dates, None) if use_dates else None
        subswaths = sorted({by_date[d][b].subswath for d in use_dates for b in use_bursts})
        granules = sorted(
            by_date[d][b].granule_id for d in use_dates for b in use_bursts if b in by_date[d]
        )
        # {date_iso: {burst_id: granule_id}} — contract consumed by engines.hyp3.load_candidates
        granules_by_date = {
            d.isoformat(): {
                b: by_date[d][b].granule_id for b in sorted(use_bursts) if b in by_date[d]
            }
            for d in use_dates
        }
        product_type = grp[0].product_type
        candidates.append(
            StackCandidate(
                relative_orbit=rel_orbit,
                flight_direction=direction,
                polarization=pol,
                subswaths=subswaths,
                burst_ids=sorted(use_bursts),
                dates=list(use_dates),
                reference_date=ref,
                coverage_of_aoi=float(chosen["coverage_of_aoi"]),
                pairs=pairs,
                n_dates_dropped=int(chosen["n_dates_dropped"]),
                n_bursts_dropped=int(chosen["n_bursts_dropped"]),
                product_type=product_type,
                notes={
                    "mode": mode,
                    "selected_alternative": chosen["name"],
                    "alternatives": [alt_a, alt_b],
                    "all_dates": [d.isoformat() for d in dates],
                    "all_bursts": sorted(union),
                    "granule_ids": granules,
                    "granules": granules_by_date,
                    "network": network_summary(use_dates, pairs, selection.network),
                },
            )
        )
    return candidates


# --------------------------------------------------------------------------- network


def _pair(a: date, b: date, perp_by_date: Mapping[date, float] | None) -> Pair:
    ref, sec = (a, b) if a < b else (b, a)
    perp: float | None = None
    if perp_by_date is not None and ref in perp_by_date and sec in perp_by_date:
        perp = float(perp_by_date[sec]) - float(perp_by_date[ref])
    return Pair(
        reference=ref,
        secondary=sec,
        temporal_baseline_days=(sec - ref).days,
        perp_baseline_m=perp,
    )


def build_network(
    dates: Sequence[date],
    method: NetworkMethod | str,
    max_temporal_days: int,
    max_perp_m: float,
    perp_by_date: Mapping[date, float] | None = None,
    connections: int = 3,
) -> list[Pair]:
    """Interferogram network for ``dates`` (sorted, unique).

    * ``sbas``: every pair with ``dt <= max_temporal_days`` and, when both baselines
      are known, ``|dBperp| <= max_perp_m`` (unknown baselines are *kept*; SEL-06 reports).
    * ``sequential``: each date paired with the next ``connections`` dates (no
      thresholds; SEL-06/07 flag violations).
    * ``single_reference``: every date paired with :func:`recommend_reference`.

    ``Pair.reference`` is always the earlier date (schema constraint); the network's
    master date is ``StackCandidate.reference_date``.
    """
    ds = sorted(set(dates))
    if len(ds) < 2:
        return []
    if method == "sbas":
        out: list[Pair] = []
        for i, a in enumerate(ds):
            for b in ds[i + 1 :]:
                if (b - a).days > max_temporal_days:
                    break
                p = _pair(a, b, perp_by_date)
                if p.perp_baseline_m is not None and abs(p.perp_baseline_m) > max_perp_m:
                    continue
                out.append(p)
        return out
    if method == "sequential":
        n = max(1, int(connections))
        return [
            _pair(ds[i], ds[j], perp_by_date)
            for i in range(len(ds))
            for j in range(i + 1, min(len(ds), i + n + 1))
        ]
    if method == "single_reference":
        ref = recommend_reference(ds, perp_by_date)
        return [_pair(ref, d, perp_by_date) for d in ds if d != ref]
    msg = f"unknown network method {method!r} (sbas|sequential|single_reference)"
    raise ValueError(msg)


def recommend_reference(dates: Sequence[date], perp_by_date: Mapping[date, float] | None) -> date:
    """Date that is temporally central *and* minimises sum|Bperp| to the other dates.

    Score = |t - t_mid| / (span/2) + sum|B_d - B_k| / max_d sum|B_d - B_k| (each term in
    [0, 1]); when no baselines are known only the temporal term is used. Ties -> earlier
    date. Raises ``ValueError`` on an empty list.
    """
    ds = sorted(set(dates))
    if not ds:
        msg = "recommend_reference needs at least one date"
        raise ValueError(msg)
    if len(ds) == 1:
        return ds[0]
    t0 = ds[0].toordinal()
    span = ds[-1].toordinal() - t0
    mid = t0 + span / 2.0
    t_score = {d: abs(d.toordinal() - mid) / (span / 2.0) if span else 0.0 for d in ds}
    b_score: dict[date, float] = dict.fromkeys(ds, 0.0)
    if perp_by_date:
        known = [d for d in ds if d in perp_by_date]
        if len(known) >= 2:
            sums = {
                d: sum(abs(float(perp_by_date[d]) - float(perp_by_date[k])) for k in known)
                for d in known
            }
            worst = max(sums.values()) or 1.0
            b_score = {d: (sums[d] / worst if d in sums else 1.0) for d in ds}
    return min(ds, key=lambda d: (t_score[d] + b_score[d], d))


def perp_by_date_from_pairs(pairs: Iterable[Pair], origin: date | None = None) -> dict[date, float]:
    """Integrate pairwise Bperp into per-date values relative to ``origin`` (default: the
    earliest date). Dates not connected to the origin through known baselines are omitted.
    """
    adj: dict[date, list[tuple[date, float]]] = defaultdict(list)
    for p in pairs:
        if p.perp_baseline_m is None:
            continue
        adj[p.reference].append((p.secondary, float(p.perp_baseline_m)))
        adj[p.secondary].append((p.reference, -float(p.perp_baseline_m)))
    if not adj:
        return {}
    start = origin if origin is not None and origin in adj else min(adj)
    out: dict[date, float] = {start: 0.0}
    queue = [start]
    while queue:
        d = queue.pop(0)
        for nxt, delta in adj[d]:
            if nxt not in out:
                out[nxt] = out[d] + delta
                queue.append(nxt)
    return out


def network_components(dates: Sequence[date], pairs: Iterable[Pair]) -> int:
    """Number of connected components of the pair graph over ``dates``."""
    return len(connected_components(dates, pairs))


def connected_components(dates: Sequence[date], pairs: Iterable[Pair]) -> list[list[date]]:
    """Connected components of the pair graph over ``dates``, each sorted, largest first.

    A stack whose network falls apart into several components cannot be inverted as one
    time series, so the component count and the dates outside the largest component are
    reported by :func:`network_summary` and by the precheck candidate table.
    """
    parent: dict[date, date] = {d: d for d in dates}

    def find(x: date) -> date:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        if p.reference in parent and p.secondary in parent:
            parent[find(p.reference)] = find(p.secondary)
    groups: dict[date, list[date]] = defaultdict(list)
    for d in sorted(parent):
        groups[find(d)].append(d)
    return sorted(groups.values(), key=lambda g: (-len(g), g[0]))


def network_summary(dates: Sequence[date], pairs: Sequence[Pair], method: str) -> dict[str, Any]:
    perps = [abs(p.perp_baseline_m) for p in pairs if p.perp_baseline_m is not None]
    temps = [p.temporal_baseline_days for p in pairs]
    comps = connected_components(dates, pairs)
    # Dates outside the largest component: the ones to drop (or re-connect) so that the
    # stack inverts as a single time series.
    disconnected = [d for g in comps[1:] for d in g]
    return {
        "method": method,
        "n_pairs": len(pairs),
        "n_components": len(comps),
        "n_dates_disconnected": len(disconnected),
        "dates_disconnected": [d.isoformat() for d in sorted(disconnected)],
        "n_with_baseline": len(perps),
        "temporal_days": _min_med_max(temps),
        "perp_m": _min_med_max(perps),
    }


def _min_med_max(values: Sequence[float] | Sequence[int]) -> dict[str, float] | None:
    if not values:
        return None
    s = sorted(float(v) for v in values)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    return {"min": s[0], "median": med, "max": s[-1]}


def with_baselines(
    candidate: StackCandidate, pairs: Sequence[Pair], selection: SelectionCfg
) -> StackCandidate:
    """Return a copy of ``candidate`` with baseline-bearing ``pairs``: for ``sbas`` the
    perpendicular threshold is re-applied, the reference date is re-recommended with the
    integrated per-date baselines and the network summary is refreshed.

    Re-applying the threshold is the prescribed SBAS remedy (ADR-0016) but it can leave
    the network in several components, so the removed pairs (``n_pairs_dropped_perp`` /
    ``pairs_dropped_perp``) and the connectivity of what remains (``n_components`` /
    ``dates_disconnected``) are recorded in ``notes["network"]`` and shown in the
    precheck candidate table. ``candidate.dates`` is never changed here.
    """
    new_pairs = list(pairs)
    dropped: list[Pair] = []
    if selection.network == "sbas":
        kept: list[Pair] = []
        for p in new_pairs:
            over = (
                p.perp_baseline_m is not None
                and abs(p.perp_baseline_m) > selection.max_perp_baseline_m
            )
            (dropped if over else kept).append(p)
        new_pairs = kept
    perp = perp_by_date_from_pairs(pairs)
    ref = recommend_reference(candidate.dates, perp) if candidate.dates else None
    notes = dict(candidate.notes)
    summary = network_summary(candidate.dates, new_pairs, selection.network)
    # Pairs the SBAS perpendicular threshold removed: without this the removal is
    # invisible (the pairs are simply gone from ``candidate.pairs``).
    summary["n_pairs_dropped_perp"] = len(dropped)
    summary["pairs_dropped_perp"] = [p.key for p in dropped]
    notes["network"] = summary
    notes["perp_by_date"] = {d.isoformat(): v for d, v in sorted(perp.items())}
    return candidate.model_copy(update={"pairs": new_pairs, "reference_date": ref, "notes": notes})
