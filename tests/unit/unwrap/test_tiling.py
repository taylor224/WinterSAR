"""Tile grid, seam detector (boundary_jumps) and simple merge (R-06, PERF-04)."""

from __future__ import annotations

import numpy as np
import pytest

from wintersar.unwrap.tiling import (
    Tile,
    adjacent_pairs,
    boundary_jumps,
    merge_labels,
    merge_tiles,
    tile_grid,
)

TWO_PI = 2 * np.pi


def _smooth(shape):
    y, x = np.mgrid[0 : shape[0], 0 : shape[1]].astype(np.float64)
    return 0.02 * x + 0.01 * y + 0.5 * np.sin(x / 17.0)


# ------------------------------------------------------------------ grid


@pytest.mark.parametrize(
    ("shape", "rows", "cols", "overlap"),
    [((100, 130), 3, 4, 10), ((64, 64), 2, 2, 7), ((50, 200), 1, 5, 20), ((33, 10), 4, 1, 0)],
)
def test_tile_grid_covers_everything_with_requested_overlap(shape, rows, cols, overlap):
    tiles = tile_grid(shape, rows, cols, overlap)
    assert len(tiles) == rows * cols
    cover = np.zeros(shape, dtype=int)
    for t in tiles:
        cover[t.core] += 1
        assert t.slice_y.start <= t.core_slice_y.start < t.core_slice_y.stop <= t.slice_y.stop
        assert t.slice_x.start <= t.core_slice_x.start < t.core_slice_x.stop <= t.slice_x.stop
        assert t.shape == (t.slice_y.stop - t.slice_y.start, t.slice_x.stop - t.slice_x.start)
    assert (cover == 1).all()  # cores are disjoint and exhaustive
    for ia, ib, axis in adjacent_pairs(tiles):
        a, b = tiles[ia], tiles[ib]
        if axis == "row":
            assert a.slice_y.stop - b.slice_y.start == overlap
            assert a.core_slice_y.stop == b.core_slice_y.start
        else:
            assert a.slice_x.stop - b.slice_x.start == overlap
            assert a.core_slice_x.stop == b.core_slice_x.start
    # extents never leave the raster
    assert all(t.slice_y.start >= 0 and t.slice_y.stop <= shape[0] for t in tiles)
    assert all(t.slice_x.start >= 0 and t.slice_x.stop <= shape[1] for t in tiles)
    assert tiles[0].to_dict()["row"] == 0


def test_tile_grid_single_tile_is_whole_raster():
    (t,) = tile_grid((10, 20), 1, 1, 5)
    assert t.extent == (slice(0, 10), slice(0, 20)) and t.core == t.extent
    assert t.core_in_tile == (slice(0, 10), slice(0, 20))


def test_tile_grid_rejects_bad_arguments():
    with pytest.raises(ValueError):
        tile_grid((10, 10), 0, 1, 0)
    with pytest.raises(ValueError):
        tile_grid((10, 10), 11, 1, 0)
    with pytest.raises(ValueError):
        tile_grid((10, 10), 2, 2, -1)
    with pytest.raises(ValueError):
        tile_grid((10, 10), 2, 2, 6)  # overlap larger than the 5-px cores
    with pytest.raises(ValueError):
        tile_grid((0, 10), 1, 1, 0)


def test_adjacent_pairs_count():
    tiles = tile_grid((30, 30), 3, 3, 2)
    pairs = adjacent_pairs(tiles)
    assert len(pairs) == 2 * 3 * 2  # 6 row boundaries + 6 col boundaries


# ------------------------------------------------------------------ seam detector


def test_boundary_jumps_overlap_mode_detects_injected_offsets_exactly():
    shape = (60, 80)
    field = _smooth(shape)
    tiles = tile_grid(shape, 2, 2, 8)
    offsets = {(0, 0): 0, (0, 1): 1, (1, 0): 0, (1, 1): -2}
    arrays = [field[t.extent] + TWO_PI * offsets[(t.row, t.col)] for t in tiles]
    res = boundary_jumps(arrays, tiles)
    assert res["mode"] == "overlap"
    assert res["n_boundaries"] == 4
    expected = {((0, 0), (0, 1)): -1, ((0, 0), (1, 0)): 0, ((0, 1), (1, 1)): 3, ((1, 0), (1, 1)): 2}
    for b in res["boundaries"]:
        key = (tuple(b["tile_a"]), tuple(b["tile_b"]))
        assert b["mode_offset_cycles"] == expected[key], b
        assert b["n_pixels"] > 0
        # every overlap pixel agrees (clean synthetic field)
        assert b["histogram"] == {str(expected[key]): b["n_pixels"]}
        assert b["n_jump_pixels"] == (b["n_pixels"] if expected[key] != 0 else 0)
    assert res["n_boundaries_with_jump"] == 3
    assert res["n_jump_pixels"] == sum(
        b["n_pixels"] for b in res["boundaries"] if b["mode_offset_cycles"]
    )
    assert set(res["histogram"]) == {"-1", "0", "3", "2"}


def test_boundary_jumps_overlap_mode_no_offsets_is_clean():
    shape = (40, 40)
    tiles = tile_grid(shape, 2, 2, 6)
    field = _smooth(shape)
    res = boundary_jumps([field[t.extent] for t in tiles], tiles)
    assert res["n_boundaries_with_jump"] == 0 and res["n_jump_pixels"] == 0
    assert res["histogram"] == {"0": res["n_pixels"]}


def test_boundary_jumps_seam_mode_on_merged_raster():
    shape = (50, 70)
    tiles = tile_grid(shape, 2, 2, 4)
    merged = _smooth(shape)
    t11 = next(t for t in tiles if (t.row, t.col) == (1, 1))
    merged[t11.core] += TWO_PI  # tile (1,1) is one cycle high
    res = boundary_jumps(merged, tiles)
    assert res["mode"] == "seam"
    by_key = {(tuple(b["tile_a"]), tuple(b["tile_b"])): b for b in res["boundaries"]}
    assert by_key[((0, 1), (1, 1))]["mode_offset_cycles"] == 1
    assert by_key[((1, 0), (1, 1))]["mode_offset_cycles"] == 1
    assert by_key[((0, 0), (0, 1))]["mode_offset_cycles"] == 0
    assert by_key[((0, 0), (1, 0))]["mode_offset_cycles"] == 0
    assert res["n_boundaries_with_jump"] == 2
    # NaN pixels on the seam are ignored, not counted
    merged[t11.core_slice_y.start, :] = np.nan
    res2 = boundary_jumps(merged, tiles)
    assert by_key[((0, 1), (1, 1))]["n_pixels"] > res2["boundaries"][2]["n_pixels"] == 0


def test_boundary_jumps_single_tile_has_no_boundaries():
    tiles = tile_grid((10, 10), 1, 1, 0)
    res = boundary_jumps(np.zeros((10, 10)), tiles)
    assert res["n_boundaries"] == 0 and res["histogram"] == {}


def test_boundary_jumps_rejects_length_mismatch():
    tiles = tile_grid((10, 10), 2, 1, 2)
    with pytest.raises(ValueError):
        boundary_jumps([np.zeros((6, 10))], tiles)


# ------------------------------------------------------------------ merge


def test_merge_tiles_feather_reconstructs_consistent_field():
    shape = (70, 90)
    field = _smooth(shape).astype(np.float32)
    tiles = tile_grid(shape, 3, 2, 9)
    merged = merge_tiles([field[t.extent] for t in tiles], tiles, shape)
    assert merged.dtype == np.float32 and merged.shape == shape
    np.testing.assert_allclose(merged, field, atol=1e-5)
    core = merge_tiles([field[t.extent] for t in tiles], tiles, shape, method="core")
    np.testing.assert_array_equal(core, field)


def test_merge_tiles_feather_blends_offsets_and_ignores_nan():
    shape = (40, 40)
    field = _smooth(shape)
    tiles = tile_grid(shape, 1, 2, 10)
    a = field[tiles[0].extent].copy()
    b = field[tiles[1].extent].copy() + 1.0
    merged = merge_tiles([a, b], tiles, shape)
    # far from the seam each side keeps its own value; inside the overlap it is a blend
    assert merged[20, 0] == pytest.approx(field[20, 0], abs=1e-5)
    assert merged[20, 39] == pytest.approx(field[20, 39] + 1.0, abs=1e-5)
    mid = merged[20, 20] - field[20, 20]
    assert 0.0 < mid < 1.0
    # NaN in one tile's overlap -> value taken from the other tile alone
    a_nan = a.copy()
    a_nan[:, -5:] = np.nan
    merged2 = merge_tiles([a_nan, b], tiles, shape)
    assert np.isfinite(merged2).all()
    # NaN in both -> NaN
    b_nan = b.copy()
    b_nan[:, 5:10] = np.nan  # same global columns (20..25) as a_nan's last five
    merged3 = merge_tiles([a_nan, b_nan], tiles, shape)
    assert np.isnan(merged3[:, 20:25]).all()
    assert np.isfinite(merged3[:, :20]).all() and np.isfinite(merged3[:, 25:]).all()


def test_merge_tiles_shape_checks():
    tiles = tile_grid((10, 10), 1, 2, 2)
    with pytest.raises(ValueError):
        merge_tiles([np.zeros((10, 6))], tiles, (10, 10))
    with pytest.raises(ValueError):
        merge_tiles([np.zeros((10, 6)), np.zeros((9, 6))], tiles, (10, 10))


def test_merge_labels_are_unique_across_tiles():
    shape = (20, 20)
    tiles = tile_grid(shape, 2, 2, 4)
    labels = []
    for t in tiles:
        lab = np.ones(t.shape, dtype=np.uint16)
        cy, cx = t.core_in_tile
        lab[cy.start, cx.start] = 0  # a masked pixel inside the core
        lab[cy.start + 1, cx.start] = 2  # a second component inside the core
        lab[-1, -1] = 3  # label only in the overlap margin -> dropped unless it is core
        labels.append(lab)
    merged = merge_labels(labels, tiles, shape)
    assert merged.dtype == np.uint16
    values = set(np.unique(merged).tolist())
    assert 0 in values
    # 2 core labels x 4 tiles, all distinct, plus label 3 only where it falls in a core
    n_corner = sum(
        1
        for t in tiles
        if t.core_in_tile[0].stop == t.shape[0] and t.core_in_tile[1].stop == t.shape[1]
    )
    assert len(values - {0}) == 8 + n_corner
    assert (merged[tiles[0].core] > 0).sum() == np.prod(tiles[0].core_shape) - 1


def test_tile_dataclass_properties():
    t = Tile(0, 0, slice(0, 10), slice(0, 12), slice(0, 8), slice(0, 9))
    assert t.core_shape == (8, 9) and t.shape == (10, 12)
