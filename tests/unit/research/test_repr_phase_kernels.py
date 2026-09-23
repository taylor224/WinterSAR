"""research/repr_phase.py delegates its pixel primitives to ``wintersar.compute.kernels``
(PERF-10, ADR-0096/0097).

The former private implementations (block mean, Goldstein filter with a Bartlett blend on a
zero-padded patch grid, periodic spectrum smoothing, magnitude kept) are frozen below as
``_legacy_*`` references so that the shared kernels are proven numerically equivalent
before the duplicates were deleted:

* ``multilook`` — bit-identical (``assert_array_equal``): same crop-then-``mean`` arithmetic.
* ``goldstein_filter`` — complex128 ``assert_allclose(rtol=1e-7, atol=1e-9)``; the only
  differences are rounding (summed-area-table box means vs scipy's running sums, and the
  ``acc / max(wsum, 1e-12)`` normalisation whose scale cancels when the magnitude is
  restored).
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.ndimage import uniform_filter

from wintersar.compute import xp as xpmod
from wintersar.research import repr_phase as rp
from wintersar.research import synth

# ---------------------------------------------------------------- frozen legacy references


def _legacy_multilook(arr: np.ndarray, factor: int) -> np.ndarray:
    a = rp.crop_to_multiple(np.asarray(arr), factor)
    if factor == 1:
        return np.array(a, copy=True)
    ny, nx = a.shape[-2], a.shape[-1]
    lead = a.shape[:-2]
    blocks = a.reshape(*lead, ny // factor, factor, nx // factor, factor)
    return np.asarray(blocks.mean(axis=(-3, -1)))


def _legacy_bartlett2d(size: int) -> np.ndarray:
    half = size // 2
    ramp = 1.0 - np.abs(np.arange(half) - (half - 0.5)) / half
    quad = np.outer(ramp, ramp)
    top = np.concatenate([quad, quad[:, ::-1]], axis=1)
    return np.asarray(np.concatenate([top, top[::-1, :]], axis=0), dtype=np.float64)


def _legacy_goldstein_filter(
    igram: np.ndarray,
    alpha: float = 0.5,
    window: int = 32,
    overlap: float = 0.5,
    spectrum_smooth: int = 1,
) -> np.ndarray:
    z = np.asarray(igram)
    if not np.iscomplexobj(z):
        z = np.exp(1j * z.astype(np.float64))
    z = np.asarray(np.where(np.isfinite(z), z, 0.0), dtype=np.complex128)
    ny, nx = z.shape
    step = max(1, round(window * (1.0 - overlap)))
    ny_p = window + step * int(np.ceil(max(ny - window, 0) / step))
    nx_p = window + step * int(np.ceil(max(nx - window, 0) / step))
    padded = np.zeros((ny_p, nx_p), dtype=np.complex128)
    padded[:ny, :nx] = z
    weight = _legacy_bartlett2d(window)
    acc = np.zeros_like(padded)
    wsum = np.zeros((ny_p, nx_p), dtype=np.float64)
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


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def noisy_igram() -> synth.SynthIgram:
    rng = np.random.default_rng(21)
    return synth.make_interferogram(
        (70, 90), rng, atmosphere_std_rad=0.4, coherence_base=0.6, looks=1, water_fraction=0.15
    )


# ---------------------------------------------------------------- multilook


@pytest.mark.parametrize("factor", [1, 2, 3, 5])
def test_multilook_is_bit_identical_to_legacy_block_mean(noisy_igram, factor: int) -> None:
    z = noisy_igram.complex
    np.testing.assert_array_equal(rp.multilook(z, factor), _legacy_multilook(z, factor))
    coh = noisy_igram.coherence.astype(np.float32)
    out = rp.multilook(coh, factor)
    np.testing.assert_array_equal(out, _legacy_multilook(coh, factor))
    assert out.dtype == np.float32
    stack = np.stack([z, z.conj()])  # leading axes are preserved
    np.testing.assert_array_equal(rp.multilook(stack, factor), _legacy_multilook(stack, factor))


def test_multilook_degenerate_shapes_keep_the_historical_empty_result() -> None:
    a = np.ones((2, 9))
    out = rp.multilook(a, 3)
    assert out.shape == (0, 3) and out.dtype == np.float64
    assert rp.multilook(np.ones((4, 4), np.int32), 8).shape == (0, 0)
    assert rp.multilook(np.ones((3, 4, 4), np.complex64), 2).shape == (3, 2, 2)
    with pytest.raises(rp.ResearchError):
        rp.multilook(a, 0)


# ---------------------------------------------------------------- Goldstein


@pytest.mark.parametrize(
    ("alpha", "window", "overlap", "smooth"),
    [
        (0.5, 32, 0.5, 1),
        (0.8, 16, 0.5, 3),
        (0.8, 8, 0.0, 3),
        (0.3, 16, 0.25, 4),  # even smoothing size: centring must match scipy's
        (0.0, 16, 0.75, 1),  # alpha 0 → identity in phase
        (1.0, 64, 0.5, 5),  # window larger than the 70-row image: zero-padded grid
    ],
)
def test_goldstein_matches_legacy_within_tolerance(
    noisy_igram, alpha: float, window: int, overlap: float, smooth: int
) -> None:
    z = noisy_igram.complex
    ref = _legacy_goldstein_filter(z, alpha, window, overlap, smooth)
    out = rp.goldstein_filter(z, alpha, window, overlap, smooth)
    assert out.dtype == np.complex128 and out.shape == z.shape
    np.testing.assert_allclose(out, ref, rtol=1e-7, atol=1e-9)
    # masked (zero) pixels stay zero and the magnitude is the input magnitude
    np.testing.assert_allclose(np.abs(out), np.abs(z), atol=1e-9)


def test_goldstein_real_and_nonfinite_input_match_legacy(noisy_igram) -> None:
    ph = noisy_igram.wrapped.astype(np.float64)
    ph[3:6, 10:14] = np.nan
    np.testing.assert_allclose(
        rp.goldstein_filter(ph, 0.6, 16, 0.5, 3),
        _legacy_goldstein_filter(ph, 0.6, 16, 0.5, 3),
        rtol=1e-7,
        atol=1e-9,
    )


def test_goldstein_validation_is_unchanged(noisy_igram) -> None:
    z = noisy_igram.complex
    for kw in ({"window": 15}, {"window": 2}, {"alpha": -0.1}, {"overlap": 1.0}):
        with pytest.raises(rp.ResearchError) as exc:
            rp.goldstein_filter(z, **kw)
        assert exc.value.rule_id == "RES-006"
    with pytest.raises(rp.ResearchError):
        rp.goldstein_filter(np.zeros((2, 16, 16), np.complex128))


def test_repr_filtered_matches_legacy_pipeline(noisy_igram) -> None:
    z = noisy_igram.complex
    f = 3
    ref = _legacy_multilook(_legacy_goldstein_filter(z, 0.8, 16, 0.5, 3), f)
    est = rp.representative_phase(
        "filtered", z, None, f, alpha=0.8, window=16, overlap=0.5, spectrum_smooth=3
    )
    assert est.dtype == np.complex128 and est.shape == rp.lowres_shape(z.shape, f)
    np.testing.assert_allclose(est, ref, rtol=1e-7, atol=1e-9)
    # the gpu keyword is accepted by the dispatcher and by every method (ignored where unused)
    same = rp.representative_phase(
        "filtered", z, None, f, alpha=0.8, window=16, overlap=0.5, spectrum_smooth=3, gpu=False
    )
    np.testing.assert_allclose(same, est)
    np.testing.assert_array_equal(
        rp.representative_phase("ml", z, None, f, gpu=False), rp.repr_ml(z, None, f)
    )


def test_no_private_goldstein_or_block_mean_left_in_research() -> None:
    """The duplicates are gone: only the shared kernels do the arithmetic."""
    import inspect

    src = inspect.getsource(rp)
    assert "np.fft.fft2" not in src and "np.fft.ifft2" not in src
    assert "_bartlett2d" not in src and "uniform_filter" not in src
    assert ".reshape(*lead, ny // factor, factor" not in src
    assert "kernels.goldstein_filter(" in src and "kernels.multilook(" in src


def test_forced_gpu_without_cupy_degrades_with_env_005(
    noisy_igram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xpmod, "cupy_available", lambda: False)
    monkeypatch.setenv("WINTERSAR_GPU", "1")
    z = noisy_igram.complex
    cpu = _legacy_goldstein_filter(z, 0.5, 16, 0.5, 1)
    np.testing.assert_allclose(rp.goldstein_filter(z, 0.5, 16), cpu, rtol=1e-7, atol=1e-9)
    np.testing.assert_array_equal(rp.multilook(z, 2), _legacy_multilook(z, 2))
    b = xpmod.resolve_backend()
    assert b.xp is np and [f.rule_id for f in b.findings] == ["ENV-005"]
    xpmod.reset_backend_cache()
