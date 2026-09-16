"""research/stitching.py — coarse_ref and overlap_consensus recover injected 2π offsets
(R-06/R-07, ADR-0063)."""

from __future__ import annotations

import numpy as np
import pytest

from wintersar.research import stitching as st
from wintersar.research import synth
from wintersar.research.repr_phase import ResearchError, truth_lowres_phase
from wintersar.unwrap.tiling import Tile, tile_grid


def test_tiles_from_slices_reproduces_tile_grid_cores():
    grid = tile_grid((100, 90), 3, 2, 10)
    rebuilt = st.tiles_from_slices([(t.slice_y, t.slice_x) for t in grid], (100, 90))
    assert all(isinstance(t, Tile) for t in rebuilt)
    for a, b in zip(grid, rebuilt, strict=True):
        assert (a.row, a.col) == (b.row, b.col)
        assert a.core_slice_y == b.core_slice_y and a.core_slice_x == b.core_slice_x
    assert len(st.adjacent_tiles(rebuilt)) == 3 * 1 + 2 * 2  # col-edges + row-edges


def test_weighted_mode_and_median():
    v = np.array([1, 1, 2, 2, 2, -3])
    assert st.weighted_mode(v, np.ones(6)) == (2, 0.5)
    assert st.weighted_mode(v, np.array([5, 5, 1, 1, 1, 0]))[0] == 1
    assert st.weighted_mode(np.array([1, -1]), np.ones(2))[0] == -1  # tie -> smaller |k|
    assert st.weighted_median(v, np.ones(6))[0] == 1
    assert st.weighted_mode(np.zeros(0), np.zeros(0)) == (0, 0.0)


@pytest.mark.parametrize("method", ["coarse_ref", "overlap_consensus"])
def test_stitch_recovers_injected_offsets_exactly(tiled_truth, method):
    tt = tiled_truth
    ref = truth_lowres_phase(tt.unw_true, 3) if method == "coarse_ref" else None
    merged, offsets, report = st.stitch(
        tt.tiles, tt.shape, method, lowres_ref=ref, coh=tt.coherence
    )
    assert offsets == tt.offsets_cycles
    assert merged.shape == tt.shape and merged.dtype == np.float32
    assert np.isfinite(merged).all()
    assert report["boundary_jumps"]["n_boundaries"] == 12
    assert report["boundary_jumps"]["n_boundaries_with_jump"] == 0
    assert report["boundary_jumps"]["n_jump_pixels"] == 0
    assert synth.unwrap_error_fraction(merged.astype(np.float64), tt.unw_true) == 0.0
    if method == "overlap_consensus":
        assert report["n_edges"] == 12 and report["n_inconsistent_edges"] == 0
        assert all(b["agreement"] > 0.9 for b in report["boundaries"])
    else:
        assert all(abs(r["residual_rad"]) < 0.5 for r in report["tiles"])


def test_coarse_ref_tolerates_reference_noise_and_uses_median(tiled_truth):
    tt = tiled_truth
    rng = np.random.default_rng(0)
    ref = truth_lowres_phase(tt.unw_true, 3) + rng.standard_normal((32, 32)) * 0.8
    _, offsets, _ = st.stitch(tt.tiles, tt.shape, "coarse_ref", lowres_ref=ref, ref_stat="median")
    assert offsets == tt.offsets_cycles


def test_overlap_consensus_weights_and_loop_residual():
    rng = np.random.default_rng(1)
    tt = synth.make_tiled_truth((64, 64), 2, 2, 8, rng, offsets_cycles=[0, 1, -2, 3])
    # corrupt one overlap with a 2π error in *half* of the pixels but give them zero weight
    arr, sy, sx = tt.tiles[1]
    bad = arr.copy()
    bad[:, :4] += 2 * np.pi  # left margin overlaps tile 0
    tiles = [tt.tiles[0], (bad, sy, sx), tt.tiles[2], tt.tiles[3]]
    coh = np.ones(tt.shape)
    coh[:, sx.start : sx.start + 4] = 0.0
    _, offsets, report = st.stitch(tiles, tt.shape, "overlap_consensus", coh=coh)
    assert offsets == [0, 1, -2, 3]
    # without the weights the corrupted overlap makes the graph inconsistent but the
    # least-squares adjustment still returns integers and reports the residual
    _, offsets2, report2 = st.stitch(tiles, tt.shape, "overlap_consensus", consensus="median")
    assert len(offsets2) == 4 and offsets2[0] == 0
    assert report2["residual_max_cycles"] >= 0.0
    assert {b["axis"] for b in report["boundaries"]} == {"row", "col"}


def test_stitch_input_validation(tiled_truth):
    tt = tiled_truth
    with pytest.raises(ResearchError) as ei:
        st.stitch(tt.tiles, tt.shape, "coarse_ref")
    assert ei.value.rule_id == "RES-004"
    with pytest.raises(ResearchError) as ei:
        st.stitch(tt.tiles, tt.shape, "bogus")
    assert ei.value.rule_id == "RES-001"
    arr, sy, sx = tt.tiles[0]
    with pytest.raises(ResearchError) as ei:
        st.stitch([(arr[:-1], sy, sx)], tt.shape, "overlap_consensus")
    assert ei.value.rule_id == "RES-006"


def test_core_merge_and_npz_roundtrip(tmp_path, tiled_truth):
    tt = tiled_truth
    merged, _, _ = st.stitch(
        tt.tiles, tt.shape, "overlap_consensus", coh=tt.coherence, merge="core"
    )
    np.testing.assert_allclose(merged, tt.unw_true, atol=1.0)  # tiles carry 0.2 rad noise
    p = st.save_tiles_npz(
        tmp_path / "t.npz", tt.tiles, tt.shape, coh=tt.coherence, offsets_cycles=tt.offsets_cycles
    )
    back = st.load_tiles_npz(p)
    assert back["shape"] == tt.shape and len(back["tiles"]) == 9
    assert [int(v) for v in back["offsets_cycles"]] == tt.offsets_cycles
    assert back["lowres_ref"] is None and back["coh"].shape == tt.shape
    for (a, sy, sx), (b, ty, tx) in zip(tt.tiles, back["tiles"], strict=True):
        assert (sy, sx) == (ty, tx)
        np.testing.assert_allclose(a, b, atol=1e-5)
