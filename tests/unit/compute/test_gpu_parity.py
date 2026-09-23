"""CPU vs GPU parity for every kernel and every ported stage function (PERF-10, ADR-0096).

Each test computes the CPU (numpy) result first — the reference — and then, when CuPy and a
CUDA device are present, the same call on the GPU, compared with ``assert_allclose`` at the
tolerance stated in ADR-0096. Without CuPy the GPU half is *skipped with a reason*, never
silently passed; the degrade path (a GPU request on a CPU-only machine → numpy + ENV-005)
is exercised everywhere by monkeypatching the device probe.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from tests.unit.select_geometry.synthetic import ridge_dem
from wintersar.compute import kernels as K
from wintersar.compute import xp as xpmod
from wintersar.research import repr_phase as rp
from wintersar.research import synth
from wintersar.select import geometry_masks as gm
from wintersar.unwrap.masks import combine_masks
from wintersar.unwrap.scheduler import fringe_density

VALUE_TOL = {"rtol": 1e-5, "atol": 1e-5}  # float32 kernels (ADR-0053/0096)
PHASE_TOL_RAD = 1e-4
F64_TOL = {"rtol": 1e-9, "atol": 1e-9}  # float64 geometry / research paths
BOOL_MISMATCH_MAX = 1e-3  # threshold comparisons may flip on boundary pixels only

SKIP_REASON = "CuPy / CUDA not available on this machine: GPU half of the parity test skipped"


def _gpu_or_skip() -> Any:
    if not xpmod.cupy_available():
        pytest.skip(SKIP_REASON)
    import cupy  # pragma: no cover - CUDA machines only

    return cupy


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WINTERSAR_GPU", raising=False)
    xpmod.reset_backend_cache()
    yield
    xpmod.reset_backend_cache()


@pytest.fixture
def igram() -> synth.SynthIgram:
    rng = np.random.default_rng(31)
    return synth.make_interferogram(
        (96, 128), rng, atmosphere_std_rad=0.5, coherence_base=0.7, looks=2, water_fraction=0.1
    )


# ---------------------------------------------------------------------- kernels (input-driven backend)


def _kernel_cases(z: np.ndarray, s2: np.ndarray) -> dict[str, Callable[[Any], Any]]:
    return {
        "box_sum": lambda a: K.box_sum(a.real.astype(np.float32), (3, 5)),
        "box_filter": lambda a: K.box_filter(a.real.astype(np.float32), 4),
        "multilook_mean": lambda a: K.multilook(a, 4, 2),
        "multilook_power": lambda a: K.multilook(a, 3, 3, method="power"),
        "goldstein_hann_clamp": lambda a: K.goldstein_filter(a, 0.6, 32),
        "goldstein_bartlett_zero_wrap": lambda a: K.goldstein_filter(
            a,
            0.8,
            16,
            overlap=8,
            smooth=3,
            weight="bartlett",
            pad="zero",
            smooth_mode="wrap",
            keep_magnitude=True,
        ),
        "coherence_estimate": lambda a: K.coherence_estimate(
            a, xpmod.asarray(s2, xpmod.xp_of(a)), 5
        ),
        "phase_noise_std": lambda a: K.phase_noise_std(
            K.coherence_estimate(a, xpmod.asarray(s2, xpmod.xp_of(a)), 5), looks=4
        ),
    }


@pytest.mark.gpu
@pytest.mark.parametrize(
    "name",
    [
        "box_sum",
        "box_filter",
        "multilook_mean",
        "multilook_power",
        "goldstein_hann_clamp",
        "goldstein_bartlett_zero_wrap",
        "coherence_estimate",
        "phase_noise_std",
    ],
)
def test_kernel_gpu_matches_cpu(igram: synth.SynthIgram, name: str) -> None:
    z = igram.complex.astype(np.complex64)
    rng = np.random.default_rng(2)
    s2 = 0.7 * z + 0.5 * (rng.standard_normal(z.shape) + 1j * rng.standard_normal(z.shape))
    s2 = s2.astype(np.complex64)
    fn = _kernel_cases(z, s2)[name]
    cpu = fn(z)
    assert not xpmod.is_cupy_array(cpu)
    cupy = _gpu_or_skip()
    gpu = xpmod.to_numpy(fn(cupy.asarray(z)))  # pragma: no cover - CUDA machines only
    assert gpu.shape == cpu.shape and gpu.dtype == cpu.dtype
    if np.iscomplexobj(cpu):
        assert np.abs(np.angle(gpu * np.conj(cpu))).max() < PHASE_TOL_RAD
        np.testing.assert_allclose(np.abs(gpu), np.abs(cpu), **VALUE_TOL)
    else:
        np.testing.assert_allclose(gpu, cpu, **VALUE_TOL)


# ---------------------------------------------------------------------- ported functions (gpu= keyword)


def _ridge_inputs() -> tuple[np.ndarray, float, float]:
    dem = ridge_dem(48, 64, 30.0, 400.0, 5.0)
    dem[10:12, 20:23] = np.nan  # nodata pixels must stay NaN on both backends
    return dem, 30.0, 39.0


def _ported_cases(igram: synth.SynthIgram) -> dict[str, Callable[[Any], Any]]:
    dem, dx, inc = _ridge_inputs()
    coh = igram.coherence.astype(np.float32).copy()
    coh[0, 0] = np.nan
    water = igram.mask
    phi = gm.sensor_azimuth(-12.0)
    slope, aspect = gm.slope_aspect(dem, dx, dx)

    def geometry(gpu: Any) -> dict[str, Any]:
        r = gm.compute_geometry_masks(dem, dx, dx, -12.0, inc, "ASCENDING", gpu=gpu)
        return {
            "layover": r.layover,
            "shadow": r.shadow,
            "foreshortening": r.foreshortening,
            "local_incidence_deg": r.local_incidence_deg,
            "stats": np.array([r.stats[k] for k in sorted(r.stats)]),
        }

    return {
        "combine_masks": lambda gpu: combine_masks(coh, 0.4, water=water, gpu=gpu),
        "fringe_density": lambda gpu: np.array(fringe_density(igram.wrapped, igram.mask, gpu=gpu)),
        "fringe_density_complex": lambda gpu: np.array(fringe_density(igram.complex, gpu=gpu)),
        "slope_aspect": lambda gpu: np.stack(gm.slope_aspect(dem, dx, dx, gpu=gpu)),
        "local_incidence": lambda gpu: gm.local_incidence(slope, aspect, phi, inc, gpu=gpu),
        "range_slope": lambda gpu: gm.range_slope(slope, aspect, phi, gpu=gpu),
        "compute_geometry_masks": geometry,
        "research_goldstein": lambda gpu: rp.goldstein_filter(
            igram.complex, 0.6, 16, 0.5, 3, gpu=gpu
        ),
        "research_repr_filtered": lambda gpu: rp.repr_filtered(
            igram.complex, None, 3, alpha=0.6, window=16, spectrum_smooth=3, gpu=gpu
        ),
    }


PORTED = [
    "combine_masks",
    "fringe_density",
    "fringe_density_complex",
    "slope_aspect",
    "local_incidence",
    "range_slope",
    "compute_geometry_masks",
    "research_goldstein",
    "research_repr_filtered",
]


def _compare(cpu: Any, gpu: Any) -> None:
    if isinstance(cpu, dict):
        for k in cpu:
            _compare(cpu[k], gpu[k])
        return
    cpu = np.asarray(cpu)
    gpu = np.asarray(gpu)
    assert gpu.shape == cpu.shape and gpu.dtype == cpu.dtype
    if cpu.dtype == bool:
        assert np.mean(cpu != gpu) <= BOOL_MISMATCH_MAX
    elif np.iscomplexobj(cpu):
        np.testing.assert_allclose(gpu, cpu, rtol=1e-6, atol=1e-8)
    else:
        np.testing.assert_allclose(gpu, cpu, **F64_TOL)


@pytest.mark.gpu
@pytest.mark.parametrize("name", PORTED)
def test_ported_function_gpu_matches_cpu(igram: synth.SynthIgram, name: str) -> None:
    fn = _ported_cases(igram)[name]
    cpu = fn(False)
    _gpu_or_skip()
    gpu = fn(True)  # pragma: no cover - CUDA machines only
    _compare(cpu, gpu)


@pytest.mark.parametrize("name", PORTED)
def test_ported_function_forced_gpu_without_cupy_degrades_to_cpu(
    igram: synth.SynthIgram, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``WINTERSAR_GPU=1`` (or ``compute.gpu: true``) on a machine without CuPy must give
    the CPU result plus the ENV-005 finding — never an exception."""
    fn = _ported_cases(igram)[name]
    cpu = fn(False)
    monkeypatch.setattr(xpmod, "cupy_available", lambda: False)
    monkeypatch.setenv("WINTERSAR_GPU", "1")
    degraded = fn(True)
    _compare(cpu, degraded)
    if isinstance(cpu, dict):
        assert all(np.array_equal(cpu[k], degraded[k], equal_nan=True) for k in cpu)
    else:
        assert np.array_equal(np.asarray(cpu), np.asarray(degraded), equal_nan=True)
    b = xpmod.resolve_backend()
    assert b.xp is np and b.degraded and [f.rule_id for f in b.findings] == ["ENV-005"]
    # the stage-context route (executor ``_gpu`` param) degrades the same way
    monkeypatch.delenv("WINTERSAR_GPU")
    with xpmod.stage_gpu(True):
        via_stage = fn(None)
        assert xpmod.resolve_backend().findings[0].evidence["requested_by"] == "stage"
    _compare(cpu, via_stage)
