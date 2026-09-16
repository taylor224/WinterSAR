"""before/after comparison of two ``bench_result.json`` files (plan §6.3 items 4-5, ADR-0054).

:func:`compare` pairs every stage present in both results, computes percentage deltas of the
measured quantities and flags a **regression** when the *after* median wall time exceeds the
*before* one by more than ``threshold`` (default 15 %, plan §6.3: "기준선 대비 15% 이상
느려지면 실패"). :meth:`CompareReport.to_markdown` renders the table (headers from the i18n
catalogue) for PR comments and ``docs/``; README links to that table, never to numbers typed
by hand (rule 11.8).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wintersar.i18n import t

QUANTITIES: tuple[str, ...] = ("wall_time_s", "peak_rss_gb", "disk_peak_gb", "network_bytes")
DEFAULT_THRESHOLD = 0.15
_SLOWER_IS_WORSE: dict[str, bool] = {
    "wall_time_s": True,
    "peak_rss_gb": True,
    "disk_peak_gb": True,
    "network_bytes": True,
    "closure_rms": True,
    "unwrap_error_fraction": True,
    "gt_rmse": True,
}


@dataclass
class Delta:
    name: str  # stage or metric name
    quantity: str  # wall_time_s | peak_rss_gb | ... | metric name
    before: float | None
    after: float | None
    delta_pct: float | None  # (after - before) / before * 100, None when undefined
    regression: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "quantity": self.quantity,
            "before": self.before,
            "after": self.after,
            "delta_pct": self.delta_pct,
            "regression": self.regression,
        }


@dataclass
class CompareReport:
    threshold: float
    rows: list[Delta] = field(default_factory=list)  # stage x quantity
    metric_rows: list[Delta] = field(default_factory=list)
    before_meta: dict[str, Any] = field(default_factory=dict)
    after_meta: dict[str, Any] = field(default_factory=dict)
    regress_on: tuple[str, ...] = ("wall_time_s",)

    @property
    def regressions(self) -> list[Delta]:
        return [r for r in self.rows if r.regression]

    @property
    def ok(self) -> bool:
        return not self.regressions

    def stage_names(self) -> list[str]:
        out: list[str] = []
        for r in self.rows:
            if r.name not in out:
                out.append(r.name)
        return out

    def row(self, name: str, quantity: str) -> Delta | None:
        return next((r for r in self.rows if r.name == name and r.quantity == quantity), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "regress_on": list(self.regress_on),
            "ok": self.ok,
            "before": self.before_meta,
            "after": self.after_meta,
            "rows": [r.to_dict() for r in self.rows],
            "metrics": [r.to_dict() for r in self.metric_rows],
            "regressions": [r.to_dict() for r in self.regressions],
        }

    # ------------------------------------------------------------------ markdown
    def to_markdown(self, lang: str | None = None) -> str:
        pct = self.threshold * 100.0
        lines = [f"### {t('bench.compare.title', lang, threshold=pct)}", ""]
        lines.append(
            "- "
            + t(
                "bench.compare.baseline",
                lang,
                site=self.before_meta.get("site", "?"),
                git_sha=_short(self.before_meta.get("git_sha")),
                created_at=self.before_meta.get("created_at", "?"),
            )
        )
        lines.append(
            "- "
            + t(
                "bench.compare.current",
                lang,
                site=self.after_meta.get("site", "?"),
                git_sha=_short(self.after_meta.get("git_sha")),
                created_at=self.after_meta.get("created_at", "?"),
            )
        )
        lines.append("")
        h = [
            t("bench.table.stage", lang),
            f"{t('bench.table.wall', lang)} {t('bench.table.before', lang)}",
            f"{t('bench.table.wall', lang)} {t('bench.table.after', lang)}",
            t("bench.table.delta", lang),
            f"{t('bench.table.rss', lang)} {t('bench.table.before', lang)}",
            f"{t('bench.table.rss', lang)} {t('bench.table.after', lang)}",
            t("bench.table.delta", lang),
            f"{t('bench.table.disk', lang)} {t('bench.table.after', lang)}",
            f"{t('bench.table.net', lang)} {t('bench.table.after', lang)}",
        ]
        lines.append("| " + " | ".join(h) + " |")
        lines.append("|" + "---|" * len(h))
        for name in self.stage_names():
            w = self.row(name, "wall_time_s")
            r = self.row(name, "peak_rss_gb")
            d = self.row(name, "disk_peak_gb")
            n = self.row(name, "network_bytes")
            flag = " **!**" if (w is not None and w.regression) else ""
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"{name}{flag}",
                        _fmt(w and w.before, 2),
                        _fmt(w and w.after, 2),
                        _pct(w and w.delta_pct),
                        _fmt(r and r.before, 3),
                        _fmt(r and r.after, 3),
                        _pct(r and r.delta_pct),
                        _fmt(d and d.after, 3),
                        _fmt(None if n is None or n.after is None else n.after / 1e6, 1),
                    ]
                )
                + " |"
            )
        if self.metric_rows:
            lines.append("")
            mh = [
                t("bench.table.metric", lang),
                t("bench.table.before", lang),
                t("bench.table.after", lang),
                t("bench.table.delta", lang),
            ]
            lines.append("| " + " | ".join(mh) + " |")
            lines.append("|" + "---|" * len(mh))
            for m in self.metric_rows:
                label = t(f"bench.metrics.{m.name}", lang)
                lines.append(
                    f"| {label} | {_fmt(m.before, 4)} | {_fmt(m.after, 4)} | {_pct(m.delta_pct)} |"
                )
        lines.append("")
        if self.ok:
            lines.append(t("bench.compare.ok", lang, threshold=pct))
        else:
            lines.append(
                t(
                    "bench.compare.regressions",
                    lang,
                    n=len(self.regressions),
                    stages=", ".join(
                        f"{r.name} (+{r.delta_pct:.0f}%)"
                        for r in self.regressions
                        if r.delta_pct is not None
                    ),
                )
            )
        return "\n".join(lines) + "\n"


def _short(sha: Any) -> str:
    return str(sha)[:8] if sha else "?"


def _fmt(v: Any, nd: int) -> str:
    if v is None or v is False:
        return "-"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def _pct(v: Any) -> str:
    if v is None or v is False:
        return "-"
    return f"{float(v):+.1f}%"


def delta_pct(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    if before == 0:
        return None if after == 0 else float("inf")
    return (after - before) / abs(before) * 100.0


def load_result(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "stages" not in data:
        msg = f"{p}: not a bench_result.json (missing 'stages')"
        raise ValueError(msg)
    return data


def _meta(d: dict[str, Any]) -> dict[str, Any]:
    site = d.get("site")
    return {
        "site": site.get("name") if isinstance(site, dict) else site,
        "git_sha": d.get("git_sha"),
        "created_at": d.get("created_at"),
        "wintersar_version": d.get("wintersar_version"),
        "repeats": d.get("repeats"),
    }


def compare(
    before: dict[str, Any] | Path | str,
    after: dict[str, Any] | Path | str,
    threshold: float = DEFAULT_THRESHOLD,
    quantities: tuple[str, ...] = QUANTITIES,
    regress_on: tuple[str, ...] = ("wall_time_s",),
) -> CompareReport:
    """Pair stages/metrics of two results; flag ``after > before * (1 + threshold)`` on
    ``regress_on`` quantities as regressions (a *decrease* is never a regression)."""
    b = before if isinstance(before, dict) else load_result(before)
    a = after if isinstance(after, dict) else load_result(after)
    bs, as_ = dict(b.get("stages", {})), dict(a.get("stages", {}))
    names = list(bs) + [s for s in as_ if s not in bs]
    if "total" in b and "total" in a and b["total"] and a["total"]:
        names.append("total")
        bs["total"], as_["total"] = b["total"], a["total"]
    report = CompareReport(
        threshold=threshold, before_meta=_meta(b), after_meta=_meta(a), regress_on=regress_on
    )
    for name in names:
        sb, sa = bs.get(name, {}), as_.get(name, {})
        for q in quantities:
            vb, va = _num(sb.get(q)), _num(sa.get(q))
            pct = delta_pct(vb, va)
            reg = (
                q in regress_on
                and vb is not None
                and va is not None
                and vb > 0
                and va > vb * (1.0 + threshold)
            )
            report.rows.append(Delta(name, q, vb, va, pct, reg))
    mb, ma = dict(b.get("metrics", {})), dict(a.get("metrics", {}))
    for m in [k for k in mb if k in ma] + [k for k in ma if k not in mb]:
        vb, va = _num(mb.get(m)), _num(ma.get(m))
        report.metric_rows.append(Delta(m, m, vb, va, delta_pct(vb, va)))
    return report


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_regression(
    before: dict[str, Any] | Path | str,
    after: dict[str, Any] | Path | str,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[bool, list[Delta]]:
    """``(ok, regressions)`` — convenience for CI scripts."""
    rep = compare(before, after, threshold=threshold)
    return rep.ok, rep.regressions
