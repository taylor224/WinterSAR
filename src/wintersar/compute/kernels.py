"""Pixel kernels written against the ``xp`` array module (PERF-10, plan §6.2, ADR-0053).

Every function accepts numpy *or* CuPy arrays: the backend is taken from the input array
(:func:`wintersar.compute.xp.xp_of`) or forced with ``xp=``. Only operations that exist in
both libraries are used — verified against the CuPy reference (2026-09-16):

* ``fft.fft2`` / ``fft.ifft2`` — https://docs.cupy.dev/en/stable/reference/fft.html
* ``pad`` (modes constant/reflect/edge) — https://docs.cupy.dev/en/stable/reference/generated/cupy.pad.html
* ``cumsum``, ``angle``, ``exp``, ``abs``, ``conj``, ``isfinite``, ``hanning`` —
  https://docs.cupy.dev/en/stable/reference/comparison.html

Box sums use cumulative sums (no ``scipy.ndimage`` dependency) so CPU and GPU execute the
same arithmetic; the CPU result is the reference in tests (tolerance stated per kernel).

Kernels
-------
* :func:`multilook` — coherent (complex) or incoherent averaging by ``(az, rg)`` looks.
* :func:`goldstein_filter` — Goldstein & Werner (1998) adaptive spectral filter
  ``H(u,v) = S{|Z(u,v)|}^alpha * Z(u,v)`` on overlapping patches, doi:10.1029/1998GL900033.
* :func:`coherence_estimate` — ``|sum(s1*conj(s2))| / sqrt(sum|s1|^2 * sum|s2|^2)`` over a moving window
  (the standard sample coherence magnitude estimator, e.g. Hanssen 2001 §4.3).
* :func:`box_filter` / :func:`box_sum` — moving-window sums/means shared by the above.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import numpy as np

from wintersar.compute.xp import xp_of

# ---------------------------------------------------------------- moving windows


def _as_pair(v: int | tuple[int, int]) -> tuple[int, int]:
    if isinstance(v, tuple):
        return (int(v[0]), int(v[1]))
    return (int(v), int(v))


def box_sum(a: Any, window: int | tuple[int, int], xp: ModuleType | None = None) -> Any:
    """Sum over a centred ``window`` (rows, cols) for the last two axes, zero-padded edges.

    Implemented with 2-D cumulative sums (summed-area table); output has the input shape.
    """
    xp = xp or xp_of(a)
    wy, wx = _as_pair(window)
    if wy < 1 or wx < 1:
        msg = f"window must be >= 1, got {(wy, wx)}"
        raise ValueError(msg)
    ny, nx = a.shape[-2], a.shape[-1]
    ty, tx = wy // 2, wx // 2  # pixels before the centre
    by, bx = wy - 1 - ty, wx - 1 - tx  # pixels after the centre
    pad = [(0, 0)] * (a.ndim - 2) + [(ty + 1, by), (tx + 1, bx)]
    p = xp.pad(a, pad, mode="constant")
    # summed-area tables cancel large partial sums: accumulate in double precision and cast
    # back, otherwise float32 cumsums over multi-megapixel images lose the small-window sums
    acc_dtype = np.complex128 if np.issubdtype(p.dtype, np.complexfloating) else np.float64
    c = xp.cumsum(xp.cumsum(p, axis=-2, dtype=acc_dtype), axis=-1, dtype=acc_dtype)
    # S[i,j] = C[i+wy, j+wx] - C[i, j+wx] - C[i+wy, j] + C[i, j] with the +1 shift above
    out = (
        c[..., wy : wy + ny, wx : wx + nx]
        - c[..., 0:ny, wx : wx + nx]
        - c[..., wy : wy + ny, 0:nx]
        + c[..., 0:ny, 0:nx]
    )
    out_dtype = a.dtype if np.issubdtype(a.dtype, np.inexact) else acc_dtype
    return out.astype(out_dtype)


def box_filter(
    a: Any, window: int | tuple[int, int], xp: ModuleType | None = None, normalize: str = "valid"
) -> Any:
    """Moving-window mean. ``normalize='valid'`` divides by the number of in-image pixels
    (edges unbiased); ``'full'`` divides by ``wy*wx`` (edges taper to zero)."""
    xp = xp or xp_of(a)
    wy, wx = _as_pair(window)
    s = box_sum(a, (wy, wx), xp)
    if normalize == "full":
        return s / float(wy * wx)
    ones = xp.ones(a.shape[-2:], dtype=np.float32)
    n = box_sum(ones, (wy, wx), xp)
    return s / n


# ---------------------------------------------------------------- multilook


def multilook(
    a: Any,
    rg: int,
    az: int,
    xp: ModuleType | None = None,
    method: str = "mean",
) -> Any:
    """Average ``az`` rows x ``rg`` columns (last two axes); trailing partial blocks are cropped.

    ``method='mean'`` averages the values as given (coherent averaging for complex
    interferograms, plain mean for real fields). ``method='power'`` averages ``|a|²`` and
    returns the real intensity (incoherent multilook of SLC amplitude).
    """
    xp = xp or xp_of(a)
    if rg < 1 or az < 1:
        msg = f"looks must be >= 1, got rg={rg}, az={az}"
        raise ValueError(msg)
    ny, nx = a.shape[-2], a.shape[-1]
    ny2, nx2 = (ny // az) * az, (nx // rg) * rg
    if ny2 == 0 or nx2 == 0:
        msg = f"array {a.shape[-2:]} smaller than one look window ({az}, {rg})"
        raise ValueError(msg)
    x = a[..., :ny2, :nx2]
    if method == "power":
        x = xp.abs(x) ** 2
    elif method != "mean":
        msg = f"unknown multilook method {method!r} (mean | power)"
        raise ValueError(msg)
    lead = x.shape[:-2]
    x = x.reshape(*lead, ny2 // az, az, nx2 // rg, rg)
    return x.mean(axis=(-3, -1))


# ---------------------------------------------------------------- Goldstein filter


def _hann2d(n: int, xp: ModuleType) -> Any:
    # Computed with numpy (tiny) and uploaded: avoids any backend difference in window formulas.
    w = np.hanning(n + 2)[1:-1]  # strictly positive taper so every pixel gets weight
    return xp.asarray(np.outer(w, w).astype(np.float32))


def goldstein_filter(
    igram: Any,
    alpha: float = 0.6,
    window: int = 64,
    overlap: int | None = None,
    smooth: int = 3,
    xp: ModuleType | None = None,
) -> Any:
    """Goldstein-Werner adaptive filter of a complex interferogram (2-D, last two axes).

    Patches of ``window`` x ``window`` pixels, stepped by ``window - overlap`` (default overlap
    ``window // 2``), are transformed with ``fft2``; the spectrum is multiplied by the
    ``smooth`` x ``smooth`` box-smoothed magnitude raised to ``alpha`` (``alpha=0`` → identity)
    and the filtered patches are blended with a 2-D Hann weight (normalised by the summed
    weights). The last patch row/column is clamped to the image edge, so no data outside the
    image is synthesised and the output has the input shape. Returns a complex array; the
    phase is ``xp.angle(result)``.

    Reference: Goldstein & Werner (1998), Radar interferogram filtering for geophysical
    applications, GRL 25(21), doi:10.1029/1998GL900033 — ``H = S{|Z|}^alpha * Z``. Patch size,
    overlap and the spectral smoothing kernel are implementation choices (ADR-0053).
    """
    xp = xp or xp_of(igram)
    if alpha < 0:
        msg = f"alpha must be >= 0, got {alpha}"
        raise ValueError(msg)
    if window < 4:
        msg = f"window must be >= 4, got {window}"
        raise ValueError(msg)
    ov = window // 2 if overlap is None else int(overlap)
    if not 0 <= ov < window:
        msg = f"overlap must be in [0, window), got {ov}"
        raise ValueError(msg)
    step = window - ov
    z = xp.asarray(igram)
    if z.ndim != 2:
        msg = f"goldstein_filter expects a 2-D array, got {z.shape}"
        raise ValueError(msg)
    if not np.issubdtype(z.dtype, np.complexfloating):
        z = xp.exp(1j * z.astype(np.float32))
    z = z.astype(np.complex64)
    ny, nx = z.shape
    if ny < window or nx < window:
        msg = f"array {z.shape} smaller than the filter window {window}"
        raise ValueError(msg)
    # patch origins stepped by ``step``; the last patch is clamped to the image edge so every
    # pixel is covered without synthesising data outside the image (no padding)
    ys = _patch_origins(ny, window, step)
    xs = _patch_origins(nx, window, step)
    acc = xp.zeros((ny, nx), dtype=np.complex64)
    wacc = xp.zeros((ny, nx), dtype=np.float32)
    w2 = _hann2d(window, xp)
    for y0 in ys:
        for x0 in xs:
            patch = z[y0 : y0 + window, x0 : x0 + window]
            spec = xp.fft.fft2(patch)
            if alpha > 0:
                mag = xp.abs(spec)
                if smooth > 1:
                    # box sums can produce tiny negatives where the true value is 0
                    mag = xp.maximum(box_filter(mag, smooth, xp, normalize="full"), 0.0)
                spec = spec * (mag**alpha)
            filt = xp.fft.ifft2(spec)
            acc[y0 : y0 + window, x0 : x0 + window] += filt * w2
            wacc[y0 : y0 + window, x0 : x0 + window] += w2
    return acc / xp.maximum(wacc, np.float32(1e-12))


def _patch_origins(n: int, window: int, step: int) -> list[int]:
    """``0, step, 2·step, …`` plus a final origin ``n - window`` when the grid leaves a gap."""
    origins = list(range(0, n - window + 1, step))
    if not origins or origins[-1] + window < n:
        origins.append(n - window)
    return origins


# ---------------------------------------------------------------- coherence


def coherence_estimate(
    s1: Any, s2: Any, window: int | tuple[int, int] = 5, xp: ModuleType | None = None
) -> Any:
    """Sample coherence magnitude of two co-registered complex images over a moving window.

    ``|g| = |sum(s1*conj(s2))| / sqrt(sum|s1|^2 * sum|s2|^2)``, clipped to ``[0, 1]``; pixels whose
    window has zero power are ``0``. The window is centred and truncated at the image edge
    (numerator and denominator use the same pixels, so the estimator stays normalised).
    """
    xp = xp or xp_of(s1)
    a = xp.asarray(s1).astype(np.complex64)
    b = xp.asarray(s2).astype(np.complex64)
    if a.shape != b.shape:
        msg = f"shape mismatch {a.shape} vs {b.shape}"
        raise ValueError(msg)
    cross = box_sum(a * xp.conj(b), window, xp)
    p1 = box_sum((xp.abs(a) ** 2).astype(np.float32), window, xp)
    p2 = box_sum((xp.abs(b) ** 2).astype(np.float32), window, xp)
    denom = xp.sqrt(xp.maximum(p1, 0.0) * xp.maximum(p2, 0.0))
    coh = xp.abs(cross) / xp.maximum(denom, np.float32(1e-20))
    coh = xp.where(denom > 0, coh, np.float32(0.0))
    return xp.minimum(coh, np.float32(1.0)).astype(np.float32)


def phase_noise_std(coherence: Any, looks: int = 1, xp: ModuleType | None = None) -> Any:
    """Cramér-Rao-type phase standard deviation ``sqrt((1-g^2)/(2*L*g^2))`` clipped to the
    uniform-phase limit ``pi/sqrt(3)`` (same model as :mod:`wintersar.research.synth`)."""
    xp = xp or xp_of(coherence)
    g2 = xp.asarray(coherence).astype(np.float32) ** 2
    g2 = xp.maximum(g2, np.float32(1e-12))
    var = (1.0 - g2) / (2.0 * max(int(looks), 1) * g2)
    return xp.sqrt(xp.minimum(xp.maximum(var, np.float32(0.0)), np.float32(np.pi**2 / 3.0)))
