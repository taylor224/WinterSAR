"""tophu adapter — multi-scale tiled 2-D unwrapping (plan §5.2 "tophu.py", §5.4 item 3).

Verified API (ADR-0024, tophu v0.2.1 ``src/tophu/_multiscale.py``)::

    tophu.multiscale_unwrap(
        unwrapped, conncomp, igram, coherence, nlooks, unwrap_func,
        downsample_factor, ntiles, min_conncomp_overlap=0.5, scratchdir=None, *,
        do_lowpass_filter=True, shape_factor=1.5, overhang=0.5, ripple=0.01, attenuation=40.0,
    ) -> None

``unwrapped``/``conncomp`` are ``DatasetWriter`` objects (numpy arrays qualify; float32 /
uint32 as in tophu's own tests) that are filled in place. ``unwrap_func`` follows the
``UnwrapCallback`` protocol ``(igram, coherence, nlooks, scratchdir) -> (unw, conncomp)``;
tophu ships ``SnaphuUnwrap(cost, cost_params, init_method)``, ``ICUUnwrap()`` and
``PhassUnwrap()`` (all backed by isce3, which tophu imports at module level — it is only on
conda-forge, not PyPI). wintersar adds a ``snaphu-py`` callback that needs no isce3 unwrapper.

Masking: tophu has no mask argument; masked pixels are zeroed in both the interferogram and
the coherence before the call (the approach dolphin uses, ``zero_where_masked``) and set to
NaN / label 0 afterwards.
"""

from __future__ import annotations

import importlib
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from numpy.typing import NDArray

from wintersar.engines._unwrap_common import (
    BoolArray,
    EngineLog,
    FloatArray,
    UnwrapEngineBase,
    UnwrapError,
    UnwrapResult,
    build_masked,
    clean_coherence,
    conncomp_masked,
    conncomp_stats,
    make_finding,
    nan_masked,
    resolve_nlooks,
    resolve_tiles,
    to_complex64,
    unwrap_cfg,
)
from wintersar.engines.base import python_module_version, register_engine
from wintersar.engines.snaphu import SNAPHU_PY_COST_MODES, snaphu_py_available
from wintersar.io.schemas import Finding

# source: https://github.com/isce-framework/tophu/releases (v0.2.1, 2024-02-14; __version__ "0.2.0"
# on main) and conda-forge tophu-feedstock (tophu 0.2.1, run: isce3 >=0.12, dask, h5py, rasterio)
TOPHU_CONSTRAINT = ">=0.2,<1"

# wintersar choice, not an upstream default: tophu's own tests use (3, 3); dolphin's
# TophuOptions default is (1, 1) (no coarse level). Recorded in ADR-0024 / open-questions.
DEFAULT_DOWNSAMPLE_FACTOR: tuple[int, int] = (3, 3)
# source: tophu v0.2.1 _multiscale.py signature default
DEFAULT_MIN_CONNCOMP_OVERLAP = 0.5

UNWRAP_FUNCS = ("auto", "snaphu", "snaphu-py", "icu", "phass")

UnwrapCallable = Callable[..., tuple[NDArray[Any], NDArray[Any]]]


class SnaphuPyCallback:
    """``tophu.UnwrapCallback``-compatible callable backed by snaphu-py (no isce3 unwrapper).

    Protocol (tophu v0.2.1 ``_unwrap.py``): ``__call__(igram, coherence, nlooks, scratchdir)``
    returning ``(unwphase, conncomp)``.
    """

    def __init__(self, cost: str = "smooth", init: str = "mcf", **snaphu_kwargs: Any) -> None:
        if cost not in SNAPHU_PY_COST_MODES:
            raise UnwrapError.from_rule("UNW-004", cost=cost)
        self.cost = cost
        self.init = init
        self.snaphu_kwargs = snaphu_kwargs

    def __call__(
        self,
        igram: NDArray[np.complexfloating[Any, Any]],
        coherence: NDArray[np.floating[Any]],
        nlooks: float,
        scratchdir: Path,
    ) -> tuple[NDArray[np.floating[Any]], NDArray[np.unsignedinteger[Any]]]:
        snaphu = importlib.import_module("snaphu")
        # source: snaphu-py v0.4.1 unwrap() keyword names (ADR-0023)
        unw, cc = snaphu.unwrap(
            igram=np.asarray(igram, dtype=np.complex64),
            corr=np.asarray(coherence, dtype=np.float32),
            nlooks=float(nlooks),
            cost=self.cost,
            init=self.init,
            scratchdir=str(scratchdir),
            delete_scratch=False,
            **self.snaphu_kwargs,
        )
        return np.asarray(unw, dtype=np.float32), np.asarray(cc, dtype=np.uint32)


def make_unwrap_func(tophu: Any, cfg: Mapping[str, Any]) -> tuple[Any, str]:
    """Build the ``unwrap_func`` for :func:`tophu.multiscale_unwrap` from ``cfg``.

    ``tophu_unwrap_func``: ``auto`` (tophu's ``SnaphuUnwrap``; falls back to the snaphu-py
    callback when isce3's SNAPHU is missing but snaphu-py is importable), ``snaphu``,
    ``snaphu-py``, ``icu`` or ``phass``.
    """
    name = str(cfg.get("tophu_unwrap_func", "auto"))
    cost = str(cfg.get("cost", "defo"))
    init = str(cfg.get("init", "mcf"))
    if name not in UNWRAP_FUNCS:
        raise UnwrapError.from_rule("UNW-011", name=name, available=", ".join(UNWRAP_FUNCS))
    snaphu_cls = getattr(tophu, "SnaphuUnwrap", None)
    icu_cls = getattr(tophu, "ICUUnwrap", None)
    phass_cls = getattr(tophu, "PhassUnwrap", None)
    if name == "icu":
        if icu_cls is None:
            raise UnwrapError.from_rule("UNW-011", name=name, available=", ".join(UNWRAP_FUNCS))
        return icu_cls(), "icu"
    if name == "phass":
        if phass_cls is None:
            raise UnwrapError.from_rule("UNW-011", name=name, available=", ".join(UNWRAP_FUNCS))
        return phass_cls(), "phass"
    if name == "snaphu-py" or (name == "auto" and snaphu_cls is None):
        if not snaphu_py_available():
            raise UnwrapError.from_rule(
                "UNW-011", name="snaphu-py", available=", ".join(UNWRAP_FUNCS)
            )
        return SnaphuPyCallback(cost=cost, init=init), "snaphu-py"
    if snaphu_cls is None:
        raise UnwrapError.from_rule("UNW-011", name=name, available=", ".join(UNWRAP_FUNCS))
    # source: tophu v0.2.1 _unwrap.py SnaphuUnwrap.__init__(cost, cost_params, init_method)
    return snaphu_cls(cost=cost, init_method=init), "snaphu"


@register_engine
class TophuEngine(UnwrapEngineBase):
    name: ClassVar[str] = "tophu"
    version_constraint: ClassVar[str] = TOPHU_CONSTRAINT
    diagnose_engine: ClassVar[str | None] = "snaphu"  # tophu's unwrap_func logs are SNAPHU's
    install_hint: ClassVar[str] = (
        "conda install -c conda-forge tophu  (depends on isce3; conda-forge only, no PyPI wheel)"
    )
    # source: tophu README + LICENSE-Apache-2.0 / LICENSE-BSD-3-Clause, setup.cfg
    # license = "BSD-3-Clause OR Apache-2.0" (v0.2.1)
    license_note: ClassVar[str] = "BSD-3-Clause OR Apache-2.0 (tophu v0.2.1; runtime dep isce3)"

    def detect_version(self) -> str | None:
        return python_module_version("tophu")

    # ------------------------------------------------------------------ unwrap
    def unwrap(
        self,
        igram: FloatArray,
        coh: FloatArray,
        mask: BoolArray | None,
        params: Mapping[str, Any],
    ) -> UnwrapResult:
        self.require_available()
        cfg = unwrap_cfg(params)
        tophu = importlib.import_module("tophu")
        igram = np.asarray(igram)
        coh = np.asarray(coh)
        if igram.ndim != 2 or coh.shape != igram.shape:
            msg = f"igram must be 2-D and coh the same shape, got {igram.shape} / {coh.shape}"
            raise ValueError(msg)
        shape = (int(igram.shape[0]), int(igram.shape[1]))
        log = EngineLog(Path(str(cfg["_log_path"]))) if cfg.get("_log_path") else None
        mask_cfg: Mapping[str, Any] = cfg["mask"] if isinstance(cfg.get("mask"), Mapping) else {}
        masked = build_masked(
            igram,
            coh,
            mask,
            float(cfg.get("coherence_threshold", 0.3)),
            use_coherence=bool(mask_cfg.get("coherence", True)),
        )
        nlooks, nlooks_src = resolve_nlooks(cfg, cfg.get("_igram_attrs"))
        tiles = resolve_tiles(cfg, shape)
        findings: list[Finding] = []
        if nlooks_src == "default":
            findings.append(make_finding("UNW-007", severity="WARN", nlooks=nlooks))
        unwrap_func, func_name = make_unwrap_func(tophu, cfg)
        ds = cfg.get("downsample_factor", DEFAULT_DOWNSAMPLE_FACTOR)
        downsample = (
            (int(ds[0]), int(ds[1])) if isinstance(ds, list | tuple) else (int(ds), int(ds))
        )
        scratch, scratch_is_temp = self._scratch_dir(cfg)
        c64 = to_complex64(igram, coh, masked)
        corr = clean_coherence(coh, masked)
        unw = np.zeros(shape, dtype=np.float32)
        cc = np.zeros(shape, dtype=np.uint32)
        # source: tophu v0.2.1 src/tophu/_multiscale.py multiscale_unwrap() parameter names
        kwargs: dict[str, Any] = {
            "unwrapped": unw,
            "conncomp": cc,
            "igram": c64,
            "coherence": corr,
            "nlooks": float(nlooks),
            "unwrap_func": unwrap_func,
            "downsample_factor": downsample,
            "ntiles": tiles.ntiles,
            "min_conncomp_overlap": float(
                cfg.get("min_conncomp_overlap", DEFAULT_MIN_CONNCOMP_OVERLAP)
            ),
            "scratchdir": str(scratch),
        }
        if "do_lowpass_filter" in cfg:
            kwargs["do_lowpass_filter"] = bool(cfg["do_lowpass_filter"])
        if log is not None:
            for f in findings:
                log.finding(f)
            log.write(
                f"tophu multiscale_unwrap unwrap_func={func_name} downsample_factor={downsample} "
                f"ntiles={tiles.ntiles} nlooks={nlooks} scratchdir={scratch}"
            )
        t0 = time.perf_counter()
        tophu.multiscale_unwrap(**kwargs)
        wall = time.perf_counter() - t0
        unw_out = nan_masked(unw, masked)
        cc_out = conncomp_masked(cc, masked)
        stats: dict[str, Any] = {
            "engine": self.name,
            "backend": f"tophu/{func_name}",
            "tophu_version": self.detect_version(),
            "wall_time_s": round(wall, 3),
            "cost": cfg.get("cost"),
            "init": cfg.get("init"),
            "nlooks": nlooks,
            "nlooks_source": nlooks_src,
            "coherence_threshold": float(cfg.get("coherence_threshold", 0.3)),
            "masked_fraction": float(masked.mean()),
            "ntiles": list(tiles.ntiles),
            "downsample_factor": list(downsample),
            "findings": [f.rule_id for f in findings],
            **conncomp_stats(cc_out),
        }
        if tiles.tiled:
            stats["tile_dir"] = None if scratch_is_temp else str(scratch)
            stats["assemble_only_capable"] = False  # tophu has no SNAPHU assemble-only path
        if scratch_is_temp and bool(cfg.get("delete_scratch", True)):
            shutil.rmtree(scratch, ignore_errors=True)
        return UnwrapResult(unw=unw_out, conncomp=cc_out, stats=stats)

    @staticmethod
    def _scratch_dir(cfg: Mapping[str, Any]) -> tuple[Path, bool]:
        raw = cfg.get("_scratch_dir") or cfg.get("scratch_dir")
        if raw:
            p = Path(str(raw))
            p.mkdir(parents=True, exist_ok=True)
            return p, False
        return Path(tempfile.mkdtemp(prefix="wintersar-tophu-")), True
