"""Stage registry of the pipeline DAG (plan §5.3).

``STAGE_ORDER`` is the fixed linear order in which stages execute. Each :class:`StageSpec`
declares which *artifact names* it consumes and produces; the DAG links a consumer to the
latest upstream producer of that name (``multilook`` re-exports ``igrams``, so ``unwrap``
reads the multilooked stack, not the raw interferograms).

Engine stages are executed by an :class:`wintersar.engines.base.Engine` chosen by
:func:`resolve_engine`; python stages (``search``, ``precheck``, ``validate``) call a
function in another wintersar module lazily (:data:`PYTHON_STAGE_ENTRYPOINTS`).

``timeseries``, ``corrections`` and ``geocode`` are keyed on ``timeseries.engine`` (MintPy
runs all three); the interferogram engine only owns ``fetch`` … ``multilook``.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from wintersar.io.schemas import Artifact, Artifacts, Finding

if TYPE_CHECKING:
    from wintersar.pipeline.config import Config

STAGE_ORDER: list[str] = [
    "search",
    "precheck",
    "fetch",
    "coregister",
    "interferogram",
    "multilook",
    "unwrap",
    "timeseries",
    "corrections",
    "geocode",
    "validate",
]

EngineKey = Literal["engine.interferogram", "unwrap.method", "timeseries.engine"]

FAKE_ENGINE = "fake"
DEFAULT_UNWRAP_METHOD = "snaphu"


@dataclass(frozen=True)
class StageSpec:
    """Static description of one stage.

    ``inputs`` are artifact names that must be produced upstream; ``optional_inputs`` are
    passed to the engine when available but never block the stage. ``engine_key`` names the
    config field that selects the engine (``None`` = python stage). ``optional`` stages are
    skipped when their configuration is absent (``validate`` without ground truth).
    """

    name: str
    inputs: list[str]
    outputs: list[str]
    engine_key: EngineKey | None
    optional: bool = False
    optional_inputs: list[str] = field(default_factory=list)

    @property
    def is_python(self) -> bool:
        return self.engine_key is None


STAGES: dict[str, StageSpec] = {
    s.name: s
    for s in (
        StageSpec("search", [], ["candidates"], None),
        StageSpec("precheck", ["candidates"], ["stack", "precheck_report"], None),
        StageSpec("fetch", ["stack"], ["slc_manifest"], "engine.interferogram"),
        # ``stack`` (precheck) is optional for the engine stages but every real adapter needs
        # it: hyp3 reads notes["granules"] to build its jobs, isce2_topsstack reads
        # notes["aoi_wkt"]/["bbox_snwe"] for -b and the date list for -c.
        # source: engines/hyp3.py::STACK_INPUT_NAMES, engines/isce2_topsstack.py::bbox_snwe
        StageSpec(
            "coregister",
            ["slc_manifest"],
            ["coreg_manifest"],
            "engine.interferogram",
            optional_inputs=["stack"],
        ),
        StageSpec(
            "interferogram",
            ["coreg_manifest"],
            ["igrams"],
            "engine.interferogram",
            optional_inputs=["stack"],
        ),
        StageSpec(
            "multilook",
            ["igrams"],
            ["igrams"],
            "engine.interferogram",
            optional_inputs=["stack"],
        ),
        StageSpec("unwrap", ["igrams"], ["unw"], "unwrap.method"),
        # ``mintpy_workdir`` is emitted by the MintPy adapter only; dolphin/fake simply do not
        # produce it, which is why every consumer takes it as an optional input.
        # source: engines/mintpy.py::MintPyEngine._collect_artifacts
        StageSpec(
            "timeseries",
            ["unw", "igrams"],
            ["timeseries", "mintpy_workdir"],
            "timeseries.engine",
        ),
        # MintPy's corrections step list ends with ``velocity`` and its artifact collector
        # requires ``velocity.h5`` there, so corrections already produces a (radar-coordinate)
        # velocity; ``geocode`` re-exports the geocoded one.
        # source: src/wintersar/engines/mintpy.py::MintPyEngine._collect_artifacts (stage
        #         "corrections" -> Artifact(name="velocity", path=workdir/"velocity.h5"))
        StageSpec(
            "corrections",
            ["timeseries"],
            ["timeseries", "velocity", "mintpy_workdir"],
            "timeseries.engine",
            # MintPy re-enters its own work dir instead of re-deriving it from params.
            # source: engines/mintpy.py::MintPyEngine._inherit_workdir (inputs["mintpy_workdir"])
            optional_inputs=["mintpy_workdir"],
        ),
        # geocode is a post-processing stage of the *time-series* engine: MintPy declares it,
        # the interferogram engines (hyp3, isce2_topsstack, compass_isce3) never do.
        # source: src/wintersar/engines/mintpy.py::MintPyEngine.stages
        StageSpec(
            "geocode",
            ["timeseries"],
            ["velocity"],
            "timeseries.engine",
            optional_inputs=["mintpy_workdir"],
        ),
        # ``run_validate`` only consumes the optional ``timeseries`` artifact, so neither
        # velocity nor timeseries may block the plan when the chosen engine does not geocode.
        # source: src/wintersar/validate/api.py::run_validate (inputs.items.get("timeseries"))
        StageSpec(
            "validate",
            [],
            ["validation_report"],
            None,
            optional=True,
            optional_inputs=["velocity", "timeseries"],
        ),
    )
}

# python stages: (module, function). Expected signature (lazy import, may be absent):
#   fn(cfg: Config, inputs: Artifacts, params: dict[str, Any], out_dir: Path, log_dir: Path)
#       -> Artifacts | tuple[Artifacts, list[Finding]]
PYTHON_STAGE_ENTRYPOINTS: dict[str, tuple[str, str]] = {
    "search": ("wintersar.select.api", "run_search"),
    "precheck": ("wintersar.select.api", "run_precheck"),
    "validate": ("wintersar.validate.api", "run_validate"),
}

# unwrap scheduler entry point on the non-fake path (contract owned by wintersar.unwrap):
#   run_unwrap(igrams: Artifact, params: dict, out_dir: Path, log_dir: Path, machine: MachineSpec)
#       -> Artifacts
UNWRAP_ENTRYPOINT: tuple[str, str] = ("wintersar.unwrap.api", "run_unwrap")

# diagnose hooks (lazy; absent -> no findings / empty estimate)
DIAGNOSE_LOGS_ENTRYPOINT: tuple[str, str] = ("wintersar.diagnose.api", "diagnose_logs")
DIAGNOSE_RESOURCES_ENTRYPOINT: tuple[str, str] = ("wintersar.diagnose.resources", "estimate")


def stage_index(stage: str) -> int:
    """Position of ``stage`` in :data:`STAGE_ORDER` (``KeyError`` for unknown names)."""
    if stage not in STAGES:
        msg = f"unknown stage {stage!r}; known: {STAGE_ORDER}"
        raise KeyError(msg)
    return STAGE_ORDER.index(stage)


def stage_window(until: str | None = None, from_stage: str | None = None) -> list[str]:
    """Stages considered by a run: everything up to ``until`` (inclusive).

    ``from_stage`` does not shrink the window (upstream stages are still resolved from the
    cache); it only changes how the executor treats the stages before it.
    """
    end = len(STAGE_ORDER) if until is None else stage_index(until) + 1
    if from_stage is not None:
        start = stage_index(from_stage)
        if start >= end:
            msg = f"--from {from_stage!r} is after --until {until!r}"
            raise ValueError(msg)
    return STAGE_ORDER[:end]


def is_fake_path(cfg: Config) -> bool:
    """The synthetic end-to-end path (``engine.interferogram: fake``)."""
    return cfg.engine.interferogram == FAKE_ENGINE


def resolve_engine(cfg: Config, stage: str) -> str | None:
    """Engine registry key for ``stage`` under ``cfg`` (``None`` for python stages).

    On the fake path every engine stage uses ``fake``. ``unwrap.method: auto`` resolves to
    ``snaphu`` (the scheduler in :mod:`wintersar.unwrap` may still pick tophu per
    interferogram; the DAG only needs a stable identity for hashing).
    """
    spec = STAGES[stage]
    if spec.engine_key is None:
        return None
    if is_fake_path(cfg):
        return FAKE_ENGINE
    if spec.engine_key == "engine.interferogram":
        return str(cfg.engine.interferogram)
    if spec.engine_key == "unwrap.method":
        method = str(cfg.unwrap.method)
        return DEFAULT_UNWRAP_METHOD if method == "auto" else method
    return str(cfg.timeseries.engine)


def stage_is_configured(cfg: Config, stage: str) -> bool:
    """Optional stages run only when their configuration is present."""
    if stage == "validate":
        return cfg.validation.leveling_csv is not None or cfg.validation.gnss is not None
    return True


def load_entrypoint(module: str, function: str) -> Callable[..., Any] | None:
    """Import ``module`` lazily and return ``function`` (``None`` when unavailable)."""
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return None
    fn = getattr(mod, function, None)
    return fn if callable(fn) else None


class StageFailureError(RuntimeError):
    """Raised by python stages; ``findings`` are attached to the failed manifest."""

    def __init__(self, message: str, findings: list[Finding] | None = None) -> None:
        super().__init__(message)
        self.findings: list[Finding] = list(findings or [])


def _stage_log(log_dir: Path, stage: str, text: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"{stage}.log").open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


# --------------------------------------------------------------- select adapters
# Until ``wintersar.select.api`` exists, the search/precheck stages call the select
# module's own functions (verified in-tree):
# source: src/wintersar/select/search.py::search_from_config / save_records
# source: src/wintersar/select/cli.py::run_precheck / load_candidates_file / aoi_file_to_wkt
# source: src/wintersar/select/report.py::write_precheck_report


def select_search_stage(
    cfg: Config, inputs: Artifacts, params: dict[str, Any], out_dir: Path, log_dir: Path
) -> tuple[Artifacts, list[Finding]]:
    from wintersar.select.search import save_records, search_from_config

    result = search_from_config(cfg)
    path = save_records(result, out_dir / "candidates.json")
    _stage_log(
        log_dir,
        "search",
        f"records={len(result.records)} dates={len(result.dates)} "
        f"findings={[f.rule_id for f in result.findings]}",
    )
    findings = list(result.findings)
    if not result.ok:
        msg = "search returned FAIL findings"
        raise StageFailureError(msg, findings)
    meta = {
        "n_records": len(result.records),
        "n_dates": len(result.dates),
        "product_type": str(result.product_type),
        "relative_orbits": list(result.relative_orbits),
    }
    return Artifacts().add(Artifact(name="candidates", path=path, kind="json", meta=meta)), findings


def _aoi_bbox_snwe(aoi_wkt: str) -> tuple[float, float, float, float] | None:
    """``(S, N, W, E)`` of an AOI WKT, the ordering ISCE2 topsStack expects for ``-b``."""
    try:
        from shapely import wkt as _wkt

        minx, miny, maxx, maxy = _wkt.loads(aoi_wkt).bounds
    except Exception:
        return None
    return (float(miny), float(maxy), float(minx), float(maxx))


def select_precheck_stage(
    cfg: Config, inputs: Artifacts, params: dict[str, Any], out_dir: Path, log_dir: Path
) -> tuple[Artifacts, list[Finding]]:
    from wintersar.select.cli import aoi_file_to_wkt, load_candidates_file, run_precheck
    from wintersar.select.report import write_precheck_report

    records = load_candidates_file(Path(inputs["candidates"].path))
    aoi_wkt = aoi_file_to_wkt(cfg.aoi)
    result = run_precheck(records, cfg, aoi_wkt)
    rec_id = result.recommended.stack_id if result.recommended else None
    paths = write_precheck_report(
        result.candidates,
        result.findings,
        out_dir,
        cfg.project.language,
        looks=result.looks,
        resources=result.resources,
        recommended=rec_id,
    )
    _stage_log(
        log_dir,
        "precheck",
        f"candidates={[c.stack_id for c in result.candidates]} recommended={rec_id} "
        f"findings={[f.rule_id for f in result.findings]}",
    )
    findings = list(result.findings)
    stack = result.recommended or (result.candidates[0] if result.candidates else None)
    if stack is None or result.has_fail:
        msg = "precheck found no usable stack" if stack is None else "precheck has FAIL findings"
        raise StageFailureError(msg, findings)
    # Carry the AOI into stack.json: the local processing engines derive their processing
    # bounds from it (ISCE2 topsStack -b S N W E) and cannot read cfg.aoi themselves.
    stack.notes.setdefault("aoi_wkt", aoi_wkt)
    bbox = _aoi_bbox_snwe(aoi_wkt)
    if bbox is not None:
        stack.notes.setdefault("bbox_snwe", list(bbox))
    stack_path = out_dir / "stack.json"
    stack_path.write_text(stack.model_dump_json(indent=2), encoding="utf-8")
    meta = {
        "stack_id": stack.stack_id,
        "n_dates": len(stack.dates),
        "n_pairs": len(stack.pairs),
        "n_bursts": len(stack.burst_ids),
        "coverage_of_aoi": stack.coverage_of_aoi,
        "product_type": str(stack.product_type),
    }
    report = paths.get("json") or next(iter(paths.values()))
    arts = Artifacts()
    arts.add(Artifact(name="stack", path=stack_path, kind="json", meta=meta))
    arts.add(Artifact(name="precheck_report", path=report, kind="json"))
    return arts, findings


# stage -> (adapter, required (module, attribute) pairs that must import for it to be used)
PYTHON_STAGE_ADAPTERS: dict[str, tuple[Callable[..., Any], list[tuple[str, str]]]] = {
    "search": (
        select_search_stage,
        [
            ("wintersar.select.search", "search_from_config"),
            ("wintersar.select.search", "save_records"),
        ],
    ),
    "precheck": (
        select_precheck_stage,
        [
            ("wintersar.select.cli", "run_precheck"),
            ("wintersar.select.cli", "load_candidates_file"),
            ("wintersar.select.cli", "aoi_file_to_wkt"),
            ("wintersar.select.report", "write_precheck_report"),
        ],
    ),
}


def load_python_stage(stage: str) -> Callable[..., Any] | None:
    """Callable for a python stage: ``<module>.api`` entry point first, then the adapter."""
    entry = PYTHON_STAGE_ENTRYPOINTS.get(stage)
    if entry is None:
        return None
    fn = load_entrypoint(*entry)
    if fn is not None:
        return fn
    adapter = PYTHON_STAGE_ADAPTERS.get(stage)
    if adapter is None:
        return None
    impl, requirements = adapter
    if all(load_entrypoint(m, a) is not None for m, a in requirements):
        return impl
    return None
