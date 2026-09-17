"""Unwrap strategy scheduler (plan §5.4, R-06, PERF-04).

Decides, per stack, how the interferograms are unwrapped — never *how* the unwrapping
algorithm works (rule 11.3):

1. **Memory model** ``m ≈ c · P / 1e6`` MB for one single-tile interferogram of ``P``
   pixels. ``c = cfg.memory_mb_per_mpixel`` (default 100; SNAPHU documentation, refitted by
   ``wintersar bench`` — open question #8, ADR-0045).
2. **Tiling** only when one interferogram alone exceeds the RAM budget (``m > budget``):
   ``ntiles = ceil(m / per-process budget)`` on a near-square grid, overlap
   ``max(25 % · tile, 200 px)`` (ADR-0046). An explicit ``unwrap.tiles`` always wins.
3. **Method** (``unwrap.method: auto``): ``tophu`` when tiling is needed or the fringe
   density is high *and* tophu is available, else ``snaphu``.
4. **Parallelism** (ADR-0047): interferogram-level first, tile-level (SNAPHU ``nproc``)
   only for tiled interferograms; ``n_parallel = min(floor(cores / nproc_per_igram),
   floor(RAM / m), n_igrams)``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wintersar.i18n import t
from wintersar.pipeline.config import TilesCfg, UnwrapCfg
from wintersar.util.sysinfo import MachineSpec

__all__ = [
    "DEFAULT_MB_PER_MPIXEL",
    "DEFAULT_MIN_OVERLAP_PX",
    "DEFAULT_OVERLAP_FRACTION",
    "ENGINE_METHODS",
    "FRINGE_HIGH",
    "MAX_TILES",
    "MB_PER_GB",
    "REASON_PREFIX",
    "UnwrapPlan",
    "choose_strategy",
    "core_shape",
    "estimate_memory_mb",
    "fringe_density",
    "near_square_grid",
    "overlap_pixels",
    "tile_shape_with_overlap",
]

# source: https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/  (SNAPHU home page,
# "In single-tile mode the required memory is on the order of 100 MB per 1,000,000 pixels
# in the input interferogram."). Initial value only — re-fitted by `wintersar bench`
# (docs/open-questions.md #8, ADR-0045). The man page itself does not state a figure.
DEFAULT_MB_PER_MPIXEL: float = 100.0
MB_PER_GB: float = 1024.0

# ADR-0046 — must equal the ``TilesCfg`` defaults (asserted by tests/unit/unwrap).
DEFAULT_OVERLAP_FRACTION: float = 0.25
DEFAULT_MIN_OVERLAP_PX: int = 200

# ADR-0045 — fringe density (mean |Δφ| / π over pixel neighbours of *both* axes; 1.0 = one
# cycle every two pixels along each axis, the aliasing limit) above which the multiresolution
# unwrapper is preferred. Because the two axes are pooled, a one-directional fringe pattern
# reaches 0.25 at π/2 rad/px (a fringe every 4 px) — see :func:`fringe_density`. The
# threshold is calibrated against this pooled score by ``wintersar bench`` (open question #8),
# so it must not be re-scaled without the domain review (rule 11.10).
FRINGE_HIGH: float = 0.25

MAX_TILES: int = 4096
ENGINE_METHODS: tuple[str, ...] = ("snaphu", "tophu", "spurt")
REASON_PREFIX: str = "unwrap.plan.reason."


def _reason(key: str) -> str:
    return REASON_PREFIX + key


@dataclass
class UnwrapPlan:
    """Result of :func:`choose_strategy` (serialised into ``stats.json`` and ``--json``)."""

    method: str
    rows: int
    cols: int
    overlap_px: int
    n_parallel: int
    nproc_per_igram: int
    est_mb_per_igram: float
    reason_keys: list[str] = field(default_factory=list)
    tile_shape: tuple[int, int] = (0, 0)
    # context used by ``explain()`` and the stats file (not part of the decision contract)
    shape: tuple[int, int] = (0, 0)
    n_igrams: int = 0
    budget_mb: float = 0.0
    cores: int = 0
    single_tile_mb: float = 0.0
    tile_mb: float = 0.0
    fringe: float | None = None
    executor: str = "process"

    @property
    def n_tiles(self) -> int:
        return self.rows * self.cols

    @property
    def tiled(self) -> bool:
        return self.n_tiles > 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tile_shape"] = list(self.tile_shape)
        d["shape"] = list(self.shape)
        d["n_tiles"] = self.n_tiles
        return d

    def explain(self, lang: str | None = None) -> list[str]:
        """Human-readable reasons (i18n) in decision order."""
        params = self.to_dict()
        params["fringe"] = "-" if self.fringe is None else f"{self.fringe:.2f}"
        params["tile_shape"] = f"{self.tile_shape[0]}x{self.tile_shape[1]}"
        params["shape"] = f"{self.shape[0]}x{self.shape[1]}"
        return [t(k, lang, **params) for k in self.reason_keys]


# ---------------------------------------------------------------------------- memory


def estimate_memory_mb(
    shape: tuple[int, int], c_mb_per_mpixel: float = DEFAULT_MB_PER_MPIXEL
) -> float:
    """Single-tile unwrapper memory ``c · ny · nx / 1e6`` in MB (plan §5.4 step 1)."""
    ny, nx = int(shape[0]), int(shape[1])
    if ny < 0 or nx < 0:
        msg = f"shape must be non-negative, got {shape}"
        raise ValueError(msg)
    if c_mb_per_mpixel <= 0.0:
        msg = f"c_mb_per_mpixel must be > 0, got {c_mb_per_mpixel}"
        raise ValueError(msg)
    return float(c_mb_per_mpixel) * ny * nx / 1e6


# ---------------------------------------------------------------------------- fringes


def fringe_density(wrapped: NDArray[Any], mask: NDArray[np.bool_] | None = None) -> float:
    """Mean absolute phase gradient of a wrapped interferogram, normalised to [0, 1].

    The gradient is taken as the angle of complex neighbour differences
    (``angle(z[i+1] · conj(z[i]))``), so it is insensitive to the 2π wrap, and **both axes
    are pooled into one mean**: the score is the mean over all vertical *and* horizontal
    neighbour pairs of ``|Δφ| / π``.

    Scale, read with that pooling in mind: ``1.0`` means π rad/px in *both* directions (one
    fringe every two pixels along each axis — the aliasing limit) and pure noise gives
    ≈ 0.5. Fringes that run in one direction only score **half** of their per-axis value
    (a π/4 rad/px ramp along x alone gives 0.125, the same ramp along both axes 0.25), so
    :data:`FRINGE_HIGH` is a threshold on this pooled mean, not on a single-axis gradient.

    Pixels under ``mask`` (``True`` = masked) or with non-finite phase are ignored; returns
    NaN when nothing is valid.
    """
    w = np.asarray(wrapped)
    if w.ndim != 2:
        msg = f"wrapped must be 2-D, got shape {w.shape}"
        raise ValueError(msg)
    phase = np.angle(w).astype(np.float64) if np.iscomplexobj(w) else w.astype(np.float64)
    valid = np.isfinite(phase)
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        if m.shape != phase.shape:
            msg = f"mask shape {m.shape} != wrapped shape {phase.shape}"
            raise ValueError(msg)
        valid &= ~m
    z = np.exp(1j * np.where(valid, phase, 0.0))
    dy = np.angle(z[1:, :] * np.conj(z[:-1, :]))
    dx = np.angle(z[:, 1:] * np.conj(z[:, :-1]))
    vy = valid[1:, :] & valid[:-1, :]
    vx = valid[:, 1:] & valid[:, :-1]
    vals = np.concatenate([np.abs(dy[vy]), np.abs(dx[vx])])
    if vals.size == 0:
        return float("nan")
    return float(np.mean(vals) / np.pi)


# ---------------------------------------------------------------------------- tiles


def near_square_grid(ntiles: int, shape: tuple[int, int]) -> tuple[int, int]:
    """``(rows, cols)`` with ``rows · cols >= ntiles`` and tiles as square as possible."""
    ny, nx = int(shape[0]), int(shape[1])
    ntiles = max(1, int(ntiles))
    rows = round(math.sqrt(ntiles * ny / nx)) if nx > 0 else 1
    rows = min(max(rows, 1), max(ny, 1))
    cols = min(max(math.ceil(ntiles / rows), 1), max(nx, 1))
    while rows * cols < ntiles and rows < ny:
        rows += 1
    return rows, cols


def core_shape(shape: tuple[int, int], rows: int, cols: int) -> tuple[int, int]:
    """Largest tile core in a ``rows x cols`` partition of ``shape``."""
    return (math.ceil(int(shape[0]) / rows), math.ceil(int(shape[1]) / cols))


def overlap_pixels(core: tuple[int, int], overlap_fraction: float, min_overlap_px: int) -> int:
    """``max(fraction · min(core), min_overlap_px)``, capped at the smallest core side."""
    core_min = int(min(core))
    o = max(round(overlap_fraction * core_min), int(min_overlap_px))
    return max(0, min(o, core_min))


def tile_shape_with_overlap(
    shape: tuple[int, int], rows: int, cols: int, overlap_px: int
) -> tuple[int, int]:
    """Largest tile extent (core + overlap) the unwrapper will see."""
    cy, cx = core_shape(shape, rows, cols)
    return (
        min(int(shape[0]), cy + (overlap_px if rows > 1 else 0)),
        min(int(shape[1]), cx + (overlap_px if cols > 1 else 0)),
    )


@dataclass(frozen=True)
class _Tiling:
    rows: int
    cols: int
    overlap_px: int
    tile_shape: tuple[int, int]
    tile_mb: float
    nproc: int


def _auto_tiling(
    shape: tuple[int, int],
    single_tile_mb: float,
    c: float,
    budget_mb: float,
    nproc_target: int,
    overlap_fraction: float,
    min_overlap_px: int,
) -> _Tiling:
    """Smallest near-square grid such that ``nproc`` concurrent tiles fit the budget.

    The per-process budget is ``budget / nproc_target`` (plan §5.4 step 2 with the
    tile-parallel workers of ADR-0047), so ``ntiles = ceil(m · nproc_target / budget)``;
    the overlap margins are then accounted for and, if they push a tile over the budget,
    the grid is refined (``nproc`` shrinks first, the tile count grows second).
    """
    ntiles = max(2, math.ceil(single_tile_mb * nproc_target / budget_mb))
    last: _Tiling | None = None
    while ntiles <= MAX_TILES:
        rows, cols = near_square_grid(ntiles, shape)
        overlap = overlap_pixels(core_shape(shape, rows, cols), overlap_fraction, min_overlap_px)
        tshape = tile_shape_with_overlap(shape, rows, cols, overlap)
        tile_mb = estimate_memory_mb(tshape, c)
        fit = int(budget_mb // tile_mb) if tile_mb > 0 else nproc_target
        nproc = min(nproc_target, fit, rows * cols)
        last = _Tiling(rows, cols, overlap, tshape, tile_mb, max(1, nproc))
        if nproc >= 1:
            return last
        ntiles = rows * cols + 1
    assert last is not None
    return last  # memory insufficient even at MAX_TILES; caller records the reason


# ---------------------------------------------------------------------------- strategy


def choose_strategy(
    shape: tuple[int, int],
    n_igrams: int,
    machine: MachineSpec,
    cfg: UnwrapCfg,
    fringe: float | None = None,
    available: list[str] | None = None,
) -> UnwrapPlan:
    """Plan §5.4 rules. ``machine`` is the *budgeted* spec (``MachineSpec.budget``).

    ``available`` lists the installed engine backends (``None`` = assume all of
    :data:`ENGINE_METHODS`); it only matters when ``cfg.method == "auto"``. It must contain
    backends that can unwrap **one** interferogram: stack-only engines (``spurt``) are no
    auto-selection candidates (ADR-0045) and are filtered out by
    :func:`wintersar.unwrap.api.resolve_plan`, which knows the engine registry — this
    function takes the list as given.
    """
    ny, nx = int(shape[0]), int(shape[1])
    if ny <= 0 or nx <= 0:
        msg = f"shape must be positive, got {shape}"
        raise ValueError(msg)
    n_igrams = max(1, int(n_igrams))
    c = float(cfg.memory_mb_per_mpixel)
    budget_mb = max(0.0, float(machine.memory_gb)) * MB_PER_GB
    cores = max(1, int(machine.cores))
    nproc_cfg = max(1, int(cfg.nproc_per_igram))
    single_mb = estimate_memory_mb((ny, nx), c)
    reasons: list[str] = []

    # -- 1/2. tiles ---------------------------------------------------------------
    if isinstance(cfg.tiles, TilesCfg):
        rows = min(int(cfg.tiles.rows), ny)
        cols = min(int(cfg.tiles.cols), nx)
        overlap = (
            overlap_pixels(
                core_shape((ny, nx), rows, cols), cfg.tiles.overlap, cfg.tiles.min_overlap_px
            )
            if rows * cols > 1
            else 0
        )
        tshape = tile_shape_with_overlap((ny, nx), rows, cols, overlap)
        tile_mb = estimate_memory_mb(tshape, c)
        nproc = nproc_cfg
        reasons.append(_reason("tiles_explicit"))
        if budget_mb > 0.0 and tile_mb * nproc > budget_mb:
            reasons.append(_reason("memory_insufficient"))
    elif budget_mb <= 0.0 or single_mb <= budget_mb:
        rows, cols, overlap = 1, 1, 0
        tshape = (ny, nx)
        tile_mb = single_mb
        nproc = nproc_cfg
        reasons.append(_reason("single_tile"))
    else:
        reasons.append(_reason("tiled_memory"))
        nproc_target = nproc_cfg if nproc_cfg > 1 else cores
        tiling = _auto_tiling(
            (ny, nx),
            single_mb,
            c,
            budget_mb,
            nproc_target,
            DEFAULT_OVERLAP_FRACTION,
            DEFAULT_MIN_OVERLAP_PX,
        )
        rows, cols, overlap = tiling.rows, tiling.cols, tiling.overlap_px
        tshape, tile_mb, nproc = tiling.tile_shape, tiling.tile_mb, tiling.nproc
        if nproc_cfg == 1 and nproc > 1:
            reasons.append(_reason("nproc_raised"))
        if tile_mb > budget_mb:
            reasons.append(_reason("memory_insufficient"))

    tiled = rows * cols > 1
    est_mb = tile_mb * nproc if tiled else single_mb

    # -- 4. parallelism (ADR-0047) ---------------------------------------------------
    by_cores = max(1, cores // nproc)
    by_mem = max(1, int(budget_mb // est_mb)) if (budget_mb > 0.0 and est_mb > 0.0) else by_cores
    n_parallel = max(1, min(by_cores, by_mem, n_igrams))
    if n_parallel == n_igrams and n_igrams < min(by_cores, by_mem):
        reasons.append(_reason("parallel_igrams_bound"))
    elif by_cores <= by_mem:
        reasons.append(_reason("parallel_cores_bound"))
    else:
        reasons.append(_reason("parallel_memory_bound"))

    # -- 3. method -------------------------------------------------------------------
    avail = list(available) if available is not None else list(ENGINE_METHODS)
    high_fringe = fringe is not None and math.isfinite(fringe) and fringe >= FRINGE_HIGH
    if cfg.method != "auto":
        method = str(cfg.method)
        reasons.append(_reason("method_explicit"))
    elif (tiled or high_fringe) and "tophu" in avail:
        method = "tophu"
        reasons.append(_reason("method_tophu_large" if tiled else "method_tophu_fringe"))
    elif "snaphu" in avail:
        method = "snaphu"
        reasons.append(_reason("method_snaphu_default"))
    elif avail:
        method = avail[0]
        reasons.append(_reason("method_first_available"))
    else:
        method = "snaphu"
        reasons.append(_reason("method_none_available"))

    return UnwrapPlan(
        method=method,
        rows=rows,
        cols=cols,
        overlap_px=overlap,
        n_parallel=n_parallel,
        nproc_per_igram=nproc,
        est_mb_per_igram=est_mb,
        reason_keys=reasons,
        tile_shape=tshape,
        shape=(ny, nx),
        n_igrams=n_igrams,
        budget_mb=budget_mb,
        cores=cores,
        single_tile_mb=single_mb,
        tile_mb=tile_mb,
        fringe=fringe,
    )
