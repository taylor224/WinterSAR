"""Tile geometry, seam detector and the *simple* tile merge (plan §5.4, R-06, PERF-04).

* :func:`tile_grid` partitions a ``(ny, nx)`` raster into ``rows x cols`` tiles whose
  *cores* are disjoint and cover the raster, and whose *extents* overlap neighbours by
  ``overlap_px`` pixels (the same meaning as SNAPHU ``ROWOVRLP``/``COLOVRLP`` and snaphu-py
  ``tile_overlap``; source: https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/snaphu_man1.html
  "Tiles overlap by rowovrlp and colovrlp pixels in the row and column directions").
* :func:`boundary_jumps` is the tile-seam detector shared with ``research.stitching``
  (plan §6.2 PERF-04): the per-boundary histogram of ``round(Δφ / 2π)`` and the number of
  non-zero jumps.
* :func:`merge_tiles` is the plain overlap-feathered average used when a backend cannot
  tile natively. The 2π-consistent stitching methods (``coarse_ref``,
  ``overlap_consensus``) belong to ``wintersar.research.stitching`` and are *not* here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

TWO_PI = 2.0 * np.pi

__all__ = [
    "Tile",
    "adjacent_pairs",
    "boundary_jumps",
    "merge_labels",
    "merge_tiles",
    "tile_grid",
]


@dataclass(frozen=True, eq=True)
class Tile:
    """One tile: ``slice_*`` is the extent read by the unwrapper (core + overlap margins),
    ``core_slice_*`` the region this tile owns in the merged output."""

    row: int
    col: int
    slice_y: slice
    slice_x: slice
    core_slice_y: slice
    core_slice_x: slice

    @property
    def shape(self) -> tuple[int, int]:
        return (self.slice_y.stop - self.slice_y.start, self.slice_x.stop - self.slice_x.start)

    @property
    def core_shape(self) -> tuple[int, int]:
        return (
            self.core_slice_y.stop - self.core_slice_y.start,
            self.core_slice_x.stop - self.core_slice_x.start,
        )

    @property
    def extent(self) -> tuple[slice, slice]:
        return (self.slice_y, self.slice_x)

    @property
    def core(self) -> tuple[slice, slice]:
        return (self.core_slice_y, self.core_slice_x)

    @property
    def core_in_tile(self) -> tuple[slice, slice]:
        """Core region expressed in the tile's own (extent-relative) coordinates."""
        return (
            slice(
                self.core_slice_y.start - self.slice_y.start,
                self.core_slice_y.stop - self.slice_y.start,
            ),
            slice(
                self.core_slice_x.start - self.slice_x.start,
                self.core_slice_x.stop - self.slice_x.start,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "col": self.col,
            "slice_y": [self.slice_y.start, self.slice_y.stop],
            "slice_x": [self.slice_x.start, self.slice_x.stop],
            "core_slice_y": [self.core_slice_y.start, self.core_slice_y.stop],
            "core_slice_x": [self.core_slice_x.start, self.core_slice_x.stop],
        }


def _axis_partition(n: int, k: int, overlap: int) -> list[tuple[int, int, int, int]]:
    """``(extent_start, extent_stop, core_start, core_stop)`` for ``k`` pieces of length ``n``.

    Adjacent pieces share exactly ``overlap`` pixels (``overlap // 2`` taken from the lower
    neighbour's core, the rest from the upper one) unless clipped by the raster edge.
    """
    edges = [round(i * n / k) for i in range(k + 1)]
    lo = overlap // 2
    hi = overlap - lo
    out: list[tuple[int, int, int, int]] = []
    for i in range(k):
        cs, ce = edges[i], edges[i + 1]
        es = 0 if i == 0 else max(0, cs - lo)
        ee = n if i == k - 1 else min(n, ce + hi)
        out.append((es, ee, cs, ce))
    return out


def tile_grid(shape: tuple[int, int], rows: int, cols: int, overlap_px: int) -> list[Tile]:
    """Row-major list of ``rows x cols`` tiles covering ``shape`` with ``overlap_px`` overlap.

    Raises ``ValueError`` when a dimension cannot hold the requested tiles or when the
    overlap is larger than the smallest tile core (overlaps must only involve *adjacent*
    tiles, which is what the seam detector and the merge assume).
    """
    ny, nx = int(shape[0]), int(shape[1])
    if ny <= 0 or nx <= 0:
        msg = f"shape must be positive, got {shape}"
        raise ValueError(msg)
    if rows < 1 or cols < 1 or rows > ny or cols > nx:
        msg = f"rows/cols must be in [1, ny] x [1, nx]; got rows={rows} cols={cols} shape={shape}"
        raise ValueError(msg)
    if overlap_px < 0:
        msg = f"overlap_px must be >= 0, got {overlap_px}"
        raise ValueError(msg)
    ys = _axis_partition(ny, rows, overlap_px)
    xs = _axis_partition(nx, cols, overlap_px)
    core_lengths: list[int] = []
    if rows > 1:
        core_lengths += [ce - cs for _, _, cs, ce in ys]
    if cols > 1:
        core_lengths += [ce - cs for _, _, cs, ce in xs]
    if core_lengths and overlap_px > min(core_lengths):
        msg = f"overlap_px={overlap_px} exceeds the smallest tile core ({min(core_lengths)} px)"
        raise ValueError(msg)
    tiles: list[Tile] = []
    for r, (es_y, ee_y, cs_y, ce_y) in enumerate(ys):
        for c, (es_x, ee_x, cs_x, ce_x) in enumerate(xs):
            tiles.append(
                Tile(
                    row=r,
                    col=c,
                    slice_y=slice(es_y, ee_y),
                    slice_x=slice(es_x, ee_x),
                    core_slice_y=slice(cs_y, ce_y),
                    core_slice_x=slice(cs_x, ce_x),
                )
            )
    return tiles


def adjacent_pairs(tiles: Sequence[Tile]) -> list[tuple[int, int, Literal["row", "col"]]]:
    """``(index_a, index_b, axis)`` for every pair of 4-neighbours (b below / right of a)."""
    idx = {(t.row, t.col): i for i, t in enumerate(tiles)}
    pairs: list[tuple[int, int, Literal["row", "col"]]] = []
    for (r, c), i in sorted(idx.items()):
        if (r + 1, c) in idx:
            pairs.append((i, idx[(r + 1, c)], "row"))
        if (r, c + 1) in idx:
            pairs.append((i, idx[(r, c + 1)], "col"))
    return pairs


def _intersect(a: slice, b: slice) -> slice:
    return slice(max(a.start, b.start), min(a.stop, b.stop))


def _cycles(diff: NDArray[Any]) -> NDArray[np.int64]:
    d = np.asarray(diff, dtype=np.float64).ravel()
    d = d[np.isfinite(d)]
    return np.rint(d / TWO_PI).astype(np.int64)


def _seam_offsets(merged: NDArray[Any], a: Tile, b: Tile, axis: str) -> NDArray[np.int64]:
    """2π-cycle jumps across the seam between the cores of ``a`` and ``b`` in a merged raster.

    In a correctly unwrapped field neighbouring pixels differ by less than π, so any
    ``round(Δφ/2π) != 0`` across the seam line is a tile offset.
    """
    ny, nx = merged.shape
    if axis == "row":
        y0 = b.core_slice_y.start
        xs = _intersect(a.core_slice_x, b.core_slice_x)
        if y0 <= 0 or y0 >= ny or xs.stop <= xs.start:
            return np.zeros(0, dtype=np.int64)
        return _cycles(merged[y0, xs].astype(np.float64) - merged[y0 - 1, xs].astype(np.float64))
    x0 = b.core_slice_x.start
    ys = _intersect(a.core_slice_y, b.core_slice_y)
    if x0 <= 0 or x0 >= nx or ys.stop <= ys.start:
        return np.zeros(0, dtype=np.int64)
    return _cycles(merged[ys, x0].astype(np.float64) - merged[ys, x0 - 1].astype(np.float64))


def _overlap_offsets(
    arr_a: NDArray[Any], arr_b: NDArray[Any], a: Tile, b: Tile
) -> NDArray[np.int64]:
    """``round((A - B) / 2π)`` on the extent overlap of two independently unwrapped tiles."""
    ys = _intersect(a.slice_y, b.slice_y)
    xs = _intersect(a.slice_x, b.slice_x)
    if ys.stop <= ys.start or xs.stop <= xs.start:
        return np.zeros(0, dtype=np.int64)
    ra = (
        slice(ys.start - a.slice_y.start, ys.stop - a.slice_y.start),
        slice(xs.start - a.slice_x.start, xs.stop - a.slice_x.start),
    )
    rb = (
        slice(ys.start - b.slice_y.start, ys.stop - b.slice_y.start),
        slice(xs.start - b.slice_x.start, xs.stop - b.slice_x.start),
    )
    return _cycles(
        np.asarray(arr_a, dtype=np.float64)[ra] - np.asarray(arr_b, dtype=np.float64)[rb]
    )


def _histogram(k: NDArray[np.int64]) -> dict[int, int]:
    if k.size == 0:
        return {}
    values, counts = np.unique(k, return_counts=True)
    return {int(v): int(c) for v, c in zip(values, counts, strict=True)}


def boundary_jumps(
    data: NDArray[Any] | Sequence[NDArray[Any]],
    tiles: Sequence[Tile],
) -> dict[str, Any]:
    """Tile-seam detector: distribution of 2π-integer offsets on every tile boundary.

    ``data`` is either the merged 2-D raster (offsets measured across the seam line
    between neighbouring cores) or the list of per-tile unwrapped arrays in extent
    coordinates (offsets measured on the overlap region, ``A - B`` in cycles). Returns::

        {"mode": "seam" | "overlap",
         "n_boundaries": int, "n_boundaries_with_jump": int,
         "n_pixels": int, "n_jump_pixels": int,
         "histogram": {"<cycles>": count, ...},          # aggregated over boundaries
         "boundaries": [{"tile_a": [r, c], "tile_b": [r, c], "axis": "row"|"col",
                         "n_pixels": int, "n_jump_pixels": int,
                         "mode_offset_cycles": int, "histogram": {...}}, ...]}

    A boundary "has a jump" when the modal offset is non-zero; ``n_jump_pixels`` counts
    every pixel whose offset is non-zero (also catches partial / spatially varying seams).
    """
    arrays: list[NDArray[Any]] | None
    if isinstance(data, np.ndarray) and data.ndim == 2:
        mode: Literal["seam", "overlap"] = "seam"
        merged: NDArray[Any] | None = data
        arrays = None
    else:
        mode = "overlap"
        merged = None
        arrays = [np.asarray(a) for a in data]
        if len(arrays) != len(tiles):
            msg = f"{len(arrays)} tile arrays for {len(tiles)} tiles"
            raise ValueError(msg)
    boundaries: list[dict[str, Any]] = []
    total: dict[int, int] = {}
    n_with_jump = 0
    for ia, ib, axis in adjacent_pairs(tiles):
        a, b = tiles[ia], tiles[ib]
        if merged is not None:
            k = _seam_offsets(merged, a, b, axis)
        else:
            assert arrays is not None
            k = _overlap_offsets(arrays[ia], arrays[ib], a, b)
        hist = _histogram(k)
        mode_k = max(hist.items(), key=lambda kv: (kv[1], -abs(kv[0])))[0] if hist else 0
        n_nonzero = int(np.count_nonzero(k))
        if mode_k != 0:
            n_with_jump += 1
        for kk, cnt in hist.items():
            total[kk] = total.get(kk, 0) + cnt
        boundaries.append(
            {
                "tile_a": [a.row, a.col],
                "tile_b": [b.row, b.col],
                "axis": axis,
                "n_pixels": int(k.size),
                "n_jump_pixels": n_nonzero,
                "mode_offset_cycles": int(mode_k),
                "histogram": {str(kk): cnt for kk, cnt in sorted(hist.items())},
            }
        )
    return {
        "mode": mode,
        "n_boundaries": len(boundaries),
        "n_boundaries_with_jump": n_with_jump,
        "n_pixels": int(sum(b["n_pixels"] for b in boundaries)),
        "n_jump_pixels": int(sum(b["n_jump_pixels"] for b in boundaries)),
        "histogram": {str(kk): cnt for kk, cnt in sorted(total.items())},
        "boundaries": boundaries,
    }


def _axis_weights(ext: slice, core: slice) -> NDArray[np.float64]:
    """Feather ramp along one axis: 1 inside, decaying to ~0 at the extent edge.

    The ramp spans twice the overlap margin so that the two neighbours' weights cross at
    the core boundary (their sum is renormalised in :func:`merge_tiles`).
    """
    n = ext.stop - ext.start
    w = np.ones(n, dtype=np.float64)
    m_lo = core.start - ext.start
    m_hi = ext.stop - core.stop
    if m_lo > 0:
        length = min(n, 2 * m_lo)
        ramp = (np.arange(length, dtype=np.float64) + 1.0) / length
        w[:length] = np.minimum(w[:length], ramp)
    if m_hi > 0:
        length = min(n, 2 * m_hi)
        ramp = 1.0 - np.arange(length, dtype=np.float64) / length
        w[n - length :] = np.minimum(w[n - length :], ramp)
    return w


def merge_tiles(
    tile_arrays: Sequence[NDArray[Any]],
    tiles: Sequence[Tile],
    shape: tuple[int, int],
    method: Literal["feather", "core"] = "feather",
) -> NDArray[np.float32]:
    """Merge per-tile unwrapped arrays (extent coordinates) into one ``shape`` raster.

    ``feather``: NaN-aware weighted average over the overlaps (linear ramps). ``core``:
    every pixel is taken from the tile that owns it. Neither method removes 2π offsets
    between tiles — that is the job of ``research.stitching`` / the backend's own tile
    assembly; use :func:`boundary_jumps` to measure what is left.
    """
    if len(tile_arrays) != len(tiles):
        msg = f"{len(tile_arrays)} tile arrays for {len(tiles)} tiles"
        raise ValueError(msg)
    ny, nx = int(shape[0]), int(shape[1])
    if method == "core":
        out = np.full((ny, nx), np.nan, dtype=np.float32)
        for arr, t in zip(tile_arrays, tiles, strict=True):
            out[t.core] = np.asarray(arr, dtype=np.float32)[t.core_in_tile]
        return out
    acc = np.zeros((ny, nx), dtype=np.float64)
    wsum = np.zeros((ny, nx), dtype=np.float64)
    for arr, t in zip(tile_arrays, tiles, strict=True):
        a = np.asarray(arr, dtype=np.float64)
        if a.shape != t.shape:
            msg = f"tile ({t.row},{t.col}) array shape {a.shape} != tile shape {t.shape}"
            raise ValueError(msg)
        w = np.outer(
            _axis_weights(t.slice_y, t.core_slice_y), _axis_weights(t.slice_x, t.core_slice_x)
        )
        valid = np.isfinite(a)
        acc[t.extent] += np.where(valid, a * w, 0.0)
        wsum[t.extent] += np.where(valid, w, 0.0)
    out64 = np.full((ny, nx), np.nan, dtype=np.float64)
    nz = wsum > 0
    out64[nz] = acc[nz] / wsum[nz]
    return out64.astype(np.float32)


def merge_labels(
    tile_labels: Sequence[NDArray[Any]],
    tiles: Sequence[Tile],
    shape: tuple[int, int],
) -> NDArray[np.integer[Any]]:
    """Core-assign connected-component labels, renumbered so that labels are unique
    across tiles (a component spanning two tiles keeps two labels; 0 stays 0)."""
    if len(tile_labels) != len(tiles):
        msg = f"{len(tile_labels)} label arrays for {len(tiles)} tiles"
        raise ValueError(msg)
    out = np.zeros((int(shape[0]), int(shape[1])), dtype=np.uint32)
    next_label = 1
    for lab, t in zip(tile_labels, tiles, strict=True):
        core = np.asarray(lab)[t.core_in_tile]
        values = np.unique(core[core > 0])
        if values.size == 0:
            continue
        lut = {int(v): next_label + i for i, v in enumerate(values)}
        next_label += int(values.size)
        mapped = np.zeros(core.shape, dtype=np.uint32)
        for v, new in lut.items():
            mapped[core == v] = new
        out[t.core] = mapped
    if next_label - 1 < np.iinfo(np.uint16).max:
        return out.astype(np.uint16)
    return out
