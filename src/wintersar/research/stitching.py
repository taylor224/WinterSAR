"""Integer-2π tile stitching for independently unwrapped tiles (plan §5.7, §12.1, R-06/R-07;
ADR-0063).

Tiles are ``(unw_tile, slice_y, slice_x)`` triples in extent coordinates (the layout
``wintersar.unwrap.tiling.tile_grid`` produces: disjoint cores, extents overlapping by
``overlap`` pixels). Independent unwrapping leaves each tile with an unknown ambiguity
``2π·k_i``; stitching estimates ``k_i`` and subtracts it.

* ``coarse_ref`` (tophu style): ``k_i = round(mean(unw_tile - upsample(lowres_ref)) / 2π)``
  per tile against an unwrapped low-resolution reference.
  # source: https://raw.githubusercontent.com/isce-framework/tophu/v0.2.1/src/tophu/_multiscale.py
  #   adjust_conncomp_offset_cycles: avg_offset = mean(unwrapped_hires - unwrapped_lores);
  #   avg_offset_cycles = round(avg_offset / (2π)); hires -= 2π·avg_offset_cycles
  #   (tophu applies it per connected component; here per tile, see ADR-0063)
* ``overlap_consensus``: for every adjacent tile pair the per-pixel ``round((A - B) / 2π)``
  on the extent overlap is reduced to one integer by a coherence-weighted **mode** (or
  median); the pairwise offsets ``o_a - o_b = k_ab`` are then adjusted by weighted least
  squares over the tile graph (incidence matrix, weights = summed overlap coherence, gauge
  ``o_0 = 0``) and rounded. Inconsistent loops show up as non-zero residuals.

The tile geometry (:class:`wintersar.unwrap.tiling.Tile`), the seam detector
(``boundary_jumps``) and the plain merge (``merge_tiles``) are shared with the unwrap module
(ADR-0048) and imported directly — there is exactly one implementation of each.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from wintersar.research.repr_phase import ResearchError, upsample_nearest
from wintersar.unwrap.tiling import Tile, adjacent_pairs, boundary_jumps, merge_tiles

FloatArray = NDArray[np.float64]
TileArray = tuple[NDArray[Any], slice, slice]
StitchMethod = Literal["coarse_ref", "overlap_consensus"]
Consensus = Literal["mode", "median"]
Merge = Literal["feather", "core"]

TWO_PI = 2.0 * np.pi
METHODS: tuple[str, ...] = ("coarse_ref", "overlap_consensus")

__all__ = [
    "METHODS",
    "TileArray",
    "adjacent_tiles",
    "coarse_ref_offsets",
    "load_tiles_npz",
    "overlap_consensus_offsets",
    "save_tiles_npz",
    "stitch",
    "tiles_from_slices",
    "weighted_median",
    "weighted_mode",
]


def _axis_cores(extents: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
    """Core intervals for sorted, overlapping extents along one axis: the overlap between
    consecutive extents is split ``overlap // 2`` to the lower tile (the ``tile_grid`` rule)."""
    cores: list[tuple[int, int]] = []
    starts = [0]
    for prev, cur in itertools.pairwise(extents):
        ov = max(prev[1] - cur[0], 0)
        starts.append(cur[0] + ov // 2)
    for i, (_, stop) in enumerate(extents):
        end = starts[i + 1] if i + 1 < len(extents) else max(stop, n)
        cores.append((starts[i], end))
    return cores


def tiles_from_slices(slices: Sequence[tuple[slice, slice]], shape: tuple[int, int]) -> list[Any]:
    """Rebuild the grid geometry (row/col index, cores) from tile extents as
    :class:`wintersar.unwrap.tiling.Tile` objects. Extents must form a rectangular grid.
    """
    ys = sorted({(s[0].start, s[0].stop) for s in slices})
    xs = sorted({(s[1].start, s[1].stop) for s in slices})
    cy = _axis_cores(ys, int(shape[0]))
    cx = _axis_cores(xs, int(shape[1]))
    out: list[Any] = []
    for sy, sx in slices:
        r = ys.index((sy.start, sy.stop))
        c = xs.index((sx.start, sx.stop))
        out.append(
            Tile(
                row=r,
                col=c,
                slice_y=slice(sy.start, sy.stop),
                slice_x=slice(sx.start, sx.stop),
                core_slice_y=slice(cy[r][0], cy[r][1]),
                core_slice_x=slice(cx[c][0], cx[c][1]),
            )
        )
    return out


def adjacent_tiles(tiles: Sequence[Any]) -> list[tuple[int, int, str]]:
    """``(index_a, index_b, axis)`` for every 4-neighbour pair (b below / right of a)."""
    return [(a, b, str(axis)) for a, b, axis in adjacent_pairs(tiles)]


def _intersect(a: slice, b: slice) -> slice:
    return slice(max(a.start, b.start), min(a.stop, b.stop))


def _local(region: slice, extent: slice) -> slice:
    return slice(region.start - extent.start, region.stop - extent.start)


# ---------------------------------------------------------------- consensus statistics
def weighted_mode(values: NDArray[Any], weights: NDArray[Any]) -> tuple[int, float]:
    """``(mode, fraction)``: the integer with the largest summed weight (ties → smaller
    ``|k|``) and its share of the total weight."""
    v = np.asarray(values).astype(np.int64).ravel()
    w = np.asarray(weights, dtype=np.float64).ravel()
    if v.size == 0 or not np.isfinite(w).any():
        return 0, 0.0
    uniq, inv = np.unique(v, return_inverse=True)
    sums = np.bincount(inv, weights=w, minlength=uniq.size)
    total = float(sums.sum())
    if total <= 0:
        return 0, 0.0
    best = max(range(uniq.size), key=lambda i: (sums[i], -abs(int(uniq[i]))))
    return int(uniq[best]), float(sums[best] / total)


def weighted_median(values: NDArray[Any], weights: NDArray[Any]) -> tuple[int, float]:
    """``(median, fraction_at_median)`` of integer values under weights (lower weighted median)."""
    v = np.asarray(values).astype(np.int64).ravel()
    w = np.asarray(weights, dtype=np.float64).ravel()
    if v.size == 0:
        return 0, 0.0
    order = np.argsort(v, kind="stable")
    v, w = v[order], w[order]
    total = float(w.sum())
    if total <= 0:
        return 0, 0.0
    cum = np.cumsum(w)
    k = int(v[int(np.searchsorted(cum, total / 2.0, side="left"))])
    return k, float(w[v == k].sum() / total)


# ---------------------------------------------------------------- offsets
def _check_tiles(tiles: Sequence[TileArray], shape: tuple[int, int]) -> list[TileArray]:
    if not tiles:
        raise ResearchError("RES-006", name="tiles", value=0, allowed=">= 1")
    out: list[TileArray] = []
    for i, (arr, sy, sx) in enumerate(tiles):
        a = np.asarray(arr, dtype=np.float64)
        expect = (sy.stop - sy.start, sx.stop - sx.start)
        if a.shape != expect:
            raise ResearchError(
                "RES-006", name=f"tiles[{i}].shape", value=a.shape, allowed=str(expect)
            )
        if sy.start < 0 or sx.start < 0 or sy.stop > shape[0] or sx.stop > shape[1]:
            raise ResearchError(
                "RES-006", name=f"tiles[{i}].slices", value=(sy, sx), allowed=f"within {shape}"
            )
        out.append((a, sy, sx))
    return out


def coarse_ref_offsets(
    tiles: Sequence[TileArray],
    shape: tuple[int, int],
    lowres_ref: NDArray[Any],
    stat: Literal["mean", "median"] = "mean",
) -> tuple[list[int], list[dict[str, Any]]]:
    """Per-tile ``round(stat(unw_tile - upsampled ref) / 2π)`` (tophu uses the mean)."""
    ref_up = np.asarray(upsample_nearest(np.asarray(lowres_ref, dtype=np.float64), shape))
    ks: list[int] = []
    rows: list[dict[str, Any]] = []
    for i, (arr, sy, sx) in enumerate(tiles):
        d = arr - ref_up[sy, sx]
        valid = np.isfinite(d)
        if not valid.any():
            ks.append(0)
            rows.append({"tile": i, "n_valid": 0, "offset_cycles": 0, "residual_rad": None})
            continue
        centre = float(np.mean(d[valid]) if stat == "mean" else np.median(d[valid]))
        k = int(np.rint(centre / TWO_PI))
        ks.append(k)
        rows.append(
            {
                "tile": i,
                "n_valid": int(valid.sum()),
                "offset_cycles": k,
                "residual_rad": float(centre - TWO_PI * k),
            }
        )
    return ks, rows


def overlap_consensus_offsets(
    tiles: Sequence[TileArray],
    shape: tuple[int, int],
    coh: NDArray[Any] | None = None,
    weights: NDArray[Any] | None = None,
    consensus: Consensus = "mode",
    min_overlap_pixels: int = 1,
) -> tuple[list[int], dict[str, Any]]:
    """Pairwise overlap offsets + weighted least squares over the tile graph.

    ``coh`` (full-resolution, ``shape``) weights every overlap pixel; ``weights`` is an
    alternative per-pixel quality map with the same role (``coh`` wins when both are given).
    Returns ``(offsets_cycles, report)`` with ``offsets[0] == 0``.
    """
    boxes = tiles_from_slices([(sy, sx) for _, sy, sx in tiles], shape)
    if coh is not None:
        wmap = np.asarray(coh, dtype=np.float64)
    elif weights is not None:
        wmap = np.asarray(weights, dtype=np.float64)
    else:
        wmap = np.ones(shape, dtype=np.float64)
    if wmap.shape != tuple(shape):
        raise ResearchError("RES-006", name="coh.shape", value=wmap.shape, allowed=str(shape))
    wmap = np.where(np.isfinite(wmap), np.clip(wmap, 0.0, None), 0.0)
    n = len(tiles)
    rows_a: list[list[float]] = []
    rhs: list[float] = []
    edge_w: list[float] = []
    boundaries: list[dict[str, Any]] = []
    for ia, ib, axis in adjacent_tiles(boxes):
        a, b = boxes[ia], boxes[ib]
        ys = _intersect(a.slice_y, b.slice_y)
        xs = _intersect(a.slice_x, b.slice_x)
        if ys.stop <= ys.start or xs.stop <= xs.start:
            continue
        arr_a = tiles[ia][0][_local(ys, a.slice_y), _local(xs, a.slice_x)]
        arr_b = tiles[ib][0][_local(ys, b.slice_y), _local(xs, b.slice_x)]
        diff = np.asarray(arr_a, dtype=np.float64) - np.asarray(arr_b, dtype=np.float64)
        valid = np.isfinite(diff)
        w = wmap[ys, xs]
        info: dict[str, Any] = {
            "tile_a": ia,
            "tile_b": ib,
            "axis": axis,
            "n_pixels": int(valid.sum()),
        }
        if int(valid.sum()) < max(min_overlap_pixels, 1) or float(w[valid].sum()) <= 0:
            info.update({"offset_cycles": None, "agreement": 0.0, "weight": 0.0})
            boundaries.append(info)
            continue
        cycles = np.rint(diff[valid] / TWO_PI).astype(np.int64)
        k, frac = (
            weighted_mode(cycles, w[valid])
            if consensus == "mode"
            else weighted_median(cycles, w[valid])
        )
        weight = float(w[valid].sum()) * frac
        info.update({"offset_cycles": int(k), "agreement": frac, "weight": weight})
        boundaries.append(info)
        row = [0.0] * n
        row[ia], row[ib] = 1.0, -1.0
        rows_a.append(row)
        rhs.append(float(k))
        edge_w.append(weight)
    # gauge: tile 0 fixed at 0 (heavy weight keeps the system full rank)
    gauge = [0.0] * n
    gauge[0] = 1.0
    rows_a.append(gauge)
    rhs.append(0.0)
    edge_w.append(max(edge_w, default=1.0) * 10.0 + 1.0)
    a_mat = np.asarray(rows_a, dtype=np.float64)
    b_vec = np.asarray(rhs, dtype=np.float64)
    sw = np.sqrt(np.asarray(edge_w, dtype=np.float64))
    sol, _, rank, _ = np.linalg.lstsq(a_mat * sw[:, None], b_vec * sw, rcond=None)
    connected = np.abs(a_mat[:-1]).sum(axis=0) > 0 if len(rows_a) > 1 else np.zeros(n, bool)
    connected[0] = True
    sol = np.where(connected, sol, 0.0)
    offsets = [int(v) for v in np.rint(sol)]
    residuals = (a_mat[:-1] @ np.asarray(offsets, dtype=np.float64) - b_vec[:-1]).tolist()
    for bd, r in zip(
        [b for b in boundaries if b["offset_cycles"] is not None], residuals, strict=True
    ):
        bd["residual_cycles"] = float(r)
    report = {
        "consensus": consensus,
        "n_edges": int(len(rhs) - 1),
        "rank": int(rank),
        "n_disconnected_tiles": int((~connected).sum()),
        "residual_max_cycles": float(np.max(np.abs(residuals))) if residuals else 0.0,
        "n_inconsistent_edges": int(np.sum(np.abs(residuals) > 0.5)) if residuals else 0,
        "boundaries": boundaries,
    }
    return offsets, report


# ---------------------------------------------------------------- merge + stitch
def _merge(
    arrays: Sequence[NDArray[Any]], boxes: Sequence[Any], shape: tuple[int, int], method: Merge
) -> NDArray[np.float32]:
    return np.asarray(merge_tiles(arrays, boxes, shape, method=method), dtype=np.float32)


def _seam_report(merged: NDArray[Any], boxes: Sequence[Any]) -> dict[str, Any]:
    """Tile-seam jump statistics from the shared detector (ADR-0048)."""
    rep = boundary_jumps(merged, boxes)
    return {
        "n_boundaries": int(rep["n_boundaries"]),
        "n_boundaries_with_jump": int(rep["n_boundaries_with_jump"]),
        "n_jump_pixels": int(rep["n_jump_pixels"]),
        "histogram": dict(rep["histogram"]),
    }


def stitch(
    tiles: Sequence[TileArray],
    shape: tuple[int, int],
    method: str,
    lowres_ref: NDArray[Any] | None = None,
    coh: NDArray[Any] | None = None,
    weights: NDArray[Any] | None = None,
    *,
    merge: Merge = "feather",
    consensus: Consensus = "mode",
    ref_stat: Literal["mean", "median"] = "mean",
) -> tuple[NDArray[np.float32], list[int], dict[str, Any]]:
    """Estimate per-tile 2π offsets, subtract them and merge.

    Returns ``(merged (shape, float32, NaN outside tiles), offsets_cycles, report)`` where
    ``offsets_cycles[i]`` is the number of cycles subtracted from tile ``i``. The report
    carries the per-boundary evidence and the post-stitch seam statistics (ADR-0048
    ``boundary_jumps`` in seam mode).
    """
    if method not in METHODS:
        raise ResearchError("RES-001", method=method, methods=", ".join(METHODS))
    checked = _check_tiles(tiles, (int(shape[0]), int(shape[1])))
    shp = (int(shape[0]), int(shape[1]))
    report: dict[str, Any] = {"method": method, "n_tiles": len(checked)}
    if method == "coarse_ref":
        if lowres_ref is None:
            raise ResearchError("RES-004")
        offsets, rows = coarse_ref_offsets(checked, shp, lowres_ref, ref_stat)
        report["tiles"] = rows
        report["ref_stat"] = ref_stat
    else:
        offsets, consensus_report = overlap_consensus_offsets(
            checked, shp, coh=coh, weights=weights, consensus=consensus
        )
        report.update(consensus_report)
    corrected = [arr - TWO_PI * k for (arr, _, _), k in zip(checked, offsets, strict=True)]
    boxes = tiles_from_slices([(sy, sx) for _, sy, sx in checked], shp)
    merged = _merge(corrected, boxes, shp, merge)
    report["offsets_cycles"] = list(offsets)
    report["merge"] = merge
    report["boundary_jumps"] = _seam_report(merged, boxes)
    return merged, list(offsets), report


# ---------------------------------------------------------------- npz interchange
def save_tiles_npz(
    path: Path | str,
    tiles: Sequence[TileArray],
    shape: tuple[int, int],
    *,
    coh: NDArray[Any] | None = None,
    lowres_ref: NDArray[Any] | None = None,
    unw_true: NDArray[Any] | None = None,
    mask: NDArray[Any] | None = None,
    offsets_cycles: Sequence[int] | None = None,
) -> Path:
    """``tiles.npz``: ``tile_<i>`` arrays, ``slices`` (n, 4) ``[y0, y1, x0, x1]``, ``shape``,
    and the optional ``coh`` / ``lowres_ref`` / ``unw_true`` / ``mask`` / ``offsets_cycles``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, NDArray[Any]] = {
        "shape": np.asarray(shape, dtype=np.int64),
        "slices": np.asarray(
            [[sy.start, sy.stop, sx.start, sx.stop] for _, sy, sx in tiles], dtype=np.int64
        ),
    }
    for i, (arr, _, _) in enumerate(tiles):
        arrays[f"tile_{i}"] = np.asarray(arr, dtype=np.float32)
    if coh is not None:
        arrays["coh"] = np.asarray(coh, dtype=np.float32)
    if lowres_ref is not None:
        arrays["lowres_ref"] = np.asarray(lowres_ref, dtype=np.float32)
    if unw_true is not None:
        arrays["unw_true"] = np.asarray(unw_true, dtype=np.float32)
    if mask is not None:
        arrays["mask"] = np.asarray(mask, dtype=bool)
    if offsets_cycles is not None:
        arrays["offsets_cycles"] = np.asarray(offsets_cycles, dtype=np.int64)
    np.savez_compressed(p, **arrays)  # type: ignore[arg-type]
    return p


def load_tiles_npz(path: Path | str) -> dict[str, Any]:
    """Inverse of :func:`save_tiles_npz`: ``{"tiles": [...], "shape": (ny, nx), "coh": ...}``."""
    with np.load(Path(path), allow_pickle=False) as z:
        shape = (int(z["shape"][0]), int(z["shape"][1]))
        sl = z["slices"]
        tiles: list[TileArray] = [
            (
                np.asarray(z[f"tile_{i}"], dtype=np.float64),
                slice(int(sl[i, 0]), int(sl[i, 1])),
                slice(int(sl[i, 2]), int(sl[i, 3])),
            )
            for i in range(sl.shape[0])
        ]
        out: dict[str, Any] = {"tiles": tiles, "shape": shape}
        for key in ("coh", "lowres_ref", "unw_true", "mask", "offsets_cycles"):
            out[key] = np.asarray(z[key]) if key in z.files else None
    return out
