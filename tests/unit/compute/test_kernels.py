"""compute.kernels: CPU numerics (reference) and CPU-vs-GPU parity when CuPy exists (ADR-0053).

Tolerances: box sums 1e-4 relative to scipy (float32 accumulation), multilook exact for
constants, Goldstein pure plane |dphi| < 1e-3 rad, coherence of identical images 1 - 1e-4,
GPU parity 1e-5 (values) / 1e-4 rad (phase).
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.ndimage import uniform_filter

from wintersar.compute import kernels as K
from wintersar.compute import xp as xpmod


def _plane(ny: int = 128, nx: int = 160, cx: float = 4.0, cy: float = 2.0, window: int = 64):
    y, x = np.mgrid[0:ny, 0:nx]
    ph = 2 * np.pi * (cx * x / window + cy * y / window)
    return ph, np.exp(1j * ph).astype(np.complex64)


# ---------------------------------------------------------------------- box sums


@pytest.mark.parametrize("window", [1, 3, 4, 5, (3, 7), (6, 2)])
def test_box_sum_matches_scipy_zero_padded(window) -> None:
    rng = np.random.default_rng(0)
    a = rng.standard_normal((37, 53)).astype(np.float32)
    n = int(np.prod(window)) if isinstance(window, tuple) else window * window
    ref = uniform_filter(a, size=window, mode="constant", cval=0.0) * n
    out = K.box_sum(a, window)
    assert out.shape == a.shape and out.dtype == np.float32
    np.testing.assert_allclose(out, ref, atol=1e-4)


def test_box_sum_complex_and_batched() -> None:
    rng = np.random.default_rng(1)
    z = (rng.standard_normal((2, 20, 24)) + 1j * rng.standard_normal((2, 20, 24))).astype(
        np.complex64
    )
    out = K.box_sum(z, 3)
    assert out.shape == z.shape and out.dtype == np.complex64
    ref_r = uniform_filter(z.real[1], 3, mode="constant") * 9
    np.testing.assert_allclose(out[1].real, ref_r, atol=1e-4)


def test_box_filter_normalisation() -> None:
    a = np.ones((10, 10), np.float32)
    np.testing.assert_allclose(K.box_filter(a, 3), 1.0, atol=1e-6)  # 'valid' is unbiased at edges
    full = K.box_filter(a, 3, normalize="full")
    assert full[0, 0] == pytest.approx(4 / 9) and full[5, 5] == pytest.approx(1.0)
    with pytest.raises(ValueError, match="window"):
        K.box_sum(a, 0)


def test_box_sum_large_image_float32_stays_accurate() -> None:
    a = np.random.default_rng(2).standard_normal((1500, 1500)).astype(np.float32) + 3.0
    ref = uniform_filter(a, 3, mode="constant") * 9
    np.testing.assert_allclose(K.box_sum(a, 3), ref, atol=1e-3)


# ---------------------------------------------------------------------- multilook


def test_multilook_constant_and_shapes() -> None:
    c = np.full((20, 30), 2.5, np.float32)
    ml = K.multilook(c, rg=3, az=4)
    assert ml.shape == (5, 10) and np.all(ml == 2.5)
    z = np.full((3, 20, 30), 1 + 1j, np.complex64)
    mlz = K.multilook(z, 2, 2)
    assert mlz.shape == (3, 10, 15) and np.all(mlz == 1 + 1j)


def test_multilook_mean_and_power_values() -> None:
    a = np.arange(16, dtype=np.float32).reshape(4, 4)
    ml = K.multilook(a, 2, 2)
    np.testing.assert_allclose(ml, [[2.5, 4.5], [10.5, 12.5]])
    z = np.array([[1 + 0j, -1 + 0j], [1j, -1j]], np.complex64)
    assert K.multilook(z, 2, 2)[0, 0] == 0  # coherent sum cancels
    assert K.multilook(z, 2, 2, method="power")[0, 0] == pytest.approx(1.0)
    # trailing partial blocks are cropped
    assert K.multilook(np.zeros((7, 9)), 4, 3).shape == (2, 2)
    with pytest.raises(ValueError):
        K.multilook(a, 0, 1)
    with pytest.raises(ValueError):
        K.multilook(a, 1, 1, method="median")
    with pytest.raises(ValueError, match="smaller"):
        K.multilook(a, 8, 8)


# ---------------------------------------------------------------------- Goldstein


def test_goldstein_leaves_pure_plane_unchanged() -> None:
    """A plane with an integer number of cycles per patch is a single spectral bin: the
    filter scales its magnitude but cannot change the phase (tolerance 1e-3 rad)."""
    _ph, z = _plane()
    out = K.goldstein_filter(z, alpha=0.6, window=64)
    assert out.shape == z.shape and out.dtype == np.complex64
    d = np.angle(out * np.conj(z))
    assert np.abs(d).max() < 1e-3
    # also with a non-multiple image size (last patch clamped to the edge)
    _ph2, z2 = _plane(100, 150)
    assert np.abs(np.angle(K.goldstein_filter(z2, 0.8, 32) * np.conj(z2))).max() < 1e-3


def test_goldstein_alpha_zero_is_identity_and_reduces_noise() -> None:
    ph, z = _plane(96, 128)
    rng = np.random.default_rng(0)
    noisy = np.exp(1j * (ph + 0.8 * rng.standard_normal(ph.shape))).astype(np.complex64)
    ident = K.goldstein_filter(noisy, alpha=0.0, window=32)
    assert np.abs(np.angle(ident * np.conj(noisy))).max() < 1e-4
    filt = K.goldstein_filter(noisy, alpha=0.8, window=32)
    before = np.angle(noisy * np.conj(z)).std()
    after = np.angle(filt * np.conj(z)).std()
    assert after < 0.5 * before
    # real (phase) input is accepted
    filt_ph = K.goldstein_filter(np.angle(noisy).astype(np.float32), alpha=0.8, window=32)
    np.testing.assert_allclose(np.angle(filt_ph), np.angle(filt), atol=1e-4)


def test_goldstein_argument_validation() -> None:
    _, z = _plane(64, 64)
    with pytest.raises(ValueError, match="alpha"):
        K.goldstein_filter(z, alpha=-1)
    with pytest.raises(ValueError, match="window"):
        K.goldstein_filter(z, window=2)
    with pytest.raises(ValueError, match="overlap"):
        K.goldstein_filter(z, window=32, overlap=32)
    with pytest.raises(ValueError, match="smaller"):
        K.goldstein_filter(z, window=128)
    with pytest.raises(ValueError, match="2-D"):
        K.goldstein_filter(np.zeros((2, 64, 64), np.complex64))
    assert K._patch_origins(100, 32, 16) == [0, 16, 32, 48, 64, 68]
    assert K._patch_origins(64, 32, 16) == [0, 16, 32]


# ---------------------------------------------------------------------- coherence


def test_coherence_identical_independent_and_edges() -> None:
    rng = np.random.default_rng(3)
    s1 = (rng.standard_normal((64, 64)) + 1j * rng.standard_normal((64, 64))).astype(np.complex64)
    same = K.coherence_estimate(s1, s1, 5)
    assert same.dtype == np.float32 and same.shape == (64, 64)
    assert same.min() > 1 - 1e-4 and same.max() <= 1.0
    s2 = (rng.standard_normal((64, 64)) + 1j * rng.standard_normal((64, 64))).astype(np.complex64)
    indep = K.coherence_estimate(s1, s2, 7)
    assert 0.0 < indep.mean() < 0.4  # E|g| ~ sqrt(pi/(4N)) ~ 0.13 for N = 49 looks
    # partially correlated: coherence rises with the correlation
    mix = (0.9 * s1 + np.sqrt(1 - 0.81) * s2).astype(np.complex64)
    assert K.coherence_estimate(s1, mix, 7).mean() > indep.mean() + 0.3
    zero = K.coherence_estimate(np.zeros((8, 8), np.complex64), s1[:8, :8], 3)
    assert np.all(zero == 0)
    with pytest.raises(ValueError, match="shape"):
        K.coherence_estimate(s1, s1[:10], 3)


def test_phase_noise_std_monotone() -> None:
    s = K.phase_noise_std(np.array([0.99, 0.5, 0.05, 0.0], np.float32), looks=4)
    assert np.all(np.diff(s) >= 0) and s[-1] == pytest.approx(np.pi / np.sqrt(3), abs=1e-5)
    assert (
        K.phase_noise_std(np.array([0.9]), looks=16)[0]
        < K.phase_noise_std(np.array([0.9]), looks=1)[0]
    )


# ---------------------------------------------------------------------- GPU parity


@pytest.mark.gpu
def test_gpu_matches_cpu() -> None:
    if not xpmod.cupy_available():
        pytest.skip("CuPy / CUDA not available")
    import cupy  # pragma: no cover - CUDA machines only

    ph, _z = _plane(96, 128)
    rng = np.random.default_rng(0)
    noisy = np.exp(1j * (ph + 0.5 * rng.standard_normal(ph.shape))).astype(np.complex64)
    cpu = K.goldstein_filter(noisy, 0.6, 32)
    gpu = xpmod.to_numpy(K.goldstein_filter(cupy.asarray(noisy), 0.6, 32))
    assert np.abs(np.angle(gpu * np.conj(cpu))).max() < 1e-4
    s1 = (rng.standard_normal((64, 64)) + 1j * rng.standard_normal((64, 64))).astype(np.complex64)
    s2 = (0.7 * s1 + rng.standard_normal((64, 64))).astype(np.complex64)
    np.testing.assert_allclose(
        xpmod.to_numpy(K.coherence_estimate(cupy.asarray(s1), cupy.asarray(s2), 5)),
        K.coherence_estimate(s1, s2, 5),
        rtol=1e-5,
        atol=1e-5,
    )
    np.testing.assert_allclose(
        xpmod.to_numpy(K.multilook(cupy.asarray(noisy), 4, 2)), K.multilook(noisy, 4, 2), rtol=1e-5
    )


# ---------------------------------------------------------------------- Goldstein variants (ADR-0096)


def test_goldstein_dtype_follows_input_and_validates_variant_options() -> None:
    _, z = _plane(64, 80, window=32)
    assert K.goldstein_filter(z, 0.5, 32).dtype == np.complex64
    out128 = K.goldstein_filter(z.astype(np.complex128), 0.5, 32)
    assert out128.dtype == np.complex128
    # the two precisions agree on the phase (float32 accumulation vs float64; compared as
    # a wrapped difference so the ±π seam does not count)
    out64 = K.goldstein_filter(z, 0.5, 32)
    assert np.abs(np.angle(out128 * np.conj(out64))).max() < 1e-4
    for kw in ({"weight": "boxcar"}, {"pad": "reflect"}, {"smooth_mode": "mirror"}):
        with pytest.raises(ValueError, match="unknown"):
            K.goldstein_filter(z, 0.5, 32, **kw)
    with pytest.raises(ValueError, match="even"):
        K.goldstein_filter(z, 0.5, 33, weight="bartlett")
    # 'zero' padding accepts an image smaller than the window, 'clamp' does not
    small = z[:20, :24]
    with pytest.raises(ValueError, match="smaller"):
        K.goldstein_filter(small, 0.5, 32)
    out = K.goldstein_filter(small, 0.5, 32, pad="zero")
    assert out.shape == small.shape
    assert K._padded_length(70, 32, 16) == 80 and K._padded_length(32, 32, 16) == 32
    assert K._padded_length(20, 32, 16) == 32


@pytest.mark.parametrize("size", [2, 3, 4, 5])
def test_wrap_smoothing_matches_scipy_periodic_uniform_filter(size: int) -> None:
    """``smooth_mode='wrap'`` reproduces ``uniform_filter(mode='wrap')`` including the
    asymmetric centring of even sizes (size // 2 before, size - 1 - size // 2 after)."""
    rng = np.random.default_rng(4)
    mag64 = rng.random((16, 16))
    np.testing.assert_allclose(
        K._smooth_spectrum(mag64, size, "wrap", np),
        uniform_filter(mag64, size, mode="wrap"),
        atol=1e-12,
    )
    mag32 = mag64.astype(np.float32)
    np.testing.assert_allclose(
        K._smooth_spectrum(mag32, size, "wrap", np),
        uniform_filter(mag32, size, mode="wrap"),
        atol=1e-6,
    )
    # 'box' is the zero-padded mean
    np.testing.assert_allclose(
        K._smooth_spectrum(mag64, size, "box", np),
        uniform_filter(mag64, size, mode="constant", cval=0.0),
        atol=1e-12,
    )


def test_goldstein_bartlett_zero_pad_keep_magnitude_variant() -> None:
    ph, _z = _plane(70, 90, cx=3.0, cy=1.0, window=16)
    rng = np.random.default_rng(5)
    noisy = (np.exp(1j * (ph + 0.6 * rng.standard_normal(ph.shape))) * 0.7).astype(np.complex128)
    noisy[10:26, 20:36] = 0.0  # a masked (all-zero) patch-sized hole
    out = K.goldstein_filter(
        noisy,
        0.8,
        16,
        overlap=8,
        smooth=3,
        weight="bartlett",
        pad="zero",
        smooth_mode="wrap",
        keep_magnitude=True,
    )
    assert out.dtype == np.complex128 and out.shape == noisy.shape
    # magnitude restored, zeros stay zero
    np.testing.assert_allclose(np.abs(out), np.abs(noisy), atol=1e-12)
    assert np.all(out[12:24, 22:34] == 0)
    # noise reduced on the valid part
    err_before = np.angle(noisy * np.exp(-1j * ph))[np.abs(noisy) > 0].std()
    err_after = np.angle(out * np.exp(-1j * ph))[np.abs(out) > 0].std()
    assert err_after < 0.6 * err_before
    # the Bartlett blend weight is symmetric, strictly positive and peaks at the centre
    w = K._bartlett2d(16, np, np.float64)
    assert w.shape == (16, 16) and w.min() > 0 and w.max() == w[7, 7] == w[8, 8]
    np.testing.assert_array_equal(w, w[::-1, ::-1])


def test_goldstein_empty_patches_do_not_attenuate_neighbours() -> None:
    """A patch that is entirely zero adds neither data nor weight: the pixels of an adjacent,
    fully valid patch come out with the same values as when the hole is absent."""
    _, z = _plane(64, 128, cx=2.0, cy=1.0, window=32)
    z = z.astype(np.complex64)
    holed = z.copy()
    holed[:, 96:] = 0  # the right-most patch column (origins 96) is all zero
    ref = K.goldstein_filter(z, 0.6, 32, overlap=0)  # non-overlapping patches
    out = K.goldstein_filter(holed, 0.6, 32, overlap=0)
    np.testing.assert_allclose(out[:, :96], ref[:, :96], rtol=1e-5, atol=1e-5)
    assert np.all(out[:, 96:] == 0)


# ---------------------------------------------------------------------- bench helper


def test_bench_kernels_returns_numbers_only() -> None:
    from wintersar.compute import bench_kernels

    r = bench_kernels((48, 40), repeats=2, window=16, looks=(2, 2), coherence_window=3)
    assert r["shape"] == [48, 40] and r["repeats"] == 2
    assert set(r["cpu"]) == {"multilook", "goldstein_filter", "coherence_estimate"}
    assert all(isinstance(v, float) and v > 0 for v in r["cpu"].values())
    assert all(len(v) == 2 for v in r["samples"]["cpu"].values())
    if not xpmod.cupy_available():
        assert r["gpu"] is None and r["speedup"] is None and r["samples"]["gpu"] is None
        assert r["device"] == {"backend": "numpy"}
    else:  # pragma: no cover - CUDA machines
        assert set(r["gpu"]) == set(r["cpu"]) and r["device"]["backend"] == "cupy"
