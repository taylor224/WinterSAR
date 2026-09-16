"""Unwrapper protocol, test backends and the engine lookup (plan §5.2 signature)."""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
import pytest

from wintersar.engines.base import Engine, register_engine
from wintersar.unwrap import backends
from wintersar.unwrap.backends import (
    IdentityUnwrapper,
    TruthUnwrapper,
    Unwrapper,
    UnwrapResult,
    call_unwrapper,
    coerce_result,
    get_unwrapper,
    wrapped_phase,
)


def test_wrapped_phase_complex_and_float():
    z = np.exp(1j * np.array([[0.5, -2.0]])).astype(np.complex64)
    np.testing.assert_allclose(wrapped_phase(z), [[0.5, -2.0]], atol=1e-6)
    f = np.array([[0.5, -2.0]], dtype=np.float64)
    assert wrapped_phase(f).dtype == np.float32


def test_truth_backend_returns_truth_with_mask_nan():
    truth = np.arange(12, dtype=np.float32).reshape(3, 4)
    mask = np.zeros((3, 4), dtype=bool)
    mask[0, 0] = True
    res = TruthUnwrapper().unwrap(
        np.zeros((3, 4), np.float32), np.ones((3, 4)), mask, {"_truth": truth}
    )
    assert isinstance(res, UnwrapResult)
    assert np.isnan(res.unw[0, 0]) and res.conncomp[0, 0] == 0
    assert res.unw[1, 1] == 5.0 and res.conncomp[1, 1] == 1
    assert res.unw.dtype == np.float32 and res.conncomp.dtype == np.uint16
    off = TruthUnwrapper().unwrap(
        np.zeros((3, 4)), np.ones((3, 4)), None, {"_truth": truth, "_offset_cycles": 1}
    )
    np.testing.assert_allclose(off.unw, truth + 2 * np.pi, rtol=1e-6)
    with pytest.raises(ValueError):
        TruthUnwrapper().unwrap(np.zeros((3, 4)), np.ones((3, 4)), None, {})
    with pytest.raises(ValueError):
        TruthUnwrapper().unwrap(
            np.zeros((3, 4)), np.ones((3, 4)), None, {"_truth": np.zeros((2, 2))}
        )


def test_identity_backend_returns_wrapped_phase():
    w = np.array([[0.1, -3.0], [2.0, 0.0]], dtype=np.float32)
    mask = np.array([[False, True], [False, False]])
    res = IdentityUnwrapper().unwrap(np.exp(1j * w), np.ones((2, 2)), mask, {})
    np.testing.assert_allclose(res.unw[0, 0], 0.1, atol=1e-6)
    assert np.isnan(res.unw[0, 1]) and res.conncomp[0, 1] == 0
    assert isinstance(IdentityUnwrapper(), Unwrapper)
    assert isinstance(TruthUnwrapper(), Unwrapper)


def test_local_registry_and_lookup():
    assert backends.list_local_backends() == ["identity", "truth"]
    assert backends.is_test_backend("truth") and not backends.is_test_backend("snaphu")
    assert isinstance(get_unwrapper("truth"), TruthUnwrapper)
    assert isinstance(get_unwrapper("identity"), IdentityUnwrapper)
    with pytest.raises(KeyError):
        get_unwrapper("does-not-exist")
    # the fake engine is registered but has no ``unwrap`` method
    with pytest.raises(TypeError):
        get_unwrapper("fake")


def test_engine_with_unwrap_is_accepted_and_tuple_result_coerced():
    @register_engine
    class _TupleUnwrapEngine(Engine):
        name: ClassVar[str] = "_test_tuple_unwrapper"
        stages: ClassVar[tuple[str, ...]] = ("unwrap",)

        def detect_version(self) -> str | None:
            return "1.0"

        def run(self, stage: str, inputs: Any, params: dict[str, Any], log_dir: Any) -> Any:
            raise NotImplementedError

        def unwrap(self, igram: Any, coh: Any, mask: Any, params: dict[str, Any]) -> Any:
            unw = np.zeros(np.shape(igram), dtype=np.float64)
            return unw, np.ones(np.shape(igram), dtype=np.int32), {"tile_dir": "/tmp/x"}

    u = get_unwrapper("_test_tuple_unwrapper")
    assert u.name == "_test_tuple_unwrapper"
    res = call_unwrapper(u, np.zeros((2, 2), np.float32), np.ones((2, 2)), None, {})
    assert res.unw.dtype == np.float32 and res.conncomp.dtype.kind in "ui"
    assert res.stats["tile_dir"] == "/tmp/x"
    assert "_test_tuple_unwrapper" in backends.available_backends(("_test_tuple_unwrapper", "nope"))
    backends.require_available(u)  # version "1.0" satisfies "*"


def test_coerce_result_and_shape_validation():
    with pytest.raises(TypeError):
        coerce_result("nope")
    res = coerce_result((np.zeros((2, 2)), np.zeros((2, 2)), None))
    assert res.stats == {}

    class _Bad:
        name = "bad"

        def unwrap(self, igram, coh, mask, params):
            return UnwrapResult(np.zeros((1, 1), np.float32), np.zeros((1, 1), np.uint8))

    with pytest.raises(ValueError):
        call_unwrapper(_Bad(), np.zeros((2, 2)), np.ones((2, 2)), None, {})  # type: ignore[arg-type]


def test_available_backends_skips_missing_adapters():
    # snaphu/tophu/spurt adapters may or may not be written yet; the probe must not raise
    names = backends.available_backends()
    assert all(n in backends.ENGINE_BACKENDS for n in names)


def test_duck_typed_result_from_engine_common_module_is_accepted():
    """Adapters return ``wintersar.engines._unwrap_common.UnwrapResult`` (another class)."""
    from dataclasses import dataclass

    @dataclass
    class _OtherResult:
        unw: np.ndarray
        conncomp: np.ndarray
        stats: dict

    res = coerce_result(
        _OtherResult(np.zeros((2, 2)), np.ones((2, 2), np.uint32), {"tile_dir": "d"})
    )
    assert isinstance(res, UnwrapResult) and res.unw.dtype == np.float32
    assert res.stats == {"tile_dir": "d"}


def test_native_tiles_default_engine_true_test_backend_false():
    class _Engine:
        name = "some_engine"

        def unwrap(self, igram, coh, mask, params):
            raise NotImplementedError

    assert backends.supports_native_tiles(_Engine()) is True  # type: ignore[arg-type]
    assert backends.supports_native_tiles(TruthUnwrapper()) is False
    _Engine.supports_tiles = False  # type: ignore[attr-defined]
    assert backends.supports_native_tiles(_Engine()) is False  # type: ignore[arg-type]
