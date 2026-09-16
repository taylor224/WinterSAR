"""Lightweight linear DAG with content-addressed nodes (plan §5.3, PERF-03; ADR-0030/0031).

A :class:`Node` is one stage of :data:`wintersar.pipeline.stages.STAGE_ORDER` together with
its canonical parameters, the engine that will run it and the hashes of the input
artifacts it consumes. ``node_hash`` is only defined once every input hash is known, which
is the case when all upstream producers are cached (resolved at :meth:`Dag.build`) or have
just run (resolved by the executor). A node whose inputs are unresolved is *to run*.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from wintersar.engines.base import Engine, get_engine, list_engines
from wintersar.io.schemas import Artifacts, Finding, StageRecord
from wintersar.pipeline import cache
from wintersar.pipeline.config import Config
from wintersar.pipeline.stages import (
    FAKE_ENGINE,
    STAGES,
    StageSpec,
    is_fake_path,
    load_python_stage,
    resolve_engine,
    stage_index,
    stage_is_configured,
    stage_window,
)
from wintersar.util.hashing import hash_params

NodeStatus = Literal["cached", "to_run", "skipped", "blocked"]
SkipReason = Literal[
    "fake_path", "not_configured", "unavailable", "engine_no_stage", "produced_upstream"
]

PRIVATE_PREFIX = "_"


# ---------------------------------------------------------------------- params


def canonicalise(obj: Any) -> Any:
    """Sort mapping keys recursively and turn tuples into lists (stable JSON)."""
    if isinstance(obj, dict):
        return {str(k): canonicalise(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, list | tuple):
        return [canonicalise(x) for x in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def strip_private(params: dict[str, Any]) -> dict[str, Any]:
    """Drop executor-private keys (``_out_dir`` …) which never enter the hash."""
    return {k: v for k, v in params.items() if not str(k).startswith(PRIVATE_PREFIX)}


def canonical_params(
    cfg: Config, stage: str, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    """``Config.stage_params(stage)`` deep-merged with ``overrides`` (ADR-0034).

    Config sections stay nested (``params["unwrap"]["cost"]``); overrides are merged at
    the top level (``{"n_dates": 5}``) or nested (``{"unwrap": {"cost": "smooth"}}``).
    """
    base = cfg.stage_params(stage)
    merged = deep_merge(base, strip_private(overrides or {}))
    result = canonicalise(merged)
    if not isinstance(result, dict):  # pragma: no cover - canonicalise keeps dict type
        msg = "canonical params must be a mapping"
        raise TypeError(msg)
    return result


def node_hash(
    stage: str,
    params: dict[str, Any],
    input_hashes: dict[str, str],
    engine: str | None,
    engine_version: str | None,
) -> str:
    """``hash(stage, canonical params, input artifact hashes, engine, engine version)``."""
    payload = {
        "stage": stage,
        "params": canonicalise(strip_private(params)),
        "inputs": {k: input_hashes[k] for k in sorted(input_hashes)},
        "engine": engine,
        "engine_version": engine_version,
    }
    return hash_params(payload)


# ---------------------------------------------------------------------- nodes


@dataclass
class Node:
    stage: str
    spec: StageSpec
    params: dict[str, Any]
    engine: str | None
    engine_version: str | None
    input_hashes: dict[str, str] = field(default_factory=dict)
    node_hash: str | None = None
    producers: dict[str, str] = field(default_factory=dict, metadata={"doc": "input -> stage"})
    record: StageRecord | None = None
    forced: bool = False
    skip_reason: SkipReason | None = None
    fallback: bool = False
    findings: list[Finding] = field(default_factory=list)

    # -------------------------------------------------------------- state
    @property
    def cached(self) -> bool:
        return self.record is not None and self.record.status == "ok" and not self.forced

    @property
    def blocked(self) -> bool:
        return any(f.is_fail for f in self.findings)

    @property
    def resolved(self) -> bool:
        return self.node_hash is not None

    @property
    def status(self) -> NodeStatus:
        if self.skip_reason is not None:
            return "skipped"
        if self.blocked:
            return "blocked"
        if self.cached:
            return "cached"
        return "to_run"

    @property
    def artifacts(self) -> Artifacts:
        if self.record is None:
            return Artifacts()
        return cache.record_artifacts(self.record)

    def provisional_hash(self) -> str:
        """Identity used in plans while inputs are unresolved (never a cache key)."""
        return "pending-" + node_hash(
            self.stage,
            self.params,
            {k: self.producers.get(k, "?") for k in self.spec.inputs},
            self.engine,
            self.engine_version,
        )

    def pending_record(self) -> StageRecord:
        status: Literal["pending", "skipped"] = (
            "skipped" if self.skip_reason is not None else "pending"
        )
        return StageRecord(
            stage=self.stage,
            node_hash=self.node_hash or self.provisional_hash(),
            engine=self.engine,
            engine_version=self.engine_version,
            params=self.params,
            inputs=dict(self.input_hashes),
            status=status,
            findings=list(self.findings),
            extra={
                "provisional": self.node_hash is None,
                "skip_reason": self.skip_reason,
                "forced": self.forced,
            },
        )


# ---------------------------------------------------------------------- dag


class Dag:
    """Build and resolve the stage graph for one configuration."""

    def __init__(self, cfg: Config, workdir: Path | None = None) -> None:
        self.cfg = cfg
        self.workdir = Path(workdir or cfg.workdir)
        self.nodes: list[Node] = []
        self._engines: dict[str, Engine | None] = {}
        self._versions: dict[str, str | None] = {}

    # -------------------------------------------------------------- engines
    def engine(self, name: str) -> Engine | None:
        if name not in self._engines:
            try:
                self._engines[name] = get_engine(name)
            except KeyError:
                self._engines[name] = None
        return self._engines[name]

    def engine_version(self, name: str) -> str | None:
        if name not in self._versions:
            eng = self.engine(name)
            self._versions[name] = eng.detect_version() if eng is not None else None
        return self._versions[name]

    # -------------------------------------------------------------- build
    def build(
        self,
        param_overrides: dict[str, dict[str, Any]] | None = None,
        until: str | None = None,
        from_stage: str | None = None,
        force: list[str] | None = None,
    ) -> list[Node]:
        overrides = param_overrides or {}
        for s in list(overrides) + list(force or []):
            stage_index(s)  # raises KeyError for unknown stages
        window = stage_window(until, from_stage)
        self.nodes = [self._make_node(s, overrides.get(s)) for s in window]
        self._link_producers()
        self._apply_force(set(force or []))
        self.resolve_all(from_stage=from_stage)
        return self.nodes

    def _make_node(self, stage: str, overrides: dict[str, Any] | None) -> Node:
        spec = STAGES[stage]
        engine = resolve_engine(self.cfg, stage)
        node = Node(
            stage=stage,
            spec=spec,
            params=canonical_params(self.cfg, stage, overrides),
            engine=engine,
            engine_version=self.engine_version(engine) if engine else None,
        )
        if spec.is_python:
            if is_fake_path(self.cfg) and stage in ("search", "precheck"):
                node.skip_reason = "fake_path"
            elif spec.optional and not stage_is_configured(self.cfg, stage):
                node.skip_reason = "not_configured"
                if stage == "validate":
                    node.findings.append(
                        Finding(
                            rule_id="PIPELINE-010",
                            severity="INFO",
                            message_key="pipeline.PIPELINE-010.cause",
                            fix_key="pipeline.PIPELINE-010.fix",
                            scope=stage,
                        )
                    )
            elif load_python_stage(stage) is None:
                from wintersar.pipeline.stages import PYTHON_STAGE_ENTRYPOINTS

                mod, fn = PYTHON_STAGE_ENTRYPOINTS[stage]
                node.skip_reason = "unavailable"
                node.findings.append(
                    Finding(
                        rule_id="PIPELINE-004",
                        severity="INFO",
                        message_key="pipeline.PIPELINE-004.cause",
                        fix_key="pipeline.PIPELINE-004.fix",
                        params={"stage": stage, "module": mod, "function": fn},
                        scope=stage,
                    )
                )
            return node
        assert engine is not None
        eng = self.engine(engine)
        # Engines that bundle unwrapping (HyP3 burst InSAR) declare ``produces`` with "unw":
        # the interferogram node then also outputs "unw" and the local unwrap stage is skipped.
        ig_engine = resolve_engine(self.cfg, "interferogram")
        ig_cls = self.engine(ig_engine) if ig_engine else None
        upstream_unw = bool(ig_cls is not None and "unw" in getattr(ig_cls, "produces", ()))
        if stage == "interferogram" and upstream_unw and "unw" not in spec.outputs:
            node.spec = replace(spec, outputs=[*spec.outputs, "unw"])
        if stage == "unwrap" and upstream_unw:
            node.skip_reason = "produced_upstream"
            node.findings.append(
                Finding(
                    rule_id="PIPELINE-011",
                    severity="INFO",
                    message_key="pipeline.PIPELINE-011.cause",
                    fix_key="pipeline.PIPELINE-011.fix",
                    params={"engine": ig_engine or ""},
                    scope=stage,
                )
            )
            return node
        if eng is None:
            node.findings.append(
                Finding(
                    rule_id="PIPELINE-005",
                    severity="FAIL",
                    message_key="pipeline.PIPELINE-005.cause",
                    fix_key="pipeline.PIPELINE-005.fix",
                    params={
                        "stage": stage,
                        "engine": engine,
                        "engine_key": spec.engine_key,
                        "known": ", ".join(sorted(list_engines())),
                    },
                    evidence={"engine": engine},
                    scope=stage,
                )
            )
        elif stage not in eng.stages and not (stage == "unwrap" and engine != FAKE_ENGINE):
            # unwrap on the real path goes through wintersar.unwrap.api (scheduler), which
            # picks the engine itself; other stages need the engine to declare them.
            node.skip_reason = "engine_no_stage"
            node.findings.append(
                Finding(
                    rule_id="PIPELINE-007",
                    severity="INFO",
                    message_key="pipeline.PIPELINE-007.cause",
                    fix_key="pipeline.PIPELINE-007.fix",
                    params={"stage": stage, "engine": engine, "stages": ", ".join(eng.stages)},
                    scope=stage,
                )
            )
        return node

    def _link_producers(self) -> None:
        """input artifact name -> latest upstream (non-skipped) stage producing it."""
        producers: dict[str, str] = {}
        for node in self.nodes:
            for name in [*node.spec.inputs, *node.spec.optional_inputs]:
                if name in producers:
                    node.producers[name] = producers[name]
            if node.skip_reason is None:
                for name in node.spec.outputs:
                    producers[name] = node.stage

    def _apply_force(self, forced: set[str]) -> None:
        """``--force STAGE`` re-runs the stage and everything downstream of it (ADR-0033)."""
        if not forced:
            return
        by_stage = {n.stage: n for n in self.nodes}
        closure = {s for s in forced if s in by_stage}
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                if node.stage in closure:
                    continue
                if any(p in closure for p in node.producers.values()):
                    closure.add(node.stage)
                    changed = True
        for node in self.nodes:
            if node.stage in closure and node.skip_reason is None:
                node.forced = True

    # -------------------------------------------------------------- resolve
    def resolve(self, node: Node, available: Artifacts) -> bool:
        """Fill ``input_hashes``/``node_hash`` from ``available`` upstream artifacts.

        Returns ``False`` (and leaves ``node_hash`` ``None``) when a required input is not
        available yet. A required input whose producer was *skipped* is tolerated when the
        consumer is the fake engine or the same engine that skipped the producer.
        """
        node.findings = [f for f in node.findings if f.rule_id != "PIPELINE-002"]
        hashes: dict[str, str] = {}
        for name in node.spec.inputs:
            if name in available and available[name].sha256:
                hashes[name] = str(available[name].sha256)
                continue
            producer_stage = node.producers.get(name)
            producer = next((n for n in self.nodes if n.stage == producer_stage), None)
            if producer is None:
                # nobody in the window produces it: either skipped upstream or not in graph
                skipper = self._skipped_producer(name)
                if skipper is not None and self._tolerates_missing(node, skipper):
                    continue
                node.findings.append(self._missing_input(node, name, skipper))
                node.node_hash = None
                return False
            node.node_hash = None
            return False  # producer will run first; resolve again afterwards
        for name in node.spec.optional_inputs:
            if name in available and available[name].sha256:
                hashes[name] = str(available[name].sha256)
        node.input_hashes = hashes
        node.node_hash = node_hash(
            node.stage, node.params, hashes, node.engine, node.engine_version
        )
        return True

    def _skipped_producer(self, name: str) -> Node | None:
        for n in reversed(self.nodes):
            if name in n.spec.outputs and n.skip_reason is not None:
                return n
        return None

    @staticmethod
    def _tolerates_missing(node: Node, skipper: Node) -> bool:
        return node.engine == FAKE_ENGINE or (
            node.engine is not None and node.engine == skipper.engine
        )

    @staticmethod
    def _missing_input(node: Node, name: str, skipper: Node | None) -> Finding:
        producer = skipper.stage if skipper is not None else "?"
        return Finding(
            rule_id="PIPELINE-002",
            severity="FAIL",
            message_key="pipeline.PIPELINE-002.cause",
            fix_key="pipeline.PIPELINE-002.fix",
            params={"stage": node.stage, "artifact": name, "producer": producer},
            evidence={"artifact": name, "producer": producer},
            scope=node.stage,
        )

    def lookup_cache(self, node: Node) -> StageRecord | None:
        if node.node_hash is None or node.forced:
            return None
        return cache.find_cached(self.workdir, node.stage, node.node_hash)

    def resolve_all(self, from_stage: str | None = None) -> Artifacts:
        """Forward pass using only cached upstream outputs (the *plan* view).

        Stages before ``from_stage`` that have no matching cache entry fall back to their
        most recent ``ok`` manifest (WARN ``PIPELINE-003``) or block (``PIPELINE-002``).
        """
        available = Artifacts()
        start = stage_index(from_stage) if from_stage else 0
        for node in self.nodes:
            node.record = None
            node.fallback = False
            node.findings = [f for f in node.findings if f.rule_id != "PIPELINE-003"]
            if node.skip_reason is not None:
                continue
            resolved = self.resolve(node, available)
            record = self.lookup_cache(node) if resolved else None
            if record is None and stage_index(node.stage) < start:
                record = cache.latest_record(self.workdir, node.stage)
                if record is None:
                    node.findings.append(self._missing_input(node, "*", None))
                    node.findings[-1].params.update(
                        {"artifact": ",".join(node.spec.outputs), "producer": node.stage}
                    )
                    continue
                node.fallback = True
                node.forced = False
                node.findings.append(
                    Finding(
                        rule_id="PIPELINE-003",
                        severity="WARN",
                        message_key="pipeline.PIPELINE-003.cause",
                        fix_key="pipeline.PIPELINE-003.fix",
                        params={
                            "stage": node.stage,
                            "node_hash": record.node_hash,
                            "finished_at": (
                                record.finished_at.isoformat() if record.finished_at else "?"
                            ),
                        },
                        scope=node.stage,
                    )
                )
                node.input_hashes = dict(record.inputs)
                node.node_hash = record.node_hash
            if record is not None:
                node.record = record
                available = available.merged(cache.record_artifacts(record))
        return available

    # -------------------------------------------------------------- views
    def to_run(self) -> list[Node]:
        return [n for n in self.nodes if n.status == "to_run"]

    def cached(self) -> list[Node]:
        return [n for n in self.nodes if n.status == "cached"]

    def skipped(self) -> list[Node]:
        return [n for n in self.nodes if n.status == "skipped"]

    def blocked(self) -> list[Node]:
        return [n for n in self.nodes if n.status == "blocked"]

    def node(self, stage: str) -> Node:
        for n in self.nodes:
            if n.stage == stage:
                return n
        msg = f"stage {stage!r} is not part of this DAG"
        raise KeyError(msg)

    def downstream(self, stage: str) -> list[str]:
        """Stages that (transitively) consume an output of ``stage``."""
        closure = {stage}
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                if node.stage not in closure and any(p in closure for p in node.producers.values()):
                    closure.add(node.stage)
                    changed = True
        closure.discard(stage)
        return [n.stage for n in self.nodes if n.stage in closure and n.skip_reason is None]
