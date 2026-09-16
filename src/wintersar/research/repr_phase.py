"""Low-resolution "representative phase" estimators for multiresolution unwrapping
(plan §5.7, §12.1, R-07; ADR-0060/0061/0062).

Multiresolution unwrappers (tophu, SARscape decomposition levels) unwrap a low-resolution
version of the interferogram first and then add/subtract 2π cycles per tile so that the
high-resolution tiles agree with it. The quality of that low-resolution *representative*
phase decides the result. tophu forms it with a plain complex multilook; this module makes
the estimator pluggable so the alternatives can be compared on synthetic ground truth.

Common contract — every method returns a ``complex128`` array of shape
``(ny // factor, nx // factor)`` (input cropped from the end to a multiple of ``factor``,
exactly like tophu's ``multilook``). The angle is the representative phase; the magnitude is
a quality measure in ``[0, 1]`` where the method defines one (phasor coherence, temporal
coherence) and the plain multilook magnitude for ``ml``/``filtered``.

Methods (``METHODS`` registry, :func:`representative_phase`):

* ``ml`` — complex multilook: arithmetic mean of the complex values over ``factor x factor``
  blocks. Identical to tophu's low-resolution interferogram with ``do_lowpass_filter=False``.
  # source: https://raw.githubusercontent.com/isce-framework/tophu/v0.2.1/src/tophu/_multilook.py
  #   (crop to a multiple of nlooks, then ``da.coarsen(np.mean, arr, nlooks)``)
  # source: https://raw.githubusercontent.com/isce-framework/tophu/v0.2.1/src/tophu/_multiscale.py
  #   coarse_unwrap: ``igram_lores = multilook(igram, downsample_factor)`` when the low-pass
  #   filter is off (default on: ``lowpass_filter_and_multilook``), ``coherence_lores =
  #   multilook(coherence, downsample_factor)``
* ``coh_weighted`` — mean of the unit phasors ``exp(j·phase)`` weighted by ``coherence ** p``.
* ``shp`` — adaptive multilook over statistically homogeneous pixels (SHP) selected with a
  two-sample Kolmogorov-Smirnov test on the amplitude time series (SqueeSAR, Ferretti et al.
  2011, IEEE TGRS 49(9):3460-3470, doi:10.1109/TGRS.2011.2124465; test comparison in Parizzi &
  Brcic 2011, IEEE GRSL 8(3):441-445, doi:10.1109/LGRS.2010.2083631). Window ≤ 15 x 15.
* ``phase_link`` — mini-stack phase linking of an SLC stack (EVD: leading eigenvector of the
  coherence-weighted sample covariance; EMI: Ansari, De Zan & Bamler 2018, IEEE TGRS
  56(7):4109-4125, doi:10.1109/TGRS.2018.2826045) with the optional sequential estimator of
  Ansari, De Zan & Bamler 2017 (IEEE TGRS 55(10):5637-5652, doi:10.1109/TGRS.2017.2711037);
  the pairwise low-resolution reference phase is ``phi_j - phi_i`` for pair ``(i, j)``.
* ``filtered`` — Goldstein & Werner 1998 (GRL 25(21):4035-4038, doi:10.1029/1998GL900033)
  spectral filter on overlapping patches, then multilook.

Everything is numpy (research code; dolphin/tophu are not installed, rule 11.2). Masked
pixels should be zeroed by the caller (the tophu/dolphin convention ``zero_where_masked``);
zeros drop out of every complex average.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from wintersar.i18n import t

FloatArray = NDArray[np.float64]
ComplexArray = NDArray[np.complex128]
BoolArray = NDArray[np.bool_]
LinkMethod = Literal["evd", "emi"]
ShpTest = Literal["ks", "t"]

TWO_PI = 2.0 * np.pi
#: Largest SHP search window (odd side length) — plan/task limit, keeps the vectorised test
#: at <= 225 neighbour comparisons per block.
MAX_SHP_WINDOW = 15
DEFAULT_SHP_ALPHA = 0.05
#: dolphin regularises |C| as ``(1 - β)·|C| + β·I`` before the EMI inversion.
# source: https://raw.githubusercontent.com/isce-framework/dolphin/main/src/dolphin/phase_link/_core.py
DEFAULT_EMI_BETA = 0.01

__all__ = [
    "DEFAULT_EMI_BETA",
    "DEFAULT_SHP_ALPHA",
    "MAX_SHP_WINDOW",
    "METHODS",
    "STACK_METHODS",
    "ResearchError",
    "crop_to_multiple",
    "default_shp_window",
    "goldstein_filter",
    "ks_critical_value",
    "ks_statistic",
    "link_phases",
    "lowres_shape",
    "multilook",
    "phase_link_stack",
    "repr_coh_weighted",
    "repr_filtered",
    "repr_ml",
    "repr_phase_link",
    "repr_shp",
    "representative_phase",
    "sample_coherence_matrix",
    "shp_adaptive_multilook",
    "shp_family_size",
    "shp_neighbors",
    "t_test_accept",
    "temporal_coherence",
    "truth_lowres_phase",
    "upsample_nearest",
    "wrap",
]


class ResearchError(ValueError):
    """Invalid input; ``rule_id`` (``RES-00x``) selects the i18n cause → fix text."""

    def __init__(self, rule_id: str, **params: Any) -> None:
        self.rule_id = rule_id
        self.params = params
        super().__init__(t(f"research.{rule_id}.cause", **params))

    @property
    def fix(self) -> str:
        return t(f"research.{self.rule_id}.fix", **self.params)


# ---------------------------------------------------------------- small array helpers
def wrap(phase: NDArray[Any]) -> FloatArray:
    return np.asarray(np.angle(np.exp(1j * np.asarray(phase, dtype=np.float64))), dtype=np.float64)


def lowres_shape(shape: tuple[int, int], factor: int) -> tuple[int, int]:
    return (int(shape[0]) // factor, int(shape[1]) // factor)


def crop_to_multiple(arr: NDArray[Any], factor: int) -> NDArray[Any]:
    """Drop trailing rows/columns so the last two axes are multiples of ``factor``
    (tophu ``multilook`` crops the remainder from the end)."""
    ny, nx = arr.shape[-2], arr.shape[-1]
    return arr[..., : ny - ny % factor, : nx - nx % factor]


def multilook(arr: NDArray[Any], factor: int) -> NDArray[Any]:
    """Block mean over the last two axes (``factor x factor`` non-overlapping boxes).

    Works for real and complex arrays; ``factor == 1`` returns a copy. NaNs propagate (zero
    masked pixels first, tophu/dolphin convention).
    """
    if factor < 1:
        raise ResearchError("RES-006", name="factor", value=factor, allowed=">= 1")
    a = crop_to_multiple(np.asarray(arr), factor)
    if factor == 1:
        return np.array(a, copy=True)
    ny, nx = a.shape[-2], a.shape[-1]
    lead = a.shape[:-2]
    blocks = a.reshape(*lead, ny // factor, factor, nx // factor, factor)
    return np.asarray(blocks.mean(axis=(-3, -1)))


def upsample_nearest(arr: NDArray[Any], shape: tuple[int, int]) -> NDArray[Any]:
    """Nearest-neighbour upsampling of a low-resolution raster to ``shape`` (tophu
    ``upsample_nearest`` semantics: each high-res pixel takes the low-res pixel it falls in;
    rows beyond an exact multiple map to the last low-res row)."""
    a = np.asarray(arr)
    ny_lo, nx_lo = a.shape[-2], a.shape[-1]
    ny, nx = int(shape[0]), int(shape[1])
    iy = np.minimum((np.arange(ny) * ny_lo) // max(ny, 1), ny_lo - 1)
    ix = np.minimum((np.arange(nx) * nx_lo) // max(nx, 1), nx_lo - 1)
    # exact multiples must reduce to ``y // factor``
    fy, fx = ny // ny_lo, nx // nx_lo
    if fy * ny_lo <= ny and fy > 0:
        iy = np.minimum(np.arange(ny) // fy, ny_lo - 1)
    if fx * nx_lo <= nx and fx > 0:
        ix = np.minimum(np.arange(nx) // fx, nx_lo - 1)
    return np.asarray(a[..., iy[:, None], ix[None, :]])


def truth_lowres_phase(unw_true: NDArray[Any], factor: int) -> FloatArray:
    """Reference for the tests/metrics: block mean of the *unwrapped* truth (radians,
    unwrapped). Compare estimates with ``wrap(angle(est) - truth)``."""
    return np.asarray(multilook(np.asarray(unw_true, dtype=np.float64), factor), dtype=np.float64)


def _as_complex(igram: NDArray[Any] | None, factor: int, method: str) -> ComplexArray:
    if igram is None:
        raise ResearchError("RES-007", method=method)
    z = np.asarray(igram)
    if z.ndim != 2:
        raise ResearchError("RES-006", name="igram.ndim", value=z.ndim, allowed="2")
    if not np.iscomplexobj(z):
        # a real array is taken as wrapped phase (unit magnitude)
        z = np.exp(1j * z.astype(np.float64))
    z = np.asarray(z, dtype=np.complex128)
    z = np.where(np.isfinite(z), z, 0.0)
    if z.shape[0] < factor or z.shape[1] < factor:
        raise ResearchError(
            "RES-006", name="factor", value=factor, allowed=f"<= min{tuple(z.shape)}"
        )
    return np.asarray(z, dtype=np.complex128)


def _as_coh(coh: NDArray[Any] | None, shape: tuple[int, ...]) -> FloatArray:
    if coh is None:
        return np.ones(shape, dtype=np.float64)
    c = np.asarray(coh, dtype=np.float64)
    if c.shape != tuple(shape):
        raise ResearchError("RES-006", name="coh.shape", value=c.shape, allowed=str(tuple(shape)))
    return np.asarray(np.clip(np.nan_to_num(c, nan=0.0), 0.0, 1.0), dtype=np.float64)


# ---------------------------------------------------------------- ml / coh_weighted
def repr_ml(
    igram: NDArray[Any] | None, coh: NDArray[Any] | None, factor: int, **_: Any
) -> ComplexArray:
    """Complex multilook (tophu baseline): block mean of the complex interferogram."""
    z = _as_complex(igram, factor, "ml")
    return np.asarray(multilook(z, factor), dtype=np.complex128)


def repr_coh_weighted(
    igram: NDArray[Any] | None,
    coh: NDArray[Any] | None,
    factor: int,
    p: float = 1.0,
    **_: Any,
) -> ComplexArray:
    """Coherence-weighted mean of unit phasors: ``Σ gamma^p e^{jφ} / Σ gamma^p`` per block.

    ``p = 0`` is the unweighted phasor mean (amplitude ignored); larger ``p`` trusts
    high-coherence pixels more. The magnitude is the weighted phasor coherence in ``[0, 1]``.
    """
    if p < 0:
        raise ResearchError("RES-006", name="p", value=p, allowed=">= 0")
    z = _as_complex(igram, factor, "coh_weighted")
    c = _as_coh(coh, z.shape)
    mag = np.abs(z)
    unit = np.where(mag > 0, z / np.where(mag > 0, mag, 1.0), 0.0)
    w = np.where(mag > 0, c**p, 0.0)
    num = multilook(w * unit, factor)
    den = multilook(w, factor)
    out = np.where(den > 0, num / np.where(den > 0, den, 1.0), 0.0)
    return np.asarray(out, dtype=np.complex128)


# ---------------------------------------------------------------- SHP (KS / t test)
def ks_statistic(a: NDArray[Any], b: NDArray[Any]) -> FloatArray:
    """Two-sample Kolmogorov-Smirnov statistic ``D = sup_x |F_a(x) - F_b(x)|`` along axis 0,
    vectorised over the remaining axes (pooled stable sort + cumulative ECDF steps).

    Ties are broken with ``a`` first, which can only over-estimate ``D`` at tied values;
    SAR amplitudes are continuous, so ties are negligible.
    """
    aa = np.asarray(a, dtype=np.float64)
    bb = np.asarray(b, dtype=np.float64)
    n, m = aa.shape[0], bb.shape[0]
    values = np.concatenate([aa, bb], axis=0)
    steps = np.concatenate([np.full(aa.shape, 1.0 / n), np.full(bb.shape, -1.0 / m)], axis=0)
    order = np.argsort(values, axis=0, kind="stable")
    ecdf_diff = np.cumsum(np.take_along_axis(steps, order, axis=0), axis=0)
    return np.asarray(np.max(np.abs(ecdf_diff), axis=0), dtype=np.float64)


def ks_critical_value(alpha: float, n: int, m: int) -> float:
    """Asymptotic two-sided critical value ``D_crit = K_alpha · sqrt((n + m) / (n m))``.

    ``K_alpha`` is the ``1 - alpha`` quantile of the Kolmogorov limiting distribution, the
    same quantity scipy's ``ks_2samp(method="asymp")`` uses (``en = m n / (m + n)``,
    ``prob = kstwobign.sf(sqrt(en) · d)``).
    # source: .venv/lib/python3.11/site-packages/scipy/stats/_stats_py.py (ks_2samp, asymptotic branch)
    """
    from scipy.stats import kstwobign

    if not 0.0 < alpha < 1.0:
        raise ResearchError("RES-006", name="alpha", value=alpha, allowed="(0, 1)")
    k_alpha = float(kstwobign.isf(alpha))
    return k_alpha * float(np.sqrt((n + m) / (n * m)))


def t_test_accept(a: NDArray[Any], b: NDArray[Any], alpha: float) -> BoolArray:
    """Welch two-sample t-test on the means along axis 0 — ``True`` where the hypothesis of
    equal means is *not* rejected at level ``alpha`` (the pixels are taken as homogeneous).

    Equivalent to ``scipy.stats.ttest_ind(a, b, equal_var=False).pvalue > alpha`` but
    vectorised with the Welch-Satterthwaite degrees of freedom per pixel.
    # source: .venv/lib/python3.11/site-packages/scipy/stats/_stats_py.py (ttest_ind, equal_var=False)
    """
    from scipy.stats import t as student_t

    aa = np.asarray(a, dtype=np.float64)
    bb = np.asarray(b, dtype=np.float64)
    n, m = aa.shape[0], bb.shape[0]
    if n < 2 or m < 2:
        raise ResearchError("RES-006", name="n_dates", value=min(n, m), allowed=">= 2")
    va = aa.var(axis=0, ddof=1) / n
    vb = bb.var(axis=0, ddof=1) / m
    se2 = va + vb
    with np.errstate(divide="ignore", invalid="ignore"):
        tstat = np.abs(aa.mean(axis=0) - bb.mean(axis=0)) / np.sqrt(se2)
        df = se2**2 / (va**2 / (n - 1) + vb**2 / (m - 1))
    df = np.where(np.isfinite(df) & (df > 0), df, 1.0)
    crit = np.asarray(student_t.isf(alpha / 2.0, df), dtype=np.float64)
    return np.asarray(np.where(np.isfinite(tstat), tstat <= crit, True), dtype=bool)


def default_shp_window(factor: int) -> int:
    """Smallest odd window that covers one ``factor x factor`` block (``factor`` rounded up
    to odd, at least 3), capped at :data:`MAX_SHP_WINDOW` — so the default support of ``shp``
    is comparable to the plain multilook and a wider window is an explicit choice."""
    w = max(factor, 3)
    w = w if w % 2 == 1 else w + 1
    return min(w, MAX_SHP_WINDOW)


def _amplitude_stack(stack: NDArray[Any] | None, factor: int, method: str) -> FloatArray:
    if stack is None:
        raise ResearchError("RES-003", method=method)
    s = np.asarray(stack)
    if s.ndim != 3:
        raise ResearchError("RES-006", name="stack.ndim", value=s.ndim, allowed="3")
    amp = np.abs(s).astype(np.float64) if np.iscomplexobj(s) else s.astype(np.float64)
    amp = np.where(np.isfinite(amp), amp, 0.0)
    return np.asarray(crop_to_multiple(amp, factor), dtype=np.float64)


def _block_centres(ny: int, nx: int, factor: int) -> tuple[NDArray[np.intp], NDArray[np.intp]]:
    cy = np.arange(ny // factor) * factor + factor // 2
    cx = np.arange(nx // factor) * factor + factor // 2
    return cy.astype(np.intp), cx.astype(np.intp)


def shp_neighbors(
    amp_stack: NDArray[Any],
    factor: int,
    window: int | None = None,
    alpha: float = DEFAULT_SHP_ALPHA,
    test: ShpTest = "ks",
) -> BoolArray:
    """Statistically homogeneous pixels of every block centre.

    Returns a boolean array ``(NY, NX, window, window)``: entry ``[i, j, dy, dx]`` is ``True``
    when the pixel at offset ``(dy - h, dx - h)`` from the centre of low-res block ``(i, j)``
    passes the homogeneity test against the centre's amplitude series (the centre itself is
    always ``True``; out-of-image offsets are ``False``). Amplitudes are compared with the KS
    test (``test="ks"``, SqueeSAR) or the Welch t-test (``test="t"``, Parizzi & Brcic 2011).
    Pixels are *selected* when the test does **not** reject equality at level ``alpha``.
    # source (dolphin excludes the centre from its neighbour mask; we keep it so the family
    # is never empty): https://raw.githubusercontent.com/isce-framework/dolphin/main/src/dolphin/shp/_ks.py
    """
    amp = _amplitude_stack(amp_stack, factor, "shp")
    n, ny, nx = amp.shape
    w = default_shp_window(factor) if window is None else int(window)
    if w < 3 or w > MAX_SHP_WINDOW or w % 2 == 0:
        raise ResearchError("RES-006", name="window", value=w, allowed=f"odd, 3..{MAX_SHP_WINDOW}")
    if test not in ("ks", "t"):
        raise ResearchError("RES-006", name="test", value=test, allowed="ks | t")
    if n < 2:
        raise ResearchError("RES-006", name="n_dates", value=n, allowed=">= 2")
    h = w // 2
    cy, cx = _block_centres(ny, nx, factor)
    centre = amp[:, cy[:, None], cx[None, :]]  # (n, NY, NX)
    out = np.zeros((cy.size, cx.size, w, w), dtype=bool)
    crit = ks_critical_value(alpha, n, n) if test == "ks" else None
    for iy, dy in enumerate(range(-h, h + 1)):
        yy = cy + dy
        vy = (yy >= 0) & (yy < ny)
        yy = np.clip(yy, 0, ny - 1)
        for ix, dx in enumerate(range(-h, h + 1)):
            if dy == 0 and dx == 0:
                out[:, :, iy, ix] = True
                continue
            xx = cx + dx
            vx = (xx >= 0) & (xx < nx)
            xx = np.clip(xx, 0, nx - 1)
            nb = amp[:, yy[:, None], xx[None, :]]
            if crit is not None:
                sel = ks_statistic(centre, nb) <= crit
            else:
                sel = t_test_accept(centre, nb, alpha)
            out[:, :, iy, ix] = sel & vy[:, None] & vx[None, :]
    return out


def shp_family_size(neighbors: NDArray[np.bool_]) -> NDArray[np.int64]:
    """Number of SHPs (including the centre) per low-res block, ``(NY, NX)``."""
    return np.asarray(np.asarray(neighbors, dtype=bool).sum(axis=(2, 3)), dtype=np.int64)


def _shp_average(zc: ComplexArray, nb: NDArray[np.bool_], factor: int) -> ComplexArray:
    """Coherent mean of ``zc`` over each block centre's SHP family (``nb`` from
    :func:`shp_neighbors` with the same ``factor``)."""
    ny, nx = zc.shape
    cy, cx = _block_centres(ny, nx, factor)
    w = nb.shape[-1]
    h = w // 2
    acc = np.zeros((cy.size, cx.size), dtype=np.complex128)
    cnt = np.zeros((cy.size, cx.size), dtype=np.float64)
    for iy, dy in enumerate(range(-h, h + 1)):
        yy = np.clip(cy + dy, 0, ny - 1)
        for ix, dx in enumerate(range(-h, h + 1)):
            xx = np.clip(cx + dx, 0, nx - 1)
            sel = nb[:, :, iy, ix]
            acc += np.where(sel, zc[yy[:, None], xx[None, :]], 0.0)
            cnt += sel
    out = acc / np.maximum(cnt, 1.0)
    return np.asarray(out, dtype=np.complex128)


def shp_adaptive_multilook(
    igram: NDArray[Any],
    amp_stack: NDArray[Any],
    window: int = 7,
    alpha: float = DEFAULT_SHP_ALPHA,
    test: ShpTest = "ks",
) -> ComplexArray:
    """Full-resolution adaptive multilook: every pixel becomes the coherent mean over its own
    SHP family (window ≤ 15 x 15). The DS filtering step of SqueeSAR at the pixel level."""
    z = _as_complex(igram, 1, "shp")
    amp = _amplitude_stack(amp_stack, 1, "shp")
    if amp.shape[1:] != z.shape:
        raise ResearchError(
            "RES-006", name="stack.shape[1:]", value=amp.shape[1:], allowed=str(z.shape)
        )
    return _shp_average(z, shp_neighbors(amp, 1, window, alpha, test), 1)


def repr_shp(
    igram: NDArray[Any] | None,
    coh: NDArray[Any] | None,
    factor: int,
    stack: NDArray[Any] | None = None,
    window: int | None = None,
    alpha: float = DEFAULT_SHP_ALPHA,
    test: ShpTest = "ks",
    mode: Literal["centre", "pixelwise"] = "centre",
    **_: Any,
) -> ComplexArray:
    """SHP-based adaptive multilook (window ≤ 15 x 15, default :func:`default_shp_window`).

    * ``mode="centre"`` (fast): one SHP family per low-res block, that of the block centre
      pixel; the block value is the coherent mean over that family.
    * ``mode="pixelwise"`` (SqueeSAR-like, ``factor²`` x more tests): every full-resolution
      pixel is replaced by the mean over its own family, then the result is multilooked by
      ``factor`` like ``ml``.

    ``stack`` is the amplitude (or complex SLC) stack ``(n_dates, ny, nx)`` used for the
    homogeneity test; the interferogram itself is only averaged.
    """
    z = _as_complex(igram, factor, "shp")
    amp = _amplitude_stack(stack, factor, "shp")
    zc = np.asarray(crop_to_multiple(z, factor), dtype=np.complex128)
    if amp.shape[1:] != zc.shape:
        raise ResearchError(
            "RES-006", name="stack.shape[1:]", value=amp.shape[1:], allowed=str(zc.shape)
        )
    w = default_shp_window(factor) if window is None else int(window)
    if mode == "pixelwise":
        return np.asarray(
            multilook(shp_adaptive_multilook(zc, amp, w, alpha, test), factor), dtype=np.complex128
        )
    if mode != "centre":
        raise ResearchError("RES-006", name="mode", value=mode, allowed="centre | pixelwise")
    return _shp_average(zc, shp_neighbors(amp, factor, w, alpha, test), factor)


# ---------------------------------------------------------------- phase linking
def sample_coherence_matrix(slc: NDArray[Any], factor: int) -> ComplexArray:
    """Per-block sample coherence matrix ``(NY, NX, n, n)``: ``C_ij = <s_i s_j^*> / sqrt(<|s_i|²><|s_j|²>)``
    over the ``factor x factor`` block (``factor²`` looks). Phase of ``C_ij`` is ``φ_i - φ_j``."""
    s = np.asarray(slc)
    if s.ndim != 3:
        raise ResearchError("RES-006", name="stack.ndim", value=s.ndim, allowed="3")
    s = np.asarray(crop_to_multiple(np.where(np.isfinite(s), s, 0.0), factor), dtype=np.complex128)
    n, ny, nx = s.shape
    if ny < factor or nx < factor:
        raise ResearchError("RES-006", name="factor", value=factor, allowed=f"<= min{(ny, nx)}")
    blocks = (
        s.reshape(n, ny // factor, factor, nx // factor, factor)
        .transpose(1, 3, 0, 2, 4)
        .reshape(ny // factor, nx // factor, n, factor * factor)
    )
    cov = blocks @ np.conj(blocks).transpose(0, 1, 3, 2) / float(factor * factor)
    power = np.sqrt(np.maximum(np.real(np.diagonal(cov, axis1=-2, axis2=-1)), 0.0))
    norm = power[..., :, None] * power[..., None, :]
    coh = np.where(norm > 0, cov / np.where(norm > 0, norm, 1.0), 0.0)
    return np.asarray(coh, dtype=np.complex128)


def temporal_coherence(coh_matrix: NDArray[Any], phases: NDArray[Any]) -> FloatArray:
    """dolphin-style temporal coherence ``|Σ_{i<j} exp(j(arg C_ij - (φ_i - φ_j)))| / (n(n-1)/2)``.
    # source: https://raw.githubusercontent.com/isce-framework/dolphin/main/src/dolphin/phase_link/metrics.py
    """
    c = np.asarray(coh_matrix, dtype=np.complex128)
    ph = np.asarray(phases, dtype=np.float64)
    n = c.shape[-1]
    if n < 2:
        return np.ones(c.shape[:-2], dtype=np.float64)
    iu, ju = np.triu_indices(n, 1)
    diff = np.angle(c[..., iu, ju]) - (ph[..., iu] - ph[..., ju])
    return np.asarray(np.abs(np.mean(np.exp(1j * diff), axis=-1)), dtype=np.float64)


def link_phases(
    coh_matrix: NDArray[Any],
    method: LinkMethod = "evd",
    beta: float = DEFAULT_EMI_BETA,
    reference: int = 0,
) -> tuple[FloatArray, FloatArray]:
    """Phase linking of coherence matrices ``(..., n, n)`` → ``(phases (..., n), temporal coherence (...))``.

    * ``evd``: eigenvector of the **largest** eigenvalue of ``C ∘ |C|`` (coherence-weighted
      sample covariance).
    * ``emi``: eigenvector of the **smallest** eigenvalue of ``Γ⁻¹ ∘ C`` with
      ``Γ = (1 - β)|C| + β I`` (Ansari et al. 2018; regularisation as in dolphin).
    # source: https://raw.githubusercontent.com/isce-framework/dolphin/main/src/dolphin/phase_link/_core.py
    #   evd: eigh_largest_stack(C_arrays * abs(C_arrays)); emi: eigh_smallest_stack(Gamma_inv * C_arrays)
    #   Gamma = (1 - beta) * Gamma + beta * Id; estimate *= exp(-1j * angle(ref))
    The phase of the ``reference`` date is set to zero.
    """
    c = np.asarray(coh_matrix, dtype=np.complex128)
    n = c.shape[-1]
    if not 0 <= reference < n:
        raise ResearchError("RES-006", name="reference", value=reference, allowed=f"0..{n - 1}")
    if method == "evd":
        m = c * np.abs(c)
        _, vecs = np.linalg.eigh(m)
        vec = vecs[..., :, -1]
    elif method == "emi":
        if not 0.0 <= beta < 1.0:
            raise ResearchError("RES-006", name="beta", value=beta, allowed="[0, 1)")
        gamma = (1.0 - beta) * np.abs(c) + beta * np.eye(n)
        gamma_inv = np.linalg.inv(gamma)
        m = gamma_inv * c
        _, vecs = np.linalg.eigh(m)
        vec = vecs[..., :, 0]
    else:
        raise ResearchError("RES-006", name="method", value=method, allowed="evd | emi")
    ph = np.angle(vec)
    ph = wrap(ph - ph[..., reference : reference + 1])
    return ph, temporal_coherence(c, ph)


def phase_link_stack(
    slc: NDArray[Any],
    factor: int,
    method: LinkMethod = "evd",
    beta: float = DEFAULT_EMI_BETA,
    ministack_size: int | None = None,
    reference: int = 0,
) -> tuple[FloatArray, FloatArray]:
    """Linked phases ``(n_dates, NY, NX)`` (reference date = 0) and temporal coherence
    ``(NY, NX)`` of an SLC stack ``(n_dates, ny, nx)``, estimated per ``factor x factor`` block.

    ``ministack_size`` switches to the sequential estimator (Ansari, De Zan & Bamler 2017):
    each mini-stack is linked on its own (reference = its first date), compressed to one
    "compressed SLC" ``c_k = v_kᴴ s_k / sqrt(m)`` per pixel, the compressed SLCs are linked
    again to get the inter-mini-stack phases, and the final phase of date ``i`` in
    mini-stack ``k`` is ``φ_i^(k) + θ_k``.
    """
    s = np.asarray(slc)
    if s.ndim != 3:
        raise ResearchError("RES-006", name="stack.ndim", value=s.ndim, allowed="3")
    s = np.asarray(crop_to_multiple(np.where(np.isfinite(s), s, 0.0), factor), dtype=np.complex128)
    n, ny, nx = s.shape
    if ministack_size is None or ministack_size >= n:
        ph, tc = link_phases(sample_coherence_matrix(s, factor), method, beta, reference)
        return np.asarray(np.moveaxis(ph, -1, 0), dtype=np.float64), tc
    m = ministack_size
    if m < 2:
        raise ResearchError("RES-006", name="ministack_size", value=m, allowed=">= 2")
    chunks = [list(range(k, min(k + m, n))) for k in range(0, n, m)]
    phases = np.zeros((n, ny // factor, nx // factor), dtype=np.float64)
    compressed: list[ComplexArray] = []
    for idx in chunks:
        sk = s[idx]
        ph_k, _ = link_phases(sample_coherence_matrix(sk, factor), method, beta, 0)
        phases[idx] = np.moveaxis(ph_k, -1, 0)
        v_full = np.exp(1j * np.repeat(np.repeat(phases[idx], factor, axis=1), factor, axis=2))
        compressed.append(
            np.asarray((np.conj(v_full) * sk).sum(axis=0) / np.sqrt(len(idx)), dtype=np.complex128)
        )
    if len(chunks) > 1:
        theta, _ = link_phases(
            sample_coherence_matrix(np.stack(compressed), factor), method, beta, 0
        )
        for k, idx in enumerate(chunks):
            phases[idx] += theta[..., k][None, :, :]
    phases = wrap(phases - phases[reference][None, :, :])
    tc = temporal_coherence(sample_coherence_matrix(s, factor), np.moveaxis(phases, 0, -1))
    return np.asarray(phases, dtype=np.float64), tc


def repr_phase_link(
    igram: NDArray[Any] | None,
    coh: NDArray[Any] | None,
    factor: int,
    stack: NDArray[Any] | None = None,
    pair: tuple[int, int] | None = None,
    link_method: LinkMethod = "evd",
    beta: float = DEFAULT_EMI_BETA,
    ministack_size: int | None = None,
    **_: Any,
) -> ComplexArray:
    """Pairwise low-resolution reference phase from phase linking: ``tc · exp(j(φ_j - φ_i))``
    for ``pair = (i, j)`` (secondary minus reference, the ``synth.make_stack`` convention);
    the magnitude is the temporal coherence. ``igram``/``coh`` are not used;
    ``link_method`` is ``evd`` or ``emi`` (named so it never clashes with the dispatcher's
    ``method`` argument)."""
    if stack is None:
        raise ResearchError("RES-003", method="phase_link")
    if pair is None:
        raise ResearchError("RES-007", method="phase_link")
    i, j = int(pair[0]), int(pair[1])
    phases, tc = phase_link_stack(stack, factor, link_method, beta, ministack_size)
    n = phases.shape[0]
    if not (0 <= i < n and 0 <= j < n):
        raise ResearchError("RES-006", name="pair", value=(i, j), allowed=f"0..{n - 1}")
    return np.asarray(tc * np.exp(1j * (phases[j] - phases[i])), dtype=np.complex128)


# ---------------------------------------------------------------- Goldstein filter
def _bartlett2d(size: int) -> FloatArray:
    """Symmetric triangular weight (dolphin ``goldstein``: mirrored quadrant of a linear
    ramp), used to overlap-add the filtered patches."""
    half = size // 2
    # strictly positive at the patch edge (0.5/half) so border pixels covered by a single
    # patch still get a weight; dolphin's ramp reaches exactly 0 there
    ramp = 1.0 - np.abs(np.arange(half) - (half - 0.5)) / half
    quad = np.outer(ramp, ramp)
    top = np.concatenate([quad, quad[:, ::-1]], axis=1)
    return np.asarray(np.concatenate([top, top[::-1, :]], axis=0), dtype=np.float64)


def goldstein_filter(
    igram: NDArray[Any],
    alpha: float = 0.5,
    window: int = 32,
    overlap: float = 0.5,
    spectrum_smooth: int = 1,
) -> ComplexArray:
    """Goldstein & Werner 1998 adaptive spectral filter.

    Each ``window x window`` patch (step ``window·(1 - overlap)``) is multiplied in the
    frequency domain by ``|S|^alpha`` (``S`` = its 2-D spectrum, optionally smoothed with a
    ``spectrum_smooth x spectrum_smooth`` box, 1 = none), transformed back and overlap-added
    with a triangular weight. The output keeps the input magnitude and takes the filtered
    phase, so it can be multilooked like the original.
    # source: https://raw.githubusercontent.com/isce-framework/dolphin/main/src/dolphin/goldstein.py
    #   (psize=32, step=psize//2, weight=|fft2(patch)|^alpha, overlap-add / weight_sum)
    """
    z = np.asarray(igram)
    if not np.iscomplexobj(z):
        z = np.exp(1j * z.astype(np.float64))
    z = np.asarray(np.where(np.isfinite(z), z, 0.0), dtype=np.complex128)
    if z.ndim != 2:
        raise ResearchError("RES-006", name="igram.ndim", value=z.ndim, allowed="2")
    if alpha < 0:
        raise ResearchError("RES-006", name="alpha", value=alpha, allowed=">= 0")
    if window < 4 or window % 2:
        raise ResearchError("RES-006", name="window", value=window, allowed="even, >= 4")
    if not 0.0 <= overlap < 1.0:
        raise ResearchError("RES-006", name="overlap", value=overlap, allowed="[0, 1)")
    ny, nx = z.shape
    step = max(1, round(window * (1.0 - overlap)))
    ny_p = window + step * int(np.ceil(max(ny - window, 0) / step))
    nx_p = window + step * int(np.ceil(max(nx - window, 0) / step))
    padded = np.zeros((ny_p, nx_p), dtype=np.complex128)
    padded[:ny, :nx] = z
    weight = _bartlett2d(window)
    acc = np.zeros_like(padded)
    wsum = np.zeros((ny_p, nx_p), dtype=np.float64)
    if spectrum_smooth > 1:
        from scipy.ndimage import uniform_filter
    for y0 in range(0, ny_p - window + 1, step):
        for x0 in range(0, nx_p - window + 1, step):
            patch = padded[y0 : y0 + window, x0 : x0 + window]
            if not patch.any():
                continue
            spec = np.fft.fft2(patch)
            power = np.abs(spec)
            if spectrum_smooth > 1:
                power = uniform_filter(power, size=spectrum_smooth, mode="wrap")
            filtered = np.fft.ifft2(spec * power**alpha)
            acc[y0 : y0 + window, x0 : x0 + window] += filtered * weight
            wsum[y0 : y0 + window, x0 : x0 + window] += weight
    out = np.where(wsum > 0, acc / np.where(wsum > 0, wsum, 1.0), 0.0)[:ny, :nx]
    mag = np.abs(z)
    unit = np.where(np.abs(out) > 0, out / np.where(np.abs(out) > 0, np.abs(out), 1.0), 0.0)
    return np.asarray(mag * unit, dtype=np.complex128)


def repr_filtered(
    igram: NDArray[Any] | None,
    coh: NDArray[Any] | None,
    factor: int,
    alpha: float = 0.5,
    window: int = 32,
    overlap: float = 0.5,
    spectrum_smooth: int = 1,
    **_: Any,
) -> ComplexArray:
    """Goldstein-filtered interferogram, then complex multilook."""
    z = _as_complex(igram, factor, "filtered")
    filt = goldstein_filter(z, alpha, window, overlap, spectrum_smooth)
    return np.asarray(multilook(filt, factor), dtype=np.complex128)


# ---------------------------------------------------------------- registry / dispatcher
ReprMethod = Callable[..., ComplexArray]

METHODS: dict[str, ReprMethod] = {
    "ml": repr_ml,
    "coh_weighted": repr_coh_weighted,
    "shp": repr_shp,
    "phase_link": repr_phase_link,
    "filtered": repr_filtered,
}
#: Methods that need ``stack`` (amplitude / SLC stack) in addition to the interferogram.
STACK_METHODS: frozenset[str] = frozenset({"shp", "phase_link"})


def representative_phase(
    method: str,
    igram: NDArray[Any] | None,
    coh: NDArray[Any] | None,
    factor: int,
    stack: NDArray[Any] | None = None,
    **kw: Any,
) -> ComplexArray:
    """Dispatch to :data:`METHODS`. Returns the complex low-res representative phase of shape
    ``(ny // factor, nx // factor)``; see the module docstring for the per-method contract."""
    fn = METHODS.get(method)
    if fn is None:
        raise ResearchError("RES-001", method=method, methods=", ".join(sorted(METHODS)))
    if factor < 1:
        raise ResearchError("RES-006", name="factor", value=factor, allowed=">= 1")
    if method in STACK_METHODS and stack is None:
        raise ResearchError("RES-003", method=method)
    return fn(igram, coh, factor, stack=stack, **kw)
