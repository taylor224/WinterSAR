"""Precheck rules SEL-01 ... SEL-13 (R-01, R-02, R-03, PERF-13; plan §5.1.3, ADR-0014).

Each rule is a function ``sel_xx(ctx: RuleContext) -> list[Finding]``. Findings carry
only i18n keys (``select.SEL-xx.fail|warn|info`` + ``select.SEL-xx.fix``) and the
values used for the verdict (``evidence``), so reports render "cause -> fix" in any
language. The registry :data:`RULES` keeps the rule order; :func:`rule_table` exposes
id / severities / description keys for the documentation.

Thresholds come from ``Config.selection`` (``max_perp_baseline_m``,
``max_temporal_baseline_days``, ``min_coverage``, ``pixel_spacing_tolerance``,
``max_layover_shadow_fraction``, ``budget_credits``) and ``Config.engine``
(``target_pixel_m``, ``looks``). Their defaults are a *domain review checkpoint*
(rule 11.10): do not change them before the researcher confirms ADR-0014.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from wintersar.io.schemas import BurstRecord, Finding, Resources, Severity, StackCandidate
from wintersar.pipeline.config import Config
from wintersar.select.looks import looks_for_records, spacing_stats

PoeorbHook = Callable[[date], bool | None]
RuleFn = Callable[["RuleContext"], list[Finding]]

# ASF full burst id = "<relative orbit>_<burst id>_<subswath>", e.g. "017_034465_IW2"
# source: https://docs.asf.alaska.edu/api/keywords/ (fullBurstID example value)
_FULL_BURST_ID_RE = re.compile(r"^(?P<track>\d{3})_(?P<burst>\d{6})_(?P<swath>[A-Z]{1,2}\d)$")

CO_POLARIZATIONS = ("VV", "HH")

# Season model for SEL-07 (northern hemisphere; shifted by six months south of the
# equator). Leaf-on = May-Sep, snow / leaf-off = Nov-Mar, Apr and Oct transitional.
# Heuristic - researcher confirmation required (ADR-0014).
SNOW_MONTHS_NH = frozenset({11, 12, 1, 2, 3})
LEAF_ON_MONTHS_NH = frozenset({5, 6, 7, 8, 9})

_MAX_LISTED_DATES = 6


@dataclass
class RuleContext:
    """Everything a rule may look at for one candidate."""

    candidate: StackCandidate
    records: list[BurstRecord]
    cfg: Config
    all_candidates: list[StackCandidate] = field(default_factory=list)
    geometry: Mapping[str, Any] | None = None
    resources: Resources | None = None
    poeorb_available: PoeorbHook | None = None
    aoi_lat: float | None = None

    @property
    def stack_id(self) -> str:
        return self.candidate.stack_id

    @property
    def selection(self) -> Any:
        return self.cfg.selection


def _finding(
    rule_id: str,
    severity: Severity,
    kind: str,
    scope: str | None,
    params: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message_key=f"select.{rule_id}.{kind}",
        params=params or {},
        evidence=evidence or {},
        fix_key=f"select.{rule_id}.fix",
        scope=scope,
    )


def _fmt_dates(dates: Sequence[date]) -> str:
    shown = [d.isoformat() for d in dates[:_MAX_LISTED_DATES]]
    if len(dates) > _MAX_LISTED_DATES:
        shown.append("...")
    return ", ".join(shown)


def burst_track(full_burst_id: str) -> int | None:
    """Relative orbit encoded in an ASF full burst id (``None`` if not that format)."""
    m = _FULL_BURST_ID_RE.match(full_burst_id)
    return int(m.group("track")) if m else None


def records_for_candidate(
    candidate: StackCandidate, records: Sequence[BurstRecord]
) -> list[BurstRecord]:
    """Records that make up ``candidate``.

    Matching is by granule id when ``notes["granule_ids"]`` exists (set by
    :func:`wintersar.select.network.group_candidates`), otherwise by
    ``(full_burst_id ∈ burst_ids, date ∈ dates)``. Track / direction / polarization are
    deliberately *not* used so that SEL-01/02/05 can catch inconsistent candidates.
    """
    granules = candidate.notes.get("granule_ids")
    if isinstance(granules, list) and granules:
        wanted = set(granules)
        hit = [r for r in records if r.granule_id in wanted]
        if hit:
            return hit
    bursts = set(candidate.burst_ids)
    dates = set(candidate.dates)
    return [r for r in records if r.full_burst_id in bursts and r.acquisition_date in dates]


# ----------------------------------------------------------------------------- rules


def sel_01(ctx: RuleContext) -> list[Finding]:
    """SEL-01: every scene of the stack has the same relative orbit (track) -> else FAIL."""
    tracks = {ctx.candidate.relative_orbit}
    tracks.update(r.relative_orbit for r in ctx.records)
    for b in ctx.candidate.burst_ids:
        t = burst_track(b)
        if t is not None:
            tracks.add(t)
    if len(tracks) <= 1:
        return []
    return [
        _finding(
            "SEL-01",
            "FAIL",
            "fail",
            ctx.stack_id,
            {"stack": ctx.stack_id, "tracks": ", ".join(str(t) for t in sorted(tracks))},
            {"tracks": sorted(tracks), "candidate_track": ctx.candidate.relative_orbit},
        )
    ]


def sel_01_cross(candidates: Sequence[StackCandidate]) -> list[Finding]:
    """SEL-01 (informational, across candidates): several tracks share a direction."""
    by_dir: dict[str, set[int]] = defaultdict(set)
    for c in candidates:
        by_dir[c.flight_direction].add(c.relative_orbit)
    out: list[Finding] = []
    for direction in sorted(by_dir):
        tracks = sorted(by_dir[direction])
        if len(tracks) < 2:
            continue
        out.append(
            _finding(
                "SEL-01",
                "INFO",
                "info",
                None,
                {
                    "direction": direction,
                    "n": len(tracks),
                    "tracks": ", ".join(str(t) for t in tracks),
                },
                {"direction": direction, "tracks": tracks},
            )
        )
    return out


def sel_02(ctx: RuleContext) -> list[Finding]:
    """SEL-02: same flight direction -> else FAIL."""
    dirs = {ctx.candidate.flight_direction, *(r.flight_direction for r in ctx.records)}
    if len(dirs) <= 1:
        return []
    return [
        _finding(
            "SEL-02",
            "FAIL",
            "fail",
            ctx.stack_id,
            {"stack": ctx.stack_id, "directions": ", ".join(sorted(dirs))},
            {"directions": sorted(dirs)},
        )
    ]


def sel_03(ctx: RuleContext) -> list[Finding]:
    """SEL-03: same acquisition mode and identical sub-swath set on every date -> else FAIL."""
    modes = sorted({r.mode for r in ctx.records})
    per_date: dict[date, set[str]] = defaultdict(set)
    for r in ctx.records:
        per_date[r.acquisition_date].add(r.subswath)
    layouts = {tuple(sorted(s)) for s in per_date.values()}
    if len(modes) <= 1 and len(layouts) <= 1:
        return []
    layout_text = "; ".join(
        f"{d.isoformat()}: {'+'.join(sorted(per_date[d]))}"
        for d in sorted(per_date)[:_MAX_LISTED_DATES]
    )
    return [
        _finding(
            "SEL-03",
            "FAIL",
            "fail",
            ctx.stack_id,
            {"stack": ctx.stack_id, "modes": ", ".join(modes) or "-", "subswaths": layout_text},
            {
                "modes": modes,
                "subswaths_by_date": {
                    d.isoformat(): sorted(s) for d, s in sorted(per_date.items())
                },
            },
        )
    ]


def _alternatives(c: StackCandidate) -> dict[str, dict[str, Any]]:
    alts = c.notes.get("alternatives")
    out: dict[str, dict[str, Any]] = {}
    if isinstance(alts, list):
        for a in alts:
            if isinstance(a, dict) and "name" in a:
                out[str(a["name"])] = a
    return out


def sel_04(ctx: RuleContext) -> list[Finding]:
    """SEL-04: common bursts cover the AOI. none -> FAIL; coverage < min_coverage -> WARN."""
    c = ctx.candidate
    min_cov = float(ctx.selection.min_coverage)
    alts = _alternatives(c)
    drop_dates = alts.get("drop_dates", {})
    drop_bursts = alts.get("drop_bursts", {})
    params: dict[str, Any] = {
        "stack": c.stack_id,
        "n_dates": len(c.dates),
        "coverage": c.coverage_of_aoi,
        "min_coverage": min_cov,
        "n_bursts_dropped": int(drop_bursts.get("n_bursts_dropped", c.n_bursts_dropped)),
        "n_dates_dropped_alt": int(drop_dates.get("n_dates_dropped", c.n_dates_dropped)),
        "cov_drop_dates": float(drop_dates.get("coverage_of_aoi", c.coverage_of_aoi)),
        "cov_drop_bursts": float(drop_bursts.get("coverage_of_aoi", c.coverage_of_aoi)),
    }
    evidence = {
        "burst_ids": list(c.burst_ids),
        "coverage_of_aoi": c.coverage_of_aoi,
        "min_coverage": min_cov,
        "product_type": c.product_type,
        "alternatives": alts,
    }
    if not c.burst_ids or c.coverage_of_aoi <= 0.0:
        return [_finding("SEL-04", "FAIL", "fail", c.stack_id, params, evidence)]
    if c.coverage_of_aoi < min_cov:
        return [_finding("SEL-04", "WARN", "warn", c.stack_id, params, evidence)]
    return []


def _is_co_pol(pol: str) -> bool:
    parts = {p.strip().upper() for p in pol.replace("/", "+").split("+")}
    return any(p in parts for p in CO_POLARIZATIONS)


def sel_05(ctx: RuleContext) -> list[Finding]:
    """SEL-05: a common co-polarised channel (VV or HH) exists -> else FAIL."""
    pols = [ctx.candidate.polarization, *(r.polarization for r in ctx.records)]
    if all(_is_co_pol(p) for p in pols):
        return []
    return [
        _finding(
            "SEL-05",
            "FAIL",
            "fail",
            ctx.stack_id,
            {"stack": ctx.stack_id, "polarization": ", ".join(sorted(set(pols)))},
            {"polarizations": sorted(set(pols)), "co_polarizations": list(CO_POLARIZATIONS)},
        )
    ]


def sel_06(ctx: RuleContext) -> list[Finding]:
    """SEL-06: |Bperp| > max_perp_baseline_m -> WARN per pair; no baselines at all -> INFO."""
    c = ctx.candidate
    max_perp = float(ctx.selection.max_perp_baseline_m)
    out: list[Finding] = []
    known = 0
    for p in c.pairs:
        if p.perp_baseline_m is None:
            continue
        known += 1
        if abs(p.perp_baseline_m) > max_perp:
            out.append(
                _finding(
                    "SEL-06",
                    "WARN",
                    "warn",
                    f"{c.stack_id}:{p.key}",
                    {"pair": p.key, "perp_m": abs(p.perp_baseline_m), "max_perp_m": max_perp},
                    {"pair": p.key, "perp_baseline_m": p.perp_baseline_m, "max": max_perp},
                )
            )
    if c.pairs and known == 0:
        out.append(
            _finding(
                "SEL-06",
                "INFO",
                "info",
                c.stack_id,
                {"stack": c.stack_id, "n_pairs": len(c.pairs)},
                {"n_pairs": len(c.pairs), "n_with_baseline": 0},
            )
        )
    return out


def season_of(d: date, northern: bool = True) -> str:
    """``snow`` (leaf-off/snow), ``leaf_on`` or ``transition`` for ``d``."""
    month = d.month if northern else ((d.month + 5) % 12) + 1
    if month in SNOW_MONTHS_NH:
        return "snow"
    if month in LEAF_ON_MONTHS_NH:
        return "leaf_on"
    return "transition"


def crosses_season(a: date, b: date, northern: bool = True) -> bool:
    return {season_of(a, northern), season_of(b, northern)} == {"snow", "leaf_on"}


def sel_07(ctx: RuleContext) -> list[Finding]:
    """SEL-07: temporal baseline > max -> WARN per pair; leaf-on ↔ snow crossing -> INFO."""
    c = ctx.candidate
    max_days = int(ctx.selection.max_temporal_baseline_days)
    northern = ctx.aoi_lat is None or ctx.aoi_lat >= 0.0
    out: list[Finding] = []
    seasonal: list[str] = []
    for p in c.pairs:
        if p.temporal_baseline_days > max_days:
            out.append(
                _finding(
                    "SEL-07",
                    "WARN",
                    "warn",
                    f"{c.stack_id}:{p.key}",
                    {"pair": p.key, "days": p.temporal_baseline_days, "max_days": max_days},
                    {"pair": p.key, "temporal_baseline_days": p.temporal_baseline_days},
                )
            )
        if crosses_season(p.reference, p.secondary, northern):
            seasonal.append(p.key)
    if seasonal:
        out.append(
            _finding(
                "SEL-07",
                "INFO",
                "info",
                c.stack_id,
                {"stack": c.stack_id, "n_pairs": len(seasonal), "example": seasonal[0]},
                {"seasonal_pairs": seasonal, "northern_hemisphere": northern},
            )
        )
    return out


def _ipf_major(version: str) -> int | None:
    m = re.match(r"\s*(\d+)", version)
    return int(m.group(1)) if m else None


def sel_08(ctx: RuleContext) -> list[Finding]:
    """SEL-08: different IPF major versions within the stack -> INFO."""
    versions = sorted({r.ipf_version for r in ctx.records if r.ipf_version})
    majors = {m for m in (_ipf_major(v) for v in versions) if m is not None}
    if len(majors) <= 1:
        return []
    return [
        _finding(
            "SEL-08",
            "INFO",
            "info",
            ctx.stack_id,
            {"stack": ctx.stack_id, "versions": ", ".join(versions)},
            {"ipf_versions": versions, "majors": sorted(majors)},
        )
    ]


def sel_09(ctx: RuleContext) -> list[Finding]:
    """SEL-09: pixel-spacing deviation > tolerance -> WARN; always INFO with auto looks."""
    c = ctx.candidate
    tol = float(ctx.selection.pixel_spacing_tolerance)
    st = spacing_stats(ctx.records)
    out: list[Finding] = []
    if st.n_with_metadata >= 2 and st.deviation > tol:
        out.append(
            _finding(
                "SEL-09",
                "WARN",
                "warn",
                c.stack_id,
                {
                    "stack": c.stack_id,
                    "rg_min": st.rg_min,
                    "rg_max": st.rg_max,
                    "az_min": st.az_min,
                    "az_max": st.az_max,
                    "deviation": st.deviation,
                    "tolerance": tol,
                },
                {
                    "rg_deviation": st.rg_deviation,
                    "az_deviation": st.az_deviation,
                    "tolerance": tol,
                    "n_with_metadata": st.n_with_metadata,
                },
            )
        )
    target = float(ctx.cfg.engine.target_pixel_m)
    auto = looks_for_records(ctx.records, target)
    evidence: dict[str, Any] = {
        "spacing": {
            "rg_median": st.rg_median,
            "az_median": st.az_median,
            "incidence_deg": st.incidence_deg,
            "nominal": st.nominal,
        },
        "auto_looks": auto.as_dict(),
    }
    configured = ctx.cfg.engine.looks
    if isinstance(configured, tuple):
        chosen = looks_for_records(ctx.records, target, looks=configured)
        evidence["configured_looks"] = chosen.as_dict()
    out.append(
        _finding(
            "SEL-09",
            "INFO",
            "info",
            c.stack_id,
            {
                "stack": c.stack_id,
                "rg": st.rg_median,
                "az": st.az_median,
                "ground_rg": auto.ground_range_spacing_m,
                "incidence": st.incidence_deg,
                "target": target,
                "rg_looks": auto.rg_looks,
                "az_looks": auto.az_looks,
                "pixel_rg": auto.pixel_rg_m,
                "pixel_az": auto.pixel_az_m,
                "aspect": auto.aspect_ratio,
            },
            evidence,
        )
    )
    return out


def _missing_lines(r: BurstRecord) -> bool:
    # Internal convention (filled by wintersar.select.metadata when annotation XML is read):
    # extra["missing_lines"] is a bool or a count of missing lines.
    v = r.extra.get("missing_lines")
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return v > 0
    return False


def sel_10(ctx: RuleContext) -> list[Finding]:
    """SEL-10: burst count per date != expected, or missing lines reported -> WARN."""
    c = ctx.candidate
    expected = len(c.burst_ids)
    wanted = set(c.burst_ids)
    per_date: dict[date, set[str]] = {d: set() for d in c.dates}
    bad_lines: set[date] = set()
    for r in ctx.records:
        d = r.acquisition_date
        if d not in per_date:
            continue
        if r.full_burst_id in wanted:
            per_date[d].add(r.full_burst_id)
        if _missing_lines(r):
            bad_lines.add(d)
    if not ctx.records:
        return []
    bad = sorted(d for d, s in per_date.items() if len(s) != expected or d in bad_lines)
    if not bad:
        return []
    return [
        _finding(
            "SEL-10",
            "WARN",
            "warn",
            c.stack_id,
            {
                "stack": c.stack_id,
                "n_dates": len(bad),
                "dates": _fmt_dates(bad),
                "expected": expected,
            },
            {
                "expected_bursts": expected,
                "burst_count_by_date": {d.isoformat(): len(per_date[d]) for d in bad},
                "missing_lines_dates": [d.isoformat() for d in sorted(bad_lines)],
            },
        )
    ]


def sel_11(ctx: RuleContext) -> list[Finding]:
    """SEL-11: precise orbit (POEORB) not available for some dates -> INFO.

    ``ctx.poeorb_available(date)`` returns ``True``/``False`` or ``None`` (unknown ->
    skipped). Without a hook the rule is skipped entirely (no network access here).
    """
    hook = ctx.poeorb_available
    if hook is None:
        return []
    c = ctx.candidate
    missing = [d for d in c.dates if hook(d) is False]
    if not missing:
        return []
    return [
        _finding(
            "SEL-11",
            "INFO",
            "info",
            c.stack_id,
            {"stack": c.stack_id, "n_dates": len(missing), "dates": _fmt_dates(missing)},
            {"dates_without_poeorb": [d.isoformat() for d in missing]},
        )
    ]


def geometry_stats_for(
    geometry: Mapping[str, Any] | Any | None, candidate: StackCandidate
) -> dict[str, float] | None:
    """Resolve layover/shadow statistics for ``candidate`` from ``geometry``.

    Accepted shapes: ``{stack_id: stats}``, ``{"ASCENDING"|"DESCENDING": stats}``,
    ``{"asc"|"desc": stats}``, a flat ``stats`` dict, or objects with ``.stats`` (and
    optionally ``.flight_direction``) such as ``GeometryMaskResult``.
    """
    if geometry is None:
        return None
    direction = candidate.flight_direction
    short = "asc" if direction == "ASCENDING" else "desc"
    obj: Any = geometry
    if isinstance(obj, Mapping):
        for key in (candidate.stack_id, direction, direction.lower(), short):
            if key in obj:
                obj = obj[key]
                break
    if not isinstance(obj, Mapping) and hasattr(obj, "stats"):
        fd = getattr(obj, "flight_direction", None)
        if fd is not None and str(fd).upper() != direction:
            return None
        obj = obj.stats
    if not isinstance(obj, Mapping) or "layover_fraction" not in obj:
        return None
    return {
        "layover_fraction": float(obj.get("layover_fraction", 0.0)),
        "shadow_fraction": float(obj.get("shadow_fraction", 0.0)),
        "foreshortening_mean": float(obj.get("foreshortening_mean", 0.0)),
    }


def sel_12(ctx: RuleContext) -> list[Finding]:
    """SEL-12: layover + shadow fraction of the AOI > threshold -> WARN, else INFO."""
    stats = geometry_stats_for(ctx.geometry, ctx.candidate)
    if stats is None:
        return []
    max_frac = float(ctx.selection.max_layover_shadow_fraction)
    total = stats["layover_fraction"] + stats["shadow_fraction"]
    sev: Severity = "WARN" if total > max_frac else "INFO"
    return [
        _finding(
            "SEL-12",
            sev,
            sev.lower(),
            ctx.stack_id,
            {
                "direction": ctx.candidate.flight_direction,
                "layover": stats["layover_fraction"],
                "shadow": stats["shadow_fraction"],
                "total": total,
                "max_fraction": max_frac,
                "foreshortening": stats["foreshortening_mean"],
            },
            {**stats, "total_fraction": total, "max_layover_shadow_fraction": max_frac},
        )
    ]


def evaluate_resources(
    resources: Resources | None, cfg: Config, scope: str | None
) -> list[Finding]:
    """SEL-13 core: credits > budget -> WARN; otherwise INFO (or INFO 'unknown')."""
    if resources is None:
        return []
    budget = cfg.selection.budget_credits
    credits = resources.credits
    n_jobs = int(resources.n_jobs or 0)
    evidence = {
        "credits": credits,
        "budget_credits": budget,
        "n_jobs": n_jobs,
        "wall_time_s": resources.wall_time_s,
        "disk_gb": resources.disk_gb,
        "network_gb": resources.network_gb,
    }
    if credits is None:
        return [_finding("SEL-13", "INFO", "info_unknown", scope, {"n_jobs": n_jobs}, evidence)]
    if budget is not None and credits > budget:
        return [
            _finding(
                "SEL-13",
                "WARN",
                "warn",
                scope,
                {"credits": credits, "budget": budget, "n_jobs": n_jobs},
                evidence,
            )
        ]
    budget_text = f"{budget:.0f}" if budget is not None else "-"
    return [
        _finding(
            "SEL-13",
            "INFO",
            "info",
            scope,
            {"credits": credits, "n_jobs": n_jobs, "budget_text": budget_text},
            evidence,
        )
    ]


def sel_13(ctx: RuleContext) -> list[Finding]:
    """SEL-13: credit / resource estimate vs ``selection.budget_credits``."""
    return evaluate_resources(ctx.resources, ctx.cfg, ctx.stack_id)


# --------------------------------------------------------------------------- registry

RULES: dict[str, RuleFn] = {
    "SEL-01": sel_01,
    "SEL-02": sel_02,
    "SEL-03": sel_03,
    "SEL-04": sel_04,
    "SEL-05": sel_05,
    "SEL-06": sel_06,
    "SEL-07": sel_07,
    "SEL-08": sel_08,
    "SEL-09": sel_09,
    "SEL-10": sel_10,
    "SEL-11": sel_11,
    "SEL-12": sel_12,
    "SEL-13": sel_13,
}

_SEVERITIES: dict[str, tuple[str, ...]] = {
    "SEL-01": ("FAIL", "INFO"),
    "SEL-02": ("FAIL",),
    "SEL-03": ("FAIL",),
    "SEL-04": ("FAIL", "WARN"),
    "SEL-05": ("FAIL",),
    "SEL-06": ("WARN", "INFO"),
    "SEL-07": ("WARN", "INFO"),
    "SEL-08": ("INFO",),
    "SEL-09": ("WARN", "INFO"),
    "SEL-10": ("WARN",),
    "SEL-11": ("INFO",),
    "SEL-12": ("WARN", "INFO"),
    "SEL-13": ("WARN", "INFO"),
}

_EXTRA_MESSAGE_KEYS: dict[str, tuple[str, ...]] = {"SEL-13": ("info_unknown",)}


def rule_table() -> list[dict[str, Any]]:
    """Rows for the documentation: id, severities, i18n keys of description/messages/fix."""
    rows: list[dict[str, Any]] = []
    for rid in RULES:
        sevs = _SEVERITIES[rid]
        kinds = [s.lower() for s in sevs] + list(_EXTRA_MESSAGE_KEYS.get(rid, ()))
        rows.append(
            {
                "id": rid,
                "severities": list(sevs),
                "desc_key": f"select.{rid}.desc",
                "message_keys": [f"select.{rid}.{k}" for k in kinds],
                "fix_key": f"select.{rid}.fix",
                "function": RULES[rid].__name__,
            }
        )
    return rows


def all_message_keys() -> list[str]:
    keys: list[str] = []
    for row in rule_table():
        keys.append(row["desc_key"])
        keys.extend(row["message_keys"])
        keys.append(row["fix_key"])
    return keys


def run_rules(
    candidates: Sequence[StackCandidate],
    records: Sequence[BurstRecord],
    cfg: Config,
    geometry: Mapping[str, Any] | None = None,
    resources: Resources | Mapping[str, Resources] | None = None,
    poeorb_available: PoeorbHook | None = None,
    aoi_lat: float | None = None,
) -> list[Finding]:
    """Run every rule on every candidate (plus the cross-candidate SEL-01 note).

    ``resources`` may be one :class:`Resources` (evaluated once, global scope) or a
    mapping ``{stack_id: Resources}`` (evaluated per candidate). ``geometry`` see
    :func:`geometry_stats_for`. ``poeorb_available`` see :func:`sel_11`. ``aoi_lat``
    selects the hemisphere for the seasonal check of SEL-07.
    """
    findings: list[Finding] = list(sel_01_cross(candidates))
    per_stack: Mapping[str, Resources] | None = None
    if isinstance(resources, Mapping):
        per_stack = resources
    for c in candidates:
        ctx = RuleContext(
            candidate=c,
            records=records_for_candidate(c, records),
            cfg=cfg,
            all_candidates=list(candidates),
            geometry=geometry,
            resources=per_stack.get(c.stack_id) if per_stack is not None else None,
            poeorb_available=poeorb_available,
            aoi_lat=aoi_lat,
        )
        for fn in RULES.values():
            findings.extend(fn(ctx))
    if isinstance(resources, Resources):
        findings.extend(evaluate_resources(resources, cfg, None))
    return findings
