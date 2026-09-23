"""Pixel kernels written against the ``xp`` array module (PERF-10, plan §6.2, ADR-0053).

Every function accepts numpy *or* CuPy arrays: the backend is taken from the input array
(:func:`wintersar.compute.xp.xp_of`) or forced with ``xp=``. Only operations that exist in
both libraries are used — verified against the CuPy reference (2026-09-16):

* ``fft.fft2`` / ``fft.ifft2`` — https://docs.cupy.dev/en/stable/reference/fft.html
* ``pad`` (modes constant/reflect/edge/wrap) — https://docs.cupy.dev/en/stable/reference/generated/cupy.pad.html
* ``cumsum``, ``angle``, ``exp``, ``abs``, ``conj``, ``isfinite``, ``hanning`` —
  https://docs.cupy.dev/en/stable/reference/comparison.html

Box sums use cumulative sums (no ``scipy.ndimage`` dependency) so CPU and GPU execute the
same arithmetic; the CPU result is the reference in tests (tolerance stated per kernel).

Non-finite input (ADR-0096): NaN is absorbing in a cumulative sum, so a single NaN/±inf
sample would contaminate every pixel to its lower-right. :func:`box_sum` therefore drops
non-finite samples from the table and returns NaN exactly on the pixels whose window
contains one — the footprint a direct moving-window sum would give — and
:func:`coherence_estimate` reports those pixels as NaN, never as coherence 0.
``assume_finite=True`` skips the extra pass where the caller knows its input is finite.

Kernels
-------
* :func:`multilook` — coherent (complex) or incoherent averaging by ``(az, rg)`` looks.
* :func:`goldstein_filter` — Goldstein & Werner (1998) adaptive spectral filter
  ``H(u,v) = S{|Z(u,v)|}^alpha * Z(u,v)`` on overlapping patches, doi:10.1029/1998GL900033.
  Two documented variants share the loop (ADR-0096): the pipeline default (Hann blend,
  last patch clamped to the edge, zero-padded box smoothing of the spectrum) and the
  research/dolphin-style variant (Bartlett blend, zero-padded patch grid, periodic
  smoothing, input magnitude kept) that ``wintersar.research.repr_phase`` uses.
* :func:`coherence_estimate` — ``|sum(s1*conj(s2))| / sqrt(sum|s1|^2 * sum|s2|^2)`` over a moving window
  (the standard sample coherence magnitude estimator, e.g. Hanssen 2001 §4.3).
* :func:`box_filter` / :func:`box_sum` — moving-window sums/means shared by the above.
"""

from __future__ import annotations

import math
from types import ModuleType
from typing import Any

import numpy as np

from wintersar.compute.xp import xp_of

# ---------------------------------------------------------------- moving windows


def _as_pair(v: int | tuple[int, int]) -> tuple[int, int]:
    if isinstance(v, tuple):
        return (int(v[0]), int(v[1]))
    return (int(v), int(v))


def _window_sum(p: Any, wy: int, wx: int, ny: int, nx: int, xp: ModuleType, acc_dtype: Any) -> Any:
    """Window sums of the padded array ``p`` (see :func:`box_sum` for the padding) through
    a summed-area table accumulated in ``acc_dtype``; the result has the unpadded shape."""
    c = xp.cumsum(xp.cumsum(p, axis=-2, dtype=acc_dtype), axis=-1, dtype=acc_dtype)
    # S[i,j] = C[i+wy, j+wx] - C[i, j+wx] - C[i+wy, j] + C[i, j] with the +1 shift of the pad
    return (
        c[..., wy : wy + ny, wx : wx + nx]
        - c[..., 0:ny, wx : wx + nx]
        - c[..., wy : wy + ny, 0:nx]
        + c[..., 0:ny, 0:nx]
    )


def box_sum(
    a: Any,
    window: int | tuple[int, int],
    xp: ModuleType | None = None,
    *,
    assume_finite: bool = False,
) -> Any:
    """Sum over a centred ``window`` (rows, cols) for the last two axes, zero-padded edges.

    Implemented with 2-D cumulative sums (summed-area table); output has the input shape.

    Non-finite samples (NaN, ±inf) are left out of the table — NaN is absorbing in a
    cumulative sum and would otherwise contaminate every pixel to its lower-right — and the
    output is NaN exactly where the window contains one, as a direct moving-window sum
    gives; finite input is unaffected (bit-identical). ``assume_finite=True`` skips that
    guard (one extra cumulative-sum pass) for callers that know the input is finite; the
    result is then undefined for non-finite input. Integer and boolean input is finite by
    construction and never guarded.
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
    finite = None
    if not assume_finite and np.issubdtype(p.dtype, np.inexact):
        # source: https://github.com/cupy/cupy/blob/main/cupy/_logic/content.py
        #   (isfinite loops e/f/d/F/D -> ?, i.e. complex input is supported)
        finite = xp.isfinite(p)
        p = xp.where(finite, p, 0).astype(p.dtype, copy=False)
    # summed-area tables cancel large partial sums: accumulate in double precision and cast
    # back, otherwise float32 cumsums over multi-megapixel images lose the small-window sums
    acc_dtype = np.complex128 if np.issubdtype(p.dtype, np.complexfloating) else np.float64
    out = _window_sum(p, wy, wx, ny, nx, xp, acc_dtype)
    if finite is not None:
        # count the non-finite samples in every window: an int64 table is exact, so ``> 0``
        # is exact too — no rounding can hide or invent a hit
        touched = _window_sum(~finite, wy, wx, ny, nx, xp, np.int64) > 0
        out = xp.where(touched, np.nan, out)
    out_dtype = a.dtype if np.issubdtype(a.dtype, np.inexact) else acc_dtype
    return out.astype(out_dtype)


def box_filter(
    a: Any,
    window: int | tuple[int, int],
    xp: ModuleType | None = None,
    normalize: str = "valid",
    *,
    assume_finite: bool = False,
) -> Any:
    """Moving-window mean. ``normalize='valid'`` divides by the number of in-image pixels
    (edges unbiased); ``'full'`` divides by ``wy*wx`` (edges taper to zero). Windows that
    contain a non-finite sample are NaN (see :func:`box_sum`)."""
    xp = xp or xp_of(a)
    wy, wx = _as_pair(window)
    s = box_sum(a, (wy, wx), xp, assume_finite=assume_finite)
    if normalize == "full":
        return s / float(wy * wx)
    ones = xp.ones(a.shape[-2:], dtype=np.float32)
    n = box_sum(ones, (wy, wx), xp, assume_finite=True)  # a constant image is finite
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


def _hann2d(n: int, xp: ModuleType, dtype: Any = np.float32) -> Any:
    # Computed with numpy (tiny) and uploaded: avoids any backend difference in window formulas.
    w = np.hanning(n + 2)[1:-1]  # strictly positive taper so every pixel gets weight
    return xp.asarray(np.outer(w, w).astype(dtype))


def _bartlett2d(n: int, xp: ModuleType, dtype: Any = np.float64) -> Any:
    """Symmetric triangular blend weight (dolphin ``goldstein`` style: a linear ramp on one
    quadrant mirrored to the other three). ``n`` must be even.

    The ramp is ``1 - |k - (half - 0.5)| / half`` so the patch edge keeps a strictly positive
    weight (``0.5 / half``) and a border pixel covered by a single patch is still defined;
    dolphin's ramp reaches exactly 0 there.
    # source: https://raw.githubusercontent.com/isce-framework/dolphin/main/src/dolphin/goldstein.py
    #   (make_weight: linear ramp ``1 - |arange(n/2) - (n/2 - 1)| / (n/2 - 1)`` mirrored with
    #   np.block; psize=32, step=psize//2, out /= weight_sum)
    """
    half = n // 2
    ramp = 1.0 - np.abs(np.arange(half) - (half - 0.5)) / half
    quad = np.outer(ramp, ramp)
    top = np.concatenate([quad, quad[:, ::-1]], axis=1)
    full = np.concatenate([top, top[::-1, :]], axis=0)
    return xp.asarray(full.astype(dtype))


def _smooth_spectrum(mag: Any, size: int, mode: str, xp: ModuleType) -> Any:
    """``size x size`` box mean of a patch spectrum magnitude.

    ``mode='box'`` zero-pads the spectrum edges (divides by ``size²`` everywhere);
    ``mode='wrap'`` treats the spectrum as periodic, which is what
    ``scipy.ndimage.uniform_filter(..., mode='wrap')`` does — the window covers ``size // 2``
    bins before and ``size - 1 - size // 2`` after the centre, the same placement as
    :func:`box_sum`.
    """
    # ``assume_finite``: a non-finite sample anywhere in a patch makes every bin of its
    # spectrum non-finite (the FFT mixes all samples), so the box_sum guard would flag the
    # whole patch anyway; skipping it keeps the per-patch arithmetic of the Goldstein loop
    # exactly as before (ADR-0096 legacy equivalence, NaN input included).
    if mode == "box":
        return box_filter(mag, size, xp, normalize="full", assume_finite=True)
    ny, nx = mag.shape[-2], mag.shape[-1]
    before = size // 2
    after = size - 1 - before
    pad = [(0, 0)] * (mag.ndim - 2) + [(before, after), (before, after)]
    # source: https://docs.cupy.dev/en/stable/reference/generated/cupy.pad.html (mode='wrap')
    p = xp.pad(mag, pad, mode="wrap")
    return box_sum(p, size, xp, assume_finite=True)[
        ..., before : before + ny, before : before + nx
    ] / float(size * size)


def _padded_length(n: int, window: int, step: int) -> int:
    """Smallest ``window + k·step`` (``k >= 0``) that is ``>= n``: the zero-padded extent
    on which a regular patch grid stepped by ``step`` covers every pixel."""
    return window + step * math.ceil(max(n - window, 0) / step)


def goldstein_filter(
    igram: Any,
    alpha: float = 0.6,
    window: int = 64,
    overlap: int | None = None,
    smooth: int = 3,
    xp: ModuleType | None = None,
    *,
    weight: str = "hann",
    pad: str = "clamp",
    smooth_mode: str = "box",
    keep_magnitude: bool = False,
) -> Any:
    """Goldstein-Werner adaptive filter of a complex interferogram (2-D, last two axes).

    Patches of ``window`` x ``window`` pixels, stepped by ``window - overlap`` (default overlap
    ``window // 2``), are transformed with ``fft2``; the spectrum is multiplied by the
    ``smooth`` x ``smooth`` box-smoothed magnitude raised to ``alpha`` (``alpha=0`` → identity)
    and the filtered patches are blended with a 2-D window weight (normalised by the summed
    weights). Returns a complex array; the phase is ``xp.angle(result)``.

    Variants (ADR-0096; the defaults are the pipeline filter, the alternatives reproduce the
    research module's historical implementation bit-for-bit on CPU):

    * ``weight``: ``'hann'`` (2-D Hann, strictly positive) or ``'bartlett'`` (mirrored linear
      ramp, dolphin style; ``window`` must be even).
    * ``pad``: ``'clamp'`` — the last patch row/column is shifted back onto the image edge,
      so nothing outside the image is synthesised and arrays smaller than ``window`` are
      rejected; ``'zero'`` — the image is zero-padded to a regular grid of patches and the
      result is cropped (arrays smaller than ``window`` are padded).
    * ``smooth_mode``: ``'box'`` — zero-padded box mean of the spectrum magnitude; ``'wrap'``
      — periodic box mean (``scipy.ndimage.uniform_filter(mode='wrap')`` semantics).
    * ``keep_magnitude``: return ``|igram| · filtered / |filtered|`` (filtered phase, original
      magnitude — masked zeros stay zero) instead of the blended filtered values.

    Patches that are entirely zero (masked / nodata) contribute neither data nor weight, so a
    masked area never attenuates its neighbours. dtype: ``complex64`` in → ``complex64`` out,
    ``complex128`` in → ``complex128`` out (accumulation and blend weights in the matching real
    precision); real input is taken as wrapped phase → ``complex64``.

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
    if weight not in ("hann", "bartlett"):
        msg = f"unknown weight {weight!r} (hann | bartlett)"
        raise ValueError(msg)
    if pad not in ("clamp", "zero"):
        msg = f"unknown pad mode {pad!r} (clamp | zero)"
        raise ValueError(msg)
    if smooth_mode not in ("box", "wrap"):
        msg = f"unknown smooth_mode {smooth_mode!r} (box | wrap)"
        raise ValueError(msg)
    if weight == "bartlett" and window % 2:
        msg = f"window must be even for the bartlett weight, got {window}"
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
    if z.dtype != np.complex128:
        z = z.astype(np.complex64)
    cdtype = z.dtype
    rdtype = np.float64 if cdtype == np.complex128 else np.float32
    ny, nx = z.shape
    if pad == "clamp":
        if ny < window or nx < window:
            msg = f"array {z.shape} smaller than the filter window {window}"
            raise ValueError(msg)
        work = z
        # patch origins stepped by ``step``; the last patch is clamped to the image edge so
        # every pixel is covered without synthesising data outside the image (no padding)
        ys = _patch_origins(ny, window, step)
        xs = _patch_origins(nx, window, step)
    else:
        ny_p, nx_p = _padded_length(ny, window, step), _padded_length(nx, window, step)
        work = z
        if (ny_p, nx_p) != (ny, nx):
            work = xp.pad(z, [(0, ny_p - ny), (0, nx_p - nx)], mode="constant")
        ys = list(range(0, ny_p - window + 1, step))
        xs = list(range(0, nx_p - window + 1, step))
    acc = xp.zeros(work.shape, dtype=cdtype)
    wacc = xp.zeros(work.shape, dtype=rdtype)
    w2 = _hann2d(window, xp, rdtype) if weight == "hann" else _bartlett2d(window, xp, rdtype)
    for y0 in ys:
        for x0 in xs:
            patch = work[y0 : y0 + window, x0 : x0 + window]
            # 0-d device flag (no host sync): an all-zero patch adds neither data nor weight
            live = xp.any(patch != 0)
            spec = xp.fft.fft2(patch)
            if alpha > 0:
                mag = xp.abs(spec)
                if smooth > 1:
                    # box sums can produce tiny negatives where the true value is 0
                    mag = xp.maximum(_smooth_spectrum(mag, smooth, smooth_mode, xp), 0.0)
                spec = spec * (mag**alpha)
            filt = xp.fft.ifft2(spec)
            acc[y0 : y0 + window, x0 : x0 + window] += filt * (w2 * live)
            wacc[y0 : y0 + window, x0 : x0 + window] += w2 * live
    out = acc / xp.maximum(wacc, rdtype(1e-12))
    if pad == "zero":
        out = out[:ny, :nx]
    if keep_magnitude:
        mag_out = xp.abs(out)
        unit = xp.where(mag_out > 0, out / xp.where(mag_out > 0, mag_out, 1.0), 0.0)
        out = xp.abs(z) * unit
    return out.astype(cdtype, copy=False)


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
    window has zero power are ``0``. Pixels whose window contains a non-finite sample (NaN,
    ±inf) in *either* image are NaN: nodata propagates over the window footprint only and is
    never reported as coherence 0. The window is centred and truncated at the image edge
    (numerator and denominator use the same pixels, so the estimator stays normalised).
    """
    xp = xp or xp_of(s1)
    a = xp.asarray(s1).astype(np.complex64)
    b = xp.asarray(s2).astype(np.complex64)
    if a.shape != b.shape:
        msg = f"shape mismatch {a.shape} vs {b.shape}"
        raise ValueError(msg)
    # a non-finite sample in either image spoils every estimate whose window contains it:
    # take it out of all three sums here and mark the footprint once, instead of running
    # the box_sum guard three times (see box_sum for the summed-area-table hazard)
    finite = xp.isfinite(a) & xp.isfinite(b)
    a = xp.where(finite, a, 0).astype(np.complex64, copy=False)
    b = xp.where(finite, b, 0).astype(np.complex64, copy=False)
    cross = box_sum(a * xp.conj(b), window, xp, assume_finite=True)
    p1 = box_sum((xp.abs(a) ** 2).astype(np.float32), window, xp, assume_finite=True)
    p2 = box_sum((xp.abs(b) ** 2).astype(np.float32), window, xp, assume_finite=True)
    denom = xp.sqrt(xp.maximum(p1, 0.0) * xp.maximum(p2, 0.0))
    coh = xp.abs(cross) / xp.maximum(denom, np.float32(1e-20))
    coh = xp.where(denom > 0, coh, np.float32(0.0))
    coh = xp.minimum(coh, np.float32(1.0))
    # windows holding at least one non-finite sample (counts <= window area: exact in float32)
    touched = box_sum((~finite).astype(np.float32), window, xp, assume_finite=True) > 0
    return xp.where(touched, np.float32(np.nan), coh).astype(np.float32)


def phase_noise_std(coherence: Any, looks: int = 1, xp: ModuleType | None = None) -> Any:
    """Cramér-Rao-type phase standard deviation ``sqrt((1-g^2)/(2*L*g^2))`` clipped to the
    uniform-phase limit ``pi/sqrt(3)`` (same model as :mod:`wintersar.research.synth`).
    NaN coherence (nodata, see :func:`coherence_estimate`) stays NaN."""
    xp = xp or xp_of(coherence)
    g2 = xp.asarray(coherence).astype(np.float32) ** 2
    g2 = xp.maximum(g2, np.float32(1e-12))
    var = (1.0 - g2) / (2.0 * max(int(looks), 1) * g2)
    return xp.sqrt(xp.minimum(xp.maximum(var, np.float32(0.0)), np.float32(np.pi**2 / 3.0)))
