"""Unwrapper backend contract (plan §5.2 / §5.4, R-06).

Every phase-unwrapping engine adapter (``snaphu``, ``tophu``, ``spurt``) exposes the same
method::

    unwrap(igram, coh, mask, params) -> UnwrapResult

``igram`` is either a complex64 interferogram or the wrapped float32 phase, ``coh`` the
coherence in [0, 1], ``mask`` a boolean array (``True`` = masked out) or ``None`` and
``params`` the canonical stage parameters (``cost``, ``init``, ``ntiles``, ``tile_overlap``,
``nproc`` …) plus executor private keys (``_``-prefixed). Masked pixels come back as NaN in
``unw`` and ``0`` in ``conncomp`` (SARscape behaviour, plan §5.4).

The scheduler never re-implements an unwrapper (rule 11.3). Two **test-only** backends live
here so that the scheduler, tiling and CLI can be exercised without SNAPHU:

* ``truth`` — returns the synthetic ground truth passed as ``params["_truth"]``,
* ``identity`` — returns the wrapped phase unchanged.

They are kept in a local registry, not in :mod:`wintersar.engines.base`, so that
``wintersar check-install`` never lists them as engines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, cast, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from wintersar.engines.base import EngineNotAvailableError, get_engine

TWO_PI = 2.0 * np.pi

__all__ = [
    "IdentityUnwrapper",
    "TruthUnwrapper",
    "UnwrapResult",
    "Unwrapper",
    "available_backends",
    "call_unwrapper",
    "coerce_result",
    "get_unwrapper",
    "is_test_backend",
    "list_local_backends",
    "wrapped_phase",
]


@dataclass
class UnwrapResult:
    """Output of one ``unwrap`` call.

    ``unw``: float32 unwrapped phase (radians) with NaN where masked.
    ``conncomp``: unsigned integer connected-component labels (0 = masked / no component).
    ``stats``: backend statistics; a backend that keeps SNAPHU tile scratch files for
    assemble-only re-use (PERF-03) reports the directory under ``stats["tile_dir"]``.
    """

    unw: NDArray[np.float32]
    conncomp: NDArray[np.integer[Any]]
    stats: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Unwrapper(Protocol):
    """Structural type shared by engine adapters and the test backends."""

    name: ClassVar[str]

    def unwrap(
        self,
        igram: NDArray[Any],
        coh: NDArray[Any],
        mask: NDArray[np.bool_] | None,
        params: dict[str, Any],
    ) -> UnwrapResult: ...


def wrapped_phase(igram: NDArray[Any]) -> NDArray[np.float32]:
    """Wrapped phase (float32, radians) of a complex or already-wrapped input."""
    arr = np.asarray(igram)
    if np.iscomplexobj(arr):
        return np.asarray(np.angle(arr), dtype=np.float32)
    return arr.astype(np.float32, copy=False)


def _bool_mask(mask: NDArray[np.bool_] | None, shape: tuple[int, ...]) -> NDArray[np.bool_]:
    if mask is None:
        return np.zeros(shape, dtype=bool)
    m = np.asarray(mask, dtype=bool)
    if m.shape != shape:
        msg = f"mask shape {m.shape} != igram shape {shape}"
        raise ValueError(msg)
    return m


class TruthUnwrapper:
    """Test-only backend: returns ``params['_truth']`` (the synthetic ground truth).

    ``params['_offset_cycles']`` (float, default 0) adds that many 2π cycles to the whole
    output; the executor uses it to inject tile-seam offsets for the jump detector tests.
    """

    name: ClassVar[str] = "truth"
    supports_tiles: ClassVar[bool] = False
    test_only: ClassVar[bool] = True

    def unwrap(
        self,
        igram: NDArray[Any],
        coh: NDArray[Any],
        mask: NDArray[np.bool_] | None,
        params: dict[str, Any],
    ) -> UnwrapResult:
        truth = params.get("_truth")
        if truth is None:
            msg = "TruthUnwrapper requires params['_truth'] (synthetic unwrapped phase)"
            raise ValueError(msg)
        unw = np.array(truth, dtype=np.float32, copy=True)
        shape = tuple(np.asarray(igram).shape)
        if unw.shape != shape:
            msg = f"_truth shape {unw.shape} != igram shape {shape}"
            raise ValueError(msg)
        offset = float(params.get("_offset_cycles", 0.0) or 0.0)
        if offset:
            unw = (unw + np.float32(offset * TWO_PI)).astype(np.float32)
        m = _bool_mask(mask, shape)
        unw[m] = np.nan
        conncomp = np.where(m, 0, 1).astype(np.uint16)
        return UnwrapResult(unw=unw, conncomp=conncomp, stats={"backend": self.name})


class IdentityUnwrapper:
    """Test-only backend: the wrapped phase is returned as if it were unwrapped."""

    name: ClassVar[str] = "identity"
    supports_tiles: ClassVar[bool] = False
    test_only: ClassVar[bool] = True

    def unwrap(
        self,
        igram: NDArray[Any],
        coh: NDArray[Any],
        mask: NDArray[np.bool_] | None,
        params: dict[str, Any],
    ) -> UnwrapResult:
        unw = np.array(wrapped_phase(igram), copy=True)
        m = _bool_mask(mask, unw.shape)
        unw[m] = np.nan
        conncomp = np.where(m, 0, 1).astype(np.uint16)
        return UnwrapResult(unw=unw, conncomp=conncomp, stats={"backend": self.name})


_LOCAL_REGISTRY: dict[str, type[Unwrapper]] = {
    TruthUnwrapper.name: TruthUnwrapper,
    IdentityUnwrapper.name: IdentityUnwrapper,
}

ENGINE_BACKENDS: tuple[str, ...] = ("snaphu", "tophu", "spurt")


def list_local_backends() -> list[str]:
    return sorted(_LOCAL_REGISTRY)


def is_test_backend(name: str) -> bool:
    return name in _LOCAL_REGISTRY


def supports_native_tiles(unwrapper: Unwrapper) -> bool:
    """Whether the backend tiles by itself from ``params["ntiles"]`` / ``["tile_overlap"]``.

    Engine adapters (snaphu-py ``ntiles``, tophu ``ntiles``) do unless they set
    ``supports_tiles = False``; the test backends do not, so the executor tiles for them.
    """
    default = not is_test_backend(unwrapper.name)
    return bool(getattr(unwrapper, "supports_tiles", default))


def get_unwrapper(name: str) -> Unwrapper:
    """Resolve ``name`` to an object with ``unwrap(igram, coh, mask, params)``.

    Test backends come from the local registry; anything else is looked up with
    :func:`wintersar.engines.base.get_engine` and must expose ``unwrap``.
    """
    local = _LOCAL_REGISTRY.get(name)
    if local is not None:
        return local()
    try:
        engine = get_engine(name)
    except KeyError as e:
        msg = f"unknown unwrap backend {name!r}; test backends: {list_local_backends()}"
        raise KeyError(msg) from e
    fn = getattr(engine, "unwrap", None)
    if not callable(fn):
        msg = f"engine {name!r} does not implement unwrap(igram, coh, mask, params)"
        raise TypeError(msg)
    return cast(Unwrapper, engine)


def require_available(unwrapper: Unwrapper) -> None:
    """Raise :class:`EngineNotAvailableError` when an engine backend is not installed."""
    check = getattr(unwrapper, "require_available", None)
    if callable(check):
        check()


def available_backends(candidates: tuple[str, ...] = ENGINE_BACKENDS) -> list[str]:
    """Engine backends that are registered, installed and implement ``unwrap``."""
    out: list[str] = []
    for name in candidates:
        try:
            eng = get_unwrapper(name)
        except (KeyError, TypeError):
            continue
        is_available = getattr(eng, "is_available", None)
        try:
            ok = bool(is_available()) if callable(is_available) else True
        except Exception:  # adapter probing must never break planning
            ok = False
        if ok:
            out.append(name)
    return out


def coerce_result(obj: Any) -> UnwrapResult:
    """Accept ``UnwrapResult`` (this class or any duck-typed equivalent such as
    ``wintersar.engines._unwrap_common.UnwrapResult``) or the plan §5.2 tuple
    ``(unw, conncomp, stats)``."""
    if isinstance(obj, UnwrapResult):
        return obj
    if hasattr(obj, "unw") and hasattr(obj, "conncomp"):
        return UnwrapResult(
            unw=np.asarray(obj.unw, dtype=np.float32),
            conncomp=np.asarray(obj.conncomp),
            stats=dict(getattr(obj, "stats", None) or {}),
        )
    if isinstance(obj, tuple | list) and len(obj) == 3:
        unw, conncomp, stats = obj
        return UnwrapResult(
            unw=np.asarray(unw, dtype=np.float32),
            conncomp=np.asarray(conncomp),
            stats=dict(stats or {}),
        )
    msg = f"unwrap backend returned {type(obj).__name__}, expected UnwrapResult or a 3-tuple"
    raise TypeError(msg)


def call_unwrapper(
    unwrapper: Unwrapper,
    igram: NDArray[Any],
    coh: NDArray[Any],
    mask: NDArray[np.bool_] | None,
    params: dict[str, Any],
) -> UnwrapResult:
    """Call ``unwrapper.unwrap`` and normalise dtypes/shapes of the result."""
    shape = tuple(np.asarray(igram).shape)
    res = coerce_result(unwrapper.unwrap(igram, coh, mask, params))
    unw = np.asarray(res.unw, dtype=np.float32)
    if unw.shape != shape:
        msg = f"backend {unwrapper.name!r} returned unw of shape {unw.shape}, expected {shape}"
        raise ValueError(msg)
    conncomp = np.asarray(res.conncomp)
    if conncomp.shape != shape:
        msg = f"backend {unwrapper.name!r} returned conncomp of shape {conncomp.shape}"
        raise ValueError(msg)
    if conncomp.dtype.kind not in "ui":
        conncomp = conncomp.astype(np.uint16)
    return UnwrapResult(unw=unw, conncomp=conncomp, stats=dict(res.stats))


__all__ += [
    "ENGINE_BACKENDS",
    "EngineNotAvailableError",
    "require_available",
    "supports_native_tiles",
]
