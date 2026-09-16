"""Validation report (Markdown + HTML via Jinja2, optional PNG plots) — plan §5.6, R-10.

The report is rendered from ``templates/report.md.j2`` and ``templates/report.html.j2``.
Every label comes from ``i18n/<lang>/validate.yaml`` (rule 11.6); every path is masked with
:func:`wintersar.util.masking.mask_text` (rule 11.11). Plots need matplotlib (optional
``plots`` extra): when it is missing the report is written without figures.

# source: .venv/lib/python3.11/site-packages/jinja2/environment.py (Environment, get_template)
# source: .venv/lib/python3.11/site-packages/jinja2/loaders.py (FileSystemLoader)
# source: .venv/lib/python3.11/site-packages/matplotlib/pyplot.py (subplots, close)
"""

from __future__ import annotations

import json
import math
import warnings
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from wintersar.i18n import load_catalog, t
from wintersar.io.schemas import Finding, sort_findings
from wintersar.util.masking import mask_mapping, mask_text
from wintersar.util.output import findings_to_markdown, render_finding
from wintersar.validate.closure import ClosureResult
from wintersar.validate.metrics import ComparisonResult, SiteComparison
from wintersar.validate.refpoint import RefPointCandidate

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
REPORT_BASENAME = "validation_report"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html", "html.j2"), default=False),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def fmt_mm(value: float | None, digits: int = 1) -> str:
    """Metres → millimetres string (``-`` for None/NaN)."""
    if value is None or not math.isfinite(value):
        return "-"
    return f"{value * 1000.0:.{digits}f}"


def fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def labels(lang: str | None = None) -> dict[str, str]:
    """All ``validate.report.*`` labels plus the common cause/fix words for the templates."""
    cat = load_catalog(lang or "ko")
    prefix = "validate.report."
    out = {k[len(prefix) :]: t(k, lang) for k in cat if k.startswith(prefix)}
    out["cause"] = t("common.cause", lang)
    out["fix"] = t("common.fix", lang)
    out["no_findings"] = t("cli.no_findings", lang)
    return out


def site_rows(sites: Sequence[SiteComparison]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in sites:
        rows.append(
            {
                "site_id": s.site_id,
                "method": s.method,
                "n": s.n,
                "rmse_mm": fmt_mm(s.rmse_m),
                "bias_mm": fmt_mm(s.bias_m),
                "corr": fmt_num(s.corr),
                "v_insar": fmt_mm(s.insar_velocity_m_per_yr),
                "v_gt": fmt_mm(s.gt_velocity_m_per_yr),
                "v_diff": fmt_mm(s.velocity_diff_m_per_yr),
                "n_pixels": s.n_pixels,
                "distance_m": fmt_num(s.distance_to_pixel_m, 0),
                "reference_date": s.reference_date.isoformat() if s.reference_date else "-",
            }
        )
    return rows


def finding_rows(findings: Sequence[Finding], lang: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sort_findings(list(findings)):
        sev, rid, cause, fix = render_finding(f, lang)
        rows.append(
            {
                "severity": f.severity,
                "severity_label": sev,
                "rule_id": rid,
                "scope": f.scope,
                "cause": cause,
                "fix": fix,
            }
        )
    return rows


def refpoint_rows(candidates: Sequence[RefPointCandidate]) -> list[dict[str, Any]]:
    return [
        {
            "rank": i + 1,
            "row": c.row,
            "col": c.col,
            "lat": f"{c.lat:.5f}",
            "lon": f"{c.lon:.5f}",
            "score": fmt_num(c.score, 3),
            "coherence": fmt_num(c.coherence),
        }
        for i, c in enumerate(candidates)
    ]


def closure_context(result: ClosureResult, lang: str | None, top: int = 10) -> dict[str, Any]:
    dash = result.to_dashboard()
    rms = dash["summary"].get("rms_mean_rad")
    summary = t(
        "validate.report.closure_summary",
        lang,
        mode=result.mode,
        n_triplets=result.n_triplets,
        n_igrams=len(result.pairs),
        rms_rad=fmt_num(rms, 3),
    )
    yes, no = t("validate.report.yes", lang), t("validate.report.no", lang)
    igrams = [
        {
            "rank": r["rank"],
            "pair": r["pair"],
            "score": fmt_num(r["score"], 3),
            "n_triplets": r["n_triplets"],
            "suspicious": yes if r["suspicious"] else no,
        }
        for r in dash["igrams"][:top]
    ]
    return {"summary": summary, "igrams": igrams, "suspicious": dash["suspicious"]}


def _all_findings(result: ComparisonResult) -> list[Finding]:
    out = list(result.findings)
    for s in result.per_site:
        for f in s.findings:
            if f not in out:
                out.append(f)
    return out


def report_context(
    result: ComparisonResult,
    lang: str | None = None,
    *,
    source: str | None = None,
    closure: ClosureResult | None = None,
    refpoints: Sequence[RefPointCandidate] | None = None,
    sweep_md: str | None = None,
    sweep_html: str | None = None,
    plots: Mapping[str, Path] | None = None,
    plot_base: Path | None = None,
    generated: datetime | None = None,
) -> dict[str, Any]:
    """Template context shared by the Markdown and HTML renderers."""
    lang = lang or "ko"
    lab = labels(lang)
    when = (generated or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    lab["generated"] = t("validate.report.generated", lang, when=when)
    lab["source"] = t("validate.report.source", lang, source=mask_text(source or ""))
    lab["conventions"] = t(
        "validate.report.conventions",
        lang,
        method=result.method,
        radius_m=fmt_num(result.radius_m, 0),
        align=result.align,
        max_gap_days=result.max_gap_days,
    )
    findings = _all_findings(result)
    plot_rows: list[dict[str, str]] = []
    for site_id, p in (plots or {}).items():
        rel = p
        if plot_base is not None:
            try:
                rel = p.relative_to(plot_base)
            except ValueError:
                rel = p
        plot_rows.append({"site_id": site_id, "path": rel.as_posix()})
    return {
        "lang": lang,
        "L": lab,
        "source": mask_text(source) if source else None,
        "summary": {
            "rmse_mm": fmt_mm(result.rmse_m),
            "bias_mm": fmt_mm(result.bias_m),
            "n_sites": result.n_sites,
            "n_points": result.n_points,
        },
        "sites": site_rows(result.sites_compared),
        "findings": finding_rows(findings, lang),
        "findings_md": findings_to_markdown(findings, lang),
        "refpoints": refpoint_rows(refpoints) if refpoints else [],
        "closure": closure_context(closure, lang) if closure is not None else None,
        "sweep_md": sweep_md,
        "sweep_html": sweep_html,
        "plots": plot_rows,
    }


def render_markdown(result: ComparisonResult, lang: str | None = None, **extras: Any) -> str:
    ctx = report_context(result, lang, **extras)
    return _env().get_template("report.md.j2").render(**ctx)


def render_html(result: ComparisonResult, lang: str | None = None, **extras: Any) -> str:
    ctx = report_context(result, lang, **extras)
    return _env().get_template("report.html.j2").render(**ctx)


def plots_available() -> bool:
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        return False
    return True


def plot_sites(result: ComparisonResult, out_dir: Path, dpi: int = 100) -> dict[str, Path]:
    """One PNG per compared site (InSAR line vs GT markers, mm). Empty dict without matplotlib."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for s in result.sites_compared:
        fig, ax = plt.subplots(figsize=(6.4, 3.2))
        x = [d.toordinal() for d in s.dates]
        ax.plot(x, [v * 1000.0 for v in s.insar_m], "-o", ms=3, label="InSAR")
        ax.plot(x, [v * 1000.0 for v in s.gt_los_m], "s", ms=4, label=s.method)
        ax.set_xticks(x[:: max(1, len(x) // 6)])
        ax.set_xticklabels(
            [d.isoformat() for d in s.dates[:: max(1, len(x) // 6)]], rotation=30, fontsize=7
        )
        ax.set_ylabel("LOS (mm)")
        ax.set_title(f"{s.site_id}  RMSE {fmt_mm(s.rmse_m)} mm  bias {fmt_mm(s.bias_m)} mm")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s.site_id)
        p = out_dir / f"site_{safe}.png"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # missing glyphs for non-Latin site ids
            fig.tight_layout()
            fig.savefig(p, dpi=dpi)
        plt.close(fig)
        paths[s.site_id] = p
    return paths


def write_report(
    result: ComparisonResult,
    out_dir: Path | str,
    lang: str | None = None,
    *,
    source: str | None = None,
    closure: ClosureResult | None = None,
    refpoints: Sequence[RefPointCandidate] | None = None,
    sweep_md: str | None = None,
    sweep_html: str | None = None,
    plots: bool = True,
    basename: str = REPORT_BASENAME,
) -> dict[str, Path]:
    """Write ``<basename>.md``, ``.html``, ``.json`` (and ``plots/site_*.png``) under ``out_dir``.

    Returns ``{"markdown": ..., "html": ..., "json": ..., "plots": <dir>?}``.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    plot_paths = plot_sites(result, out / "plots") if plots else {}
    extras: dict[str, Any] = {
        "source": source,
        "closure": closure,
        "refpoints": refpoints,
        "sweep_md": sweep_md,
        "sweep_html": sweep_html,
        "plots": plot_paths,
        "plot_base": out,
    }
    md = out / f"{basename}.md"
    html = out / f"{basename}.html"
    js = out / f"{basename}.json"
    md.write_text(render_markdown(result, lang, **extras), encoding="utf-8")
    html.write_text(render_html(result, lang, **extras), encoding="utf-8")
    payload: dict[str, Any] = {"comparison": result.to_dict(), "source": source}
    if closure is not None:
        payload["closure"] = closure.to_dashboard()
    if refpoints:
        payload["refpoints"] = [c.to_dict() for c in refpoints]
    js.write_text(
        json.dumps(mask_mapping(payload), ensure_ascii=False, indent=2, allow_nan=True),
        encoding="utf-8",
    )
    paths = {"markdown": md, "html": html, "json": js}
    if plot_paths:
        paths["plots"] = out / "plots"
    return paths


__all__ = [
    "REPORT_BASENAME",
    "TEMPLATES_DIR",
    "closure_context",
    "finding_rows",
    "fmt_mm",
    "fmt_num",
    "labels",
    "plot_sites",
    "plots_available",
    "refpoint_rows",
    "render_html",
    "render_markdown",
    "report_context",
    "site_rows",
    "write_report",
]
