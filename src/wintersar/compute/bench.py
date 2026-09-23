"""Tiny kernel micro-benchmark: CPU vs GPU wall times for the PERF-10 kernels.

:func:`bench_kernels` only *returns* numbers. Rule 11.8: they may be written to a
``bench_result.json`` by ``wintersar bench`` but never into docs or prose.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from types import ModuleType
from typing import Any

import numpy as np

from wintersar.compute import kernels
from wintersar.compute import xp as xpmod

__all__ = ["KERNEL_NAMES", "bench_kernels"]

KERNEL_NAMES: tuple[str, ...] = ("multilook", "goldstein_filter", "coherence_estimate")


def _timed(fn: Callable[[], Any], xp: ModuleType, repeats: int) -> list[float]:
    # One warm-up call: on CuPy the first invocation compiles kernels / initialises the
    # context and must not be measured; the device is synchronised before every reading
    # because launches are asynchronous.
    # source: https://docs.cupy.dev/en/stable/user_guide/performance.html
    fn()
    xpmod.synchronize(xp)
    times: list[float] = []
    for _ in range(max(1, repeats)):
        t0 = time.perf_counter()
        fn()
        xpmod.synchronize(xp)
        times.append(time.perf_counter() - t0)
    return times


def _run(
    xp: ModuleType,
    z: np.ndarray[Any, Any],
    s2: np.ndarray[Any, Any],
    *,
    repeats: int,
    window: int,
    looks: tuple[int, int],
    coherence_window: int,
) -> dict[str, list[float]]:
    a = xpmod.asarray(z, xp)
    b = xpmod.asarray(s2, xp)
    rg, az = looks
    jobs: dict[str, Callable[[], Any]] = {
        "multilook": lambda: kernels.multilook(a, rg, az, xp),
        "goldstein_filter": lambda: kernels.goldstein_filter(a, window=window, xp=xp),
        "coherence_estimate": lambda: kernels.coherence_estimate(a, b, coherence_window, xp),
    }
    return {name: _timed(fn, xp, repeats) for name, fn in jobs.items()}


def bench_kernels(
    shape: tuple[int, int] = (1024, 1024),
    *,
    repeats: int = 3,
    window: int = 64,
    looks: tuple[int, int] = (4, 2),
    coherence_window: int = 5,
    seed: int = 0,
) -> dict[str, Any]:
    """Wall times (seconds) of every kernel on numpy and, when available, on CuPy.

    Returns ``{"shape", "repeats", "cpu": {kernel: median_s}, "gpu": {kernel: median_s} |
    None, "samples": {"cpu": {kernel: [s, ...]}, "gpu": ...}, "speedup": {kernel: cpu/gpu} |
    None, "device": device_info}`` — the median of ``repeats`` readings after one warm-up
    call (plan §6.3 "3회 반복 중앙값"). ``gpu`` is ``None`` when no CUDA device is visible.
    The GPU probe honours ``WINTERSAR_GPU`` (``0`` disables it).
    """
    ny, nx = int(shape[0]), int(shape[1])
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:ny, 0:nx]
    phase = 2 * np.pi * (3.0 * x / max(window, 1) + 1.5 * y / max(window, 1))
    z = np.exp(1j * (phase + 0.5 * rng.standard_normal((ny, nx)))).astype(np.complex64)
    s2 = 0.7 * z + 0.5 * (rng.standard_normal((ny, nx)) + 1j * rng.standard_normal((ny, nx)))
    s2 = s2.astype(np.complex64)
    cpu = _run(
        np, z, s2, repeats=repeats, window=window, looks=looks, coherence_window=coherence_window
    )
    result: dict[str, Any] = {
        "shape": [ny, nx],
        "repeats": int(max(1, repeats)),
        "cpu": {k: statistics.median(v) for k, v in cpu.items()},
        "gpu": None,
        "samples": {"cpu": cpu, "gpu": None},
        "speedup": None,
        "device": xpmod.device_info(np),
    }
    backend = xpmod.resolve_backend("auto")
    if backend.is_gpu:  # pragma: no cover - requires CUDA
        gpu = _run(
            backend.xp,
            z,
            s2,
            repeats=repeats,
            window=window,
            looks=looks,
            coherence_window=coherence_window,
        )
        result["gpu"] = {k: statistics.median(v) for k, v in gpu.items()}
        result["samples"]["gpu"] = gpu
        result["speedup"] = {
            k: (result["cpu"][k] / result["gpu"][k]) if result["gpu"][k] > 0 else None
            for k in KERNEL_NAMES
        }
        result["device"] = xpmod.device_info(backend.xp)
    return result
