"""The research module has exactly one implementation of each shared primitive.

Post-integration the ``except ImportError`` fallbacks over same-tree modules
(``wintersar.unwrap.tiling``, ``wintersar.validate.*``) were unreachable, so the duplicated
bodies could drift from the canonical ones without any test noticing — the ``_seam_report``
fallback had already drifted (it returned an empty ``histogram``). These tests pin the
single-implementation contract (CLAUDE.md "do not fork shared contracts; extend in place").
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from wintersar.research import metrics as mt
from wintersar.research import synth
from wintersar.research.stitching import tiles_from_slices
from wintersar.unwrap.tiling import Tile, tile_grid

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "research"


def test_no_same_tree_import_fallbacks_left():
    offenders: list[str] = []
    for py in sorted(SRC.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        for m in re.finditer(r"except ImportError", text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{py.name}:{line}")
    assert not offenders, (
        "same-tree imports must not be guarded (they can never fail and the fallback "
        f"silently diverges): {offenders}"
    )


def test_tile_geometry_is_the_unwrap_tiling_one():
    slices = synth.tile_slices((64, 48), 2, 3, 8)
    assert slices == [(t.slice_y, t.slice_x) for t in tile_grid((64, 48), 2, 3, 8)]
    boxes = tiles_from_slices(slices, (64, 48))
    assert boxes and all(isinstance(b, Tile) for b in boxes)


def test_seam_report_carries_the_shared_histogram(rng):
    tt = synth.make_tiled_truth((64, 64), 2, 2, 8, rng, offsets_cycles=[0, 1, -2, 0])
    raw = np.full(tt.shape, np.nan)
    for (arr, _, _), box in zip(tt.tiles, tiles_from_slices(tt.slices, tt.shape), strict=True):
        raw[box.core] = arr[box.core_in_tile]
    rep = mt.seam_jumps(raw, tt.slices, tt.shape)
    assert rep["n_boundaries"] > 0 and rep["n_boundaries_with_jump"] > 0
    # the dead fallback returned {} here; the shared detector fills it in
    assert rep["histogram"] and any(int(k) != 0 for k in rep["histogram"])


def test_closure_rms_has_no_silent_fallback(monkeypatch, rng):
    st = synth.make_stack(n_dates=4, shape=(8, 8), rng=rng, atmosphere_std_rad=0.2)
    keys = [p.key for p in st.pairs]
    unw = np.stack([st.igrams[k].unw_true for k in keys])
    assert mt.closure_rms(unw, keys)["n_triplets"] > 0

    def boom(*_a, **_k):
        msg = "validate.closure is broken"
        raise RuntimeError(msg)

    monkeypatch.setattr(mt, "closure_from_arrays", boom)
    with pytest.raises(RuntimeError, match=r"validate\.closure is broken"):
        mt.closure_rms(unw, keys)
