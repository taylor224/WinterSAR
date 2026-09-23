"""Unwrap masks (plan §5.4, R-06): water · low coherence · layover → fewer network nodes.

``True`` means *masked out* everywhere in this module (same convention as
``IgramStack.mask``). Masked pixels are NaN in the unwrapped output and ``0`` in the
connected-component labels (SARscape behaviour).

:func:`combine_masks` is written against the ``xp`` array module (PERF-10, ADR-0095/0097):
it runs on CuPy when the backend policy selects it and on numpy otherwise, and always
returns a numpy array. Stage code passes the executor's ``_gpu`` private param as ``gpu=``
(or wraps the call in ``wintersar.compute.xp.stage_gpu``).
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wintersar.compute.xp import GpuRequest, asarray, resolve_backend, to_numpy

__all__ = ["apply_mask", "combine_masks", "mask_stats", "masked_conncomp"]


def _as_bool(name: str, arr: NDArray[Any] | None, shape: tuple[int, ...], xp: ModuleType) -> Any:
    if arr is None:
        return xp.zeros(shape, dtype=bool)
    m = asarray(arr, xp, dtype=bool)
    if tuple(m.shape) != tuple(shape):
        msg = f"{name} mask shape {m.shape} != coherence shape {shape}"
        raise ValueError(msg)
    return m


def combine_masks(
    coh: NDArray[Any],
    threshold: float,
    water: NDArray[Any] | None = None,
    layover: NDArray[Any] | None = None,
    extra: NDArray[Any] | None = None,
    *,
    gpu: GpuRequest = None,
) -> NDArray[np.bool_]:
    """``True`` where a pixel must be excluded from the unwrapping network.

    A pixel is masked when its coherence is not finite or below ``threshold``, or when any
    of the optional ``water`` / ``layover`` / ``extra`` masks is ``True``. ``threshold <= 0``
    disables the coherence criterion (non-finite coherence is still masked).

    ``gpu`` follows the backend precedence of :mod:`wintersar.compute.xp` (``None`` = env /
    stage context / auto-detect); the result is always a numpy array.
    """
    xp = resolve_backend(gpu).xp
    c = asarray(coh, xp, dtype=np.float32)
    mask = ~xp.isfinite(c)
    if threshold > 0.0:
        mask |= c < np.float32(threshold)
    for name, m in (("water", water), ("layover", layover), ("extra", extra)):
        if m is not None:
            mask |= _as_bool(name, m, tuple(c.shape), xp)
    return np.asarray(to_numpy(mask), dtype=bool)


def apply_mask(unw: NDArray[Any], mask: NDArray[np.bool_]) -> NDArray[np.float32]:
    """Copy of ``unw`` (float32) with NaN where ``mask`` is ``True``."""
    out = np.array(unw, dtype=np.float32, copy=True)
    if mask.shape != out.shape:
        msg = f"mask shape {mask.shape} != unw shape {out.shape}"
        raise ValueError(msg)
    out[mask] = np.nan
    return out


def masked_conncomp(conncomp: NDArray[Any], mask: NDArray[np.bool_]) -> NDArray[np.integer[Any]]:
    """Copy of ``conncomp`` with label ``0`` on masked pixels."""
    out = np.array(conncomp, copy=True)
    if out.dtype.kind not in "ui":
        out = out.astype(np.uint16)
    out[mask] = 0
    return out


def mask_stats(mask: NDArray[np.bool_]) -> dict[str, Any]:
    """Node-reduction statistics: how many network nodes the mask removes."""
    n_total = int(mask.size)
    n_masked = int(np.count_nonzero(mask))
    return {
        "n_pixels": n_total,
        "n_masked": n_masked,
        "n_nodes": n_total - n_masked,
        "masked_fraction": (n_masked / n_total) if n_total else 0.0,
    }
