"""Chunked on-disk interferogram stack (PERF-08, plan §6.2, ADR-0050).

ISCE/HyP3 leave one file per interferogram, so a pixel time-series query opens hundreds of
files. :class:`IgramZarr` stores ``wrapped``, ``coherence``, ``mask`` (and optionally
``unw``/``conncomp``) as Zarr v3 arrays of shape ``(n_pairs, ny, nx)`` with one of two chunk
presets:

* ``timeseries`` — ``(all pairs, 512, 512)``: one chunk per spatial tile holds every pair, so
  ``read_pixel_series`` touches a single chunk per array.
* ``display`` — ``(1, 2048, 2048)``: one pair per chunk, for map rendering (``read_slice``).

Which preset is faster for which query is *measured* with :func:`benchmark_chunk_presets`
(numbers only go to ``bench_result.json``, rule 11.8). Compression is zstd by default.

API facts (installed zarr 3.1.6, ``.venv/lib/python3.11/site-packages/zarr``):
``zarr.open_group(store, mode=…)`` (api/synchronous.py:478), ``Group.create_array(name, *,
shape, dtype, chunks, shards, compressors, fill_value, attributes, dimension_names,
overwrite)`` (core/group.py:2613), ``Array.resize(new_shape)`` (core/array.py:4032),
``Array.chunks`` / ``Array.attrs``; ``zarr.codecs.ZstdCodec(level=…, checksum=…)``
(codecs/zstd.py:38). ``LocalStore`` is the default for a filesystem path.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
from numpy.typing import NDArray

from wintersar.io.igrams import IgramStack

ChunkPreset = Literal["timeseries", "display"]
ZarrMode = Literal["r", "r+", "a", "w", "w-"]

#: ``None`` on the pair axis means "all pairs"; spatial chunks are clamped to the array shape.
CHUNK_PRESETS: dict[str, tuple[int | None, int, int]] = {
    "timeseries": (None, 512, 512),  # plan §6.2 PERF-08 query pattern (pixel time series)
    "display": (1, 2048, 2048),  # plan §6.2 PERF-08 display pattern (one layer)
}
# source: https://github.com/facebook/zstd/blob/dev/lib/zstd.h  #define ZSTD_CLEVEL_DEFAULT 3
ZSTD_DEFAULT_LEVEL = 3
ARRAY_NAMES: tuple[str, ...] = ("wrapped", "coherence", "mask", "unw", "conncomp")
_DIMS = ("pair", "y", "x")
FORMAT_VERSION = 1


def resolve_chunks(
    preset: str | tuple[int | None, int, int], n_pairs: int, shape: tuple[int, int]
) -> tuple[int, int, int]:
    """Concrete chunk shape for ``preset`` (name or explicit triple) and an array shape."""
    spec = CHUNK_PRESETS[preset] if isinstance(preset, str) else preset
    t, y, x = spec
    return (
        max(1, n_pairs if t is None else min(int(t), max(n_pairs, 1))),
        max(1, min(int(y), shape[0])),
        max(1, min(int(x), shape[1])),
    )


def _compressors(compressor: str | None, level: int) -> list[Any] | None:
    if compressor is None or compressor == "none":
        return None
    if compressor == "zstd":
        from zarr.codecs import ZstdCodec

        return [ZstdCodec(level=level)]
    if compressor == "gzip":
        from zarr.codecs import GzipCodec

        return [GzipCodec(level=level)]
    msg = f"unknown compressor {compressor!r}; use 'zstd', 'gzip' or None"
    raise ValueError(msg)


@dataclass
class IgramZarr:
    """Handle on a Zarr group holding one interferogram stack (see module docstring)."""

    path: Path
    group: Any  # zarr.Group
    n_pairs_capacity: int
    shape: tuple[int, int]
    chunks: tuple[int, int, int]
    preset: str
    _pairs: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ construction
    @classmethod
    def create(
        cls,
        path: Path | str,
        shape: tuple[int, int],
        n_pairs: int,
        chunk_preset: str | tuple[int | None, int, int] = "timeseries",
        *,
        compressor: str | None = "zstd",
        level: int = ZSTD_DEFAULT_LEVEL,
        with_unw: bool = False,
        with_conncomp: bool = False,
        dates: list[date] | None = None,
        attrs: Mapping[str, Any] | None = None,
        overwrite: bool = False,
    ) -> IgramZarr:
        import zarr

        p = Path(path)
        if p.exists() and not overwrite:
            msg = f"{p} exists (pass overwrite=True)"
            raise FileExistsError(msg)
        chunks = resolve_chunks(chunk_preset, n_pairs, shape)
        preset_name = chunk_preset if isinstance(chunk_preset, str) else "custom"
        group = zarr.open_group(str(p), mode="w", zarr_format=3)
        comps = _compressors(compressor, level)
        full = (n_pairs, shape[0], shape[1])

        def mk(name: str, dtype: str, fill: Any) -> None:
            group.create_array(
                name,
                shape=full,
                dtype=dtype,
                chunks=chunks,
                compressors=comps,
                fill_value=fill,
                dimension_names=_DIMS,
                overwrite=True,
            )

        mk("wrapped", "float32", np.nan)
        mk("coherence", "float32", np.nan)
        mk("mask", "uint8", 0)
        if with_unw:
            mk("unw", "float32", np.nan)
        if with_conncomp:
            mk("conncomp", "uint8", 0)
        group.attrs.update(
            {
                "format": "wintersar.igram_zarr",
                "format_version": FORMAT_VERSION,
                "chunk_preset": preset_name,
                "chunks": list(chunks),
                "compressor": compressor or "none",
                "compressor_level": level,
                "pairs": [],
                "dates": [d.isoformat() for d in (dates or [])],
                "n_written": 0,
                "extra": _jsonable(dict(attrs or {})),
            }
        )
        return cls(
            path=p,
            group=group,
            n_pairs_capacity=n_pairs,
            shape=(int(shape[0]), int(shape[1])),
            chunks=chunks,
            preset=preset_name,
        )

    @classmethod
    def open(cls, path: Path | str, mode: ZarrMode = "r") -> IgramZarr:
        import zarr

        p = Path(path)
        group = zarr.open_group(str(p), mode=mode)
        a = group.attrs.asdict()
        if a.get("format") != "wintersar.igram_zarr":
            msg = f"{p} is not a wintersar interferogram Zarr store"
            raise ValueError(msg)
        arr = cast(Any, group["wrapped"])
        chunks = tuple(int(c) for c in a.get("chunks", arr.chunks))
        out = cls(
            path=p,
            group=group,
            n_pairs_capacity=int(arr.shape[0]),
            shape=(int(arr.shape[1]), int(arr.shape[2])),
            chunks=(chunks[0], chunks[1], chunks[2]),
            preset=str(a.get("chunk_preset", "custom")),
        )
        out._pairs = [str(k) for k in cast(list[Any], a.get("pairs") or [])]
        return out

    # ------------------------------------------------------------------ properties
    @property
    def pairs(self) -> list[str]:
        return list(self._pairs)

    @property
    def n_written(self) -> int:
        return len(self._pairs)

    @property
    def dates(self) -> list[date]:
        return [date.fromisoformat(d) for d in self.group.attrs.get("dates", [])]

    @property
    def has_unw(self) -> bool:
        return "unw" in self.group

    @property
    def has_conncomp(self) -> bool:
        return "conncomp" in self.group

    @property
    def arrays(self) -> list[str]:
        return [n for n in ARRAY_NAMES if n in self.group]

    def _arr(self, name: str) -> Any:
        return self.group[name]

    def index_of(self, key: str) -> int:
        try:
            return self._pairs.index(key)
        except ValueError as e:
            msg = f"pair {key!r} not in store ({self.n_written} pairs)"
            raise KeyError(msg) from e

    # ------------------------------------------------------------------ writing
    def _grow(self, new_n: int) -> None:
        for name in self.arrays:
            arr = self._arr(name)
            arr.resize((new_n, self.shape[0], self.shape[1]))
        self.n_pairs_capacity = new_n

    def append_pair(
        self,
        key: str,
        wrapped: NDArray[Any],
        coh: NDArray[Any],
        mask: NDArray[Any] | None = None,
        unw: NDArray[Any] | None = None,
        conncomp: NDArray[Any] | None = None,
    ) -> int:
        """Write one interferogram at index ``n_written`` (the store grows when full)."""
        if key in self._pairs:
            msg = f"pair {key!r} already in store"
            raise ValueError(msg)
        w = np.asarray(wrapped, dtype=np.float32)
        if w.shape != self.shape:
            msg = f"wrapped shape {w.shape} != store shape {self.shape}"
            raise ValueError(msg)
        i = self.n_written
        if i >= self.n_pairs_capacity:
            self._grow(i + 1)
        self._arr("wrapped")[i] = w
        self._arr("coherence")[i] = np.asarray(coh, dtype=np.float32)
        if mask is not None:
            self._arr("mask")[i] = np.asarray(mask, dtype=bool).astype(np.uint8)
        if unw is not None:
            if not self.has_unw:
                self._add_array("unw", "float32", np.nan)
            self._arr("unw")[i] = np.asarray(unw, dtype=np.float32)
        if conncomp is not None:
            if not self.has_conncomp:
                self._add_array("conncomp", "uint8", 0)
            self._arr("conncomp")[i] = np.asarray(conncomp, dtype=np.uint8)
        self._pairs.append(key)
        self.group.attrs["pairs"] = list(self._pairs)
        self.group.attrs["n_written"] = len(self._pairs)
        return i

    def _add_array(self, name: str, dtype: str, fill: Any) -> None:
        a = self.group.attrs.asdict()
        comps = _compressors(
            None if a.get("compressor", "zstd") == "none" else str(a.get("compressor", "zstd")),
            int(a.get("compressor_level", ZSTD_DEFAULT_LEVEL)),
        )
        self.group.create_array(
            name,
            shape=(self.n_pairs_capacity, self.shape[0], self.shape[1]),
            dtype=dtype,
            chunks=self.chunks,
            compressors=comps,
            fill_value=fill,
            dimension_names=_DIMS,
        )

    def set_dates(self, dates: list[date]) -> None:
        self.group.attrs["dates"] = [d.isoformat() for d in dates]

    # ------------------------------------------------------------------ reading
    def read_pixel_series(self, row: int, col: int) -> dict[str, NDArray[Any]]:
        """All pairs at one pixel — the PERF-08 query pattern. Keys: ``wrapped``,
        ``coherence``, ``mask`` (bool) and, when stored, ``unw``/``conncomp``."""
        n = self.n_written
        out: dict[str, NDArray[Any]] = {}
        for name in self.arrays:
            v = np.asarray(self._arr(name)[:n, row, col])
            out[name] = v.astype(bool) if name == "mask" else v
        return out

    def read_slice(self, i: int) -> dict[str, NDArray[Any]]:
        """One interferogram (all arrays) — the display pattern."""
        if not 0 <= i < self.n_written:
            msg = f"pair index {i} out of range (0..{self.n_written - 1})"
            raise IndexError(msg)
        out: dict[str, NDArray[Any]] = {}
        for name in self.arrays:
            v = np.asarray(self._arr(name)[i])
            out[name] = v.astype(bool) if name == "mask" else v
        return out

    def read_window(self, rows: slice, cols: slice, name: str = "wrapped") -> NDArray[Any]:
        """``(n_written, rows, cols)`` block of one array."""
        return np.asarray(self._arr(name)[: self.n_written, rows, cols])

    def iter_pairs(self) -> Iterator[tuple[str, dict[str, NDArray[Any]]]]:
        for i, k in enumerate(self._pairs):
            yield k, self.read_slice(i)

    # ------------------------------------------------------------------ conversion
    def to_igram_stack(self) -> IgramStack:
        n = self.n_written
        mask = np.asarray(self._arr("mask")[:n]).astype(bool)
        unw = np.asarray(self._arr("unw")[:n]) if self.has_unw else None
        cc = np.asarray(self._arr("conncomp")[:n]) if self.has_conncomp else None
        extra = dict(self.group.attrs.get("extra", {}))
        return IgramStack(
            wrapped=np.asarray(self._arr("wrapped")[:n]),
            coherence=np.asarray(self._arr("coherence")[:n]),
            pairs=self.pairs,
            dates=self.dates or _dates_from_pairs(self.pairs),
            mask=mask,
            unw=unw,
            conncomp=cc,
            attrs={**extra, "source": str(self.path), "chunk_preset": self.preset},
        )

    @classmethod
    def from_igram_stack(
        cls,
        stack: IgramStack,
        path: Path | str,
        chunk_preset: str | tuple[int | None, int, int] = "timeseries",
        *,
        compressor: str | None = "zstd",
        level: int = ZSTD_DEFAULT_LEVEL,
        overwrite: bool = False,
    ) -> IgramZarr:
        store = cls.create(
            path,
            stack.shape,
            stack.n_pairs,
            chunk_preset,
            compressor=compressor,
            level=level,
            with_unw=stack.unw is not None,
            with_conncomp=stack.conncomp is not None,
            dates=stack.dates,
            attrs=stack.attrs,
            overwrite=overwrite,
        )
        for i, key in enumerate(stack.pairs):
            store.append_pair(
                key,
                stack.wrapped[i],
                stack.coherence[i],
                mask=stack.mask_for(i),
                unw=None if stack.unw is None else stack.unw[i],
                conncomp=None if stack.conncomp is None else stack.conncomp[i],
            )
        return store

    # ------------------------------------------------------------------ info
    def info(self) -> dict[str, Any]:
        wrapped = self._arr("wrapped")
        return {
            "path": str(self.path),
            "preset": self.preset,
            "chunks": list(self.chunks),
            "shape": [self.n_pairs_capacity, *self.shape],
            "n_written": self.n_written,
            "arrays": self.arrays,
            "nchunks_per_array": int(wrapped.nchunks),
            "nbytes_stored": int(self.nbytes_stored()),
        }

    def nbytes_stored(self) -> int:
        """Bytes on disk (sum of file sizes under the store path)."""
        return sum(f.stat().st_size for f in self.path.rglob("*") if f.is_file())


def _dates_from_pairs(pairs: list[str]) -> list[date]:
    ds: set[date] = set()
    for k in pairs:
        for s in k.split("_"):
            ds.add(date(int(s[:4]), int(s[4:6]), int(s[6:8])))
    return sorted(ds)


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, np.ndarray):
            continue
        try:
            json.dumps(v)
        except (TypeError, ValueError):
            out[k] = str(v)
        else:
            out[k] = v
    return out


# ============================================================================ benchmark helper
@dataclass
class ReadTiming:
    preset: str
    chunks: tuple[int, int, int]
    pixel_series_s: float  # median seconds per read_pixel_series
    slice_s: float  # median seconds per read_slice
    n_queries: int
    nbytes_stored: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "chunks": list(self.chunks),
            "pixel_series_s": self.pixel_series_s,
            "slice_s": self.slice_s,
            "n_queries": self.n_queries,
            "nbytes_stored": self.nbytes_stored,
        }


def time_reads(store: IgramZarr, n_queries: int = 20, seed: int = 0) -> ReadTiming:
    """Median wall time of ``n_queries`` random pixel-series reads and slice reads."""
    rng = np.random.default_rng(seed)
    ny, nx = store.shape
    n = max(store.n_written, 1)
    rows = rng.integers(0, ny, n_queries)
    cols = rng.integers(0, nx, n_queries)
    idx = rng.integers(0, n, n_queries)
    t_pix: list[float] = []
    for r, c in zip(rows, cols, strict=True):
        t0 = time.perf_counter()
        store.read_pixel_series(int(r), int(c))
        t_pix.append(time.perf_counter() - t0)
    t_slice: list[float] = []
    for i in idx:
        t0 = time.perf_counter()
        store.read_slice(int(i))
        t_slice.append(time.perf_counter() - t0)
    return ReadTiming(
        preset=store.preset,
        chunks=store.chunks,
        pixel_series_s=float(np.median(t_pix)),
        slice_s=float(np.median(t_slice)),
        n_queries=n_queries,
        nbytes_stored=store.nbytes_stored(),
    )


def benchmark_chunk_presets(
    stack: IgramStack,
    out_dir: Path | str,
    presets: tuple[str, ...] = ("timeseries", "display"),
    n_queries: int = 20,
    compressor: str | None = "zstd",
) -> dict[str, dict[str, Any]]:
    """Write ``stack`` once per preset under ``out_dir`` and time both read patterns.

    Returns ``{preset: ReadTiming.to_dict()}`` plus ``write_s`` per preset. Numbers only;
    conclusions belong in ``bench_result.json`` (rule 11.8).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict[str, Any]] = {}
    for preset in presets:
        t0 = time.perf_counter()
        IgramZarr.from_igram_stack(
            stack, out / f"{preset}.zarr", preset, compressor=compressor, overwrite=True
        )
        write_s = time.perf_counter() - t0
        reopened = IgramZarr.open(out / f"{preset}.zarr")
        timing = time_reads(reopened, n_queries=n_queries)
        result[preset] = {**timing.to_dict(), "write_s": write_s}
    return result
