"""``wintersar search`` and ``wintersar precheck`` (plan section 4.5, R-01..R-03).

* ``search --config config.yaml`` -> ``<workdir>/select/candidates.json`` (delegates to
  :mod:`wintersar.select.search`).
* ``precheck CANDIDATES --config config.yaml [--out DIR]`` -> group -> network ->
  rules -> ``precheck_report.{md,html,json}``; exit code 1 when any FAIL finding exists
  (``--no-fail`` to ignore).

Both commands honour the global ``--json`` envelope (``wintersar.util.clistate.state``).
The report language is ``project.language`` from the config; console messages follow
``--lang``.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import typer

from wintersar.i18n import t
from wintersar.io.schemas import BurstRecord, Finding, Resources, StackCandidate
from wintersar.pipeline.config import Config, load_config
from wintersar.select.looks import LooksResult, looks_for_records
from wintersar.select.network import aoi_centroid_lat, group_candidates, with_baselines
from wintersar.select.report import (
    precheck_payload,
    recommend_stack,
    severity_counts,
    write_precheck_report,
)
from wintersar.select.rules import PoeorbHook, records_for_candidate, run_rules
from wintersar.util.clistate import state
from wintersar.util.masking import mask_text
from wintersar.util.output import console, emit_json, err_console, print_findings

CANDIDATES_BASENAME = "candidates.json"
BaselineOption = Literal["auto", "asf", "orbit", "none"]


class CandidatesError(ValueError):
    """The candidates file could not be read."""


@dataclass
class PrecheckResult:
    candidates: list[StackCandidate]
    findings: list[Finding]
    looks: dict[str, LooksResult] = field(default_factory=dict)
    resources: dict[str, Resources] = field(default_factory=dict)
    recommended: StackCandidate | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def has_fail(self) -> bool:
        return any(f.is_fail for f in self.findings)


# ----------------------------------------------------------------------------- helpers


def _load_cfg(path: Path) -> Config:
    if not path.exists():
        err_console.print(f"[red]{t('cli.config_not_found', path=mask_text(str(path)))}[/]")
        raise typer.Exit(code=2)
    try:
        return load_config(path)
    except Exception as e:  # pydantic / yaml errors
        err_console.print(f"[red]{t('cli.invalid_config', error=mask_text(str(e)))}[/]")
        raise typer.Exit(code=2) from None


def _records_from_json(data: Any) -> list[BurstRecord]:
    items: Any
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("records")
        if items is None and isinstance(data.get("data"), dict):
            items = data["data"].get("records")
        items = items or []
    else:
        msg = "candidates JSON must be a list or a mapping with 'records'"
        raise CandidatesError(msg)
    return [BurstRecord.model_validate(x) for x in items]


def load_candidates_file(path: Path) -> list[BurstRecord]:
    """Records of ``candidates.json``.

    Prefers ``wintersar.select.search.load_records`` (the writer's own reader); falls
    back to parsing ``{"records": [BurstRecord...]}`` or a bare list.
    """
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        from wintersar.select.search import load_records

        result = load_records(path)
        return list(result.records)
    except Exception:  # module missing or a different on-disk format
        pass
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise CandidatesError(str(e)) from e
    try:
        return _records_from_json(data)
    except CandidatesError:
        raise
    except Exception as e:
        raise CandidatesError(str(e)) from e


def aoi_file_to_wkt(path: Path) -> str:
    """WKT of the AOI file (GeoJSON Feature/FeatureCollection/Geometry or WKT text)."""
    try:
        from wintersar.select.search import aoi_to_wkt

        return str(aoi_to_wkt(path))
    except Exception:
        pass
    # source: shapely/geometry/geo.py `shape(context)`; shapely/ops.py `unary_union`
    from shapely.geometry import shape
    from shapely.ops import unary_union

    text = Path(path).read_text(encoding="utf-8").strip()
    if not text.startswith("{"):
        return text  # already WKT
    data = json.loads(text)
    kind = data.get("type")
    if kind == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in data.get("features", []) if f.get("geometry")]
    elif kind == "Feature":
        geoms = [shape(data["geometry"])]
    else:
        geoms = [shape(data)]
    return str(unary_union(geoms).wkt)


def _load_geometry(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        msg = "geometry statistics must be a JSON object"
        raise ValueError(msg)
    return data


def _apply_baselines(
    candidates: list[StackCandidate],
    records: list[BurstRecord],
    cfg: Config,
    method: BaselineOption,
    findings: list[Finding],
) -> tuple[list[StackCandidate], str | None]:
    """Fill ``Pair.perp_baseline_m`` through ``wintersar.select.baseline``.

    ``method``: ``asf`` (stack API), ``orbit`` (POEORB self-computation), ``auto`` (asf
    with orbit fallback) or ``none`` (skip; SEL-06 then reports "not computed").
    Baseline findings (SEL-SEARCH-xx) are appended to ``findings``.
    """
    if method == "none":
        return candidates, None
    try:
        # source: src/wintersar/select/baseline.py `compute_pair_baselines(records, pairs,
        # method: Literal["asf", "orbit", "auto"] = "asf", *, ..., findings=None)`
        from wintersar.select.baseline import compute_pair_baselines
    except Exception as e:  # optional deps missing
        return candidates, str(e)
    out: list[StackCandidate] = []
    error: str | None = None
    for c in candidates:
        if not c.pairs:
            out.append(c)
            continue
        recs = records_for_candidate(c, records)
        try:
            extra: list[Finding] = []
            pairs = compute_pair_baselines(recs, list(c.pairs), method, findings=extra)
            findings.extend(
                f
                if f.scope not in (None, "baseline")
                else f.model_copy(update={"scope": c.stack_id})
                for f in extra
            )
            out.append(with_baselines(c, list(pairs), cfg.selection))
        except Exception as e:
            error = str(e)
            out.append(c)
    return out, error


def run_precheck(
    records: list[BurstRecord],
    cfg: Config,
    aoi_wkt: str,
    geometry: dict[str, Any] | None = None,
    poeorb_available: PoeorbHook | None = None,
    baseline_method: BaselineOption = "auto",
) -> PrecheckResult:
    """group -> network (+baselines) -> looks -> rules -> recommendation."""
    candidates = group_candidates(records, aoi_wkt, cfg.selection, cfg.data)
    baseline_findings: list[Finding] = []
    candidates, baseline_error = _apply_baselines(
        candidates, records, cfg, baseline_method, baseline_findings
    )
    warnings: list[str] = []
    if baseline_error:
        warnings.append(t("select.cli.baseline_unavailable", error=mask_text(baseline_error)))
    explicit = cfg.engine.looks if isinstance(cfg.engine.looks, tuple) else None
    looks: dict[str, LooksResult] = {}
    resources: dict[str, Resources] = {}
    for c in candidates:
        recs = records_for_candidate(c, records)
        looks[c.stack_id] = looks_for_records(recs, cfg.engine.target_pixel_m, looks=explicit)
        # Rough job count only (pairs x bursts). Credits stay None until an engine
        # estimate exists (`wintersar plan`); no credit figure is guessed here (rule 11.3).
        resources[c.stack_id] = Resources(
            n_jobs=len(c.pairs) * max(1, len(c.burst_ids)),
            notes={"basis": "n_pairs * n_bursts", "credits": "unknown"},
        )
    findings = run_rules(
        candidates,
        records,
        cfg,
        geometry=geometry,
        resources=resources,
        poeorb_available=poeorb_available,
        aoi_lat=aoi_centroid_lat(aoi_wkt),
    )
    findings = [*baseline_findings, *findings]
    rec = recommend_stack(candidates, findings)
    if rec is not None:
        candidates = [
            c.model_copy(update={"notes": {**c.notes, "recommended": True}})
            if c.stack_id == rec.stack_id
            else c
            for c in candidates
        ]
    return PrecheckResult(candidates, findings, looks, resources, rec, warnings)


# ------------------------------------------------------------------------------ commands


def search(
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Project config.yaml (plan section 4.4).")
    ] = Path("config.yaml"),
) -> None:
    """Query candidate bursts/scenes for the AOI and period -> work/select/candidates.json."""
    cfg = _load_cfg(config)
    try:
        search_mod = importlib.import_module("wintersar.select.search")
    except Exception as e:
        err_console.print(f"[red]{t('select.cli.search_unavailable', error=mask_text(repr(e)))}[/]")
        raise typer.Exit(code=2) from None
    result = search_mod.search_from_config(cfg)
    out = cfg.workdir / "select" / CANDIDATES_BASENAME
    out.parent.mkdir(parents=True, exist_ok=True)
    search_mod.save_records(result, out)
    findings: list[Finding] = list(getattr(result, "findings", []) or [])
    n = len(getattr(result, "records", []) or [])
    product_type = str(getattr(result, "product_type", "?"))
    data = {"path": str(out), "n_records": n, "product_type": product_type}
    if state.json:
        emit_json("search", data, findings, ok=not any(f.is_fail for f in findings))
        return
    console.print(
        t("select.cli.search_done", n=n, product_type=product_type, path=mask_text(str(out)))
    )
    if findings:
        print_findings(findings)


def precheck(
    candidates: Annotated[
        Path, typer.Argument(help="candidates.json written by `wintersar search`.")
    ],
    config: Annotated[
        Path, typer.Option("--config", "-c", help="Project config.yaml (AOI, selection).")
    ] = Path("config.yaml"),
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Report directory (default work/select).")
    ] = None,
    geometry: Annotated[
        Path | None,
        typer.Option(
            "--geometry",
            help="JSON with layover/shadow statistics per stack or direction (SEL-12).",
        ),
    ] = None,
    no_fail: Annotated[
        bool, typer.Option("--no-fail", help="Exit 0 even when FAIL findings exist.")
    ] = False,
    baseline: Annotated[
        str,
        typer.Option(
            "--baseline",
            help="Perpendicular baselines: auto (ASF stack API, orbit fallback) | asf | orbit | none.",
        ),
    ] = "auto",
) -> None:
    """Run the SEL-01..SEL-13 rules on the candidates -> precheck_report.{md,html,json}."""
    cfg = _load_cfg(config)
    try:
        records = load_candidates_file(candidates)
    except FileNotFoundError:
        err_console.print(
            f"[red]{t('select.cli.candidates_not_found', path=mask_text(str(candidates)))}[/]"
        )
        raise typer.Exit(code=2) from None
    except CandidatesError as e:
        err_console.print(
            f"[red]{t('select.cli.candidates_invalid', path=mask_text(str(candidates)), error=mask_text(str(e)))}[/]"
        )
        raise typer.Exit(code=2) from None
    if not records:
        err_console.print(f"[red]{t('select.cli.no_records')}[/]")
        raise typer.Exit(code=2)
    try:
        geometry_stats = _load_geometry(geometry)
    except (OSError, ValueError) as e:
        err_console.print(
            f"[red]{t('select.cli.geometry_invalid', path=mask_text(str(geometry)), error=mask_text(str(e)))}[/]"
        )
        raise typer.Exit(code=2) from None
    if baseline not in ("auto", "asf", "orbit", "none"):
        err_console.print(f"[red]--baseline: {baseline!r} not in auto|asf|orbit|none[/]")
        raise typer.Exit(code=2)
    aoi_wkt = aoi_file_to_wkt(cfg.aoi)
    result = run_precheck(
        records,
        cfg,
        aoi_wkt,
        geometry=geometry_stats,
        baseline_method=baseline,  # type: ignore[arg-type]
    )
    for w in result.warnings:
        err_console.print(f"[yellow]{w}[/]")
    lang = cfg.project.language
    out_dir = out or (cfg.workdir / "select")
    rec_id = result.recommended.stack_id if result.recommended else None
    paths = write_precheck_report(
        result.candidates,
        result.findings,
        out_dir,
        lang,
        looks=result.looks,
        resources=result.resources,
        recommended=rec_id,
    )
    counts = severity_counts(result.findings)
    ok = not result.has_fail
    if state.json:
        payload = precheck_payload(
            result.candidates, result.findings, lang, result.looks, result.resources, rec_id
        )
        payload["paths"] = {k: str(v) for k, v in paths.items()}
        emit_json("precheck", payload, result.findings, ok=ok)
    else:
        if not result.candidates:
            err_console.print(f"[yellow]{t('select.cli.no_candidates')}[/]")
        print_findings(result.findings)
        console.print(
            t(
                "select.cli.precheck_done",
                n_candidates=len(result.candidates),
                n_fail=counts["FAIL"],
                n_warn=counts["WARN"],
                n_info=counts["INFO"],
            )
        )
        if result.recommended is not None:
            console.print(
                t(
                    "select.cli.recommended",
                    stack=result.recommended.stack_id,
                    coverage=result.recommended.coverage_of_aoi,
                    n_dates=len(result.recommended.dates),
                    n_pairs=len(result.recommended.pairs),
                )
            )
        console.print(
            t(
                "select.cli.report_written",
                paths=", ".join(mask_text(str(p)) for p in paths.values()),
            )
        )
    if not ok and not no_fail:
        if not state.json:
            err_console.print(f"[red]{t('select.cli.fail_exit')}[/]")
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    app.command("search")(search)
    app.command("precheck")(precheck)
