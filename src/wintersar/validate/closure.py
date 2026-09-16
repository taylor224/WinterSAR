"""Loop-closure (phase triplet) statistics for unwrap-error detection (plan §5.6, §12.1, ADR-0042).

Definitions (same sign convention as MintPy's triplet design matrix)::

    closure(i,j,k) = φ_ij + φ_jk - φ_ik          for dates t_i < t_j < t_k

# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/objects/stack.py
#   ifgramStack.get_design_matrix4triplet: row[idx12] = 1; row[idx23] = 1; row[idx13] = -1
# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/unwrap_error_phase_closure.py
#   closure_pha = np.dot(C, unw)
#   closure_int = np.round((closure_pha - ut.wrap(closure_pha)) / (2.*np.pi))
#   num_nonzero_closure = np.sum(closure_int != 0, axis=0)     # T_int, Yunjun et al. 2019 CAGEO

* **Unwrapped** phase: the closure of a consistent stack is an exact integer multiple of 2π
  (0 when there is no unwrapping error). ``integer multiple ≠ 0`` at a pixel flags an
  unwrapping error in at least one of the three interferograms.
* **Wrapped** phase: ``wrap(closure)`` should be ≈ 0 up to noise; a persistent non-zero
  value is the closure-phase *bias* from multilooking/filtering (Zheng et al. 2022), not an
  unwrapping error.

Per-interferogram scores aggregate the triplets an interferogram takes part in; suspicious
interferograms are found by **greedy peeling** (flag the worst, drop its triplets, repeat) so
that one bad interferogram does not drag its neighbours over the threshold (ADR-0042).
"""

from __future__ import annotations

import itertools
import json
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from wintersar.io.igrams import IgramStack
from wintersar.io.schemas import Pair
from wintersar.util.masking import mask_mapping

FloatArray = NDArray[np.float64]
Triplet = tuple[int, int, int]
Mode = Literal["unw", "wrapped"]

TWO_PI = 2.0 * np.pi
# Unwrapped mode: an interferogram is suspicious when this fraction of its valid pixels sits
# in a triplet with a non-zero integer ambiguity (initial value, ADR-0042).
DEFAULT_SUSPICIOUS_FRACTION: float = 0.05
# Wrapped mode: robust outlier rule on per-interferogram closure RMS (rad).
DEFAULT_WRAPPED_MIN_RMS: float = 0.3
DEFAULT_WRAPPED_MAD_K: float = 3.0


def wrap(phase: NDArray[Any]) -> FloatArray:
    return np.asarray(np.angle(np.exp(1j * np.asarray(phase, dtype=np.float64))), dtype=np.float64)


def _pair_key(p: str | Pair) -> str:
    return p if isinstance(p, str) else p.key


def _split(key: str) -> tuple[str, str]:
    a, b = key.split("_")
    return a, b


def triplets(pairs: Sequence[str | Pair]) -> list[Triplet]:
    """Indices ``(ij, jk, ik)`` into ``pairs`` of every date triplet whose three interferograms
    exist (MintPy ``get_design_matrix4triplet`` enumeration order: sorted date combinations)."""
    keys = [_pair_key(p) for p in pairs]
    idx = {_split(k): i for i, k in enumerate(keys)}
    dates = sorted({d for k in keys for d in _split(k)})
    out: list[Triplet] = []
    for d1, d2, d3 in itertools.combinations(dates, 3):
        try:
            out.append((idx[(d1, d2)], idx[(d2, d3)], idx[(d1, d3)]))
        except KeyError:
            continue
    return out


def design_matrix(pairs: Sequence[str | Pair]) -> NDArray[np.float32]:
    """MintPy-style ``C`` (n_triplets x n_pairs) with +1, +1, -1 per row."""
    tri = triplets(pairs)
    c = np.zeros((len(tri), len(pairs)), dtype=np.float32)
    for r, (ij, jk, ik) in enumerate(tri):
        c[r, ij] = 1.0
        c[r, jk] = 1.0
        c[r, ik] = -1.0
    return c


@dataclass
class ClosureResult:
    mode: Mode
    pairs: list[str]
    triplets: list[Triplet]
    per_triplet_rms: FloatArray  # (n_tri,) rad
    per_pixel_rms: FloatArray  # (ny, nx) rad, NaN where no valid triplet
    per_igram_score: dict[str, float]
    suspicious: list[str]
    per_triplet_nonzero_fraction: FloatArray | None = None  # unw only
    per_pixel_nonzero: FloatArray | None = None  # unw only: count of triplets with k != 0 (T_int)
    per_igram_n_triplets: dict[str, int] = field(default_factory=dict)
    integer_multiples: NDArray[np.int16] | None = None  # (n_tri, ny, nx) when keep_maps
    closure: FloatArray | None = None  # (n_tri, ny, nx) when keep_maps
    n_valid_pixels: int = 0
    thresholds: dict[str, float] = field(default_factory=dict)

    @property
    def n_triplets(self) -> int:
        return len(self.triplets)

    def triplet_keys(self, i: int) -> tuple[str, str, str]:
        ij, jk, ik = self.triplets[i]
        return self.pairs[ij], self.pairs[jk], self.pairs[ik]

    def ranked_igrams(self) -> list[tuple[str, float]]:
        return sorted(self.per_igram_score.items(), key=lambda kv: (-kv[1], kv[0]))

    def to_dashboard(self, n_bins: int = 30) -> dict[str, Any]:
        """JSON-serialisable summary for the QGIS/HTML dashboard."""
        rms = self.per_pixel_rms[np.isfinite(self.per_pixel_rms)]
        if rms.size:
            counts, edges = np.histogram(rms, bins=n_bins)
            hist = {"edges_rad": [float(e) for e in edges], "counts": [int(c) for c in counts]}
        else:
            hist = {"edges_rad": [], "counts": []}
        tri_rows = []
        for i in range(self.n_triplets):
            row: dict[str, Any] = {
                "pairs": list(self.triplet_keys(i)),
                "rms_rad": float(self.per_triplet_rms[i]),
            }
            if self.per_triplet_nonzero_fraction is not None:
                row["nonzero_fraction"] = float(self.per_triplet_nonzero_fraction[i])
            tri_rows.append(row)
        ig_rows = [
            {
                "pair": k,
                "score": float(s),
                "n_triplets": self.per_igram_n_triplets.get(k, 0),
                "suspicious": k in self.suspicious,
                "rank": r + 1,
            }
            for r, (k, s) in enumerate(self.ranked_igrams())
        ]
        summary: dict[str, Any] = {
            "rms_mean_rad": float(np.mean(rms)) if rms.size else None,
            "rms_median_rad": float(np.median(rms)) if rms.size else None,
            "rms_p95_rad": float(np.percentile(rms, 95)) if rms.size else None,
        }
        if self.per_pixel_nonzero is not None:
            nz = self.per_pixel_nonzero[np.isfinite(self.per_pixel_nonzero)]
            summary["nonzero_triplets_mean"] = float(np.mean(nz)) if nz.size else None
            summary["pixels_with_any_nonzero_fraction"] = (
                float(np.mean(nz > 0)) if nz.size else None
            )
        payload = {
            "mode": self.mode,
            "n_igrams": len(self.pairs),
            "n_triplets": self.n_triplets,
            "n_valid_pixels": self.n_valid_pixels,
            "thresholds": dict(self.thresholds),
            "summary": summary,
            "histogram_per_pixel_rms": hist,
            "triplets": tri_rows,
            "igrams": ig_rows,
            "suspicious": list(self.suspicious),
        }
        return dict(mask_mapping(payload))


def closure_from_arrays(
    phase: NDArray[Any],
    pairs: Sequence[str | Pair],
    mask: NDArray[Any] | None = None,
    *,
    wrapped: bool = False,
    keep_maps: bool = False,
) -> tuple[list[Triplet], FloatArray, NDArray[np.bool_]]:
    """Closure phase ``(n_tri, ny, nx)`` and per-triplet validity for a phase stack.

    ``mask`` (``True`` = masked out) may be 2-D or 3-D; NaN phase is treated as masked.
    In wrapped mode the closure is wrapped to (-π, π].
    """
    ph = np.asarray(phase, dtype=np.float64)
    tri = triplets(pairs)
    _n, ny, nx = ph.shape
    finite = np.isfinite(ph)
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        finite &= ~(m[None, :, :] if m.ndim == 2 else m)
    closure = np.full((len(tri), ny, nx), np.nan, dtype=np.float64)
    valid = np.zeros((len(tri), ny, nx), dtype=bool)
    for r, (ij, jk, ik) in enumerate(tri):
        v = finite[ij] & finite[jk] & finite[ik]
        c = ph[ij] + ph[jk] - ph[ik]
        if wrapped:
            c = wrap(c)
        closure[r] = np.where(v, c, np.nan)
        valid[r] = v
    _ = keep_maps
    return tri, closure, valid


def _rms(x: FloatArray, axis: int | tuple[int, ...] | None) -> FloatArray:
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN slices -> NaN, by design
        return np.asarray(np.sqrt(np.nanmean(x * x, axis=axis)), dtype=np.float64)


def _igram_scores(
    pairs: Sequence[str], tri: Sequence[Triplet], values: FloatArray, active: Sequence[bool]
) -> tuple[dict[str, float], dict[str, int]]:
    """Mean of ``values`` (per triplet) over the *active* triplets each interferogram joins."""
    sums: dict[str, float] = dict.fromkeys(pairs, 0.0)
    counts: dict[str, int] = dict.fromkeys(pairs, 0)
    for r, (ij, jk, ik) in enumerate(tri):
        if not active[r] or not np.isfinite(values[r]):
            continue
        for i in (ij, jk, ik):
            sums[pairs[i]] += float(values[r])
            counts[pairs[i]] += 1
    scores = {k: (sums[k] / counts[k] if counts[k] else 0.0) for k in pairs}
    return scores, counts


def _peel(
    pairs: Sequence[str],
    tri: Sequence[Triplet],
    values: FloatArray,
    exceeds: Any,
) -> list[str]:
    """Greedy: flag the worst interferogram, deactivate its triplets, repeat."""
    active = [True] * len(tri)
    flagged: list[str] = []
    while True:
        scores, counts = _igram_scores(pairs, tri, values, active)
        candidates = [(s, k) for k, s in scores.items() if counts[k] > 0 and k not in flagged]
        if not candidates:
            break
        worst_score, worst = max(candidates, key=lambda x: (x[0], x[1]))
        if not exceeds(worst_score, [s for s, _ in candidates]):
            break
        flagged.append(worst)
        for r, (ij, jk, ik) in enumerate(tri):
            if worst in (pairs[ij], pairs[jk], pairs[ik]):
                active[r] = False
    return flagged


def closure_phase(
    stack: IgramStack,
    use_unw: bool = True,
    *,
    suspicious_fraction: float = DEFAULT_SUSPICIOUS_FRACTION,
    wrapped_min_rms: float = DEFAULT_WRAPPED_MIN_RMS,
    wrapped_mad_k: float = DEFAULT_WRAPPED_MAD_K,
    keep_maps: bool = False,
) -> ClosureResult:
    """Closure statistics for an interferogram stack.

    ``use_unw=True`` uses ``stack.unw`` (integer-2π residuals → unwrapping errors);
    ``use_unw=False`` (or no ``unw``) uses the wrapped phase (closure bias / noise).
    """
    mode: Mode = "unw" if (use_unw and stack.unw is not None) else "wrapped"
    phase = stack.unw if mode == "unw" else stack.wrapped
    assert phase is not None
    tri, closure, valid = closure_from_arrays(
        phase, stack.pairs, stack.mask, wrapped=(mode == "wrapped")
    )
    n_tri = len(tri)
    per_triplet_rms = _rms(closure, axis=(1, 2)) if n_tri else np.zeros(0)
    per_pixel_rms = _rms(closure, axis=0) if n_tri else np.full(stack.shape, np.nan)
    any_valid = valid.any(axis=0) if n_tri else np.zeros(stack.shape, dtype=bool)
    per_pixel_rms[~any_valid] = np.nan
    ints: NDArray[np.int16] | None = None
    nonzero_frac: FloatArray | None = None
    per_pixel_nonzero: FloatArray | None = None
    thresholds: dict[str, float] = {}
    if mode == "unw":
        k = np.round((closure - wrap(closure)) / TWO_PI)
        k = np.where(valid, k, 0)
        ints = k.astype(np.int16)
        with np.errstate(invalid="ignore", divide="ignore"):
            nonzero_frac = np.asarray(
                [float((ints[r] != 0).sum() / max(int(valid[r].sum()), 1)) for r in range(n_tri)],
                dtype=np.float64,
            )
        per_pixel_nonzero = (ints != 0).sum(axis=0).astype(np.float64)
        per_pixel_nonzero[~any_valid] = np.nan
        scores, counts = _igram_scores(stack.pairs, tri, nonzero_frac, [True] * n_tri)
        thresholds["suspicious_fraction"] = suspicious_fraction
        suspicious = _peel(stack.pairs, tri, nonzero_frac, lambda s, _all: s > suspicious_fraction)
    else:
        scores, counts = _igram_scores(stack.pairs, tri, per_triplet_rms, [True] * n_tri)
        thresholds["wrapped_min_rms"] = wrapped_min_rms
        thresholds["wrapped_mad_k"] = wrapped_mad_k

        def exceeds(s: float, all_scores: list[float]) -> bool:
            arr = np.asarray(all_scores, dtype=np.float64)
            med = float(np.median(arr))
            mad = float(np.median(np.abs(arr - med))) * 1.4826
            return s > wrapped_min_rms and s > med + wrapped_mad_k * mad and len(arr) >= 3

        suspicious = _peel(stack.pairs, tri, per_triplet_rms, exceeds)
    return ClosureResult(
        mode=mode,
        pairs=list(stack.pairs),
        triplets=tri,
        per_triplet_rms=per_triplet_rms,
        per_pixel_rms=per_pixel_rms,
        per_igram_score=scores,
        suspicious=suspicious,
        per_triplet_nonzero_fraction=nonzero_frac,
        per_pixel_nonzero=per_pixel_nonzero,
        per_igram_n_triplets=counts,
        integer_multiples=ints if keep_maps else None,
        closure=closure if keep_maps else None,
        n_valid_pixels=int(any_valid.sum()),
        thresholds=thresholds,
    )


def closure_coherence(stack: IgramStack) -> FloatArray:
    """Per-pixel ``|mean_t exp(j·closure_t)|`` of the wrapped closure (1 = perfectly consistent).

    A proxy for MintPy's ``temporalCoherence`` when no network inversion residual exists
    (sweep metric ``temporal_coherence`` on the fake path; ADR-0044).
    """
    _, closure, valid = closure_from_arrays(stack.wrapped, stack.pairs, stack.mask, wrapped=True)
    if closure.shape[0] == 0:
        return np.full(stack.shape, np.nan)
    z = np.where(valid, np.exp(1j * np.nan_to_num(closure)), 0.0)
    n = valid.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        coh = np.abs(z.sum(axis=0)) / np.maximum(n, 1)
    return np.asarray(np.where(n > 0, coh, np.nan), dtype=np.float64)


def write_dashboard_json(result: ClosureResult, path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result.to_dashboard(), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def write_closure_maps(result: ClosureResult, path: Path | str) -> Path:
    """``.npz`` with the per-pixel maps (and the full closure cube when ``keep_maps`` was set)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, NDArray[Any]] = {
        "per_pixel_rms": result.per_pixel_rms.astype(np.float32),
        "per_triplet_rms": result.per_triplet_rms.astype(np.float32),
        "triplets": np.asarray(result.triplets, dtype=np.int32).reshape(-1, 3),
        "pairs": np.asarray(result.pairs),
    }
    if result.per_pixel_nonzero is not None:
        arrays["per_pixel_nonzero"] = result.per_pixel_nonzero.astype(np.float32)
    if result.integer_multiples is not None:
        arrays["integer_multiples"] = result.integer_multiples
    if result.closure is not None:
        arrays["closure"] = result.closure.astype(np.float32)
    np.savez_compressed(p, **arrays)  # type: ignore[arg-type]
    return p


def stack_dates(stack: IgramStack) -> list[date]:
    return list(stack.dates)


__all__ = [
    "DEFAULT_SUSPICIOUS_FRACTION",
    "DEFAULT_WRAPPED_MAD_K",
    "DEFAULT_WRAPPED_MIN_RMS",
    "ClosureResult",
    "closure_coherence",
    "closure_from_arrays",
    "closure_phase",
    "design_matrix",
    "triplets",
    "wrap",
    "write_closure_maps",
    "write_dashboard_json",
]
