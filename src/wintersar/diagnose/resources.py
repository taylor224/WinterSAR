"""Stage resource estimation (plan §5.5 "리소스 추정", ADR-0036).

Simple linear models::

    per_job_time_s = time_s_per_mpixel * Mpix + time_fixed_s
    per_job_mem_gb = mem_mb_per_mpixel * Mpix / 1024 + mem_fixed_gb
    workers        = min(cores, n_pairs, floor(memory_gb / per_job_mem_gb), params["nproc"])
    wall_time_s    = per_job_time_s * ceil(n_pairs / workers)      (parallel: pairs)
    peak_rss_gb    = per_job_mem_gb * workers
    disk_gb        = disk_bytes_per_pixel * pixels * n_pairs / 1e9

Coefficients live in ``resources.yaml`` and are **initial guesses** (never measured values,
rule 11.8); ``wintersar bench`` refits them. ``Resources.notes["coefficients"]`` says
``"initial_guess"`` so that ``plan`` output can label the numbers accordingly.

HyP3 credits per job are the only verified numbers (HyP3 credits page, see the YAML).
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import yaml

from wintersar.io.schemas import Resources
from wintersar.util.sysinfo import MachineSpec

MODEL_PATH = Path(__file__).resolve().parent / "resources.yaml"

#: pipeline/config engine names (source: src/wintersar/pipeline/config.py EngineCfg.interferogram,
#: TimeseriesCfg.engine, UnwrapCfg.method) -> coefficient table keys in resources.yaml
ENGINE_ALIASES: dict[str, str] = {
    "isce2_topsstack": "isce2",
    "isce2-topsstack": "isce2",
    "topsstack": "isce2",
    "compass_isce3": "isce2",
}

_LOOKS_ALIASES: dict[str, str] = {
    "20x4": "20x4",
    "10x2": "10x2",
    "5x1": "5x1",
    "80": "20x4",
    "40": "10x2",
    "20": "5x1",
    "80m": "20x4",
    "40m": "10x2",
    "20m": "5x1",
}


@lru_cache(maxsize=2)
def _load(path: str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        msg = f"{path}: top level must be a mapping"
        raise ValueError(msg)
    return cast(dict[str, Any], data)


def load_model(path: Path | None = None) -> dict[str, Any]:
    return _load(str(path or MODEL_PATH))


def stage_coefficients(
    engine: str, stage: str, model: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Coefficients for ``engine``/``stage`` (falls back to the ``default`` engine table)."""
    m = model or load_model()
    stages = m.get("stages", {})
    for key in (engine, ENGINE_ALIASES.get(engine, engine), "default"):
        table = stages.get(key) or {}
        if stage in table:
            return cast(dict[str, Any], dict(table[stage]))
    return None


# --------------------------------------------------------------------------- HyP3 credits
def normalize_looks(looks: Any) -> str | None:
    """``"20x4"`` | ``(20, 4)`` | ``80`` (pixel spacing m) -> ``"20x4"``; ``None`` if unknown."""
    if looks is None:
        return None
    if isinstance(looks, list | tuple) and len(looks) == 2:
        key = f"{int(looks[0])}x{int(looks[1])}"
    elif isinstance(looks, int | float) and not isinstance(looks, bool):
        key = str(int(looks))
    else:
        key = str(looks).lower().replace(" ", "")
    return _LOOKS_ALIASES.get(key)


def hyp3_credits_per_job(
    looks: Any = "20x4",
    n_bursts: int = 1,
    product: str = "burst",
    model: dict[str, Any] | None = None,
) -> float | None:
    """Credits for one HyP3 InSAR job (verified table) or ``None`` when not tabulated.

    ``product="burst"`` uses the Burst InSAR table with ``n_bursts`` burst pairs per job;
    ``product="slc"`` uses the Sentinel-1 InSAR table (one job = one pair).
    """
    m = model or load_model()
    table = m.get("hyp3_credits", {})
    key = normalize_looks(looks)
    if key is None:
        return None
    if product == "slc":
        v = table.get("insar", {}).get(key)
        return float(v) if v is not None else None
    rows = table.get("burst_insar", {}).get(key)
    if not rows:
        return None
    for lo, hi, credits in rows:
        if int(lo) <= int(n_bursts) <= int(hi):
            return float(credits)
    return None


def hyp3_monthly_allotment(model: dict[str, Any] | None = None) -> float | None:
    m = model or load_model()
    v = m.get("hyp3_credits", {}).get("monthly_free_allotment")
    return float(v) if v is not None else None


# --------------------------------------------------------------------------- estimate
def estimate(
    stage: str,
    n_pairs: int,
    pixels: int,
    engine: str,
    machine: MachineSpec,
    params: dict[str, Any] | None = None,
) -> Resources:
    """Estimate wall time / peak RSS / disk / network / credits for one stage.

    ``params`` (all optional): ``nproc`` or ``workers`` (cap on concurrent jobs),
    ``memory_mb_per_mpixel`` (override, e.g. from ``unwrap.memory_mb_per_mpixel``),
    ``ntiles`` ([rows, cols]: per-job memory is divided by the tile count),
    ``looks`` / ``pixel_m`` and ``n_bursts`` / ``product`` for HyP3 credits.
    Unknown engine/stage → empty ``Resources`` with ``notes["model"] == "none"``.
    """
    p = dict(params or {})
    model = load_model()
    n_pairs = max(0, int(n_pairs))
    pixels = max(0, int(pixels))
    mpix = pixels / 1e6
    notes: dict[str, Any] = {
        "engine": engine,
        "stage": stage,
        "model": "linear",
        "coefficients": model.get("status", "initial_guess"),
        "source": "src/wintersar/diagnose/resources.yaml",
    }

    if engine == "hyp3":
        return _estimate_hyp3(stage, n_pairs, p, model, notes)

    coef = stage_coefficients(engine, stage, model)
    if coef is None:
        return Resources(notes={**notes, "model": "none"})

    mem_per_mpix = float(p.get("memory_mb_per_mpixel", coef.get("mem_mb_per_mpixel", 0.0)))
    ntiles = p.get("ntiles")
    tile_div = 1
    if isinstance(ntiles, list | tuple) and len(ntiles) == 2:
        tile_div = max(1, int(ntiles[0]) * int(ntiles[1]))
    per_job_mem_gb = mem_per_mpix * mpix / tile_div / 1024.0 + float(coef.get("mem_fixed_gb", 0.0))
    per_job_time_s = float(coef.get("time_s_per_mpixel", 0.0)) * mpix + float(
        coef.get("time_fixed_s", 0.0)
    )
    parallel = str(coef.get("parallel", "pairs"))

    if parallel == "pairs" and n_pairs > 0:
        cap = int(p.get("nproc", p.get("workers", machine.cores)) or 1)
        by_mem = int(machine.memory_gb // per_job_mem_gb) if per_job_mem_gb > 0 else n_pairs
        workers = max(1, min(machine.cores, n_pairs, cap, max(by_mem, 1)))
        rounds = math.ceil(n_pairs / workers)
        wall = per_job_time_s * rounds
        peak = per_job_mem_gb * workers
        n_jobs = n_pairs
        disk = float(coef.get("disk_bytes_per_pixel", 0.0)) * pixels * n_pairs / 1e9
    else:
        workers = 1
        wall = (
            per_job_time_s * max(1, n_pairs)
            if parallel == "single" and coef.get("time_s_per_mpixel")
            else per_job_time_s
        )
        peak = per_job_mem_gb * max(1, n_pairs) if parallel == "single" else per_job_mem_gb
        n_jobs = 1
        disk = float(coef.get("disk_bytes_per_pixel", 0.0)) * pixels * max(1, n_pairs) / 1e9

    notes.update(
        {
            "basis": coef.get("basis", "guess"),
            "workers": workers,
            "per_job_time_s": round(per_job_time_s, 3),
            "per_job_mem_gb": round(per_job_mem_gb, 4),
            "mpixels": round(mpix, 4),
            "n_pairs": n_pairs,
        }
    )
    if peak > machine.memory_gb > 0:
        notes["exceeds_machine_memory"] = True
    return Resources(
        wall_time_s=round(wall, 1),
        peak_rss_gb=round(peak, 3),
        disk_gb=round(disk, 4),
        network_gb=0.0,
        credits=None,
        n_jobs=n_jobs,
        notes=notes,
    )


def _estimate_hyp3(
    stage: str,
    n_pairs: int,
    p: dict[str, Any],
    model: dict[str, Any],
    notes: dict[str, Any],
) -> Resources:
    coef = model.get("stages", {}).get("hyp3", {}).get(stage)  # no 'default' fallback: remote
    if coef is None:
        return Resources(notes={**notes, "model": "none"})
    looks = p.get("looks", p.get("pixel_m", "20x4"))
    product = str(p.get("product", "burst"))
    n_bursts = int(p.get("n_bursts", 1))
    per_job = hyp3_credits_per_job(looks, n_bursts, product, model)
    credits = per_job * n_pairs if per_job is not None else None
    net = float(coef.get("network_gb_per_job", 0.0)) * n_pairs
    notes.update(
        {
            "basis": coef.get("basis", "guess"),
            "credits_source": model.get("hyp3_credits", {}).get("source"),
            "credits_per_job": per_job,
            "looks": normalize_looks(looks),
            "product": product,
            "n_bursts": n_bursts,
            "monthly_free_allotment": hyp3_monthly_allotment(model),
            "wall_time": "remote (HyP3 queue + processing), not modelled",
        }
    )
    if per_job is None:
        notes["credits_unknown"] = True
    return Resources(
        wall_time_s=None,
        peak_rss_gb=float(coef.get("mem_fixed_gb", 0.0)),
        disk_gb=round(net, 4),
        network_gb=round(net, 4),
        credits=credits,
        n_jobs=n_pairs,
        notes=notes,
    )


__all__ = [
    "MODEL_PATH",
    "estimate",
    "hyp3_credits_per_job",
    "hyp3_monthly_allotment",
    "load_model",
    "normalize_looks",
    "stage_coefficients",
]
