"""io.zarr_store: Zarr v3 stack, chunk presets, round trip, PERF-08 read patterns (ADR-0050)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import zarr

from tests.unit.io.conftest import make_stack
from wintersar.io.igrams import IgramStack
from wintersar.io.zarr_store import (
    CHUNK_PRESETS,
    ZSTD_DEFAULT_LEVEL,
    IgramZarr,
    benchmark_chunk_presets,
    resolve_chunks,
    time_reads,
)


def test_presets_and_resolve_chunks() -> None:
    assert CHUNK_PRESETS["timeseries"] == (None, 512, 512)
    assert CHUNK_PRESETS["display"] == (1, 2048, 2048)
    assert resolve_chunks("timeseries", 10, (3000, 4000)) == (10, 512, 512)
    assert resolve_chunks("display", 10, (3000, 4000)) == (1, 2048, 2048)
    # clamped to the array shape for small stacks
    assert resolve_chunks("timeseries", 10, (40, 48)) == (10, 40, 48)
    assert resolve_chunks((2, 16, 16), 10, (40, 48)) == (2, 16, 16)
    assert ZSTD_DEFAULT_LEVEL == 3  # zstd.h ZSTD_CLEVEL_DEFAULT


def test_create_append_read_pixel_series_and_slice(tmp_path: Path, small_stack: IgramStack) -> None:
    z = IgramZarr.create(tmp_path / "s.zarr", small_stack.shape, small_stack.n_pairs, "timeseries")
    assert z.chunks == (small_stack.n_pairs, 40, 48) and z.n_written == 0
    for i, k in enumerate(small_stack.pairs):
        idx = z.append_pair(
            k, small_stack.wrapped[i], small_stack.coherence[i], small_stack.mask_for(i)
        )
        assert idx == i
    assert z.pairs == small_stack.pairs and z.n_written == small_stack.n_pairs
    ps = z.read_pixel_series(7, 9)
    assert set(ps) == {"wrapped", "coherence", "mask"}
    np.testing.assert_allclose(ps["wrapped"], small_stack.wrapped[:, 7, 9])
    np.testing.assert_allclose(ps["coherence"], small_stack.coherence[:, 7, 9])
    assert ps["mask"].dtype == bool
    np.testing.assert_array_equal(ps["mask"], small_stack.mask[:, 7, 9])
    sl = z.read_slice(2)
    np.testing.assert_allclose(sl["wrapped"], small_stack.wrapped[2])
    with pytest.raises(IndexError):
        z.read_slice(small_stack.n_pairs)
    with pytest.raises(ValueError, match="already"):
        z.append_pair(small_stack.pairs[0], small_stack.wrapped[0], small_stack.coherence[0])
    with pytest.raises(ValueError, match="shape"):
        z.append_pair("20990101_20990113", np.zeros((3, 3)), np.zeros((3, 3)))
    with pytest.raises(FileExistsError):
        IgramZarr.create(tmp_path / "s.zarr", small_stack.shape, 1)


def test_zarr_v3_layout_and_zstd_codec(tmp_path: Path, small_stack: IgramStack) -> None:
    IgramZarr.from_igram_stack(small_stack, tmp_path / "s.zarr", "display")
    meta = json.loads((tmp_path / "s.zarr" / "zarr.json").read_text())
    assert meta["zarr_format"] == 3 and meta["node_type"] == "group"
    assert meta["attributes"]["format"] == "wintersar.igram_zarr"
    arr = zarr.open_array(str(tmp_path / "s.zarr" / "wrapped"), mode="r")
    assert arr.chunks == (1, 40, 48) and arr.shape == (small_stack.n_pairs, 40, 48)
    names = [c.to_dict()["name"] if hasattr(c, "to_dict") else str(c) for c in arr.compressors]
    assert names == ["zstd"]
    assert arr.metadata.dimension_names == ("pair", "y", "x")  # type: ignore[union-attr]


def test_roundtrip_igram_stack_all_arrays(tmp_path: Path, small_stack: IgramStack) -> None:
    IgramZarr.from_igram_stack(small_stack, tmp_path / "rt.zarr", "timeseries")
    z = IgramZarr.open(tmp_path / "rt.zarr")
    assert z.preset == "timeseries" and z.has_unw and z.has_conncomp
    assert z.dates == small_stack.dates
    back = z.to_igram_stack()
    np.testing.assert_allclose(back.wrapped, small_stack.wrapped)
    np.testing.assert_allclose(back.coherence, small_stack.coherence)
    np.testing.assert_array_equal(back.mask, small_stack.mask)
    assert back.unw is not None and back.conncomp is not None
    np.testing.assert_allclose(back.unw, small_stack.unw)
    np.testing.assert_array_equal(back.conncomp, small_stack.conncomp)
    assert back.pairs == small_stack.pairs and back.dates == small_stack.dates
    assert back.attrs["note"] == "synthetic" and back.attrs["chunk_preset"] == "timeseries"
    win = z.read_window(slice(0, 5), slice(3, 9), "coherence")
    assert win.shape == (small_stack.n_pairs, 5, 6)
    assert [k for k, _ in z.iter_pairs()] == small_stack.pairs
    info = z.info()
    assert info["n_written"] == small_stack.n_pairs and info["nbytes_stored"] > 0


def test_mask_2d_is_broadcast_and_unw_added_later(tmp_path: Path, small_stack: IgramStack) -> None:
    st = IgramStack(
        wrapped=small_stack.wrapped,
        coherence=small_stack.coherence,
        pairs=small_stack.pairs,
        dates=small_stack.dates,
        mask=small_stack.mask[0],
    )
    z = IgramZarr.from_igram_stack(st, tmp_path / "m.zarr")
    assert not z.has_unw
    w = IgramZarr.open(tmp_path / "m.zarr", mode="r+")
    w.append_pair("20990101_20990113", st.wrapped[0], st.coherence[0], unw=small_stack.unw[0])
    assert w.has_unw and w.n_written == st.n_pairs + 1
    back = IgramZarr.open(tmp_path / "m.zarr").to_igram_stack()
    assert back.unw is not None and np.isnan(back.unw[0]).all()
    np.testing.assert_allclose(back.unw[-1], small_stack.unw[0])
    np.testing.assert_array_equal(back.mask[3], small_stack.mask[0])


def test_growth_beyond_capacity(tmp_path: Path, small_stack: IgramStack) -> None:
    z = IgramZarr.create(tmp_path / "g.zarr", small_stack.shape, 2, "timeseries")
    for i, k in enumerate(small_stack.pairs[:4]):
        z.append_pair(k, small_stack.wrapped[i], small_stack.coherence[i])
    assert z.n_pairs_capacity == 4 and z.n_written == 4
    r = IgramZarr.open(tmp_path / "g.zarr")
    np.testing.assert_allclose(r.read_pixel_series(1, 1)["wrapped"], small_stack.wrapped[:4, 1, 1])


def test_open_rejects_foreign_group(tmp_path: Path) -> None:
    g = zarr.open_group(str(tmp_path / "x.zarr"), mode="w")
    g.create_array("a", shape=(2, 2), dtype="float32")
    with pytest.raises(ValueError, match="not a wintersar"):
        IgramZarr.open(tmp_path / "x.zarr")


def test_no_compressor_and_gzip(tmp_path: Path, small_stack: IgramStack) -> None:
    a = IgramZarr.from_igram_stack(small_stack, tmp_path / "none.zarr", compressor=None)
    b = IgramZarr.from_igram_stack(small_stack, tmp_path / "gz.zarr", compressor="gzip", level=1)
    assert a.nbytes_stored() > b.nbytes_stored()
    with pytest.raises(ValueError, match="compressor"):
        IgramZarr.from_igram_stack(small_stack, tmp_path / "bad.zarr", compressor="lz77")


def test_benchmark_helper_returns_numbers_only(tmp_path: Path) -> None:
    stack = make_stack(n_dates=4, shape=(24, 24))
    res = benchmark_chunk_presets(stack, tmp_path / "bench", n_queries=3)
    assert set(res) == {"timeseries", "display"}
    for preset, r in res.items():
        assert r["preset"] == preset and r["n_queries"] == 3
        for k in ("pixel_series_s", "slice_s", "write_s"):
            assert isinstance(r[k], float) and r[k] >= 0.0
        assert r["nbytes_stored"] > 0 and len(r["chunks"]) == 3
    assert res["timeseries"]["chunks"][0] == stack.n_pairs and res["display"]["chunks"][0] == 1
    t = time_reads(IgramZarr.open(tmp_path / "bench" / "display.zarr"), n_queries=2)
    assert t.preset == "display" and t.n_queries == 2
