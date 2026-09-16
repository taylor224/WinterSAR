"""Dock widget with the six panels of plan §5.8 (R-12, ADR-0070).

Layout of this module
---------------------
1. **Pure helpers** (no Qt import): the ``config.yaml`` field table (``CONFIG_FIELDS``,
   derived from ``src/wintersar/pipeline/config.py``), value parsing/formatting, YAML
   round-trip, and the row builders that turn ``--json`` payloads into table rows
   (candidates with FAIL/WARN badges, stages, findings, raster artifacts, reference-point
   candidates, validation sites). These are unit tested without QGIS.
2. **``WintersarDock``**: composes Qt widgets (imported lazily inside ``build``) around the
   helpers. CLI calls run in a worker thread; their stderr lines and the final
   :class:`CliResponse` are handed back to the GUI thread through a ``queue.Queue`` drained
   by a ``QTimer`` (no QObject subclass needed, so the module stays importable without Qt).

Verified QGIS API used inside ``build``/handlers (all cited where used):
``QgsRasterLayer(path, name)``/``isValid()``/``QgsProject.instance().addMapLayer()``,
``QgsVectorLayer(uri, name, "delimitedtext")``, ``QgsMapToolEmitPoint(canvas)`` +
``canvasClicked(point, button)``, ``QgsCoordinateTransform(src, dst, ctx)``,
``QgsMapCanvas.extent()/mapSettings().destinationCrs()/setMapTool()/unsetMapTool()``,
``QgsGeometry.fromRect()/transform()/asJson()``, ``QgsMessageBar.pushMessage()``.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .cli_client import (
    STAGE_ORDER,
    CliResponse,
    LineCallback,
    WintersarClient,
    discover_environments,
)
from .i18n import render_finding, set_lang, t
from .settings import PluginSettings

# ============================================================================= pure helpers

RASTER_KINDS: frozenset[str] = frozenset({"cog", "tif", "tiff", "geotiff", "raster", "vrt"})
RASTER_SUFFIXES: frozenset[str] = frozenset({".tif", ".tiff", ".vrt"})
BADGES: tuple[str, ...] = ("FAIL", "WARN", "OK")


class _Unset:
    """Sentinel: blank widget -> key removed from config.yaml (pydantic default applies)."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


@dataclass(frozen=True)
class FieldSpec:
    """One editable ``config.yaml`` entry (path into the YAML mapping)."""

    path: tuple[str, ...]
    kind: str
    choices: tuple[str, ...] = ()

    @property
    def section(self) -> str:
        return self.path[0]

    @property
    def dotted(self) -> str:
        return ".".join(self.path)

    @property
    def label_key(self) -> str:
        return "qgis.config.field." + self.dotted


# source: src/wintersar/pipeline/config.py (pydantic models; Literal choices copied verbatim).
# Fields without a plugin label (data.platform, selection.pixel_spacing_tolerance,
# unwrap.save_cost_file, tiles.overlap/min_overlap_px) are left untouched by the round trip.
CONFIG_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(("project", "name"), "str"),
    FieldSpec(("project", "workdir"), "path"),
    FieldSpec(("project", "language"), "choice", ("ko", "en")),
    FieldSpec(("aoi",), "required_path"),
    FieldSpec(("time_range", "start"), "date"),
    FieldSpec(("time_range", "end"), "date"),
    FieldSpec(("data", "source"), "choice", ("asf", "cdse")),
    FieldSpec(("data", "product"), "choice", ("burst", "slc")),
    FieldSpec(("data", "polarization"), "choice", ("VV", "HH", "VH", "HV")),
    FieldSpec(("data", "orbit_direction"), "choice", ("asc", "desc", "auto")),
    FieldSpec(("data", "relative_orbit"), "int_or_auto"),
    FieldSpec(("data", "credentials"), "str"),
    FieldSpec(("selection", "network"), "choice", ("sbas", "sequential", "single_reference")),
    FieldSpec(("selection", "max_perp_baseline_m"), "float"),
    FieldSpec(("selection", "max_temporal_baseline_days"), "int"),
    FieldSpec(("selection", "sequential_connections"), "int"),
    FieldSpec(("selection", "min_coverage"), "float"),
    FieldSpec(("selection", "max_layover_shadow_fraction"), "float"),
    FieldSpec(("selection", "budget_credits"), "optional_float"),
    FieldSpec(
        ("engine", "interferogram"), "choice", ("hyp3", "isce2_topsstack", "compass_isce3", "fake")
    ),
    FieldSpec(("engine", "looks"), "looks"),
    FieldSpec(("engine", "target_pixel_m"), "float"),
    FieldSpec(("engine", "filter", "type"), "choice", ("goldstein", "none")),
    FieldSpec(("engine", "filter", "alpha"), "float"),
    FieldSpec(("engine", "filter", "window"), "int"),
    FieldSpec(("engine", "esd"), "bool"),
    FieldSpec(("engine", "cleanup"), "choice", ("none", "stage", "aggressive")),
    FieldSpec(("unwrap", "method"), "choice", ("snaphu", "tophu", "spurt", "auto")),
    FieldSpec(("unwrap", "cost"), "choice", ("defo", "smooth", "topo")),
    FieldSpec(("unwrap", "init"), "choice", ("mst", "mcf")),
    FieldSpec(("unwrap", "coherence_threshold"), "float"),
    FieldSpec(("unwrap", "mask", "water"), "bool"),
    FieldSpec(("unwrap", "mask", "layover"), "bool"),
    FieldSpec(("unwrap", "mask", "coherence"), "bool"),
    FieldSpec(("unwrap", "tiles"), "tiles"),
    FieldSpec(("unwrap", "memory_mb_per_mpixel"), "float"),
    FieldSpec(("unwrap", "nproc_per_igram"), "int"),
    FieldSpec(("timeseries", "engine"), "choice", ("mintpy", "dolphin", "fake")),
    FieldSpec(("timeseries", "reference_point"), "refpoint"),
    FieldSpec(
        ("timeseries", "troposphere"), "choice", ("era5", "gacos", "height_correlation", "none")
    ),
    FieldSpec(("timeseries", "deramp"), "choice", ("linear", "quadratic", "no")),
    FieldSpec(
        ("timeseries", "unwrap_error_correction"), "choice", ("phase_closure", "bridging", "no")
    ),
    FieldSpec(("timeseries", "coherence_threshold"), "float"),
    FieldSpec(("validate", "leveling_csv"), "optional_path"),
    FieldSpec(("validate", "gnss", "source"), "choice", ("csv", "ngii")),
    FieldSpec(("validate", "gnss", "path"), "optional_path"),
    FieldSpec(("validate", "radius_m"), "float"),
    FieldSpec(("compute", "cores"), "int_or_auto"),
    FieldSpec(("compute", "memory_gb"), "float_or_auto"),
    FieldSpec(("compute", "gpu"), "bool_or_auto"),
    FieldSpec(("compute", "cache_dir"), "optional_path"),
)
CONFIG_SECTIONS: tuple[str, ...] = tuple(dict.fromkeys(f.section for f in CONFIG_FIELDS))


def get_nested(data: Mapping[str, Any], path: Sequence[str]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, Mapping) or key not in cur:
            return None
        cur = cur[key]
    return cur


def set_nested(data: dict[str, Any], path: Sequence[str], value: Any) -> None:
    cur = data
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def delete_nested(data: dict[str, Any], path: Sequence[str]) -> None:
    parent = get_nested(data, path[:-1]) if len(path) > 1 else data
    if isinstance(parent, dict):
        parent.pop(path[-1], None)


def _pair(text: str, conv: Callable[[str], Any], what: str) -> list[Any]:
    parts = [p.strip() for p in text.replace(";", ",").replace("x", ",").split(",")]
    if len(parts) != 2 or not all(parts):
        msg = f"{what}: expected two comma-separated values, got {text!r}"
        raise ValueError(msg)
    return [conv(parts[0]), conv(parts[1])]


def parse_field_value(spec: FieldSpec, raw: Any) -> Any:
    """Widget value (text or bool) -> YAML value; ``ValueError`` with a short reason."""
    kind = spec.kind
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")
    text = "" if raw is None else str(raw).strip()
    if kind == "required_path":
        if not text:
            msg = "path must not be empty"
            raise ValueError(msg)
        return text
    if kind == "optional_path":
        return text or None
    if kind == "optional_float":
        return float(text) if text else None
    if not text:
        return UNSET  # blank -> remove the key, the config default applies
    if kind in ("str", "path"):
        return text
    if kind == "choice":
        if text not in spec.choices:
            msg = f"must be one of {', '.join(spec.choices)}"
            raise ValueError(msg)
        return text
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "date":
        return date.fromisoformat(text).isoformat()
    if kind == "int_or_auto":
        return "auto" if text.lower() in ("", "auto") else int(text)
    if kind == "float_or_auto":
        return "auto" if text.lower() in ("", "auto") else float(text)
    if kind == "bool_or_auto":
        low = text.lower()
        if low in ("", "auto"):
            return "auto"
        if low in ("true", "yes", "1", "on"):
            return True
        if low in ("false", "no", "0", "off"):
            return False
        msg = "must be auto, true or false"
        raise ValueError(msg)
    if kind == "looks":
        return "auto" if text.lower() in ("", "auto") else _pair(text, int, "looks")
    if kind == "refpoint":
        low = text.lower()
        if low in ("", "auto_recommend"):
            return "auto_recommend"
        if low == "auto":
            return "auto"
        return _pair(text, float, "reference_point")
    if kind == "tiles":
        if text.lower() in ("", "auto"):
            return "auto"
        rows, cols = _pair(text.lower(), int, "tiles")
        return {"rows": rows, "cols": cols}
    msg = f"unknown field kind {kind!r}"
    raise ValueError(msg)


def format_field_value(spec: FieldSpec, value: Any) -> Any:
    """YAML value -> widget value (text, or bool for ``bool`` fields)."""
    kind = spec.kind
    if kind == "bool":
        return bool(value) if value is not None else False
    if value is None:
        return ""
    if kind in ("looks", "refpoint"):
        if isinstance(value, list | tuple):
            return ",".join(str(v) for v in value)
        return str(value)
    if kind == "tiles":
        if isinstance(value, Mapping):
            return f"{value.get('rows', '?')}x{value.get('cols', '?')}"
        return str(value)
    if kind == "bool_or_auto" and isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def config_field_by_dotted(dotted: str) -> FieldSpec:
    for f in CONFIG_FIELDS:
        if f.dotted == dotted:
            return f
    msg = f"unknown config field {dotted!r}"
    raise KeyError(msg)


def apply_form_values(
    data: dict[str, Any], values: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Apply ``{dotted: widget value}`` to a config mapping; returns (data, errors)."""
    errors: dict[str, str] = {}
    for dotted, raw in values.items():
        spec = config_field_by_dotted(dotted)
        try:
            parsed = parse_field_value(spec, raw)
        except (ValueError, TypeError) as exc:
            errors[dotted] = str(exc)
            continue
        if parsed is UNSET or (parsed is None and spec.kind in ("optional_float", "optional_path")):
            delete_nested(data, spec.path)
            if spec.path[:-1] and get_nested(data, spec.path[:-1]) == {}:
                delete_nested(data, spec.path[:-1])
            continue
        set_nested(data, spec.path, parsed)
    return data, errors


def read_config_file(path: str | Path) -> dict[str, Any]:
    """YAML -> mapping (``ImportError`` when PyYAML is missing in this Python)."""
    import yaml

    with Path(path).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        msg = "config.yaml top level must be a mapping"
        raise ValueError(msg)
    return data


def write_config_file(data: Mapping[str, Any], path: str | Path) -> Path:
    import yaml

    p = Path(path)
    p.write_text(yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def resolve_workdir(config_path: str | Path, data: Mapping[str, Any]) -> Path:
    """``project.workdir`` resolved against the config file directory (as ``load_config``)."""
    wd = get_nested(data, ("project", "workdir")) or "./work"
    p = Path(str(wd))
    return p if p.is_absolute() else (Path(config_path).resolve().parent / p).resolve()


# ---------------------------------------------------------------- candidates (panel 1)


def _pair_key(ref: str, sec: str) -> str:
    return f"{ref.replace('-', '')}_{sec.replace('-', '')}"


def badge_for_stack(
    stack_id: str, findings: Sequence[Mapping[str, Any]], pair_keys: Sequence[str] = ()
) -> str:
    """FAIL / WARN / OK from the findings whose scope is this stack (or one of its pairs).

    Scopes are ``stack_id``, ``"<stack_id>:<...>"``, a pair key ``YYYYMMDD_YYYYMMDD`` (matched
    through ``pair_keys``) or ``None`` (global -> counts for every stack).
    """
    worst = "OK"
    keys = set(pair_keys)
    for f in findings:
        scope = f.get("scope")
        if scope is not None and not (
            scope == stack_id or str(scope).startswith(stack_id + ":") or scope in keys
        ):
            continue
        sev = f.get("severity")
        if sev == "FAIL":
            return "FAIL"
        if sev == "WARN":
            worst = "WARN"
    return worst


def candidate_rows(precheck_data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Rows of the candidate table from the ``precheck`` envelope data.

    # source: src/wintersar/select/report.py precheck_payload (candidate_table/stack_summary
    #   keys: stack_id, relative_orbit, flight_direction, polarization, n_dates, coverage_of_aoi,
    #   n_pairs, first_date, last_date; findings; recommended)
    """
    if not precheck_data:
        return []
    findings = [f for f in precheck_data.get("findings") or [] if isinstance(f, Mapping)]
    recommended = precheck_data.get("recommended")
    pairs_by_stack: dict[str, list[str]] = {}
    for c in precheck_data.get("candidates") or []:
        if not isinstance(c, Mapping):
            continue
        sid = _stack_id_of(c)
        pairs_by_stack[sid] = [
            _pair_key(str(p.get("reference")), str(p.get("secondary")))
            for p in c.get("pairs") or []
            if isinstance(p, Mapping)
        ]
    rows: list[dict[str, Any]] = []
    table = precheck_data.get("candidate_table") or [
        _summary_from_candidate(c) for c in precheck_data.get("candidates") or []
    ]
    for s in table:
        if not isinstance(s, Mapping):
            continue
        sid = str(s.get("stack_id"))
        rows.append(
            {
                "stack_id": sid,
                "track": s.get("relative_orbit"),
                "direction": s.get("flight_direction"),
                "polarization": s.get("polarization"),
                "n_dates": s.get("n_dates"),
                "n_pairs": s.get("n_pairs"),
                "coverage": s.get("coverage_of_aoi"),
                "first_date": s.get("first_date"),
                "last_date": s.get("last_date"),
                "badge": badge_for_stack(sid, findings, pairs_by_stack.get(sid, ())),
                "recommended": sid == recommended,
            }
        )
    return rows


def _stack_id_of(c: Mapping[str, Any]) -> str:
    # source: src/wintersar/io/schemas.py StackCandidate.stack_id  (T{orbit:03d}{A|D}_{pol})
    d = "A" if str(c.get("flight_direction")) == "ASCENDING" else "D"
    return f"T{int(c.get('relative_orbit') or 0):03d}{d}_{c.get('polarization')}"


def _summary_from_candidate(c: Any) -> dict[str, Any]:
    if not isinstance(c, Mapping):
        return {}
    dates = list(c.get("dates") or [])
    return {
        "stack_id": _stack_id_of(c),
        "relative_orbit": c.get("relative_orbit"),
        "flight_direction": c.get("flight_direction"),
        "polarization": c.get("polarization"),
        "n_dates": len(dates),
        "n_pairs": len(c.get("pairs") or []),
        "coverage_of_aoi": c.get("coverage_of_aoi"),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
    }


def findings_rows(
    findings: Sequence[Mapping[str, Any]], lang: str | None = None
) -> list[dict[str, str]]:
    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    rows: list[dict[str, str]] = []
    for f in sorted(
        findings, key=lambda x: (order.get(str(x.get("severity")), 3), str(x.get("rule_id")))
    ):
        sev, rid, cause, fix = render_finding(dict(f), lang)
        scope = f.get("scope")
        rows.append(
            {
                "severity": str(f.get("severity", "INFO")),
                "severity_label": sev,
                "rule_id": rid + (f" [{scope}]" if scope else ""),
                "cause": cause,
                "fix": fix,
            }
        )
    return rows


def findings_summary(findings: Sequence[Mapping[str, Any]], lang: str | None = None) -> str:
    n = {s: sum(1 for f in findings if f.get("severity") == s) for s in ("FAIL", "WARN", "INFO")}
    return t(
        "qgis.status.findings_summary", lang, n_fail=n["FAIL"], n_warn=n["WARN"], n_info=n["INFO"]
    )


# ---------------------------------------------------------------- run / plan (panel 3)


def stage_rows(run_data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Stage table rows from ``run``/``plan`` data.

    # source: src/wintersar/pipeline/api.py RunResult.to_dict ("records": StageRecord dumps)
    #   and src/wintersar/io/schemas.py Plan ("stages"); StageRecord.extra.cache_hit
    """
    if not run_data:
        return []
    records = run_data.get("records")
    if records is None:
        records = run_data.get("stages") or []
    rows: list[dict[str, Any]] = []
    for r in records:
        if not isinstance(r, Mapping):
            continue
        extra = r.get("extra") or {}
        status = str(r.get("status", "pending"))
        if isinstance(extra, Mapping) and extra.get("cache_hit"):
            status = "cached"
        res = r.get("resources") or {}
        wall = res.get("wall_time_s") if isinstance(res, Mapping) else None
        rows.append(
            {
                "stage": str(r.get("stage")),
                "engine": r.get("engine") or "-",
                "status": status,
                "wall_s": wall,
                "log_path": r.get("log_path"),
            }
        )
    return rows


def failed_log_dir(run_data: Mapping[str, Any] | None) -> str | None:
    """Log directory of the failed stage (input for ``wintersar diagnose``)."""
    for row in stage_rows(run_data):
        if row["status"] == "failed" and row.get("log_path"):
            p = Path(str(row["log_path"]))
            return str(p if p.suffix == "" else p.parent)
    return None


def _fmt(value: Any, unit: str, digits: int = 1) -> str:
    if value is None:
        return t("qgis.run.unknown")
    try:
        return f"{float(value):.{digits}f} {unit}"
    except (TypeError, ValueError):
        return str(value)


def resources_line(plan_or_run: Mapping[str, Any] | None) -> str:
    """``Estimate: wall … · memory … · disk … · credits …`` from ``Plan.resources``."""
    if not plan_or_run:
        return ""
    res = plan_or_run.get("resources")
    if res is None and isinstance(plan_or_run.get("plan"), Mapping):
        res = plan_or_run["plan"].get("resources")
    if not isinstance(res, Mapping):
        return ""
    wall = res.get("wall_time_s")
    wall_txt = t("qgis.run.unknown") if wall is None else f"{float(wall) / 60:.1f} min"
    credits = res.get("credits")
    return t(
        "qgis.run.resources",
        wall=wall_txt,
        mem=_fmt(res.get("peak_rss_gb"), "GB"),
        disk=_fmt(res.get("disk_gb"), "GB"),
        credits=t("qgis.run.unknown") if credits is None else f"{float(credits):.0f}",
    )


# ---------------------------------------------------------------- results (panel 4)


def is_raster_artifact(artifact: Mapping[str, Any]) -> bool:
    kind = str(artifact.get("kind") or "").lower()
    suffix = Path(str(artifact.get("path") or "")).suffix.lower()
    return kind in RASTER_KINDS or suffix in RASTER_SUFFIXES


def artifact_rows(run_data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """All artifacts of a run (``RunResult.to_dict()["artifacts"]``), rasters flagged."""
    if not run_data:
        return []
    arts = run_data.get("artifacts") or {}
    rows: list[dict[str, Any]] = []
    items = arts.items() if isinstance(arts, Mapping) else []
    for name, a in items:
        if not isinstance(a, Mapping):
            continue
        rows.append(
            {
                "name": str(a.get("name") or name),
                "path": str(a.get("path") or ""),
                "kind": str(a.get("kind") or "file"),
                "raster": is_raster_artifact(a),
            }
        )
    return rows


def raster_artifacts(run_data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [r for r in artifact_rows(run_data) if r["raster"]]


def latest_run_summary(workdir: str | Path) -> dict[str, Any] | None:
    """Newest ``work/runs/<run_id>.json`` (written by ``wintersar run``), or ``None``.

    # source: src/wintersar/pipeline/api.py _write_run_summary / RUNS_DIRNAME = "runs"
    """
    runs = Path(workdir) / "runs"
    if not runs.is_dir():
        return None
    files = sorted(runs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data.setdefault("summary_path", str(f))
            return data
    return None


# ---------------------------------------------------------------- reference point (panel 5)


def refpoint_rows(data: Mapping[str, Any] | Sequence[Any] | None) -> list[dict[str, Any]]:
    """Rank/lat/lon/score/coherence rows from ``wintersar refpoint`` data.

    # source: src/wintersar/validate/cli.py refpoint_cmd -> data["candidates"] =
    #   [RefPointCandidate.to_dict()] (row, col, lat, lon, score, coherence, components)
    """
    if data is None:
        return []
    items: Any = data
    if isinstance(data, Mapping):
        items = data.get("candidates")
        if items is None:
            items = data.get("refpoints", data.get("top", []))
    rows: list[dict[str, Any]] = []
    for i, c in enumerate(items or [], start=1):
        if not isinstance(c, Mapping) or c.get("lat") is None or c.get("lon") is None:
            continue
        rows.append(
            {
                "rank": i,
                "lat": float(c["lat"]),
                "lon": float(c["lon"]),
                "score": c.get("score"),
                "coherence": c.get("coherence"),
                "row": c.get("row"),
                "col": c.get("col"),
            }
        )
    return rows


# ---------------------------------------------------------------- validation (panel 6)


def validation_rows(data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Per-site RMSE/bias rows (mm) from ``wintersar validate`` data.

    # source: src/wintersar/validate/metrics.py ComparisonResult.to_dict ("per_site":
    #   SiteComparison.to_dict -> site_id, method, n, rmse_m, bias_m, dates, insar_m, gt_los_m)
    """
    if not data:
        return []
    sites = data.get("per_site")
    if sites is None and isinstance(data.get("comparison"), Mapping):
        sites = data["comparison"].get("per_site")
    if sites is None:
        sites = data.get("sites", [])
    rows: list[dict[str, Any]] = []
    for s in sites or []:
        if not isinstance(s, Mapping):
            continue
        rows.append(
            {
                "site_id": str(s.get("site_id")),
                "method": str(s.get("method", "")),
                "n": s.get("n"),
                "rmse_mm": _m_to_mm(s.get("rmse_m", s.get("rmse_mm")), "rmse_m" not in s),
                "bias_mm": _m_to_mm(s.get("bias_m", s.get("bias_mm")), "bias_m" not in s),
                "lat": s.get("lat"),
                "lon": s.get("lon"),
                "dates": list(s.get("dates") or []),
                "insar_mm": [
                    _m_to_mm(v, "insar_m" not in s)
                    for v in (s.get("insar_m") or s.get("insar_mm") or [])
                ],
                "gt_mm": [
                    _m_to_mm(v, "gt_los_m" not in s)
                    for v in (s.get("gt_los_m") or s.get("gt_mm") or [])
                ],
            }
        )
    return rows


def _m_to_mm(value: Any, already_mm: bool = False) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if already_mm else v * 1000.0


def ground_truth_layer_uri(csv_path: str | Path, x_field: str = "lon", y_field: str = "lat") -> str:
    """Delimited-text provider URI for the ground-truth CSV (plan §5.6 schema: lat, lon).

    # source: https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/loadlayer.html
    #   uri = "file:///.../file.csv?delimiter=,&crs=epsg:4326&xField=..&yField=.."
    #   QgsVectorLayer(uri, name, "delimitedtext")
    """
    return f"{Path(csv_path).resolve().as_uri()}?delimiter=,&crs=epsg:4326&xField={x_field}&yField={y_field}"


def geojson_feature_collection(geometries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": dict(g)} for g in geometries
        ],
    }


def bbox_polygon(xmin: float, ymin: float, xmax: float, ymax: float) -> dict[str, Any]:
    return {
        "type": "Polygon",
        "coordinates": [[[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax], [xmin, ymin]]],
    }


# ============================================================================= Qt dock


JobFn = Callable[[LineCallback], CliResponse]
DoneFn = Callable[[CliResponse], None]


class WintersarDock:
    """Builds and drives the dock widget. Only ``build()`` and the handlers touch Qt."""

    POLL_MS = 100

    def __init__(
        self,
        iface: Any,
        client_factory: Callable[[], WintersarClient],
        settings: PluginSettings,
        on_settings_changed: Callable[[PluginSettings], None] | None = None,
    ) -> None:
        self.iface = iface
        self.client_factory = client_factory
        self.settings = settings
        self.on_settings_changed = on_settings_changed
        self.widget: Any = None
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._client: WintersarClient | None = None
        self._timer: Any = None
        self._map_tool: Any = None
        self._prev_map_tool: Any = None
        # last payloads (also useful for tests through the pure helpers)
        self.last_precheck: dict[str, Any] | None = None
        self.last_run: dict[str, Any] | None = None
        self.last_refpoints: list[dict[str, Any]] = []
        self.last_validation: list[dict[str, Any]] = []
        self.config_data: dict[str, Any] = {}
        self.w: dict[str, Any] = {}  # named widgets
        self.form: dict[str, Any] = {}  # dotted field -> widget

    # ------------------------------------------------------------------ build
    def build(self) -> Any:
        from qgis.PyQt.QtCore import QTimer
        from qgis.PyQt.QtWidgets import (
            QDockWidget,
            QScrollArea,
            QToolBox,
            QVBoxLayout,
            QWidget,
        )

        dock = QDockWidget(t("qgis.plugin.dock_title"), self.iface.mainWindow())
        dock.setObjectName("WintersarDock")
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self._build_env_box())
        toolbox = QToolBox()
        toolbox.addItem(self._build_aoi_panel(), t("qgis.aoi.title"))
        toolbox.addItem(self._build_config_panel(), t("qgis.config.title"))
        toolbox.addItem(self._build_run_panel(), t("qgis.run.title"))
        toolbox.addItem(self._build_results_panel(), t("qgis.results.title"))
        toolbox.addItem(self._build_refpoint_panel(), t("qgis.refpoint.title"))
        toolbox.addItem(self._build_validate_panel(), t("qgis.validate.title"))
        layout.addWidget(toolbox)
        self.w["toolbox"] = toolbox
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        dock.setWidget(scroll)
        self._timer = QTimer(dock)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._drain_queue)
        self._timer.start()
        self.widget = dock
        if self.settings.last_config:
            self.w["config_path"].setText(self.settings.last_config)
            self._load_config(silent=True)
        return dock

    def shutdown(self) -> None:
        if self._client is not None:
            self._client.cancel()
        if self._timer is not None:
            self._timer.stop()
        self._release_map_tool()

    # ------------------------------------------------------------------ small Qt helpers
    def _line(self, key: str, text: str = "", placeholder: str = "") -> Any:
        from qgis.PyQt.QtWidgets import QLineEdit

        e = QLineEdit(text)
        if placeholder:
            e.setPlaceholderText(placeholder)
        self.w[key] = e
        return e

    def _button(self, label: str, handler: Callable[[], None]) -> Any:
        from qgis.PyQt.QtWidgets import QPushButton

        b = QPushButton(label)
        b.clicked.connect(lambda *_: handler())
        return b

    def _browse_row(self, key: str, text: str = "", *, mode: str = "open", filt: str = "") -> Any:
        from qgis.PyQt.QtWidgets import QFileDialog, QHBoxLayout, QWidget

        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        edit = self._line(key, text)
        lay.addWidget(edit)

        def browse() -> None:
            start = edit.text() or ""
            if mode == "dir":
                chosen = QFileDialog.getExistingDirectory(row, t("qgis.aoi.browse"), start)
            elif mode == "save":
                chosen, _ = QFileDialog.getSaveFileName(row, t("qgis.aoi.browse"), start, filt)
            else:
                chosen, _ = QFileDialog.getOpenFileName(row, t("qgis.aoi.browse"), start, filt)
            if chosen:
                edit.setText(chosen)

        lay.addWidget(self._button(t("qgis.aoi.browse"), browse))
        return row

    def _table(self, key: str, headers: Sequence[str]) -> Any:
        from qgis.PyQt.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget

        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(list(headers))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        self.w[key] = table
        return table

    @staticmethod
    def _fill_table(
        table: Any,
        rows: Sequence[Sequence[Any]],
        colors: Mapping[int, Mapping[str, str]] | None = None,
    ) -> None:
        from qgis.PyQt.QtGui import QColor
        from qgis.PyQt.QtWidgets import QTableWidgetItem

        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem("" if value is None else str(value))
                if colors and c in colors:
                    color = colors[c].get(str(value))
                    if color:
                        item.setBackground(QColor(color))
                table.setItem(r, c, item)

    def _message(self, text: str, level: str = "info", title: str = "wintersar") -> None:
        try:
            from qgis.core import Qgis

            # source: https://qgis.org/pyqgis/3.44/gui/QgsMessageBar.html
            #   pushMessage(title, text, level: Qgis.MessageLevel = Info, duration: int = -1)
            levels = {
                "info": Qgis.MessageLevel.Info,
                "warning": Qgis.MessageLevel.Warning,
                "critical": Qgis.MessageLevel.Critical,
                "success": Qgis.MessageLevel.Success,
            }
            self.iface.messageBar().pushMessage(
                title, text, levels.get(level, Qgis.MessageLevel.Info), 8
            )
        except Exception:
            self._append_log(f"[{level}] {text}")

    def _append_log(self, line: str) -> None:
        log = self.w.get("log")
        if log is not None:
            log.appendPlainText(line)

    # ------------------------------------------------------------------ jobs
    def _start_job(self, label: str, fn: JobFn, on_done: DoneFn) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._message(t("qgis.status.running", command=label), "warning")
            return
        self.w["status"].setText(t("qgis.status.running", command=label))
        self.w["progress"].setRange(0, 0)
        self._append_log(f"$ wintersar {label}")

        def worker() -> None:
            try:
                resp = fn(lambda line: self._queue.put(("line", line)))
            except Exception as exc:  # defensive: never leave the GUI in "running"
                resp = CliResponse.failure("CLI_NOT_FOUND", label, detail=str(exc))
            self._queue.put(("done", (label, resp, on_done)))

        self._thread = threading.Thread(target=worker, name="wintersar-cli", daemon=True)
        self._thread.start()

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            if kind == "line":
                self._append_log(str(payload))
                continue
            label, resp, on_done = payload
            self.w["progress"].setRange(0, 1)
            self.w["progress"].setValue(1 if resp.ok else 0)
            self.w["status"].setText(
                t("qgis.status.done" if resp.ok else "qgis.status.failed", command=label)
            )
            if resp.raw_stderr and not resp.ok:
                self._append_log(resp.raw_stderr[-4000:])
            self._show_findings(self.w["run_findings"], resp.findings)
            try:
                on_done(resp)
            except Exception as exc:  # keep the dock alive
                self._message(str(exc), "critical")

    def _client_call(self, method: str, *args: Any, **kwargs: Any) -> JobFn:
        def job(on_line: LineCallback) -> CliResponse:
            self._client = self.client_factory()
            fn = getattr(self._client, method)
            return fn(*args, on_line=on_line, **kwargs)  # type: ignore[no-any-return]

        return job

    def _cancel(self) -> None:
        if self._client is not None and self._client.cancel():
            self._append_log(t("qgis.run.cancel"))

    def _show_findings(self, table: Any, findings: Sequence[Mapping[str, Any]]) -> None:
        rows = findings_rows(findings, self.settings.lang)
        self._fill_table(
            table,
            [(r["severity_label"], r["rule_id"], r["cause"], r["fix"]) for r in rows],
            colors={
                0: {t("common.severity.FAIL"): "#f8d7da", t("common.severity.WARN"): "#fff3cd"}
            },
        )

    # ------------------------------------------------------------------ env box
    def _build_env_box(self) -> Any:
        from qgis.PyQt.QtWidgets import (
            QComboBox,
            QDoubleSpinBox,
            QFormLayout,
            QGroupBox,
            QHBoxLayout,
            QLabel,
        )

        box = QGroupBox(t("qgis.env.title"))
        form = QFormLayout(box)
        form.addRow(
            t("qgis.env.python_exe"), self._browse_row("python_exe", self.settings.python_exe or "")
        )
        form.addRow(
            t("qgis.env.env_hint"),
            self._line("env_hint", self.settings.env_hint or "", t("qgis.env.env_hint_help")),
        )
        envs = QComboBox()
        envs.addItem("")
        for cand in discover_environments():
            envs.addItem(f"{cand.label} — {cand.python_exe}", cand.python_exe)
        envs.currentIndexChanged.connect(
            lambda i: self.w["python_exe"].setText(envs.itemData(i) or "") if i > 0 else None
        )
        form.addRow(t("qgis.env.candidates"), envs)
        lang = QComboBox()
        lang.addItems(["ko", "en"])
        lang.setCurrentText(self.settings.lang)
        self.w["lang"] = lang
        form.addRow(t("qgis.env.lang"), lang)
        timeout = QDoubleSpinBox()
        timeout.setRange(10.0, 7 * 24 * 3600.0)
        timeout.setDecimals(0)
        timeout.setValue(self.settings.timeout_s)
        self.w["timeout"] = timeout
        form.addRow(t("qgis.env.timeout"), timeout)
        resolved = QLabel("")
        resolved.setWordWrap(True)
        self.w["resolved"] = resolved
        form.addRow(resolved)
        buttons = QHBoxLayout()
        buttons.addWidget(self._button(t("qgis.env.save"), self._save_settings))
        buttons.addWidget(self._button(t("qgis.env.check_install"), self._check_install))
        form.addRow(buttons)
        self._refresh_resolved()
        return box

    def _collect_settings(self) -> PluginSettings:
        s = self.settings
        s.python_exe = self.w["python_exe"].text().strip() or None
        s.env_hint = self.w["env_hint"].text().strip() or None
        s.lang = self.w["lang"].currentText()
        s.timeout_s = float(self.w["timeout"].value())
        for key, attr in (
            ("config_path", "last_config"),
            ("aoi_path", "last_aoi"),
            ("candidates_path", "last_candidates"),
            ("refpoint_ts", "last_refpoint_ts"),
            ("ts_file", "last_ts_file"),
            ("leveling_csv", "last_leveling"),
            ("gnss_csv", "last_gnss"),
        ):
            if key in self.w:
                setattr(s, attr, self.w[key].text().strip() or None)
        return s

    def _save_settings(self) -> None:
        s = self._collect_settings()
        set_lang(s.lang)
        if self.on_settings_changed is not None:
            self.on_settings_changed(s)
        self._refresh_resolved()
        self._message(t("qgis.env.saved", path="settings"), "success")

    def _refresh_resolved(self) -> None:
        try:
            self._collect_settings()
            cmd = self.client_factory().resolved_command()
        except ValueError as exc:  # bad env hint
            self.w["resolved"].setText(str(exc))
            return
        self.w["resolved"].setText(
            t("qgis.env.resolved", command=cmd) if cmd else t("qgis.env.not_resolved")
        )

    def _check_install(self) -> None:
        self._save_settings()

        def done(resp: CliResponse) -> None:
            data = resp.data or {}
            if isinstance(data, Mapping) and data.get("engines"):
                lines = [
                    f"{e.get('engine')}: {e.get('version') or '-'} ({'ok' if e.get('available') else 'missing'})"
                    for e in data["engines"]
                ]
                self._append_log("\n".join(lines))

        self._start_job(
            "check-install", lambda on_line: self.client_factory().check_install(), done
        )

    # ------------------------------------------------------------------ panel 1: AOI / search
    def _build_aoi_panel(self) -> Any:
        from qgis.PyQt.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

        panel = QWidget()
        lay = QVBoxLayout(panel)
        form = QFormLayout()
        form.addRow(
            t("qgis.aoi.aoi_path"),
            self._browse_row(
                "aoi_path", self.settings.last_aoi or "", filt="GeoJSON (*.geojson *.json)"
            ),
        )
        row = QHBoxLayout()
        row.addWidget(self._button(t("qgis.aoi.from_extent"), self._aoi_from_extent))
        row.addWidget(self._button(t("qgis.aoi.from_layer"), self._aoi_from_layer))
        form.addRow(row)
        form.addRow(
            t("qgis.aoi.candidates_path"),
            self._browse_row(
                "candidates_path", self.settings.last_candidates or "", filt="JSON (*.json)"
            ),
        )
        lay.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addWidget(self._button(t("qgis.aoi.search"), self._search))
        buttons.addWidget(self._button(t("qgis.aoi.precheck"), self._precheck))
        lay.addLayout(buttons)
        lay.addWidget(
            self._table(
                "candidates",
                [
                    t("qgis.aoi.col_stack"),
                    t("qgis.aoi.col_track"),
                    t("qgis.aoi.col_direction"),
                    t("qgis.aoi.col_pol"),
                    t("qgis.aoi.col_dates"),
                    t("qgis.aoi.col_coverage"),
                    t("qgis.aoi.col_badge"),
                ],
            )
        )
        rec = QLabel("")
        rec.setWordWrap(True)
        self.w["recommended"] = rec
        lay.addWidget(rec)
        lay.addWidget(
            self._table(
                "aoi_findings",
                [
                    t("qgis.run.col_severity"),
                    t("qgis.run.col_rule"),
                    t("qgis.run.col_cause"),
                    t("qgis.run.col_fix"),
                ],
            )
        )
        return panel

    def _config_path(self) -> str | None:
        p = str(self.w["config_path"].text()).strip()
        if not p:
            self._message(t("qgis.run.no_config"), "warning")
            return None
        return p

    def _write_aoi(self, geometries: Sequence[Mapping[str, Any]]) -> None:
        from qgis.PyQt.QtWidgets import QFileDialog

        start = self.w["aoi_path"].text() or "aoi.geojson"
        chosen, _ = QFileDialog.getSaveFileName(
            self.widget, t("qgis.aoi.aoi_path"), start, "GeoJSON (*.geojson)"
        )
        if not chosen:
            return
        Path(chosen).write_text(
            json.dumps(geojson_feature_collection(geometries)), encoding="utf-8"
        )
        self.w["aoi_path"].setText(chosen)
        if "aoi" in self.form:
            self.form["aoi"].setText(chosen)
        self._message(t("qgis.aoi.aoi_written", path=chosen), "success")

    def _to_wgs84(self, src_crs: Any) -> Any:
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject

        # source: https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/crs.html
        #   QgsCoordinateTransform(crsSrc, crsDest, QgsProject.instance().transformContext())
        return QgsCoordinateTransform(
            src_crs,
            QgsCoordinateReferenceSystem("EPSG:4326"),
            QgsProject.instance().transformContext(),
        )

    def _aoi_from_extent(self) -> None:
        from qgis.core import QgsGeometry

        # source: https://qgis.org/pyqgis/3.44/gui/QgsMapCanvas.html extent(), mapSettings()
        # source: https://qgis.org/pyqgis/3.44/core/QgsGeometry.html fromRect(), transform(), asJson()
        canvas = self.iface.mapCanvas()
        geom = QgsGeometry.fromRect(canvas.extent())
        geom.transform(self._to_wgs84(canvas.mapSettings().destinationCrs()))
        self._write_aoi([json.loads(geom.asJson())])

    def _aoi_from_layer(self) -> None:
        layer = self.iface.activeLayer()
        if layer is None or not hasattr(layer, "getFeatures"):
            self._message(t("qgis.aoi.no_active_layer"), "warning")
            return
        xform = self._to_wgs84(layer.crs())
        geoms: list[dict[str, Any]] = []
        for feat in layer.getFeatures():
            g = feat.geometry()
            if g is None or g.isEmpty():
                continue
            g.transform(xform)
            geoms.append(json.loads(g.asJson()))
        if not geoms:
            self._message(t("qgis.aoi.no_active_layer"), "warning")
            return
        self._write_aoi(geoms)

    def _search(self) -> None:
        cfg = self._config_path()
        if cfg is None:
            return

        def done(resp: CliResponse) -> None:
            self._show_findings(self.w["aoi_findings"], resp.findings)
            if isinstance(resp.data, Mapping) and resp.data.get("path"):
                self.w["candidates_path"].setText(str(resp.data["path"]))
                self._message(
                    t(
                        "qgis.aoi.n_records",
                        n=resp.data.get("n_records", 0),
                        product_type=resp.data.get("product_type", "?"),
                        path=resp.data["path"],
                    ),
                    "success" if resp.ok else "warning",
                )

        self._start_job("search", self._client_call("search", cfg), done)

    def _precheck(self) -> None:
        cfg = self._config_path()
        cand = self.w["candidates_path"].text().strip()
        if cfg is None or not cand:
            return

        def done(resp: CliResponse) -> None:
            self.last_precheck = resp.data if isinstance(resp.data, dict) else None
            self._show_findings(self.w["aoi_findings"], resp.findings)
            rows = candidate_rows(self.last_precheck)
            self._fill_table(
                self.w["candidates"],
                [
                    (
                        r["stack_id"] + (" *" if r["recommended"] else ""),
                        r["track"],
                        r["direction"],
                        r["polarization"],
                        r["n_dates"],
                        "" if r["coverage"] is None else f"{float(r['coverage']):.0%}",
                        t(f"qgis.badge.{r['badge'].lower()}"),
                    )
                    for r in rows
                ],
                colors={
                    6: {
                        t("qgis.badge.fail"): "#f8d7da",
                        t("qgis.badge.warn"): "#fff3cd",
                        t("qgis.badge.ok"): "#d4edda",
                    }
                },
            )
            rec = (self.last_precheck or {}).get("recommended")
            self.w["recommended"].setText(
                t("qgis.aoi.recommended", stack_id=rec) if rec else t("qgis.aoi.no_recommendation")
            )

        self._start_job("precheck", self._client_call("precheck", cand, cfg, no_fail=True), done)

    # ------------------------------------------------------------------ panel 2: config form
    def _build_config_panel(self) -> Any:
        from qgis.PyQt.QtWidgets import (
            QCheckBox,
            QComboBox,
            QFormLayout,
            QGroupBox,
            QHBoxLayout,
            QLineEdit,
            QVBoxLayout,
            QWidget,
        )

        panel = QWidget()
        lay = QVBoxLayout(panel)
        top = QFormLayout()
        top.addRow(
            t("qgis.config.path"),
            self._browse_row(
                "config_path", self.settings.last_config or "", filt="YAML (*.yaml *.yml)"
            ),
        )
        lay.addLayout(top)
        buttons = QHBoxLayout()
        buttons.addWidget(self._button(t("qgis.config.new"), self._new_config))
        buttons.addWidget(self._button(t("qgis.config.open"), self._load_config))
        buttons.addWidget(self._button(t("qgis.config.save"), self._save_config))
        lay.addLayout(buttons)
        for section in CONFIG_SECTIONS:
            box = QGroupBox(t(f"qgis.config.section.{section}"))
            form = QFormLayout(box)
            for spec in (f for f in CONFIG_FIELDS if f.section == section):
                widget: Any
                if spec.kind == "bool":
                    widget = QCheckBox()
                elif spec.kind == "choice":
                    widget = QComboBox()
                    widget.addItems(["", *spec.choices])  # "" = keep the config default
                else:
                    widget = QLineEdit()
                self.form[spec.dotted] = widget
                form.addRow(t(spec.label_key), widget)
            lay.addWidget(box)
        return panel

    def _form_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for dotted, widget in self.form.items():
            spec = config_field_by_dotted(dotted)
            if spec.kind == "bool":
                values[dotted] = bool(widget.isChecked())
            elif spec.kind == "choice":
                values[dotted] = widget.currentText()
            else:
                values[dotted] = widget.text()
        return values

    def _set_form(self, data: Mapping[str, Any]) -> None:
        for dotted, widget in self.form.items():
            spec = config_field_by_dotted(dotted)
            value = format_field_value(spec, get_nested(data, spec.path))
            if spec.kind == "bool":
                widget.setChecked(bool(value))
            elif spec.kind == "choice":
                widget.setCurrentText(str(value) if value in spec.choices else "")
            else:
                widget.setText(str(value))

    def _new_config(self) -> None:
        from qgis.PyQt.QtWidgets import QFileDialog

        chosen, _ = QFileDialog.getSaveFileName(
            self.widget,
            t("qgis.config.new"),
            self.w["config_path"].text() or "config.yaml",
            "YAML (*.yaml)",
        )
        if not chosen:
            return

        def done(resp: CliResponse) -> None:
            if resp.ok:
                self.w["config_path"].setText(chosen)
                self._load_config()

        self._start_job(
            "init", lambda on_line: self.client_factory().init_config(chosen, force=True), done
        )

    def _load_config(self, silent: bool = False) -> None:
        path = self.w["config_path"].text().strip()
        if not path or not Path(path).exists():
            if not silent:
                self._message(t("qgis.run.no_config"), "warning")
            return
        try:
            self.config_data = read_config_file(path)
        except ImportError:
            self._message(t("qgis.config.yaml_missing"), "warning")
            return
        except (OSError, ValueError) as exc:
            self._message(t("qgis.config.invalid", field=path, error=str(exc)), "critical")
            return
        self._set_form(self.config_data)
        self.settings.remember_config(path)
        if not silent:
            self._message(t("qgis.config.loaded", path=path), "success")

    def _save_config(self) -> None:
        path = self._config_path()
        if path is None:
            return
        data, errors = apply_form_values(dict(self.config_data), self._form_values())
        if errors:
            field_name, err = next(iter(errors.items()))
            self._message(t("qgis.config.invalid", field=field_name, error=err), "critical")
            return
        try:
            write_config_file(data, path)
        except ImportError:
            self._message(t("qgis.config.yaml_missing"), "warning")
            return
        self.config_data = data
        self.settings.remember_config(path)
        self._message(t("qgis.config.saved", path=path), "success")

    # ------------------------------------------------------------------ panel 3: run
    def _build_run_panel(self) -> Any:
        from qgis.PyQt.QtWidgets import (
            QComboBox,
            QFormLayout,
            QHBoxLayout,
            QLabel,
            QPlainTextEdit,
            QProgressBar,
            QVBoxLayout,
            QWidget,
        )

        panel = QWidget()
        lay = QVBoxLayout(panel)
        form = QFormLayout()
        for key, label in (
            ("until", "qgis.run.until"),
            ("from", "qgis.run.from_stage"),
            ("force", "qgis.run.force"),
        ):
            combo = QComboBox()
            combo.addItem(t("qgis.run.any_stage"), "")
            for s in STAGE_ORDER:
                combo.addItem(s, s)
            self.w[f"stage_{key}"] = combo
            form.addRow(t(label), combo)
        lay.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addWidget(self._button(t("qgis.run.plan"), self._plan))
        buttons.addWidget(self._button(t("qgis.run.run"), self._run))
        buttons.addWidget(self._button(t("qgis.run.cancel"), self._cancel))
        buttons.addWidget(self._button(t("qgis.run.diagnose"), self._diagnose))
        lay.addLayout(buttons)
        status = QLabel(t("qgis.status.idle"))
        self.w["status"] = status
        lay.addWidget(status)
        progress = QProgressBar()
        progress.setRange(0, 1)
        progress.setValue(0)
        self.w["progress"] = progress
        lay.addWidget(progress)
        resources = QLabel("")
        resources.setWordWrap(True)
        self.w["resources"] = resources
        lay.addWidget(resources)
        lay.addWidget(QLabel(t("qgis.run.stages")))
        lay.addWidget(
            self._table(
                "stages",
                [
                    t("qgis.run.col_stage"),
                    t("qgis.run.col_engine"),
                    t("qgis.run.col_status"),
                    t("qgis.run.col_wall"),
                ],
            )
        )
        lay.addWidget(QLabel(t("qgis.run.findings")))
        lay.addWidget(
            self._table(
                "run_findings",
                [
                    t("qgis.run.col_severity"),
                    t("qgis.run.col_rule"),
                    t("qgis.run.col_cause"),
                    t("qgis.run.col_fix"),
                ],
            )
        )
        lay.addWidget(QLabel(t("qgis.run.log")))
        log = QPlainTextEdit()
        log.setReadOnly(True)
        log.setMaximumBlockCount(5000)
        self.w["log"] = log
        lay.addWidget(log)
        return panel

    def _stage_choice(self, key: str) -> str | None:
        value = self.w[f"stage_{key}"].currentData()
        return str(value) if value else None

    def _show_run(self, data: Mapping[str, Any] | None) -> None:
        rows = stage_rows(data)
        self._fill_table(
            self.w["stages"],
            [
                (
                    r["stage"],
                    r["engine"],
                    r["status"],
                    "" if r["wall_s"] is None else f"{float(r['wall_s']):.1f}",
                )
                for r in rows
            ],
            colors={
                2: {"failed": "#f8d7da", "cached": "#d4edda", "ok": "#d4edda", "to_run": "#fff3cd"}
            },
        )
        self.w["resources"].setText(resources_line(data))

    def _plan(self) -> None:
        cfg = self._config_path()
        if cfg is None:
            return
        self._start_job(
            "plan",
            self._client_call(
                "plan",
                cfg,
                until=self._stage_choice("until"),
                from_=self._stage_choice("from"),
                force=[s for s in [self._stage_choice("force")] if s],
            ),
            lambda resp: self._show_run(resp.data if isinstance(resp.data, Mapping) else None),
        )

    def _run(self) -> None:
        cfg = self._config_path()
        if cfg is None:
            return

        def done(resp: CliResponse) -> None:
            self.last_run = resp.data if isinstance(resp.data, dict) else None
            self._show_run(self.last_run)
            self._refresh_results()

        self._start_job(
            "run",
            self._client_call(
                "run_pipeline",
                cfg,
                until=self._stage_choice("until"),
                from_=self._stage_choice("from"),
                force=[s for s in [self._stage_choice("force")] if s],
            ),
            done,
        )

    def _diagnose(self) -> None:
        log_dir = failed_log_dir(self.last_run)
        if log_dir is None:
            cfg = self._config_path()
            if cfg is None:
                return
            log_dir = str(resolve_workdir(cfg, self.config_data) / "logs")
        self._start_job("diagnose", self._client_call("diagnose", log_dir), lambda resp: None)

    # ------------------------------------------------------------------ panel 4: results
    def _build_results_panel(self) -> Any:
        from qgis.PyQt.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

        panel = QWidget()
        lay = QVBoxLayout(panel)
        buttons = QHBoxLayout()
        buttons.addWidget(self._button(t("qgis.results.refresh"), self._refresh_results))
        buttons.addWidget(self._button(t("qgis.results.load"), self._load_selected))
        buttons.addWidget(self._button(t("qgis.results.load_all"), self._load_all_rasters))
        lay.addLayout(buttons)
        lay.addWidget(
            self._table(
                "artifacts",
                [
                    t("qgis.results.col_name"),
                    t("qgis.results.col_kind"),
                    t("qgis.results.col_path"),
                ],
            )
        )
        return panel

    def _refresh_results(self) -> None:
        data = self.last_run
        if data is None:
            cfg = self.w["config_path"].text().strip()
            if cfg:
                data = latest_run_summary(resolve_workdir(cfg, self.config_data))
                self.last_run = data
        rows = artifact_rows(data)
        self._fill_table(self.w["artifacts"], [(r["name"], r["kind"], r["path"]) for r in rows])
        if not raster_artifacts(data):
            self._message(t("qgis.results.none"), "info")

    def _load_raster(self, name: str, path: str) -> bool:
        from qgis.core import QgsProject, QgsRasterLayer

        # source: https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/loadlayer.html
        #   rlayer = QgsRasterLayer(path, name); rlayer.isValid(); QgsProject.instance().addMapLayer(rlayer)
        layer = QgsRasterLayer(path, name)
        if not layer.isValid():
            self._message(t("qgis.results.invalid_layer", path=path), "critical")
            return False
        QgsProject.instance().addMapLayer(layer)
        return True

    def _load_selected(self) -> None:
        table = self.w["artifacts"]
        rows = artifact_rows(self.last_run)
        selected = sorted({i.row() for i in table.selectedIndexes()})
        n = 0
        for r in selected:
            if r >= len(rows):
                continue
            row = rows[r]
            if not row["raster"]:
                self._message(
                    t("qgis.results.not_raster", name=row["name"], kind=row["kind"]), "warning"
                )
                continue
            n += self._load_raster(row["name"], row["path"])
        self._message(t("qgis.results.loaded", n=n), "success" if n else "info")

    def _load_all_rasters(self) -> None:
        n = sum(self._load_raster(r["name"], r["path"]) for r in raster_artifacts(self.last_run))
        self._message(t("qgis.results.loaded", n=n), "success" if n else "info")

    # ------------------------------------------------------------------ panel 5: reference point
    def _build_refpoint_panel(self) -> Any:
        from qgis.PyQt.QtWidgets import (
            QFormLayout,
            QHBoxLayout,
            QLabel,
            QSpinBox,
            QVBoxLayout,
            QWidget,
        )

        panel = QWidget()
        lay = QVBoxLayout(panel)
        form = QFormLayout()
        form.addRow(
            t("qgis.refpoint.ts"),
            self._browse_row(
                "refpoint_ts",
                self.settings.last_refpoint_ts or self.settings.last_ts_file or "",
                filt="Time series (*.npz *.h5);;All (*)",
            ),
        )
        top = QSpinBox()
        top.setRange(1, 50)
        top.setValue(5)
        self.w["refpoint_top"] = top
        form.addRow(t("qgis.refpoint.top"), top)
        lay.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addWidget(self._button(t("qgis.refpoint.recommend"), self._recommend_refpoint))
        buttons.addWidget(self._button(t("qgis.refpoint.pick"), self._pick_on_map))
        buttons.addWidget(self._button(t("qgis.refpoint.apply"), self._apply_refpoint))
        lay.addLayout(buttons)
        lay.addWidget(
            self._table(
                "refpoints",
                [
                    t("qgis.refpoint.col_rank"),
                    t("qgis.refpoint.col_lat"),
                    t("qgis.refpoint.col_lon"),
                    t("qgis.refpoint.col_score"),
                    t("qgis.refpoint.col_coherence"),
                ],
            )
        )
        picked = QLabel("")
        self.w["picked"] = picked
        lay.addWidget(picked)
        self._picked: tuple[float, float] | None = None
        return panel

    def _recommend_refpoint(self) -> None:
        ts = self.w["refpoint_ts"].text().strip()
        aoi = self.w["aoi_path"].text().strip() or str(get_nested(self.config_data, ("aoi",)) or "")
        if not ts:
            self._message(t("qgis.refpoint.no_candidates"), "warning")
            return

        def done(resp: CliResponse) -> None:
            self.last_refpoints = refpoint_rows(resp.data)
            self._fill_table(
                self.w["refpoints"],
                [
                    (
                        r["rank"],
                        f"{r['lat']:.5f}",
                        f"{r['lon']:.5f}",
                        "" if r["score"] is None else f"{float(r['score']):.3f}",
                        "" if r["coherence"] is None else f"{float(r['coherence']):.2f}",
                    )
                    for r in self.last_refpoints
                ],
            )
            if not self.last_refpoints:
                self._message(t("qgis.refpoint.no_candidates"), "warning")

        self._start_job(
            "refpoint",
            self._client_call("refpoint", ts, aoi or None, int(self.w["refpoint_top"].value())),
            done,
        )

    def _pick_on_map(self) -> None:
        from qgis.gui import QgsMapToolEmitPoint

        # source: https://qgis.org/pyqgis/3.44/gui/QgsMapToolEmitPoint.html
        #   QgsMapToolEmitPoint(canvas); signal canvasClicked(point: QgsPointXY, button: Qt.MouseButton)
        canvas = self.iface.mapCanvas()
        self._release_map_tool()
        self._prev_map_tool = canvas.mapTool()
        tool = QgsMapToolEmitPoint(canvas)
        tool.canvasClicked.connect(self._on_canvas_clicked)
        self._map_tool = tool
        canvas.setMapTool(tool)

    def _on_canvas_clicked(self, point: Any, button: Any) -> None:
        canvas = self.iface.mapCanvas()
        xform = self._to_wgs84(canvas.mapSettings().destinationCrs())
        pt = xform.transform(point)
        self._picked = (float(pt.y()), float(pt.x()))
        self.w["picked"].setText(
            t("qgis.refpoint.picked", lat=self._picked[0], lon=self._picked[1])
        )
        self._release_map_tool()

    def _release_map_tool(self) -> None:
        if self._map_tool is None:
            return
        try:
            canvas = self.iface.mapCanvas()
            canvas.unsetMapTool(self._map_tool)
            if self._prev_map_tool is not None:
                canvas.setMapTool(self._prev_map_tool)
        except Exception:
            pass
        self._map_tool = None
        self._prev_map_tool = None

    def _apply_refpoint(self) -> None:
        latlon = self._picked
        if latlon is None:
            table = self.w["refpoints"]
            selected = sorted({i.row() for i in table.selectedIndexes()})
            if selected and selected[0] < len(self.last_refpoints):
                r = self.last_refpoints[selected[0]]
                latlon = (r["lat"], r["lon"])
        if latlon is None:
            self._message(t("qgis.refpoint.no_candidates"), "warning")
            return
        self.form["timeseries.reference_point"].setText(f"{latlon[0]:.6f},{latlon[1]:.6f}")
        self._message(t("qgis.refpoint.applied", lat=latlon[0], lon=latlon[1]), "success")
        self.w["toolbox"].setCurrentIndex(1)

    # ------------------------------------------------------------------ panel 6: validation
    def _build_validate_panel(self) -> Any:
        from qgis.PyQt.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

        panel = QWidget()
        lay = QVBoxLayout(panel)
        form = QFormLayout()
        form.addRow(
            t("qgis.validate.ts"),
            self._browse_row(
                "ts_file", self.settings.last_ts_file or "", filt="HDF5 (*.h5);;All (*)"
            ),
        )
        form.addRow(
            t("qgis.validate.leveling"),
            self._browse_row("leveling_csv", self.settings.last_leveling or "", filt="CSV (*.csv)"),
        )
        form.addRow(
            t("qgis.validate.gnss"),
            self._browse_row("gnss_csv", self.settings.last_gnss or "", filt="CSV (*.csv)"),
        )
        lay.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addWidget(self._button(t("qgis.validate.run"), self._validate))
        buttons.addWidget(self._button(t("qgis.validate.load_points"), self._load_gt_points))
        lay.addLayout(buttons)
        table = self._table(
            "validation",
            [
                t("qgis.validate.col_site"),
                t("qgis.validate.col_method"),
                t("qgis.validate.col_n"),
                t("qgis.validate.col_rmse"),
                t("qgis.validate.col_bias"),
            ],
        )
        table.itemSelectionChanged.connect(self._plot_selected_site)
        lay.addWidget(table)
        lay.addWidget(QLabel(t("qgis.validate.plot")))
        self.w["plot_host"] = QVBoxLayout()
        lay.addLayout(self.w["plot_host"])
        hint = QLabel(t("qgis.validate.select_site"))
        hint.setWordWrap(True)
        self.w["plot_hint"] = hint
        lay.addWidget(hint)
        return panel

    def _validate(self) -> None:
        ts = self.w["ts_file"].text().strip()
        leveling = self.w["leveling_csv"].text().strip() or None
        gnss = self.w["gnss_csv"].text().strip() or None
        if not ts or (leveling is None and gnss is None):
            self._message(t("qgis.validate.select_site"), "warning")
            return

        def done(resp: CliResponse) -> None:
            self.last_validation = validation_rows(
                resp.data if isinstance(resp.data, Mapping) else None
            )
            self._fill_table(
                self.w["validation"],
                [
                    (
                        r["site_id"],
                        r["method"],
                        r["n"],
                        "" if r["rmse_mm"] is None else f"{r['rmse_mm']:.1f}",
                        "" if r["bias_mm"] is None else f"{r['bias_mm']:.1f}",
                    )
                    for r in self.last_validation
                ],
            )

        self._start_job("validate", self._client_call("validate", ts, leveling, gnss), done)

    def _load_gt_points(self) -> None:
        from qgis.core import QgsProject, QgsVectorLayer

        n = 0
        for key in ("leveling_csv", "gnss_csv"):
            path = self.w[key].text().strip()
            if not path:
                continue
            layer = QgsVectorLayer(ground_truth_layer_uri(path), Path(path).stem, "delimitedtext")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                n += 1
            else:
                self._message(t("qgis.results.invalid_layer", path=path), "critical")
        self._message(t("qgis.results.loaded", n=n), "success" if n else "info")

    def _plot_selected_site(self) -> None:
        table = self.w["validation"]
        selected = sorted({i.row() for i in table.selectedIndexes()})
        if not selected or selected[0] >= len(self.last_validation):
            return
        site = self.last_validation[selected[0]]
        host = self.w["plot_host"]
        while host.count():
            item = host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        try:
            # source: .venv/lib/python3.11/site-packages/matplotlib/backends/backend_qtagg.py
            #   (FigureCanvasQTAgg; picks the Qt binding through matplotlib.backends.qt_compat)
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from matplotlib.figure import Figure
        except Exception:
            self._plot_as_table(site)
            return
        fig = Figure(figsize=(4, 2.5))
        ax = fig.add_subplot(111)
        x = list(range(len(site["dates"])))
        if site["insar_mm"]:
            ax.plot(
                x[: len(site["insar_mm"])],
                site["insar_mm"],
                "o-",
                label=t("qgis.validate.series_insar"),
            )
        if site["gt_mm"]:
            ax.plot(
                x[: len(site["gt_mm"])], site["gt_mm"], "s--", label=t("qgis.validate.series_gt")
            )
        ax.set_xticks(x[:: max(1, len(x) // 6)])
        ax.set_xticklabels(
            [site["dates"][i] for i in x[:: max(1, len(x) // 6)]], rotation=30, fontsize=7
        )
        ax.set_title(site["site_id"], fontsize=9)
        ax.legend(fontsize=7)
        fig.tight_layout()
        host.addWidget(FigureCanvasQTAgg(fig))  # type: ignore[no-untyped-call]
        self.w["plot_hint"].setText("")

    def _plot_as_table(self, site: Mapping[str, Any]) -> None:
        from qgis.PyQt.QtWidgets import QTableWidget

        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(
            ["date", t("qgis.validate.series_insar"), t("qgis.validate.series_gt")]
        )
        rows = [
            (
                d,
                ""
                if i >= len(site["insar_mm"]) or site["insar_mm"][i] is None
                else f"{site['insar_mm'][i]:.1f}",
                ""
                if i >= len(site["gt_mm"]) or site["gt_mm"][i] is None
                else f"{site['gt_mm'][i]:.1f}",
            )
            for i, d in enumerate(site["dates"])
        ]
        self._fill_table(table, rows)
        self.w["plot_host"].addWidget(table)
        self.w["plot_hint"].setText(t("qgis.validate.no_matplotlib"))
