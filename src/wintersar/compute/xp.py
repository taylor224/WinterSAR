"""numpy / CuPy backend switch (PERF-10, plan §6.2).

``get_xp()`` returns the array module that pixel kernels (:mod:`wintersar.compute.kernels`)
are written against. CuPy is optional (``pip install wintersar[gpu]``); it is imported
lazily and only when a CUDA device is visible, so importing this module never pulls in
CUDA. The selection order is:

1. environment variable ``WINTERSAR_GPU`` (``0``/``false`` forces numpy, ``1``/``true``
   requires CuPy and raises :class:`GpuNotAvailableError` when it is missing),
2. the ``prefer_gpu`` argument (``True`` / ``False`` / ``"auto"``),
3. ``"auto"``: CuPy when importable *and* at least one device is present, else numpy.

Numerical policy (ADR-0053): every kernel is tested on numpy; when CuPy is present the
same test compares the two backends with an explicit tolerance. GPU results are never
silently used as the reference.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from functools import lru_cache
from types import ModuleType
from typing import Any

import numpy as np
from numpy.typing import NDArray

ENV_VAR = "WINTERSAR_GPU"

_FALSE = {"0", "false", "no", "off", "cpu", "numpy"}
_TRUE = {"1", "true", "yes", "on", "gpu", "cuda", "cupy"}
_AUTO = {"", "auto"}


class GpuNotAvailableError(RuntimeError):
    """Raised when a GPU backend was *required* but CuPy / a CUDA device is missing."""


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


def reset_backend_cache() -> None:
    cupy_available.cache_clear()


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


def env_preference() -> bool | None:
    """The ``WINTERSAR_GPU`` override: ``True``/``False`` or ``None`` when unset / auto."""
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return None
    return _parse_flag(raw, source=ENV_VAR)


def resolve_preference(prefer_gpu: bool | str = "auto") -> bool | None:
    """Combine the environment override (wins) with ``prefer_gpu``.

    Returns ``True`` (GPU required), ``False`` (CPU forced) or ``None`` (auto-detect).
    """
    env = env_preference()
    if env is not None:
        return env
    if isinstance(prefer_gpu, bool):
        return prefer_gpu
    return _parse_flag(str(prefer_gpu), source="prefer_gpu")


def get_xp(prefer_gpu: bool | str = "auto") -> ModuleType:
    """Return ``numpy`` or ``cupy`` according to the policy in the module docstring."""
    want = resolve_preference(prefer_gpu)
    if want is False:
        return np
    if cupy_available():
        return importlib.import_module("cupy")
    if want is True:
        msg = (
            "GPU backend required (WINTERSAR_GPU=1 or prefer_gpu=True) but CuPy is not "
            "installed or no CUDA device is visible; install `wintersar[gpu]` or set "
            "WINTERSAR_GPU=0"
        )
        raise GpuNotAvailableError(msg)
    return np


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
