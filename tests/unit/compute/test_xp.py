"""compute.xp: backend selection policy (PERF-10, ADR-0053)."""

from __future__ import annotations

import numpy as np
import pytest

from wintersar.compute import xp as xpmod


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WINTERSAR_GPU", raising=False)
    xpmod.reset_backend_cache()
    yield
    xpmod.reset_backend_cache()


def test_env_forces_numpy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINTERSAR_GPU", "0")
    assert xpmod.get_xp("auto") is np and xpmod.get_xp(True) is np
    assert xpmod.resolve_preference(True) is False


def test_env_requires_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINTERSAR_GPU", "1")
    if xpmod.cupy_available():  # pragma: no cover - CUDA machines
        assert xpmod.backend_name(xpmod.get_xp()) == "cupy"
    else:
        with pytest.raises(xpmod.GpuNotAvailableError):
            xpmod.get_xp()


def test_auto_without_cupy_is_numpy() -> None:
    if xpmod.cupy_available():  # pragma: no cover
        pytest.skip("CuPy present")
    assert xpmod.get_xp("auto") is np and xpmod.get_xp(False) is np
    with pytest.raises(xpmod.GpuNotAvailableError):
        xpmod.get_xp(True)


def test_invalid_flags() -> None:
    with pytest.raises(ValueError, match="prefer_gpu"):
        xpmod.get_xp("maybe")
    with pytest.raises(ValueError, match="WINTERSAR_GPU"):
        xpmod._parse_flag("2", source="WINTERSAR_GPU")


def test_helpers_on_numpy_arrays() -> None:
    a = np.arange(6, dtype=np.float32).reshape(2, 3)
    assert xpmod.backend_name(np) == "numpy" and not xpmod.is_cupy_array(a)
    assert xpmod.xp_of(a) is np
    assert xpmod.to_numpy(a) is not None and np.shares_memory(xpmod.to_numpy(a), a)
    b = xpmod.asarray(a, dtype=np.float64)
    assert b.dtype == np.float64 and xpmod.asarray([1, 2], np).dtype.kind == "i"
    assert xpmod.device_info(np) == {"backend": "numpy"}
