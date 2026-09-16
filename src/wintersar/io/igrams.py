"""Shared in-memory interferogram-stack container + the ``.npz`` interchange format.

The fake engine, the unwrap scheduler, the research module and the Zarr store all speak
this container. ``.npz`` is the simple interchange used in tests; :mod:`wintersar.io.zarr_store`
provides the chunked on-disk stack for real data (PERF-08).

Conventions:
* ``wrapped``: float32 phase in (-π, π], shape (n_pairs, ny, nx)
* ``coherence``: float32 in [0, 1]
* ``mask``: bool, ``True`` = masked out (water / layover / invalid)
* ``pairs``: list of ``"YYYYMMDD_YYYYMMDD"`` keys, ``dates``: ISO strings of all epochs
* optional truth arrays for synthetic data: ``unw_true``, ``velocity_true``,
  ``displacement_true``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wintersar.io.schemas import Pair


@dataclass
class IgramStack:
    wrapped: NDArray[np.floating]  # (n, ny, nx)
    coherence: NDArray[np.floating]  # (n, ny, nx)
    pairs: list[str]  # "YYYYMMDD_YYYYMMDD"
    dates: list[date]
    mask: NDArray[np.bool_] | None = None  # (n, ny, nx) or (ny, nx)
    unw: NDArray[np.floating] | None = None  # (n, ny, nx) unwrapped (NaN = masked)
    conncomp: NDArray[np.integer] | None = None
    truth: dict[str, NDArray[Any]] = field(default_factory=dict)
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def n_pairs(self) -> int:
        return int(self.wrapped.shape[0])

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.wrapped.shape[1]), int(self.wrapped.shape[2]))

    def mask_for(self, i: int) -> NDArray[np.bool_]:
        if self.mask is None:
            return np.zeros(self.shape, dtype=bool)
        return self.mask[i] if self.mask.ndim == 3 else self.mask

    def pair_objects(self) -> list[Pair]:
        out: list[Pair] = []
        for k in self.pairs:
            a, b = k.split("_")
            da, db = (
                date.fromisoformat(f"{a[:4]}-{a[4:6]}-{a[6:]}"),
                date.fromisoformat(f"{b[:4]}-{b[4:6]}-{b[6:]}"),
            )
            out.append(Pair(reference=da, secondary=db, temporal_baseline_days=(db - da).days))
        return out

    def complex(self, i: int) -> NDArray[np.complexfloating]:
        return np.asarray(self.coherence[i] * np.exp(1j * self.wrapped[i]), dtype=np.complex64)


_TRUTH_KEYS = ("unw_true", "velocity_true", "displacement_true")


def load_igram_stack(path: Path) -> IgramStack:
    """Load the ``.npz`` interchange format (as written by the fake engine / save_igram_stack)."""
    with np.load(path, allow_pickle=False) as z:
        wrapped = z["wrapped"]
        coherence = z["coherence"]
        mask = z["mask"] if "mask" in z.files else None
        unw = z["unw"] if "unw" in z.files else None
        conncomp = z["conncomp"] if "conncomp" in z.files else None
        pairs = [str(p) for p in z["pairs"]]
        dates = [date.fromisoformat(str(d)) for d in z["dates"]]
        truth = {k: z[k] for k in _TRUTH_KEYS if k in z.files}
    return IgramStack(
        wrapped=wrapped,
        coherence=coherence,
        pairs=pairs,
        dates=dates,
        mask=mask,
        unw=unw,
        conncomp=conncomp,
        truth=truth,
        attrs={"source": str(path)},
    )


def save_igram_stack(stack: IgramStack, path: Path) -> Path:
    arrays: dict[str, NDArray[Any]] = {
        "wrapped": np.asarray(stack.wrapped, dtype=np.float32),
        "coherence": np.asarray(stack.coherence, dtype=np.float32),
        "pairs": np.array(stack.pairs),
        "dates": np.array([d.isoformat() for d in stack.dates]),
    }
    if stack.mask is not None:
        arrays["mask"] = np.asarray(stack.mask, dtype=bool)
    if stack.unw is not None:
        arrays["unw"] = np.asarray(stack.unw, dtype=np.float32)
    if stack.conncomp is not None:
        cc = np.asarray(stack.conncomp)
        # SNAPHU/tophu labels are uint32; keep integer dtypes, only coerce bool/float to uint8
        arrays["conncomp"] = cc if cc.dtype.kind in "iu" else cc.astype(np.uint8)
    for k, v in stack.truth.items():
        arrays[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **arrays)  # type: ignore[arg-type]
    return path
