"""Metrics for the research experiments (plan §5.7 "metrics.py", §6.3; ADR-0060).

* :func:`unwrap_error_fraction` — fraction of pixels ≥ π off the synthetic truth after
  removing the constant offset (re-exported from :mod:`wintersar.research.synth`).
* :func:`phase_rmse` / :func:`phase_mae` — wrapped-difference error of a representative
  phase against the low-resolution truth.
* :func:`offsets_recovered` — injected vs estimated integer 2π tile offsets.
* :func:`seam_jumps` — tile-seam jump count of a merged raster (shared detector,
  ``wintersar.unwrap.tiling.boundary_jumps``, ADR-0048).
* :func:`closure_rms` — loop-closure RMS on a stack, via ``wintersar.validate.closure``
  (MintPy sign convention ``φ_ij + φ_jk - φ_ik``).
* :func:`ground_truth_rmse` — hook to ``wintersar.validate.metrics.compare`` (RMSE against
  levelling / GNSS).
* :class:`ResourceTimer` — wall time, CPU time and RSS (psutil) context manager. Numbers it
  produces belong in a results JSON, never in prose (rule 11.8).
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wintersar.research import synth
from wintersar.research.repr_phase import wrap
from wintersar.research.stitching import _seam_report, tiles_from_slices
from wintersar.validate.closure import closure_from_arrays
from wintersar.validate.metrics import compare

FloatArray = NDArray[np.float64]

#: Metric names the experiment runner knows (column order of the report tables).
METRIC_NAMES: tuple[str, ...] = (
    "phase_rmse_rad",
    "phase_mae_rad",
    "mean_magnitude",
    "offsets_exact",
    "n_wrong_offsets",
    "seam_boundaries_with_jump",
    "seam_jump_pixels",
    "unwrap_error_fraction",
    "closure_rms_rad",
    "wall_s",
    "cpu_s",
    "peak_rss_mb",
)

__all__ = [
    "METRIC_NAMES",
    "Measurement",
    "ResourceTimer",
    "closure_rms",
    "ground_truth_rmse",
    "offsets_recovered",
    "phase_mae",
    "phase_rmse",
    "rmse",
    "seam_jumps",
    "unwrap_error_fraction",
]

unwrap_error_fraction = synth.unwrap_error_fraction


# ---------------------------------------------------------------- phase errors
def _wrapped_error(
    est: NDArray[Any], truth_phase: NDArray[Any], mask: NDArray[Any] | None, remove_bias: bool
) -> FloatArray:
    e = np.asarray(est)
    ph = np.angle(e) if np.iscomplexobj(e) else np.asarray(e, dtype=np.float64)
    tr = np.asarray(truth_phase, dtype=np.float64)
    if ph.shape != tr.shape:
        msg = f"estimate shape {ph.shape} != truth shape {tr.shape}"
        raise ValueError(msg)
    diff = wrap(ph - tr)
    valid = np.isfinite(diff)
    if mask is not None:
        valid &= ~np.asarray(mask, dtype=bool)
    if np.iscomplexobj(e):
        valid &= np.abs(e) > 0
    d = diff[valid]
    if remove_bias and d.size:
        d = wrap(d - np.angle(np.mean(np.exp(1j * d))))
    return np.asarray(d, dtype=np.float64)


def phase_rmse(
    est: NDArray[Any],
    truth_phase: NDArray[Any],
    mask: NDArray[Any] | None = None,
    remove_bias: bool = False,
) -> float:
    """RMS of the wrapped difference ``wrap(angle(est) - truth)`` (radians)."""
    d = _wrapped_error(est, truth_phase, mask, remove_bias)
    return float(np.sqrt(np.mean(d * d))) if d.size else float("nan")


def phase_mae(
    est: NDArray[Any],
    truth_phase: NDArray[Any],
    mask: NDArray[Any] | None = None,
    remove_bias: bool = False,
) -> float:
    """Mean absolute wrapped difference (radians)."""
    d = _wrapped_error(est, truth_phase, mask, remove_bias)
    return float(np.mean(np.abs(d))) if d.size else float("nan")


def rmse(a: NDArray[Any], b: NDArray[Any], mask: NDArray[Any] | None = None) -> float:
    """Plain RMSE of ``a - b`` over finite (unmasked) elements."""
    d = np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64)
    valid = np.isfinite(d)
    if mask is not None:
        valid &= ~np.asarray(mask, dtype=bool)
    return float(np.sqrt(np.mean(d[valid] ** 2))) if valid.any() else float("nan")


# ---------------------------------------------------------------- stitching
def offsets_recovered(
    estimated: Sequence[int], injected: Sequence[int], relative: bool = True
) -> dict[str, Any]:
    """Compare estimated and injected tile offsets (cycles). ``relative`` compares them up to
    the common gauge (tile 0), which is all an overlap-only method can determine."""
    est = np.asarray(estimated, dtype=np.int64)
    inj = np.asarray(injected, dtype=np.int64)
    if est.shape != inj.shape:
        msg = f"{est.size} estimated offsets for {inj.size} injected"
        raise ValueError(msg)
    if relative and est.size:
        est = est - est[0]
        inj = inj - inj[0]
    err = est - inj
    return {
        "offsets_exact": float(bool(np.all(err == 0))),
        "n_wrong_offsets": int(np.count_nonzero(err)),
        "max_abs_error_cycles": int(np.max(np.abs(err))) if err.size else 0,
    }


def seam_jumps(
    merged: NDArray[Any], slices: Sequence[tuple[slice, slice]], shape: tuple[int, int]
) -> dict[str, Any]:
    """Seam-mode jump statistics of a merged raster for the given tile extents."""
    boxes = tiles_from_slices(list(slices), (int(shape[0]), int(shape[1])))
    return _seam_report(np.asarray(merged, dtype=np.float64), boxes)


# ---------------------------------------------------------------- closure
def closure_rms(
    phase: NDArray[Any],
    pairs: Sequence[str],
    mask: NDArray[Any] | None = None,
    *,
    wrapped: bool = False,
) -> dict[str, Any]:
    """Loop-closure RMS (radians) of a phase stack ``(n_pairs, ny, nx)``.

    ``wrapped=False`` treats ``phase`` as unwrapped (closure is an integer multiple of 2π;
    the fraction of non-zero multiples is the unwrap-error indicator), ``wrapped=True``
    wraps the closure (closure-phase bias / noise).
    """
    tri, closure, valid = closure_from_arrays(phase, list(pairs), mask, wrapped=wrapped)
    n_tri = len(tri)
    vals = np.asarray(closure[valid], dtype=np.float64)
    out: dict[str, Any] = {
        "n_triplets": n_tri,
        "closure_rms_rad": float(np.sqrt(np.mean(vals * vals))) if vals.size else float("nan"),
        "n_valid": int(vals.size),
    }
    if not wrapped and vals.size:
        k = np.rint((vals - wrap(vals)) / (2.0 * np.pi))
        out["nonzero_integer_fraction"] = float(np.mean(k != 0))
    return out


# ---------------------------------------------------------------- ground truth hook
def ground_truth_rmse(timeseries: Any, ground_truth: Sequence[Any], **kw: Any) -> dict[str, Any]:
    """RMSE / bias of an InSAR :class:`~wintersar.io.timeseries.TimeSeries` against levelling
    or GNSS records through ``wintersar.validate.metrics.compare`` (R-10). ``available`` is
    part of the contract the experiment runner reads and is always ``True`` here."""
    res = compare(timeseries, list(ground_truth), **kw)
    return {
        "available": True,
        "rmse_m": float(res.rmse_m),
        "bias_m": float(res.bias_m),
        "n_sites": int(res.n_sites),
        "n_points": int(res.n_points),
    }


# ---------------------------------------------------------------- resources
def _rss_mb() -> float:
    # source: .venv/lib/python3.11/site-packages/psutil/__init__.py (Process.memory_info().rss)
    try:
        import psutil

        return float(psutil.Process(os.getpid()).memory_info().rss) / 1e6
    except Exception:  # pragma: no cover - psutil is a core dependency
        return float("nan")


@dataclass
class Measurement:
    wall_s: float = 0.0
    cpu_s: float = 0.0
    rss_start_mb: float = float("nan")
    rss_end_mb: float = float("nan")
    peak_rss_mb: float = float("nan")
    samples: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_s": self.wall_s,
            "cpu_s": self.cpu_s,
            "rss_start_mb": self.rss_start_mb,
            "rss_end_mb": self.rss_end_mb,
            "peak_rss_mb": self.peak_rss_mb,
            "samples": self.samples,
            **self.extra,
        }


class ResourceTimer:
    """``with ResourceTimer() as rt: ...`` → ``rt.result`` (:class:`Measurement`).

    ``peak_rss_mb`` is the maximum of the RSS samples taken at enter, exit and every
    :meth:`sample` call (psutil reports the current RSS, not a high-water mark).
    """

    def __init__(self) -> None:
        self.result = Measurement()
        self._t0 = 0.0
        self._c0 = 0.0

    def __enter__(self) -> ResourceTimer:
        self._t0 = time.perf_counter()
        self._c0 = time.process_time()
        self.result.rss_start_mb = _rss_mb()
        self.result.peak_rss_mb = self.result.rss_start_mb
        self.result.samples = 1
        return self

    def sample(self) -> float:
        rss = _rss_mb()
        if np.isfinite(rss) and not (rss <= self.result.peak_rss_mb):
            self.result.peak_rss_mb = rss
        self.result.samples += 1
        return rss

    def __exit__(self, *exc: object) -> None:
        self.result.wall_s = time.perf_counter() - self._t0
        self.result.cpu_s = time.process_time() - self._c0
        self.result.rss_end_mb = self.sample()

    @property
    def wall_s(self) -> float:
        return self.result.wall_s

    @property
    def peak_rss_mb(self) -> float:
        return self.result.peak_rss_mb
