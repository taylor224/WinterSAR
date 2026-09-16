"""spurt adapter — 3-D (spatial + temporal) unwrapping of a phase-linked stack
(plan §5.2 "spurt.py": "스택 입력(위상 연결 결과) 전용"; ADR-0025).

spurt (isce-framework, v0.1.1, BSD-3-Clause OR Apache-2.0, PyPI ``spurt``) exposes one
workflow, EMCF (extended minimum cost flow), as the console script ``spurt-emcf`` /
``python -m spurt.workflows.emcf``. Its input is a **phase-linked SLC stack directory**
(``*.int.tif`` per date + ``temporal_coherence.tif``; ``SLCStackReader.from_phase_linked_directory``),
not per-pair interferograms, so:

* :meth:`SpurtEngine.unwrap` (single interferogram) raises :class:`StackOnlyEngineError`
  (finding ``UNW-009``) — the scheduler must route 2-D work to ``snaphu``/``tophu``;
* :meth:`SpurtEngine.run` needs the artifact ``phase_linked_stack`` (kind ``dir``) and runs
  the CLI as a subprocess (rule 11.2), writing stdout/stderr and ``--log-file`` under ``log_dir``.

CLI flags verified against ``src/spurt/workflows/emcf/_cli.py`` (v0.1.1); see
:func:`build_spurt_command`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from wintersar.engines._unwrap_common import (
    BoolArray,
    EngineLog,
    EngineRunError,
    FloatArray,
    StackOnlyEngineError,
    UnwrapEngineBase,
    UnwrapError,
    UnwrapResult,
    command_version,
    make_finding,
    out_dir_from,
    public_params,
    unwrap_cfg,
)
from wintersar.engines.base import python_module_version, register_engine
from wintersar.io.schemas import Artifact, Artifacts
from wintersar.util.masking import mask_mapping

# source: https://api.github.com/repos/isce-framework/spurt/releases (v0.1.0 2024-12-16,
# v0.1.1 2025-03-24); PyPI spurt 0.1.1
SPURT_CONSTRAINT = ">=0.1,<1"

# source: spurt v0.1.1 pyproject.toml [project.scripts] spurt-emcf; src/spurt/workflows/emcf/__main__.py
SPURT_EMCF_SCRIPT = "spurt-emcf"
SPURT_EMCF_MODULE = "spurt.workflows.emcf"

# source: spurt v0.1.1 src/spurt/workflows/emcf/_cli.py argparse definitions
# (flag, wintersar param key, type)
_SPURT_FLAGS: tuple[tuple[str, str, type], ...] = (
    ("--t-workers", "t_workers", int),  # <=0 uses ncpus - 1 (default 0)
    ("--s-workers", "s_workers", int),  # default 1
    ("--batchsize", "batchsize", int),  # links per batch (default 150000)
    ("--coh", "temporal_coherence_threshold", float),  # temporal coherence (default 0.6)
    ("--t-cost-type", "t_cost_type", str),  # constant | distance | centroid
    ("--t-cost-scale", "t_cost_scale", int),  # default 100
    ("--pts-per-tile", "pts_per_tile", int),  # default 800000
    ("--max-tiles", "max_tiles", int),  # default 49
    ("--merge-parallel-ifgs", "merge_parallel_ifgs", int),  # default 1
    ("--unwrap-parallel-tiles", "unwrap_parallel_tiles", int),  # default 1
)
_SPURT_T_COST_TYPES = frozenset({"constant", "distance", "centroid"})
# source: spurt v0.1.1 src/spurt/io/_slc_stack.py from_phase_linked_directory()
SPURT_INPUT_GLOB = "*.int.tif"
SPURT_TEMPORAL_COHERENCE = "temporal_coherence.tif"


def spurt_available() -> bool:
    try:
        return importlib.util.find_spec("spurt") is not None
    except (ImportError, ValueError):
        return False


def find_spurt_command(cfg: Mapping[str, Any] | None = None) -> list[str] | None:
    """Command prefix for the EMCF workflow: explicit exe → ``spurt-emcf`` → ``python -m``."""
    cfg = cfg or {}
    for raw in (cfg.get("spurt_executable"), os.environ.get("WINTERSAR_SPURT_EXE")):
        if raw:
            p = Path(str(raw)).expanduser()
            if p.is_file():
                return [str(p)]
    which = shutil.which(SPURT_EMCF_SCRIPT)
    if which:
        return [which]
    if spurt_available():
        return [sys.executable, "-m", SPURT_EMCF_MODULE]
    return None


def build_spurt_command(
    prefix: list[str],
    input_dir: Path,
    output_dir: Path,
    temp_dir: Path,
    cfg: Mapping[str, Any],
    log_file: Path | None = None,
) -> list[str]:
    """``spurt-emcf -i <in> -o <out> --tempdir <tmp> [flags…] [--log-file <log>]``.

    Only options present in ``cfg['spurt']`` (or top-level with the same key) are emitted, so
    spurt's own defaults apply otherwise. ``cores`` (``_cores``) fills ``--t-workers`` when
    the user did not set it. Note: spurt's ``--coh`` is the *temporal* coherence threshold —
    it is deliberately **not** fed from ``unwrap.coherence_threshold`` (spatial).
    """
    opts: dict[str, Any] = {}
    nested = cfg.get("spurt")
    if isinstance(nested, Mapping):
        opts.update(nested)
    for _flag, key, _typ in _SPURT_FLAGS:
        if key in cfg and key not in opts:
            opts[key] = cfg[key]
    if "t_workers" not in opts and cfg.get("_cores"):
        opts["t_workers"] = int(cfg["_cores"])
    cmd = [*prefix, "-i", str(input_dir), "-o", str(output_dir), "--tempdir", str(temp_dir)]
    for flag, key, typ in _SPURT_FLAGS:
        if key not in opts or opts[key] is None:
            continue
        value = opts[key]
        if key == "t_cost_type" and str(value) not in _SPURT_T_COST_TYPES:
            msg = f"spurt t_cost_type must be one of {sorted(_SPURT_T_COST_TYPES)}, got {value!r}"
            raise ValueError(msg)
        cmd.extend([flag, str(typ(value))])
    if opts.get("singletile"):
        cmd.append("--singletile")
    if log_file is not None:
        cmd.extend(["--log-file", str(log_file)])
    return cmd


def validate_phase_linked_dir(path: Path) -> dict[str, Any]:
    """Check the layout ``from_phase_linked_directory`` expects; returns a small summary."""
    if not path.is_dir():
        raise UnwrapError.from_rule("UNW-010", path=str(path))
    ints = sorted(path.glob(SPURT_INPUT_GLOB))
    temp_coh = path / SPURT_TEMPORAL_COHERENCE
    if not ints or not temp_coh.exists():
        raise UnwrapError.from_rule("UNW-010", path=str(path))
    return {"n_int_files": len(ints), "temporal_coherence": temp_coh.name}


@register_engine
class SpurtEngine(UnwrapEngineBase):
    name: ClassVar[str] = "spurt"
    version_constraint: ClassVar[str] = SPURT_CONSTRAINT
    install_hint: ClassVar[str] = (
        "pip install spurt  (PyPI spurt 0.1.1; needs ortools, rasterio, h5py, scipy) — "
        "no conda-forge feedstock was found at the time of ADR-0025"
    )
    # source: spurt README/LICENSE-BSD-3-Clause/LICENSE-Apache-2.0 (v0.1.1):
    # "licensed under your choice of BSD-3-Clause or Apache-2.0"; Copyright (c) 2024 Caltech
    license_note: ClassVar[str] = "BSD-3-Clause OR Apache-2.0 (spurt v0.1.1, Caltech)"
    stack_only: ClassVar[bool] = True

    def detect_version(self) -> str | None:
        v = python_module_version("spurt")
        if v:
            return v
        cmd = find_spurt_command()
        if cmd is None:
            return None
        # source: spurt _cli.py: argparse `--version` action with spurt.__version__
        return command_version(cmd, ("--version",))

    # ------------------------------------------------------------------ 2-D contract
    def unwrap(
        self,
        igram: FloatArray,
        coh: FloatArray,
        mask: BoolArray | None,
        params: Mapping[str, Any],
    ) -> UnwrapResult:
        raise StackOnlyEngineError(make_finding("UNW-009", engine=self.name))

    # ------------------------------------------------------------------ stack run
    def run(
        self,
        stage: str,
        inputs: Artifacts,
        params: dict[str, Any],
        log_dir: Path,
    ) -> Artifacts:
        self._check_stage(stage)
        self.require_available()
        cfg = unwrap_cfg(params)
        if "phase_linked_stack" not in inputs:
            raise UnwrapError.from_rule("UNW-010", path=str(sorted(inputs.items)))
        input_dir = Path(inputs["phase_linked_stack"].path)
        summary = validate_phase_linked_dir(input_dir)
        prefix = find_spurt_command(cfg)
        if prefix is None:
            raise EngineRunError.from_rule("UNW-003", engine=self.name, returncode="n/a", log="-")
        out = out_dir_from(params, log_dir)
        log = EngineLog(log_dir / f"{self.name}.log")
        output_dir = out / "emcf"
        temp_dir = Path(str(cfg.get("_workdir") or out)) / "emcf_tmp"
        spurt_log = log_dir / "spurt-emcf.log"
        cmd = build_spurt_command(prefix, input_dir, output_dir, temp_dir, cfg, spurt_log)
        log.write(
            f"spurt-emcf start input={input_dir} {json.dumps(summary)} "
            f"params={json.dumps(mask_mapping(public_params(cfg)), default=str, sort_keys=True)}"
        )
        log.write("cmd: " + " ".join(cmd))
        t0 = time.perf_counter()
        timeout = cfg.get("timeout_s")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(out),
                check=False,
                timeout=float(timeout) if timeout else None,
            )
        except subprocess.TimeoutExpired as e:
            raise EngineRunError.from_rule(
                "UNW-003", engine=self.name, returncode="timeout", log=str(log.path)
            ) from e
        log.write_raw(proc.stdout)
        log.write_raw(proc.stderr)
        wall = time.perf_counter() - t0
        if proc.returncode != 0:
            raise EngineRunError.from_rule(
                "UNW-003",
                engine=self.name,
                returncode=proc.returncode,
                log=str(log.path),
                evidence={"stderr_tail": (proc.stderr or "")[-2000:]},
            )
        outputs = sorted(p.name for p in output_dir.glob("*")) if output_dir.is_dir() else []
        stats = {
            "engine": self.name,
            "engine_version": self.detect_version(),
            "wall_time_s": round(wall, 3),
            "input_dir": str(input_dir),
            **summary,
            "n_outputs": len(outputs),
            "outputs": outputs[:200],
            "temp_dir": str(temp_dir),
            "params": mask_mapping(public_params(cfg)),
        }
        stats_path = out / "stats.json"
        stats_path.write_text(
            json.dumps(mask_mapping(stats), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log.write(f"spurt-emcf done wall_time_s={stats['wall_time_s']} n_outputs={len(outputs)}")
        meta = {k: v for k, v in stats.items() if k not in {"outputs", "params"}}
        return (
            Artifacts()
            .add(Artifact(name="unw_stack", path=output_dir, kind="dir", meta=meta))
            .add(Artifact(name="unw_stats", path=stats_path, kind="json", meta={}))
        )
