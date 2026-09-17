"""Precheck report (plan §5.1.6): ``precheck_report.{md,html,json}``.

* :func:`recommend_stack` - best candidate without FAIL findings (highest coverage,
  then most dates, then a connected network, then fewest WARN).
* :func:`precheck_payload` - one JSON-serialisable dict that feeds all three formats and
  the ``--json`` CLI envelope; ``load_precheck_json`` validates it back into models.
* :func:`write_precheck_report` - writes the three files (HTML through the jinja2
  template ``templates/precheck_report.html.j2``).

All text comes from ``i18n/<lang>/select.yaml``; paths and values pass through
``wintersar.util.masking`` (rule 11.11).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wintersar.i18n import load_catalog, t
from wintersar.io.schemas import SEVERITY_ORDER, Finding, Resources, StackCandidate, sort_findings
from wintersar.select.looks import LooksResult
from wintersar.select.network import network_components
from wintersar.util.masking import mask_mapping, mask_text
from wintersar.util.output import findings_to_markdown, render_finding, to_jsonable

REPORT_BASENAME = "precheck_report"
PAYLOAD_VERSION = 1
_MAX_LISTED_DATES = 4

_REPORT_LABEL_PREFIX = "select.report."


def findings_for_stack(stack_id: str, findings: Sequence[Finding]) -> list[Finding]:
    """Findings scoped to ``stack_id`` (``scope == id`` or ``scope == "<id>:<pair>"``)."""
    return [
        f
        for f in findings
        if f.scope is not None and (f.scope == stack_id or f.scope.startswith(stack_id + ":"))
    ]


def severity_counts(findings: Sequence[Finding]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f.severity == s) for s in SEVERITY_ORDER}


def network_components_of(c: StackCandidate) -> int:
    """Connected components of ``c``'s interferogram network.

    Prefers the value :func:`wintersar.select.network.network_summary` stored in
    ``notes["network"]`` and recomputes it when the candidate carries no summary.
    """
    net = c.notes.get("network")
    if isinstance(net, Mapping):
        n = net.get("n_components")
        if isinstance(n, int) and not isinstance(n, bool):
            return n
    return network_components(c.dates, c.pairs)


def recommend_stack(
    candidates: Sequence[StackCandidate], findings: Sequence[Finding]
) -> StackCandidate | None:
    """Candidate with no FAIL finding, ranked by coverage desc, dates desc, connected
    network first, WARN count asc, id asc.

    A stack whose network is split into several connected components cannot be inverted
    as one time series, so a connected candidate wins a coverage/date tie (ADR-0016).
    """
    ranked: list[tuple[float, int, int, int, str, StackCandidate]] = []
    for c in candidates:
        own = findings_for_stack(c.stack_id, findings)
        if any(f.is_fail for f in own):
            continue
        n_warn = sum(1 for f in own if f.severity == "WARN")
        split = 1 if network_components_of(c) > 1 else 0
        ranked.append((-c.coverage_of_aoi, -len(c.dates), split, n_warn, c.stack_id, c))
    if not ranked:
        return None
    ranked.sort(key=lambda r: r[:5])
    return ranked[0][5]


def _min_med_max(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    return {"min": s[0], "median": med, "max": s[-1]}


def stack_summary(c: StackCandidate) -> dict[str, Any]:
    """Row of the candidate table (track, direction, polarization, dates, coverage, baselines)."""
    perps = [abs(p.perp_baseline_m) for p in c.pairs if p.perp_baseline_m is not None]
    temps = [float(p.temporal_baseline_days) for p in c.pairs]
    net = c.notes.get("network")
    net = net if isinstance(net, Mapping) else {}
    return {
        "stack_id": c.stack_id,
        "relative_orbit": c.relative_orbit,
        "flight_direction": c.flight_direction,
        "polarization": c.polarization,
        "subswaths": list(c.subswaths),
        "product_type": c.product_type,
        "n_dates": len(c.dates),
        "n_bursts": len(c.burst_ids),
        "coverage_of_aoi": c.coverage_of_aoi,
        "n_pairs": len(c.pairs),
        "reference_date": c.reference_date.isoformat() if c.reference_date else None,
        "first_date": c.dates[0].isoformat() if c.dates else None,
        "last_date": c.dates[-1].isoformat() if c.dates else None,
        "perp_m": _min_med_max(perps),
        "temporal_days": _min_med_max(temps),
        "n_dates_dropped": c.n_dates_dropped,
        "n_bursts_dropped": c.n_bursts_dropped,
        "n_components": network_components_of(c),
        "n_pairs_dropped_perp": int(net.get("n_pairs_dropped_perp", 0)),
        "dates_disconnected": list(net.get("dates_disconnected", [])),
        "alternatives": c.notes.get("alternatives", []),
        "network": dict(net),
    }


def _resources_map(
    resources: Resources | Mapping[str, Resources] | None,
) -> dict[str, dict[str, Any]]:
    if resources is None:
        return {}
    if isinstance(resources, Resources):
        return {"*": resources.model_dump(mode="json")}
    return {k: v.model_dump(mode="json") for k, v in resources.items()}


def precheck_payload(
    candidates: Sequence[StackCandidate],
    findings: Sequence[Finding],
    lang: str,
    looks: Mapping[str, LooksResult] | None = None,
    resources: Resources | Mapping[str, Resources] | None = None,
    recommended: str | None = None,
) -> dict[str, Any]:
    """JSON-serialisable report content (also the ``--json`` envelope data)."""
    ordered = sort_findings(list(findings))
    rendered = []
    for f in ordered:
        sev, rid, cause, fix = render_finding(f, lang)
        rendered.append(
            {
                "severity": f.severity,
                "severity_label": sev,
                "rule_id": rid,
                "scope": f.scope,
                "cause": mask_text(cause),
                "fix": mask_text(fix),
            }
        )
    payload: dict[str, Any] = {
        "version": PAYLOAD_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "lang": lang,
        "recommended": recommended,
        "summary": {
            **severity_counts(ordered),
            "n_candidates": len(candidates),
            "ok": not any(f.is_fail for f in ordered),
        },
        "candidates": [c.model_dump(mode="json") for c in candidates],
        "candidate_table": [stack_summary(c) for c in candidates],
        "findings": [f.model_dump(mode="json") for f in ordered],
        "findings_rendered": rendered,
        "looks": {k: v.as_dict() for k, v in (looks or {}).items()},
        "resources": _resources_map(resources),
    }
    return mask_mapping(to_jsonable(payload))  # type: ignore[no-any-return]


def load_precheck_json(path: Path) -> dict[str, Any]:
    """Read ``precheck_report.json`` and validate candidates/findings back into models."""
    with Path(path).open(encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["candidates"] = [
        StackCandidate.model_validate(c) for c in payload.get("candidates", [])
    ]
    payload["findings"] = [Finding.model_validate(f) for f in payload.get("findings", [])]
    return payload  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- rendering


def report_labels(lang: str) -> dict[str, str]:
    cat = load_catalog(lang)
    labels = {
        k[len(_REPORT_LABEL_PREFIX) :]: v
        for k, v in cat.items()
        if k.startswith(_REPORT_LABEL_PREFIX)
    }
    labels["severity_FAIL"] = t("common.severity.FAIL", lang)
    labels["severity_WARN"] = t("common.severity.WARN", lang)
    labels["severity_INFO"] = t("common.severity.INFO", lang)
    labels["cause"] = t("common.cause", lang)
    labels["fix"] = t("common.fix", lang)
    labels["none"] = t("common.none", lang)
    labels["no_findings"] = t("cli.no_findings", lang)
    return labels


def _fmt_range(v: Mapping[str, float] | None, unknown: str, digits: int = 0) -> str:
    if not v:
        return unknown
    return " / ".join(f"{v[k]:.{digits}f}" for k in ("min", "median", "max"))


def _components_text(row: Mapping[str, Any], lang: str) -> str:
    """``1`` when the network is connected, otherwise ``n (disconnected dates: ...)``.

    A split network is only reported here and in ``notes["network"]``; turning it into a
    severity-bearing rule would change the plan §11 rule table (rule 11.10, ADR-0014).
    """
    n = int(row.get("n_components", 1) or 0)
    if n <= 1:
        return str(n)
    dates = [str(d) for d in (row.get("dates_disconnected") or [])]
    shown = ", ".join(dates[:_MAX_LISTED_DATES]) + (
        ", ..." if len(dates) > _MAX_LISTED_DATES else ""
    )
    return t("select.report.network_split", lang, n=n, dates=shown or t("common.none", lang))


def alternative_rows(payload: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """``(stack_id, alternative)`` rows worth showing: stacks where an option drops
    at least one date or burst (complete stacks have two identical options)."""
    rows: list[tuple[str, dict[str, Any]]] = []
    for row in payload["candidate_table"]:
        alts = list(row.get("alternatives") or [])
        if any(a.get("n_dates_dropped") or a.get("n_bursts_dropped") for a in alts):
            rows.extend((row["stack_id"], a) for a in alts)
    return rows


def _alt_label(name: str, lang: str) -> str:
    key = "opt_drop_bursts" if name == "drop_bursts" else "opt_drop_dates"
    return t(f"select.report.{key}", lang)


def render_markdown(payload: Mapping[str, Any], findings: Sequence[Finding], lang: str) -> str:
    lab = report_labels(lang)
    unk = lab.get("unknown", "?")
    s = payload["summary"]
    lines: list[str] = [
        f"# {lab['title']}",
        "",
        f"{lab['generated_at']}: {payload['generated_at']}",
        "",
        f"## {lab['summary']}",
        "",
        t(
            "cli.findings_summary",
            lang,
            n_fail=s["FAIL"],
            n_warn=s["WARN"],
            n_info=s["INFO"],
        ),
        "",
        f"## {lab['recommended']}",
        "",
        f"**{payload['recommended']}**" if payload["recommended"] else lab["no_recommendation"],
        "",
        f"## {lab['candidates']}",
        "",
        f"| {lab['col_stack']} | {lab['col_track']} | {lab['col_direction']} | "
        f"{lab['col_polarization']} | {lab['col_subswaths']} | {lab['col_n_dates']} | "
        f"{lab['col_n_bursts']} | {lab['col_coverage']} | {lab['col_n_pairs']} | "
        f"{lab['col_pairs_dropped']} | {lab['col_components']} | "
        f"{lab['col_reference']} | {lab['col_period']} | {lab['col_perp']} | {lab['col_temporal']} |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in payload["candidate_table"]:
        period = f"{row['first_date']} ~ {row['last_date']}" if row["first_date"] else unk
        lines.append(
            f"| {row['stack_id']} | {row['relative_orbit']} | {row['flight_direction']} | "
            f"{row['polarization']} | {'+'.join(row['subswaths']) or unk} | {row['n_dates']} | "
            f"{row['n_bursts']} | {row['coverage_of_aoi']:.1%} | {row['n_pairs']} | "
            f"{row.get('n_pairs_dropped_perp', 0)} | {_components_text(row, lang)} | "
            f"{row['reference_date'] or unk} | {period} | {_fmt_range(row['perp_m'], unk)} | "
            f"{_fmt_range(row['temporal_days'], unk)} |"
        )
    alt_rows = alternative_rows(payload)
    if alt_rows:
        lines += [
            "",
            f"## {lab['alternatives']}",
            "",
            f"| {lab['col_stack']} | {lab['col_option']} | {lab['col_n_dates']} | "
            f"{lab['col_n_bursts']} | {lab['col_coverage']} | {lab['col_dropped']} |",
            "|---|---|---|---|---|---|",
        ]
        for sid, a in alt_rows:
            dropped = (
                t("select.report.dates_dropped", lang, n=a["n_dates_dropped"])
                if a["n_dates_dropped"]
                else t("select.report.bursts_dropped", lang, n=a["n_bursts_dropped"])
            )
            lines.append(
                f"| {sid} | {_alt_label(a['name'], lang)} | {a['n_dates']} | {a['n_bursts']} | "
                f"{a['coverage_of_aoi']:.1%} | {dropped} |"
            )
    if payload["looks"]:
        lines += [
            "",
            f"## {lab['looks']}",
            "",
            f"| {lab['col_stack']} | looks (rg x az) | {lab['col_pixel']} | {lab['col_aspect']} |",
            "|---|---|---|---|",
        ]
        for sid, lk in payload["looks"].items():
            lines.append(
                f"| {sid} | {lk['rg_looks']} x {lk['az_looks']} | "
                f"{lk['pixel_rg_m']:.1f} x {lk['pixel_az_m']:.1f} | {lk['aspect_ratio']:.2f} |"
            )
    if payload["resources"]:
        lines += [
            "",
            f"## {lab['resources']}",
            "",
            f"| {lab['col_stack']} | {lab['res_n_jobs']} | {lab['res_credits']} | "
            f"{lab['res_wall_time']} | {lab['res_disk']} | {lab['res_network']} |",
            "|---|---|---|---|---|---|",
        ]
        for sid, r in payload["resources"].items():
            lines.append(
                f"| {sid} | {r.get('n_jobs') if r.get('n_jobs') is not None else unk} | "
                f"{r.get('credits') if r.get('credits') is not None else unk} | "
                f"{r.get('wall_time_s') if r.get('wall_time_s') is not None else unk} | "
                f"{r.get('disk_gb') if r.get('disk_gb') is not None else unk} | "
                f"{r.get('network_gb') if r.get('network_gb') is not None else unk} |"
            )
    lines += ["", f"## {lab['findings']}", "", findings_to_markdown(list(findings), lang)]
    return mask_text("\n".join(lines))


def render_html(payload: Mapping[str, Any], lang: str) -> str:
    # source: .venv/lib/python3.11/site-packages/jinja2/loaders.py `PackageLoader(package_name, package_path)`
    from jinja2 import Environment, PackageLoader

    env = Environment(
        loader=PackageLoader("wintersar.select", "templates"),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tmpl = env.get_template(f"{REPORT_BASENAME}.html.j2")
    lab = report_labels(lang)
    alt_rows = [
        {
            "stack_id": sid,
            "label": _alt_label(a["name"], lang),
            "n_dates": a["n_dates"],
            "n_bursts": a["n_bursts"],
            "coverage": a["coverage_of_aoi"],
            "dropped": (
                t("select.report.dates_dropped", lang, n=a["n_dates_dropped"])
                if a["n_dates_dropped"]
                else t("select.report.bursts_dropped", lang, n=a["n_bursts_dropped"])
            ),
        }
        for sid, a in alternative_rows(payload)
    ]
    html = tmpl.render(
        lang=lang,
        labels=lab,
        payload=payload,
        alt_rows=alt_rows,
        components_text=lambda row: _components_text(row, lang),
        summary_text=t(
            "cli.findings_summary",
            lang,
            n_fail=payload["summary"]["FAIL"],
            n_warn=payload["summary"]["WARN"],
            n_info=payload["summary"]["INFO"],
        ),
        fmt_range=lambda v: _fmt_range(v, lab.get("unknown", "?")),
    )
    return mask_text(html)


def write_precheck_report(
    candidates: Sequence[StackCandidate],
    findings: Sequence[Finding],
    out_dir: Path,
    lang: str,
    looks: Mapping[str, LooksResult] | None = None,
    resources: Resources | Mapping[str, Resources] | None = None,
    recommended: str | None = None,
) -> dict[str, Path]:
    """Write ``precheck_report.md``, ``.html`` and ``.json`` into ``out_dir``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = precheck_payload(candidates, findings, lang, looks, resources, recommended)
    paths = {
        "json": out_dir / f"{REPORT_BASENAME}.json",
        "md": out_dir / f"{REPORT_BASENAME}.md",
        "html": out_dir / f"{REPORT_BASENAME}.html",
    }
    paths["json"].write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    paths["md"].write_text(render_markdown(payload, findings, lang), encoding="utf-8")
    paths["html"].write_text(render_html(payload, lang), encoding="utf-8")
    return paths
