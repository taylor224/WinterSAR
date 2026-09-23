"""Sequential stage executor with cache lookup, resource budget and failure diagnosis
(plan §5.3; ADR-0033 failure/retry semantics).

Stages run in :data:`STAGE_ORDER`. Per-item parallelism *inside* a stage (one job per
interferogram, tiles, run_files) is the engine's job: it receives the machine budget as
private params (``_cores``, ``_memory_gb``, ``_gpu``) together with ``_out_dir``,
``_workdir``, ``_cache_dir`` and ``_log_dir``. The executor reserves the stage's estimated
memory against the budget (sum of reservations <= budget) so that a future stage-parallel
executor keeps the same accounting.

Incremental mode (PERF-06, ADR-0080): with ``dag.incremental`` a stale incremental node is
re-run in its own directory with ``pairs/`` kept; the engine receives ``_pairs_dir`` and
``_pairs_done`` (per-pair results already there) and reports what it reused/computed in the
artifact meta, which lands in ``record.extra["incremental"]``.

On failure the manifest is written with ``status: failed``, the engine logs are handed to
``wintersar.diagnose.api.diagnose_logs`` (lazy; absent -> no extra findings), a generic
``PIPELINE-001`` finding with the masked log excerpt is attached, an optional
``retry_hint`` is copied into ``record.extra`` and :class:`PipelineError` is raised with
every record produced so far.
"""

from __future__ import annotations

import os
import resource
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wintersar.compute.xp import stage_gpu
from wintersar.engines.base import EngineNotAvailableError
from wintersar.io.schemas import Artifact, Artifacts, Finding, Resources, StageRecord
from wintersar.pipeline import cache, incremental
from wintersar.pipeline.config import Config
from wintersar.pipeline.dag import Dag, Node
from wintersar.pipeline.stages import (
    DIAGNOSE_LOGS_ENTRYPOINT,
    FAKE_ENGINE,
    UNWRAP_ENTRYPOINT,
    load_entrypoint,
    load_python_stage,
    stage_index,
)
from wintersar.util import sysinfo
from wintersar.util.masking import mask_text

LOG_EXCERPT_LINES = 40
LOG_EXCERPT_MAX_CHARS = 8000
#: never read more than this from the end of a log file: the stage that just failed may
#: have died of OOM and ``read_text()`` on a multi-hundred-MB log would follow it.
#: (``wintersar.diagnose.api.read_log`` caps the same way.)
LOG_EXCERPT_TAIL_BYTES = 256 * 1024
RETRY_HINT_KEY = "retry_hint"

#: Registry engine name -> ``wintersar diagnose --engine`` name. The KB only accepts
#: ``KB_ENGINES`` and exits 2 for anything else, so a fix text must not suggest e.g.
#: ``--engine isce2_topsstack``.
#: source: src/wintersar/diagnose/kb_loader.py::KB_ENGINES / src/wintersar/diagnose/cli.py
#: source: src/wintersar/diagnose/kb/isce2.yaml:1 ("ISCE2 / topsStack failure knowledge base")
KB_ENGINE_ALIASES: dict[str, str] = {"isce2_topsstack": "isce2"}
_KB_ENGINES_FALLBACK: tuple[str, ...] = ("snaphu", "isce2", "mintpy", "hyp3", "asf")


class PipelineError(RuntimeError):
    """A stage failed or was blocked; carries every record produced so far."""

    def __init__(
        self,
        message: str,
        records: list[StageRecord],
        findings: list[Finding],
        artifacts: Artifacts | None = None,
        failed: StageRecord | None = None,
    ) -> None:
        super().__init__(message)
        self.records = records
        self.findings = findings
        self.artifacts = artifacts or Artifacts()
        self.failed = failed

    @property
    def failed_stage(self) -> str | None:
        return self.failed.stage if self.failed is not None else None


# ---------------------------------------------------------------------- budget


def machine_budget(cfg: Config, machine: sysinfo.MachineSpec | None = None) -> sysinfo.MachineSpec:
    """Detected machine spec with ``compute.*`` overrides applied."""
    spec = machine or sysinfo.detect()
    return spec.budget(
        cores=cfg.compute.cores, memory_gb=cfg.compute.memory_gb, gpu=cfg.compute.gpu
    )


@dataclass
class ResourceBudget:
    """Memory reservations: ``sum(reserved) <= machine.memory_gb``."""

    machine: sysinfo.MachineSpec
    reservations: dict[str, float] = field(default_factory=dict)

    @property
    def memory_gb(self) -> float:
        return float(self.machine.memory_gb)

    @property
    def reserved_gb(self) -> float:
        return sum(self.reservations.values())

    @property
    def available_gb(self) -> float:
        return max(self.memory_gb - self.reserved_gb, 0.0)

    def fits(self, need_gb: float) -> bool:
        return need_gb <= self.available_gb + 1e-9

    def reserve(self, key: str, need_gb: float | None) -> float:
        """Reserve ``need_gb`` (or everything available when unknown); returns the grant."""
        grant = self.available_gb if need_gb is None else min(max(need_gb, 0.0), self.available_gb)
        self.reservations[key] = grant
        return grant

    def release(self, key: str) -> None:
        self.reservations.pop(key, None)


# ---------------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(UTC)


def _peak_rss_gb() -> float | None:
    """Process-lifetime high-water mark of resident memory (self and children).

    ``ru_maxrss`` is in KiB on Linux and bytes on macOS.
    # source: https://man7.org/linux/man-pages/man2/getrusage.2.html ("in KiB")
    # source: local check on darwin (resource.getrusage(RUSAGE_SELF).ru_maxrss ~= 13e6 for a
    #         bare interpreter, i.e. bytes); see docs/open-questions.md
    """
    try:
        own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except (OSError, ValueError):  # pragma: no cover
        return None
    peak = max(own, children)
    scale = 1.0 if sys.platform == "darwin" else 1024.0
    return float(peak) * scale / 1e9


def tail_lines(
    path: Path, n_lines: int = LOG_EXCERPT_LINES, max_bytes: int = LOG_EXCERPT_TAIL_BYTES
) -> list[str]:
    """Last ``n_lines`` of ``path``, reading at most ``max_bytes`` from its end."""
    with path.open("rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        _, _, text = text.partition("\n")  # drop the truncated first line
    return text.splitlines()[-n_lines:]


def log_excerpt(log_dir: Path, n_lines: int = LOG_EXCERPT_LINES) -> dict[str, str]:
    """Last ``n_lines`` of every ``*.log`` under ``log_dir``, masked (rule 11.11)."""
    excerpt: dict[str, str] = {}
    if not log_dir.is_dir():
        return excerpt
    budget = LOG_EXCERPT_MAX_CHARS
    for p in sorted(log_dir.rglob("*.log")):
        try:
            lines = tail_lines(p, n_lines)
        except OSError:
            continue
        text = mask_text("\n".join(lines))[:budget]
        budget -= len(text)
        excerpt[mask_text(p.relative_to(log_dir).as_posix())] = text
        if budget <= 0:
            break
    return excerpt


def _as_artifacts(result: Any) -> tuple[Artifacts, list[Finding]]:
    """Normalise what a stage callable returned."""
    findings: list[Finding] = []
    if isinstance(result, tuple) and len(result) == 2:
        result, extra = result
        findings = [f for f in extra if isinstance(f, Finding)]
    if isinstance(result, Artifacts):
        return result, findings
    if isinstance(result, Artifact):
        return Artifacts().add(result), findings
    msg = f"stage callable must return Artifacts (got {type(result).__name__})"
    raise TypeError(msg)


def _kb_engine_names() -> tuple[str, ...]:
    try:
        from wintersar.diagnose.kb_loader import KB_ENGINES
    except ImportError:  # pragma: no cover - diagnose module absent
        return _KB_ENGINES_FALLBACK
    return tuple(KB_ENGINES)


def kb_engine(engine: str | None) -> str | None:
    """``wintersar diagnose --engine`` name for ``engine`` (``None`` -> let it auto-detect)."""
    if not engine:
        return None
    name = KB_ENGINE_ALIASES.get(engine, engine)
    return name if name in _kb_engine_names() else None


def diagnose_logs(log_dir: Path, engine: str | None) -> tuple[list[Finding], str | None]:
    """``wintersar.diagnose.api.diagnose_logs(log_dir, engine)`` if importable."""
    fn = load_entrypoint(*DIAGNOSE_LOGS_ENTRYPOINT)
    if fn is None:
        return [], None
    try:
        # source: src/wintersar/diagnose/api.py::diagnose_logs(path, engine=None, ...)
        try:
            result = fn(log_dir, engine)
        except ValueError:
            # engine name unknown to the KB (e.g. 'fake'): let the parsers auto-detect
            result = fn(log_dir, None)
    except Exception as exc:  # a diagnose bug must never mask the original failure
        return [], f"{type(exc).__name__}: {exc}"
    if not isinstance(result, list):
        return [], None
    return [f for f in result if isinstance(f, Finding)], None


def _retry_hint(exc: BaseException, findings: list[Finding]) -> Any | None:
    hint = getattr(exc, RETRY_HINT_KEY, None)
    if hint is not None:
        return hint
    for f in findings:
        for source in (f.evidence, f.params):
            if RETRY_HINT_KEY in source:
                return source[RETRY_HINT_KEY]
    return None


# ---------------------------------------------------------------------- executor


class Executor:
    def __init__(
        self,
        cfg: Config,
        dag: Dag,
        machine: sysinfo.MachineSpec | None = None,
        from_stage: str | None = None,
        estimates: dict[str, Resources] | None = None,
    ) -> None:
        self.cfg = cfg
        self.dag = dag
        self.workdir = dag.workdir
        self.machine = machine or machine_budget(cfg)
        self.budget = ResourceBudget(self.machine)
        self.from_stage = from_stage
        self.estimates = estimates or {}
        self.run_id = _now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]

    @property
    def incremental(self) -> bool:
        """PERF-06 mode is a property of the DAG the executor was built for."""
        return self.dag.incremental

    # -------------------------------------------------------------- run
    def run(self) -> tuple[list[StageRecord], Artifacts, list[Finding]]:
        records: list[StageRecord] = []
        findings: list[Finding] = []
        available = Artifacts()
        start = stage_index(self.from_stage) if self.from_stage else 0

        def fail(record: StageRecord, message: str) -> PipelineError:
            return PipelineError(message, records, findings, available, failed=record)

        for node in self.dag.nodes:
            if node.skip_reason is not None:
                records.append(node.pending_record())
                findings.extend(node.findings)
                continue
            before_start = stage_index(node.stage) < start
            if before_start and node.fallback and node.record is not None:
                records.append(_cache_hit(node.record, fallback=True))
                findings.extend(node.findings)
                available = available.merged(cache.record_artifacts(node.record))
                continue
            resolved = self.dag.resolve(node, available)
            node.pairs_expected = self.dag.expected_pairs(node, available)
            record = None
            if resolved:
                record, fresh = self.dag.lookup(node, available)
                if record is not None and not fresh:
                    node.stale = True
                    record = None
            if record is not None:
                node.record = record
                records.append(_cache_hit(record))
                available = available.merged(cache.record_artifacts(record))
                continue
            node.partial = self.dag.partial_cache(node) if resolved else None
            if not resolved or node.blocked or before_start:
                if before_start and not node.blocked:
                    node.findings.append(self.dag.no_cached_result(node))
                blocked = node.pending_record()
                blocked.status = "failed"
                records.append(blocked)
                findings.extend(node.findings)
                raise fail(blocked, f"stage {node.stage!r} is blocked")
            record = self._execute(node, available)
            records.append(record)
            findings.extend(record.findings)
            if record.status != "ok":
                raise fail(record, f"stage {node.stage!r} failed")
            node.record = record
            available = available.merged(cache.record_artifacts(record))
        return records, available, findings

    # -------------------------------------------------------------- one node
    def _execute(self, node: Node, available: Artifacts) -> StageRecord:
        """Run one node while holding its directory lock (ADR-0032).

        Two ``wintersar run`` processes on one work directory resolve the same node hash;
        without the lock the second ``prepare_node_dir(clean=True)`` would delete the
        first run's in-flight ``out/`` and both manifests would race.
        """
        assert node.node_hash is not None
        node_dir = cache.stage_dir(self.workdir, node.stage, node.node_hash)
        try:
            with cache.node_lock(node_dir):
                return self._execute_locked(node, node_dir, available)
        except cache.NodeBusyError:
            # the other run may have just finished it
            record = self.dag.lookup_cache(node, available)
            if record is not None:
                node.record = record
                return _cache_hit(record)
            return self._busy_record(node, node_dir)

    def _busy_record(self, node: Node, node_dir: Path) -> StageRecord:
        """Another process owns this node directory: fail cleanly, touch nothing."""
        record = node.pending_record()
        record.status = "failed"
        record.extra.update({"run_id": self.run_id, "cache_hit": False, "busy": True})
        record.findings.append(
            Finding(
                rule_id="PIPELINE-013",
                severity="FAIL",
                message_key="pipeline.PIPELINE-013.cause",
                fix_key="pipeline.PIPELINE-013.fix",
                params={"stage": node.stage, "node_dir": mask_text(str(node_dir))},
                evidence={"node_hash": node.node_hash, "node_dir": mask_text(str(node_dir))},
                scope=node.stage,
            )
        )
        return record

    def _execute_locked(self, node: Node, node_dir: Path, available: Artifacts) -> StageRecord:
        assert node.node_hash is not None
        partial = self.incremental and node.incremental and not node.forced
        # a full re-run (default mode, --force) starts from an empty directory; an incremental
        # one keeps pairs/ so the engine can pick up the results that are still valid
        out_dir, log_dir = cache.prepare_node_dir(node_dir, clean=True, keep_pairs=partial)
        record = StageRecord(
            stage=node.stage,
            node_hash=node.node_hash,
            engine=node.engine,
            engine_version=node.engine_version,
            params=node.params,
            inputs=dict(node.input_hashes),
            status="running",
            started_at=_now(),
            log_path=log_dir,
            extra={"forced": node.forced, "run_id": self.run_id, "cache_hit": False},
        )
        cache.write_record(record, node_dir)

        estimate = self.estimates.get(node.stage, Resources())
        need = estimate.peak_rss_gb
        if need is not None and not self.budget.fits(need):
            record.findings.append(
                Finding(
                    rule_id="PIPELINE-006",
                    severity="WARN",
                    message_key="pipeline.PIPELINE-006.cause",
                    fix_key="pipeline.PIPELINE-006.fix",
                    params={
                        "stage": node.stage,
                        "need_gb": float(need),
                        "budget_gb": self.budget.available_gb,
                    },
                    evidence={"need_gb": need, "budget_gb": self.budget.available_gb},
                    scope=node.stage,
                )
            )
        granted = self.budget.reserve(node.stage, need)
        inputs = Artifacts(
            items={
                n: available[n]
                for n in [*node.spec.inputs, *node.spec.optional_inputs]
                if n in available
            }
        )
        params: dict[str, Any] = {
            **node.params,
            "_out_dir": str(out_dir),
            "_log_dir": str(log_dir),
            "_workdir": str(self.workdir),
            "_cache_dir": str(self.cfg.cache_dir),
            "_cores": int(self.machine.cores),
            "_memory_gb": float(granted),
            "_gpu": bool(self.machine.gpu),
        }
        if partial:
            done = incremental.PairCache.load(node_dir, node.stage)
            params[incremental.PAIRS_DIR_KEY] = str(incremental.pairs_dir(node_dir))
            params[incremental.PAIRS_DONE_KEY] = done.done_entries()
            record.extra["incremental"] = {
                "requested": True,
                **incremental.pair_counts(node.pairs_expected, done.done),
            }
        t0 = time.perf_counter()
        try:
            with stage_gpu(params.get("_gpu")):
                outputs, extra_findings = self._dispatch(node, inputs, params, out_dir, log_dir)
            record.findings.extend(extra_findings)
            outputs = self._hash_outputs(node, outputs)
            record.outputs = {n: str(a.path) for n, a in outputs.items.items()}
            record.extra[cache.ARTIFACTS_KEY] = cache.artifacts_to_extra(outputs)
            self._lift_tile_dirs(record, outputs)
            self._record_actual_unwrap_engine(node, record, outputs)
            self._record_pairs(node, record, outputs, requested=partial)
            record.status = "ok"
        except Exception as exc:
            self._on_failure(node, record, log_dir, exc)
        finally:
            self.budget.release(node.stage)
            record.finished_at = _now()
            record.resources = Resources(
                wall_time_s=time.perf_counter() - t0,
                peak_rss_gb=_peak_rss_gb(),
                disk_gb=cache.dir_size(out_dir) / 1e9,
                notes={"rss_method": "ru_maxrss process high-water mark (not per-stage)"},
            )
            cache.write_record(record, node_dir)
        return record

    def _lift_tile_dirs(self, record: StageRecord, outputs: Artifacts) -> None:
        """Copy per-pair unwrapping tile directories from artifact meta into the manifest.

        Re-tuning only the tile-assembly parameters can then re-use the existing tiles
        (SNAPHU assemble-only) instead of unwrapping every tile again (plan §6.2 PERF-03).
        # source: src/wintersar/unwrap/api.py::run_unwrap -> Artifact("unw", meta["tile_dirs"])
        """
        for art in outputs.items.values():
            dirs = art.meta.get("tile_dirs")
            if isinstance(dirs, dict) and dirs:
                record.extra["tile_dirs"] = {str(k): str(v) for k, v in dirs.items()}
                return

    def _record_pairs(
        self, node: Node, record: StageRecord, outputs: Artifacts, requested: bool
    ) -> None:
        """Copy the per-pair accounting an incremental engine reports into the manifest.

        ``meta["pairs"]`` (the pair set the artifact holds) is always recorded when present;
        ``pairs_reused``/``pairs_computed`` fill ``extra["incremental"]``. An engine that was
        asked for sub-caching but reported nothing recomputed everything: say so
        (``PIPELINE-016``, INFO) instead of letting the run look incremental.
        # source: src/wintersar/pipeline/incremental.py::PairCache.summary (meta keys)
        """
        pairs: list[str] | None = None
        reused: list[str] | None = None
        computed: list[str] | None = None
        for art in outputs.items.values():
            got = art.meta.get(incremental.META_PAIRS)
            if pairs is None and isinstance(got, list | tuple):
                pairs = [str(k) for k in got]
            r, c = art.meta.get(incremental.META_REUSED), art.meta.get(incremental.META_COMPUTED)
            if reused is None and isinstance(r, list | tuple) and isinstance(c, list | tuple):
                reused, computed = [str(k) for k in r], [str(k) for k in c]
        if pairs is not None:
            record.extra["pairs"] = pairs
        if not requested:
            return
        info = record.extra.setdefault("incremental", {"requested": True})
        if reused is not None and computed is not None:
            info.update({"supported": True, "reused": len(reused), "computed": len(computed)})
            info["computed_pairs"] = computed
            return
        info.update({"supported": False, "reused": 0, "computed": len(pairs or [])})
        record.findings.append(
            Finding(
                rule_id="PIPELINE-016",
                severity="INFO",
                message_key="pipeline.PIPELINE-016.cause",
                fix_key="pipeline.PIPELINE-016.fix",
                params={"stage": node.stage, "engine": node.engine or "python"},
                evidence={"node_hash": node.node_hash},
                scope=node.stage,
            )
        )

    def _record_actual_unwrap_engine(
        self, node: Node, record: StageRecord, outputs: Artifacts
    ) -> None:
        """``unwrap.method: auto`` is hashed as snaphu, but the scheduler may pick tophu.

        The manifest/report must name the backend that actually ran; the cache key keeps
        the planned one (it is all the DAG can know before the stage runs).
        # source: src/wintersar/unwrap/api.py::run_unwrap -> Artifact("unw", meta["method"])
        """
        if node.stage != "unwrap" or node.engine == FAKE_ENGINE:
            return
        art = outputs.items.get("unw")
        method = str(art.meta.get("method") or "") if art is not None else ""
        if not method or method == record.engine:
            return
        record.extra["engine_planned"] = record.engine
        record.engine = method
        record.engine_version = self.dag.engine_version(method)

    def _dispatch(
        self,
        node: Node,
        inputs: Artifacts,
        params: dict[str, Any],
        out_dir: Path,
        log_dir: Path,
    ) -> tuple[Artifacts, list[Finding]]:
        if node.spec.is_python:
            fn = load_python_stage(node.stage)
            if fn is None:  # pragma: no cover - build() marks such nodes skipped
                msg = f"python stage {node.stage!r} has no implementation"
                raise RuntimeError(msg)
            return _as_artifacts(fn(self.cfg, inputs, params, out_dir, log_dir))
        assert node.engine is not None
        if node.stage == "unwrap" and node.engine != FAKE_ENGINE:
            run_unwrap = load_entrypoint(*UNWRAP_ENTRYPOINT)
            if run_unwrap is None:
                msg = "unwrap scheduler wintersar.unwrap.api.run_unwrap is not available"
                raise RuntimeError(msg)
            return _as_artifacts(
                run_unwrap(inputs["igrams"], params, out_dir, log_dir, self.machine)
            )
        engine = self.dag.engine(node.engine)
        if engine is None:  # pragma: no cover - build() blocks unregistered engines
            msg = f"engine {node.engine!r} is not registered"
            raise RuntimeError(msg)
        install = engine.check_install()
        if any(f.is_fail for f in install):
            err = EngineNotAvailableError(f"engine {node.engine!r} is not available")
            err.findings = install  # type: ignore[attr-defined]
            raise err
        return engine.run(node.stage, inputs, params, log_dir), [
            f for f in install if not f.is_fail
        ]

    def _hash_outputs(self, node: Node, outputs: Artifacts) -> Artifacts:
        for name, art in outputs.items.items():
            if not Path(art.path).exists():
                exc = FileNotFoundError(f"output {name!r} missing: {art.path}")
                exc.findings = [  # type: ignore[attr-defined]
                    Finding(
                        rule_id="PIPELINE-009",
                        severity="FAIL",
                        message_key="pipeline.PIPELINE-009.cause",
                        fix_key="pipeline.PIPELINE-009.fix",
                        params={
                            "stage": node.stage,
                            "artifact": name,
                            "path": mask_text(str(art.path)),
                        },
                        scope=node.stage,
                    )
                ]
                raise exc
        return cache.hash_artifacts(outputs, fast=True)

    def _on_failure(
        self, node: Node, record: StageRecord, log_dir: Path, exc: BaseException
    ) -> None:
        record.status = "failed"
        error = mask_text(f"{type(exc).__name__}: {exc}")
        record.extra["error"] = error
        attached: list[Finding] = [
            f for f in getattr(exc, "findings", []) or [] if isinstance(f, Finding)
        ]
        diagnosed, diag_error = diagnose_logs(log_dir, node.engine)
        if diag_error:
            record.extra["diagnose_error"] = diag_error
        kb = kb_engine(node.engine)
        generic = Finding(
            rule_id="PIPELINE-001",
            severity="FAIL",
            message_key="pipeline.PIPELINE-001.cause",
            fix_key=f"pipeline.PIPELINE-001.{'fix' if kb else 'fix_auto'}",
            params={
                "stage": node.stage,
                "engine": node.engine or "python",
                "kb_engine": kb or "",
                "error": error,
                "log_dir": mask_text(str(log_dir)),
            },
            evidence={
                "error": error,
                "log_excerpt": log_excerpt(log_dir),
                "node_hash": node.node_hash,
            },
            scope=node.stage,
        )
        record.findings.extend([generic, *attached, *diagnosed])
        hint = _retry_hint(exc, [*attached, *diagnosed])
        if hint is not None:
            record.extra[RETRY_HINT_KEY] = hint
            record.findings.append(
                Finding(
                    rule_id="PIPELINE-008",
                    severity="INFO",
                    message_key="pipeline.PIPELINE-008.cause",
                    fix_key="pipeline.PIPELINE-008.fix",
                    params={"stage": node.stage, "hint": mask_text(str(hint))},
                    evidence={RETRY_HINT_KEY: hint},
                    scope=node.stage,
                )
            )


def _cache_hit(record: StageRecord, fallback: bool = False) -> StageRecord:
    return record.model_copy(
        update={"extra": {**record.extra, "cache_hit": True, "fallback": fallback}}
    )
