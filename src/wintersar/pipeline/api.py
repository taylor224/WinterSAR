"""Python API of the pipeline: :func:`plan` (dry run) and :func:`run` (plan §5.3, R-05,
R-11, PERF-03, PERF-06). The CLI and the QGIS plugin call these; nothing here prints.

``incremental=True`` (PERF-06, ADR-0080) lets a stage whose date set grew reuse the per-pair
results its node directory already holds instead of recomputing the whole stack; the plan
then reports ``N pairs cached / M new`` per stage (``StageRecord.extra["incremental"]`` and
the ``PIPELINE-015`` findings).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wintersar.io.schemas import Artifacts, Finding, Plan, StageRecord
from wintersar.pipeline import cache
from wintersar.pipeline.config import Config
from wintersar.pipeline.dag import Dag
from wintersar.pipeline.executor import Executor, PipelineError, machine_budget
from wintersar.pipeline.plan import build_plan, plan_estimates
from wintersar.util import sysinfo
from wintersar.util.masking import mask_mapping
from wintersar.util.output import to_jsonable

RUNS_DIRNAME = "runs"


@dataclass
class RunResult:
    records: list[StageRecord]
    artifacts: Artifacts
    findings: list[Finding]
    plan: Plan
    ok: bool
    error: str | None = None
    failed_stage: str | None = None
    run_id: str | None = None
    dry_run: bool = False
    incremental: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ran(self) -> list[StageRecord]:
        return [
            r for r in self.records if r.status in ("ok", "failed") and not r.extra.get("cache_hit")
        ]

    @property
    def cached(self) -> list[StageRecord]:
        return [r for r in self.records if r.extra.get("cache_hit")]

    @property
    def skipped(self) -> list[StageRecord]:
        return [r for r in self.records if r.status == "skipped"]

    @property
    def partial(self) -> list[StageRecord]:
        """Stages that ran incrementally (some pairs reused, PERF-06)."""
        return [
            r
            for r in self.ran
            if isinstance(r.extra.get("incremental"), dict)
            and int(r.extra["incremental"].get("reused") or 0) > 0
        ]

    def pair_summary(self) -> dict[str, Any]:
        """Per-pair accounting of the incremental stages that ran (PERF-06).

        ``by_stage`` holds every stage's ``{"reused", "computed"}``. The top-level
        ``reused`` / ``computed`` are the counts of one stage, ``stage`` — the one that
        computed the most pairs (ties: the upstream-most), i.e. the stage that bounds what
        the run had to do. They are **never summed** over stages: every pair stage handles
        the same pair set, so a sum would count each pair once per stage and contradict the
        per-stage ``PIPELINE-015`` findings.
        """
        by_stage: dict[str, dict[str, int]] = {}
        for r in self.ran:
            info = r.extra.get("incremental")
            if isinstance(info, dict):
                by_stage[r.stage] = {
                    "reused": int(info.get("reused") or 0),
                    "computed": int(info.get("computed") or 0),
                }
        stage = max(by_stage, key=lambda s: by_stage[s]["computed"], default=None)
        top = by_stage.get(stage or "", {"reused": 0, "computed": 0})
        return {
            "reused": top["reused"],
            "computed": top["computed"],
            "stage": stage,
            "by_stage": by_stage,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "incremental": self.incremental,
            "pairs": self.pair_summary(),
            "run_id": self.run_id,
            "error": self.error,
            "failed_stage": self.failed_stage,
            "records": [r.model_dump(mode="json") for r in self.records],
            "artifacts": {n: a.model_dump(mode="json") for n, a in self.artifacts.items.items()},
            "plan": self.plan.model_dump(mode="json"),
            "findings": [f.model_dump(mode="json") for f in self.findings],
        }


def plan(
    cfg: Config,
    until: str | None = None,
    from_stage: str | None = None,
    force: list[str] | None = None,
    param_overrides: dict[str, dict[str, Any]] | None = None,
    machine: sysinfo.MachineSpec | None = None,
    incremental: bool = False,
) -> Plan:
    """Dry run: which nodes are cached / to run (or partially cached with
    ``incremental=True``), plus estimated resources."""
    return build_plan(
        cfg,
        until=until,
        from_stage=from_stage,
        force=force,
        param_overrides=param_overrides,
        machine=machine_budget(cfg, machine),
        incremental=incremental,
    )


def run(
    cfg: Config,
    until: str | None = None,
    from_stage: str | None = None,
    force: list[str] | None = None,
    param_overrides: dict[str, dict[str, Any]] | None = None,
    dry_run: bool = False,
    machine: sysinfo.MachineSpec | None = None,
    incremental: bool = False,
) -> RunResult:
    """Execute the pipeline (cached stages are reused). Never raises for stage failures:
    inspect ``RunResult.ok`` / ``findings``. Fails fast (before executing anything) when
    the plan already carries a FAIL finding (engine missing, blocked input).

    ``incremental=True``: a stage whose date set grew re-runs in its existing node directory
    and reuses the per-pair results kept there (PERF-06); the time series is re-inverted.
    """
    budget = machine_budget(cfg, machine)
    dag = Dag(cfg, incremental=incremental)
    dag.build(param_overrides, until=until, from_stage=from_stage, force=force)
    the_plan = build_plan(cfg, machine=budget, dag=dag)
    plan_ok = not any(f.is_fail for f in the_plan.findings)
    if dry_run or not plan_ok:
        result = RunResult(
            records=list(the_plan.stages),
            artifacts=_cached_artifacts(dag),
            findings=dedupe_findings(list(the_plan.findings)),
            plan=the_plan,
            ok=plan_ok,
            dry_run=dry_run,
            incremental=incremental,
            error=None if plan_ok else "plan has FAIL findings",
            failed_stage=next((n.stage for n in dag.blocked()), None),
        )
        if not dry_run:
            _write_run_summary(dag.workdir, result)
        return result
    executor = Executor(
        cfg, dag, machine=budget, from_stage=from_stage, estimates=plan_estimates(the_plan)
    )
    try:
        records, artifacts, findings = executor.run()
    except PipelineError as exc:
        result = RunResult(
            records=list(exc.records),
            artifacts=exc.artifacts,
            findings=dedupe_findings([*the_plan.findings, *exc.findings]),
            plan=the_plan,
            ok=False,
            error=str(exc),
            failed_stage=exc.failed_stage,
            run_id=executor.run_id,
            incremental=incremental,
        )
    else:
        result = RunResult(
            records=records,
            artifacts=artifacts,
            findings=dedupe_findings([*the_plan.findings, *findings]),
            plan=the_plan,
            ok=True,
            run_id=executor.run_id,
            incremental=incremental,
        )
    _write_run_summary(dag.workdir, result)
    return result


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """Drop repeats of the same (rule, severity, scope, message, params).

    ``run`` concatenates the plan's findings with the executor's, and the executor
    re-resolves the very same DAG nodes, so every DAG-level finding (PIPELINE-010, …) was
    reported twice.
    """
    seen: set[str] = set()
    out: list[Finding] = []
    for f in findings:
        key = repr(
            (f.rule_id, f.severity, f.scope, f.message_key, sorted(f.params.items(), key=str))
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _cached_artifacts(dag: Dag) -> Artifacts:
    arts = Artifacts()
    for node in dag.nodes:
        if node.record is not None and node.record.status == "ok":
            arts = arts.merged(cache.record_artifacts(node.record))
    return arts


def _write_run_summary(workdir: Path, result: RunResult) -> Path | None:
    """``work/runs/<run_id>.json`` (masked) for reports and the QGIS plugin."""
    try:
        runs = Path(workdir) / RUNS_DIRNAME
        runs.mkdir(parents=True, exist_ok=True)
        name = result.run_id or "plan"
        p = runs / f"{name}.json"
        p.write_text(
            json.dumps(mask_mapping(to_jsonable(result.to_dict())), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result.extra["summary_path"] = str(p)
        return p
    except OSError:
        return None
