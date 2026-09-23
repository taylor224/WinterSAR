"""numpy / CuPy backend switch (PERF-10, plan §6.2; ADR-0053, ADR-0095).

:func:`resolve_backend` returns the array module that the pixel kernels
(:mod:`wintersar.compute.kernels`) and the ported stage functions (unwrap masks, fringe
density, DEM geometry masks, research Goldstein/multilook) are written against. CuPy is
optional (``pip install wintersar[gpu]``); it is imported lazily and only when a CUDA device
is visible, so importing this module never pulls in CUDA.

Precedence (ADR-0095), highest first:

1. environment variable ``WINTERSAR_GPU`` — ``0``/``false`` forces numpy, ``1``/``true``
   requests CuPy, ``auto``/unset defers to the next source (operator override for one
   invocation, e.g. to switch a broken GPU off without editing the config);
2. the explicit ``gpu=`` argument of the function being called (``True``/``False``;
   ``None``/``"auto"`` defers);
3. the stage context set with :func:`stage_gpu` from the executor's private ``_gpu`` param
   (``config.compute.gpu`` after ``MachineSpec.budget``: ``auto`` → detected, ``true``/``false``
   → forced) — stage code wraps its work in ``with stage_gpu(params.get("_gpu")):`` so
   every kernel below it inherits the config without threading a keyword through;
4. auto-detect: CuPy importable **and** at least one CUDA device → cupy, else numpy.

A GPU *request* (``True``) that cannot be honoured **degrades to numpy** and reports the
``ENV-005`` finding (``WARN``, available on :attr:`Backend.findings`, logged once per
process) — never an exception — so a config written on a GPU workstation still runs on a
CPU-only box (plan §6.1 "CPU fallback"). ``strict=True`` restores the exception
(:class:`GpuNotAvailableError`) for tests and benchmarks that must know.

Numerical policy (ADR-0053/0096): every kernel is tested on numpy; when CuPy is present the
same test compares the two backends with an explicit tolerance. GPU results are never
silently used as the reference.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from types import ModuleType
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wintersar.io.schemas import Finding

ENV_VAR = "WINTERSAR_GPU"
#: Executor private parameter that carries ``config.compute.gpu`` into a stage (ADR-0034).
STAGE_PARAM = "_gpu"

_FALSE = {"0", "false", "no", "off", "cpu", "numpy"}
_TRUE = {"1", "true", "yes", "on", "gpu", "cuda", "cupy"}
_AUTO = {"", "auto"}

#: ``True`` (GPU requested), ``False`` (CPU forced), ``None``/``"auto"`` (defer).
GpuRequest = bool | str | None

_log = logging.getLogger("wintersar.compute")
_STAGE_GPU: ContextVar[bool | None] = ContextVar("wintersar_stage_gpu", default=None)
_degrade_warned = False


class GpuNotAvailableError(RuntimeError):
    """Raised only with ``strict=True`` when a GPU backend was required but CuPy / a CUDA
    device is missing. The default policy degrades to numpy with an ``ENV-005`` finding."""


@lru_cache(maxsize=1)
def cupy_available() -> bool:
    """``True`` when CuPy imports and reports at least one CUDA device.

    Cached for the process lifetime; call :func:`reset_backend_cache` in tests that
    monkeypatch the environment.
    """
    if importlib.util.find_spec("cupy") is None:
        return False
    try:  # pragma: no cover - requires CUDA hardware
        cupy = importlib.import_module("cupy")
        # source: https://docs.cupy.dev/en/stable/reference/generated/cupy.cuda.runtime.getDeviceCount.html
        return int(cupy.cuda.runtime.getDeviceCount()) > 0
    except Exception:
        return False


def cupy_installed() -> bool:
    """``True`` when the ``cupy`` package is importable (a device may still be missing)."""
    return importlib.util.find_spec("cupy") is not None


def reset_backend_cache() -> None:
    """Forget the cached device probe and the once-per-process degrade warning (tests)."""
    global _degrade_warned
    clear = getattr(cupy_available, "cache_clear", None)  # absent when tests monkeypatch it
    if clear is not None:
        clear()
    _degrade_warned = False


def _parse_flag(value: str, *, source: str) -> bool | None:
    v = value.strip().lower()
    if v in _FALSE:
        return False
    if v in _TRUE:
        return True
    if v in _AUTO:
        return None
    msg = f"{source}: expected one of 0/1/true/false/auto, got {value!r}"
    raise ValueError(msg)


def coerce_request(value: GpuRequest, *, source: str = "gpu") -> bool | None:
    """Normalise a request to ``True`` / ``False`` / ``None`` (auto)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return _parse_flag(str(value), source=source)


def env_preference() -> bool | None:
    """The ``WINTERSAR_GPU`` override: ``True``/``False`` or ``None`` when unset / auto."""
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return None
    return _parse_flag(raw, source=ENV_VAR)


# ---------------------------------------------------------------- stage context


@contextmanager
def stage_gpu(gpu: GpuRequest) -> Iterator[None]:
    """Make the executor's ``_gpu`` private param the default for every kernel inside.

    Stage code: ``with stage_gpu(params.get("_gpu")): ...``. Nesting restores the outer
    value on exit; the context is per thread / task (``contextvars``).
    """
    token = _STAGE_GPU.set(coerce_request(gpu, source=STAGE_PARAM))
    try:
        yield
    finally:
        _STAGE_GPU.reset(token)


def stage_preference() -> bool | None:
    """The value set by the innermost :func:`stage_gpu` (``None`` outside a stage)."""
    return _STAGE_GPU.get()


# ---------------------------------------------------------------- resolution


@dataclass(frozen=True)
class Backend:
    """Outcome of :func:`resolve_backend`.

    ``requested`` is the effective request (``None`` = auto), ``source`` where it came from
    (``env`` / ``argument`` / ``stage`` / ``auto``), ``degraded`` whether a GPU request fell
    back to numpy, and ``findings`` the ``ENV-005`` finding in that case (empty otherwise) so
    stage code can append it to its report.
    """

    xp: ModuleType
    name: str
    requested: bool | None
    source: str
    degraded: bool
    findings: tuple[Finding, ...] = ()

    @property
    def is_gpu(self) -> bool:
        return self.name == "cupy"


def resolve_request(gpu: GpuRequest = None) -> tuple[bool | None, str]:
    """``(request, source)`` after applying the precedence in the module docstring."""
    env = env_preference()
    if env is not None:
        return env, "env"
    arg = coerce_request(gpu, source="gpu")
    if arg is not None:
        return arg, "argument"
    stage = stage_preference()
    if stage is not None:
        return stage, "stage"
    return None, "auto"


def resolve_preference(prefer_gpu: GpuRequest = "auto") -> bool | None:
    """``True`` (GPU requested), ``False`` (CPU forced) or ``None`` (auto-detect)."""
    return resolve_request(prefer_gpu)[0]


def degrade_finding(source: str) -> Finding:
    """The ``ENV-005`` finding for a GPU request that runs on numpy instead."""
    return Finding(
        rule_id="ENV-005",
        severity="WARN",
        message_key="env.ENV-005.cause",
        fix_key="env.ENV-005.fix",
        evidence={
            "requested_by": source,
            "cupy_installed": cupy_installed(),
            "cuda_device": cupy_available(),
            "backend": "numpy",
        },
        scope="compute",
    )


def resolve_backend(gpu: GpuRequest = None, *, strict: bool = False) -> Backend:
    """Pick numpy or cupy for a piece of work (never raises unless ``strict``)."""
    global _degrade_warned
    want, source = resolve_request(gpu)
    if want is False:
        return Backend(np, "numpy", False, source, False)
    if cupy_available():
        return Backend(importlib.import_module("cupy"), "cupy", want, source, False)
    if want is True:
        if strict:
            msg = (
                f"GPU backend required ({source}) but CuPy is not installed or no CUDA "
                "device is visible; install `wintersar[gpu]` or set WINTERSAR_GPU=0"
            )
            raise GpuNotAvailableError(msg)
        finding = degrade_finding(source)
        if not _degrade_warned:
            _degrade_warned = True
            _log.warning(
                "ENV-005: GPU requested by %s but CuPy/CUDA is unavailable; running on numpy",
                source,
            )
        return Backend(np, "numpy", True, source, True, (finding,))
    return Backend(np, "numpy", None, source, False)


def get_xp(prefer_gpu: GpuRequest = "auto", *, strict: bool = False) -> ModuleType:
    """Return ``numpy`` or ``cupy`` according to the policy in the module docstring."""
    return resolve_backend(prefer_gpu, strict=strict).xp


# ---------------------------------------------------------------- array helpers


def backend_name(xp: ModuleType) -> str:
    """``"cupy"`` or ``"numpy"`` for an array module."""
    return "cupy" if xp.__name__.split(".")[0] == "cupy" else "numpy"


def is_cupy_array(a: Any) -> bool:
    """``True`` for ``cupy.ndarray`` (checked by module name; no cupy import needed)."""
    return type(a).__module__.split(".")[0] == "cupy"


def xp_of(a: Any) -> ModuleType:
    """The array module that owns ``a`` (numpy for everything that is not a cupy array)."""
    if is_cupy_array(a):
        return importlib.import_module("cupy")
    return np


def to_numpy(a: Any) -> NDArray[Any]:
    """Copy a (possibly device-resident) array to host memory as ``numpy.ndarray``."""
    if is_cupy_array(a):
        cupy = importlib.import_module("cupy")
        # source: https://docs.cupy.dev/en/stable/reference/generated/cupy.asnumpy.html
        return np.asarray(cupy.asnumpy(a))
    return np.asarray(a)


def asarray(a: Any, xp: ModuleType | None = None, dtype: Any = None) -> Any:
    """``xp.asarray`` with the backend chosen by ``xp`` (default: the module owning ``a``).

    Moving data between host and device is explicit: pass ``xp=cupy`` to upload,
    ``xp=numpy`` (or :func:`to_numpy`) to download.
    """
    mod = xp or xp_of(a)
    if backend_name(mod) == "numpy" and is_cupy_array(a):
        arr = to_numpy(a)
        return arr.astype(dtype) if dtype is not None else arr
    return mod.asarray(a, dtype=dtype) if dtype is not None else mod.asarray(a)


def synchronize(xp: ModuleType) -> None:
    """Wait for queued device work (no-op on numpy) — required around GPU wall-time
    measurements because CuPy launches kernels asynchronously."""
    if backend_name(xp) == "cupy":  # pragma: no cover - requires CUDA
        # source: https://docs.cupy.dev/en/stable/reference/generated/cupy.cuda.Device.html
        #   (Device() selects the current device; synchronize() "Synchronizes the current
        #   thread to the device")
        xp.cuda.Device().synchronize()


def device_info(xp: ModuleType) -> dict[str, Any]:
    """Small dict for logs / bench_result.json (backend name, device name when GPU)."""
    info: dict[str, Any] = {"backend": backend_name(xp)}
    if backend_name(xp) == "cupy":  # pragma: no cover - requires CUDA
        try:
            props = xp.cuda.runtime.getDeviceProperties(0)
            name = props.get("name", b"")
            info["device"] = name.decode() if isinstance(name, bytes) else str(name)
        except Exception:
            info["device"] = None
    return info
