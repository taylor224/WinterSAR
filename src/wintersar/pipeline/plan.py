"""``wintersar plan``: dry run of the DAG (plan §5.3) — which nodes are cached, which will
run, and the estimated resources/credits for the latter.

Estimates come from ``Engine.estimate`` (per engine) plus
``wintersar.diagnose.resources.estimate`` (lazy; absent -> nothing added). An incremental
node (PERF-06) is sized by the pairs it still has to compute and reported with
``extra["incremental"]`` (``N pairs cached / M new``) plus an INFO ``PIPELINE-015``; a node
whose per-pair manifest was unreadable gets ``PIPELINE-017`` (full recompute, cause named).
A GPU request the machine cannot honour (``compute.gpu: true`` without CuPy) is reported
once as ``ENV-005`` (WARN, ADR-0095) when something will run.
"""

from __future__ import annotations

from typing import Any

from wintersar.io.schemas import Artifacts, Finding, Plan, Resources, StageRecord
from wintersar.pipeline import cache
from wintersar.pipeline.config import Config
from wintersar.pipeline.dag import Dag, Node
from wintersar.pipeline.executor import cache_hit_record, gpu_findings
from wintersar.pipeline.incremental import manifest_finding
from wintersar.pipeline.stages import DIAGNOSE_RESOURCES_ENTRYPOINT, load_entrypoint
from wintersar.util import sysinfo

_SIZE_SOURCES = ("igrams", "unw", "timeseries", "stack", "slc_manifest")


def stage_size(node: Node, available: Artifacts) -> tuple[int, int]:
    """``(n_pairs, pixels)`` for the resource model, from upstream artifact ``meta``
    (``n_pairs``, ``shape``) or the node's top-level params; ``0`` when unknown."""
    n_pairs = 0
    pixels = 0
    metas: list[dict[str, Any]] = [dict(available[n].meta) for n in _SIZE_SOURCES if n in available]
    metas.append(node.params)
    for meta in metas:
        if not n_pairs:
            n_pairs = int(meta.get("n_pairs") or 0)
        shape = meta.get("shape")
        if not pixels and isinstance(shape, list | tuple) and len(shape) >= 2:
            pixels = int(shape[-2]) * int(shape[-1])
    if node.partial is not None and node.partial.usable:
        # an incremental node only computes the pairs it does not hold yet (PERF-06)
        new = node.partial.counts.get("new")
        if new is not None:
            n_pairs = int(new)
    elif not n_pairs and node.pairs_expected:
        n_pairs = len(node.pairs_expected)
    return n_pairs, pixels


def model_params(node: Node) -> dict[str, Any]:
    """Flatten config sections and map wintersar names onto the resource model's keys.

    # source: src/wintersar/diagnose/resources.py::estimate (``nproc``, ``ntiles``,
    #         ``memory_mb_per_mpixel``, ``looks``, ``pixel_m``, ``n_bursts``, ``product``)
    """
    flat: dict[str, Any] = {}
    for key, value in node.params.items():
        if isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    unwrap = node.params.get("unwrap")
    if isinstance(unwrap, dict):
        if unwrap.get("nproc_per_igram") is not None:
            flat["nproc"] = unwrap["nproc_per_igram"]
        tiles = unwrap.get("tiles")
        if isinstance(tiles, dict) and "rows" in tiles and "cols" in tiles:
            flat["ntiles"] = [int(tiles["rows"]), int(tiles["cols"])]
    engine = node.params.get("engine")
    if isinstance(engine, dict) and engine.get("target_pixel_m") is not None:
        flat["pixel_m"] = engine["target_pixel_m"]
    return {k: v for k, v in flat.items() if v is not None}


def estimate_node(
    dag: Dag,
    node: Node,
    machine: sysinfo.MachineSpec | None = None,
    available: Artifacts | None = None,
) -> Resources:
    """Engine estimate + diagnose model for one to-run node (empty when unknown)."""
    total = Resources()
    if node.engine is None:
        return total
    eng = dag.engine(node.engine)
    if eng is not None:
        single = Plan(stages=[node.pending_record()], to_run=[node.node_hash or ""])
        try:
            est = eng.estimate(single)
        except Exception as exc:  # an adapter estimate bug must not break `plan`
            total.notes["estimate_error"] = f"{type(exc).__name__}: {exc}"
        else:
            total = total + est
    fn = load_entrypoint(*DIAGNOSE_RESOURCES_ENTRYPOINT)
    if fn is not None:
        n_pairs, pixels = stage_size(node, available or Artifacts())
        spec = machine or sysinfo.detect()
        try:
            # source: src/wintersar/diagnose/resources.py::estimate(
            #     stage, n_pairs, pixels, engine, machine, params) -> Resources
            modelled: object = fn(
                node.stage, n_pairs, pixels, node.engine, spec, model_params(node)
            )
        except Exception as exc:  # signature drift or model bug: keep the engine estimate
            total.notes["model_error"] = f"{type(exc).__name__}: {exc}"
            modelled = None
        if isinstance(modelled, Resources):
            total = total + modelled
            if modelled.notes:
                total.notes["model"] = modelled.notes
    return total


def build_plan(
    cfg: Config,
    until: str | None = None,
    from_stage: str | None = None,
    force: list[str] | None = None,
    param_overrides: dict[str, dict[str, Any]] | None = None,
    machine: sysinfo.MachineSpec | None = None,
    dag: Dag | None = None,
    incremental: bool = False,
) -> Plan:
    """Build (or reuse) the DAG and describe it without executing anything."""
    if dag is None:
        dag = Dag(cfg, incremental=incremental)
        dag.build(param_overrides, until=until, from_stage=from_stage, force=force)
    stages: list[StageRecord] = []
    to_run: list[str] = []
    cached: list[str] = []
    findings: list[Finding] = []
    total = Resources()
    estimates: dict[str, Resources] = {}
    available = Artifacts()
    for node in dag.nodes:
        findings.extend(node.findings)
        if node.status == "cached" and node.record is not None:
            rec = cache_hit_record(node.record)
            cached.append(rec.node_hash)
            available = available.merged(cache.record_artifacts(node.record))
        elif node.status == "skipped":
            rec = node.pending_record()
        else:
            rec = node.pending_record()
            est = estimate_node(dag, node, machine, available)
            estimates[node.stage] = est
            rec.resources = est
            total = total + est
            to_run.append(rec.node_hash)
            if node.status == "incremental" and node.partial is not None:
                findings.append(incremental_finding(node.stage, node.partial.counts))
            if node.partial is not None and node.partial.manifest_error:
                findings.append(
                    manifest_finding(node.stage, node.partial.manifest_error, node.partial.counts)
                )
        stages.append(rec)
    for name in sorted({n.engine for n in dag.to_run() if n.engine}):
        eng = dag.engine(name)
        if eng is not None:
            findings.extend(eng.check_install())
    if to_run and machine is not None:
        findings.extend(gpu_findings(machine))
    if not to_run:
        # nothing will execute: the additional cost is zero, not "unknown"
        total = Resources(wall_time_s=0.0, peak_rss_gb=0.0, disk_gb=0.0, network_gb=0.0)
    plan = Plan(stages=stages, to_run=to_run, cached=cached, resources=total, findings=findings)
    plan.resources.notes["estimates"] = {
        s: r.model_dump(mode="json", exclude_none=True) for s, r in estimates.items()
    }
    return plan


def incremental_finding(stage: str, counts: dict[str, int | None]) -> Finding:
    """INFO ``PIPELINE-015``: ``N pairs cached / M new`` for a partially cached node."""
    new = counts.get("new")
    return Finding(
        rule_id="PIPELINE-015",
        severity="INFO",
        message_key="pipeline.PIPELINE-015.cause",
        fix_key="pipeline.PIPELINE-015.fix",
        params={
            "stage": stage,
            "n_cached": int(counts.get("cached") or 0),
            "n_new": "?" if new is None else int(new),
        },
        evidence=dict(counts),
        scope=stage,
    )


def plan_estimates(plan: Plan) -> dict[str, Resources]:
    """Per-stage estimates recorded by :func:`build_plan` (for the executor's budget)."""
    raw = plan.resources.notes.get("estimates", {})
    out: dict[str, Resources] = {}
    if isinstance(raw, dict):
        for stage, dump in raw.items():
            if isinstance(dump, dict):
                out[str(stage)] = Resources.model_validate(dump)
    return out
