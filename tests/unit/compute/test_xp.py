"""compute.xp: backend selection policy and precedence (PERF-10, ADR-0053, ADR-0095).

Precedence: ``WINTERSAR_GPU`` env > explicit ``gpu=`` argument > stage context
(``stage_gpu(params["_gpu"])``) > auto-detect. A GPU request that cannot be honoured
degrades to numpy with an ``ENV-005`` finding and never raises unless ``strict=True``.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from wintersar.compute import xp as xpmod


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WINTERSAR_GPU", raising=False)
    xpmod.reset_backend_cache()
    yield
    xpmod.reset_backend_cache()


def _no_cupy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a machine without CuPy / CUDA whatever the real one has."""
    xpmod.reset_backend_cache()
    monkeypatch.setattr(xpmod, "cupy_available", lambda: False)
    monkeypatch.setattr(xpmod, "cupy_installed", lambda: False)


# ---------------------------------------------------------------------- env override


def test_env_forces_numpy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINTERSAR_GPU", "0")
    assert xpmod.get_xp("auto") is np and xpmod.get_xp(True) is np
    assert xpmod.resolve_preference(True) is False
    b = xpmod.resolve_backend(True)
    assert b.name == "numpy" and b.requested is False and b.source == "env"
    assert not b.degraded and b.findings == ()


def test_env_gpu_request_degrades_to_cpu_with_env_005(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Forcing WINTERSAR_GPU=1 without CuPy must not crash: numpy + ENV-005 (WARN)."""
    _no_cupy(monkeypatch)
    monkeypatch.setenv("WINTERSAR_GPU", "1")
    with caplog.at_level(logging.WARNING, logger="wintersar.compute"):
        b = xpmod.resolve_backend()
        again = xpmod.resolve_backend("auto")
    assert b.xp is np and b.name == "numpy" and b.degraded and b.requested is True
    assert b.source == "env"
    assert [f.rule_id for f in b.findings] == ["ENV-005"]
    f = b.findings[0]
    assert f.severity == "WARN" and f.message_key == "env.ENV-005.cause"
    assert f.fix_key == "env.ENV-005.fix" and f.scope == "compute"
    assert f.evidence["requested_by"] == "env" and f.evidence["backend"] == "numpy"
    assert f.evidence["cupy_installed"] is False and f.evidence["cuda_device"] is False
    # get_xp never raises by default …
    assert xpmod.get_xp() is np and xpmod.get_xp(True) is np
    # … but strict mode does, for tests / benchmarks that must know
    with pytest.raises(xpmod.GpuNotAvailableError, match="WINTERSAR_GPU=0"):
        xpmod.get_xp(strict=True)
    # the warning is logged once per process (reset_backend_cache re-arms it)
    assert sum("ENV-005" in r.message for r in caplog.records) == 1
    assert again.degraded and again.findings[0].rule_id == "ENV-005"


def test_env_requires_gpu_when_present() -> None:
    if not xpmod.cupy_available():  # pragma: no cover - CUDA machines
        pytest.skip("CuPy / CUDA not available: the GPU branch of the policy is not testable")
    assert xpmod.backend_name(xpmod.resolve_backend(True).xp) == "cupy"


# ---------------------------------------------------------------------- argument / stage


def test_auto_without_cupy_is_numpy(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_cupy(monkeypatch)
    assert xpmod.get_xp("auto") is np and xpmod.get_xp(False) is np
    b = xpmod.resolve_backend()
    assert b.name == "numpy" and b.requested is None and b.source == "auto"
    assert not b.degraded and b.findings == ()
    # an explicit request degrades with the finding; strict raises
    arg = xpmod.resolve_backend(True)
    assert arg.degraded and arg.source == "argument" and arg.findings[0].rule_id == "ENV-005"
    with pytest.raises(xpmod.GpuNotAvailableError):
        xpmod.get_xp(True, strict=True)


def test_argument_beats_stage_context_and_env_beats_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_cupy(monkeypatch)
    assert xpmod.stage_preference() is None
    with xpmod.stage_gpu(True):
        assert xpmod.stage_preference() is True
        # stage context is the default …
        assert xpmod.resolve_request() == (True, "stage")
        assert xpmod.resolve_backend().source == "stage"
        # … an explicit argument overrides it …
        assert xpmod.resolve_request(False) == (False, "argument")
        assert xpmod.resolve_backend(False).xp is np
        # … and the environment overrides both
        monkeypatch.setenv("WINTERSAR_GPU", "false")
        assert xpmod.resolve_request(True) == (False, "env")
        monkeypatch.delenv("WINTERSAR_GPU")
        # nested contexts restore the outer value
        with xpmod.stage_gpu("0"):
            assert xpmod.resolve_request() == (False, "stage")
        with xpmod.stage_gpu(None):
            assert xpmod.resolve_request() == (None, "auto")
        assert xpmod.stage_preference() is True
    assert xpmod.stage_preference() is None
    assert xpmod.resolve_request() == (None, "auto")


def test_stage_context_gpu_request_degrades_with_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    """``config.compute.gpu: true`` on a CPU-only machine: the executor still hands
    ``_gpu=True`` to the stage, which must run and report ENV-005."""
    _no_cupy(monkeypatch)
    with xpmod.stage_gpu(True):
        b = xpmod.resolve_backend()
    assert b.xp is np and b.degraded and b.source == "stage"
    assert b.findings[0].evidence["requested_by"] == "stage"


def test_invalid_flags() -> None:
    with pytest.raises(ValueError, match="gpu"):
        xpmod.get_xp("maybe")
    with pytest.raises(ValueError, match="WINTERSAR_GPU"):
        xpmod._parse_flag("2", source="WINTERSAR_GPU")
    with pytest.raises(ValueError, match="_gpu"), xpmod.stage_gpu("sometimes"):
        pass
    assert xpmod.coerce_request("cuda") is True and xpmod.coerce_request("cpu") is False
    assert xpmod.coerce_request("auto") is None and xpmod.coerce_request(None) is None


# ---------------------------------------------------------------------- helpers


def test_helpers_on_numpy_arrays() -> None:
    a = np.arange(6, dtype=np.float32).reshape(2, 3)
    assert xpmod.backend_name(np) == "numpy" and not xpmod.is_cupy_array(a)
    assert xpmod.xp_of(a) is np
    assert xpmod.to_numpy(a) is not None and np.shares_memory(xpmod.to_numpy(a), a)
    b = xpmod.asarray(a, dtype=np.float64)
    assert b.dtype == np.float64 and xpmod.asarray([1, 2], np).dtype.kind == "i"
    assert xpmod.device_info(np) == {"backend": "numpy"}
    xpmod.synchronize(np)  # no-op on the CPU
    assert xpmod.Backend(np, "numpy", None, "auto", False).is_gpu is False
