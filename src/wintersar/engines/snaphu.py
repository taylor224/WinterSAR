"""SNAPHU adapter (plan §5.2 "snaphu.py", §5.4, §9; R-06, PERF-03/04).

Two backends, chosen per call (``backend: auto | snaphu-py | subprocess``):

* **snaphu-py** (``import snaphu``, permissive BSD-3/Apache-2 wrapper, conda-forge
  ``snaphu``): ``snaphu.unwrap(igram, corr, nlooks, cost, init, *, mask, ntiles,
  tile_overlap, nproc, tile_cost_thresh, min_region_size, single_tile_reoptimize,
  regrow_conncomps, scratchdir, delete_scratch, unw, conncomp)``. Parameter names verified
  in ADR-0023 (snaphu-py v0.4.1 ``src/snaphu/_unwrap.py``). It does **not** support
  ``cost="topo"``, assemble-only or ``COSTOUTFILE``.
* **subprocess**: a ``snaphu`` executable (snaphu-py's bundled binary, ``$WINTERSAR_SNAPHU_EXE``,
  ``unwrap.snaphu_executable`` or ``PATH``) driven by a generated configuration file
  (``snaphu -f <conf>``). Keywords come from ``snaphu.conf.full`` (SNAPHU v2.0.7) and are
  listed in :class:`SnaphuConfig`. This path adds ``topo`` cost, ``ASSEMBLEONLY``/``TILEDIR``
  re-assembly (PERF-03) and ``COSTOUTFILE``.

The SNAPHU C core is never bundled or re-implemented (rule 11.3, ADR-0001); its licence
mixes a permissive Stanford notice with a *non-commercial* cs2 solver clause (ADR-0023).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from numpy.typing import NDArray

from wintersar.engines._unwrap_common import (
    BoolArray,
    EngineLog,
    EngineRunError,
    FloatArray,
    TileSpec,
    UnwrapEngineBase,
    UnwrapResult,
    build_masked,
    clean_coherence,
    command_version,
    conncomp_masked,
    conncomp_stats,
    make_finding,
    nan_masked,
    resolve_nlooks,
    resolve_tiles,
    to_complex64,
    unwrap_cfg,
)
from wintersar.engines.base import (
    EngineNotAvailableError,
    python_module_version,
    register_engine,
    version_satisfies,
)
from wintersar.io.schemas import Finding

# source: https://github.com/isce-framework/snaphu-py/releases/tag/v0.4.1 (latest, 2024-09-16)
SNAPHU_PY_CONSTRAINT = ">=0.4,<1"
# source: https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/ (v2.0.7, Feb 2024);
# assemble-only became a boolean flag + TILEDIR in the 2.0 series (README_releasenotes.txt)
SNAPHU_EXE_CONSTRAINT = ">=2.0,<3"

# source: snaphu-py v0.4.1 src/snaphu/_snaphu.py get_snaphu_version(): regex on `snaphu -h`
_SNAPHU_HELP_VERSION = re.compile(r"^snaphu v(?P<version>[0-9]+(?:\.[0-9]+)*)$", re.MULTILINE)

# source: snaphu.conf.full — STATCOSTMODE {TOPO, DEFO, SMOOTH, NOSTATCOSTS}, INITMETHOD {MST, MCF}
_STATCOSTMODE = {"defo": "DEFO", "smooth": "SMOOTH", "topo": "TOPO"}
_INITMETHOD = {"mst": "MST", "mcf": "MCF"}
# source: snaphu-py v0.4.1 src/snaphu/_check.py check_cost_mode(): {"defo", "smooth"};
# "'topo' cost mode is not currently supported" (NotImplementedError)
SNAPHU_PY_COST_MODES = frozenset({"defo", "smooth"})

SNAPHU_PY_DEFAULTS: dict[str, Any] = {
    # source: snaphu-py v0.4.1 src/snaphu/_unwrap.py unwrap() signature defaults
    "min_conncomp_frac": 0.01,
    "tile_cost_thresh": 500,
    "min_region_size": 100,
    "single_tile_reoptimize": True,
    "regrow_conncomps": True,
}


def parse_snaphu_help_version(text: str) -> str | None:
    m = _SNAPHU_HELP_VERSION.search(text)
    return m.group("version") if m else None


# ---------------------------------------------------------------------------- executable


def snaphu_py_available() -> bool:
    """``import snaphu`` would succeed (without importing it yet)."""
    try:
        return importlib.util.find_spec("snaphu") is not None
    except (ImportError, ValueError):
        return False


def bundled_snaphu_executable() -> Path | None:
    """The ``snaphu`` binary shipped inside the snaphu-py wheel.

    source: snaphu-py v0.4.1 src/snaphu/_snaphu.py — ``importlib.resources.files(__package__)
    / "snaphu"`` (extracted with ``as_file``). We only use it when it is a real file.
    """
    if not snaphu_py_available():
        return None
    try:
        from importlib.resources import files

        candidate = files("snaphu") / "snaphu"
        p = Path(str(candidate))
    except (ImportError, TypeError, ValueError, OSError):
        return None
    if p.is_file() and os.access(p, os.X_OK):
        return p
    return None


def find_snaphu_executable(cfg: Mapping[str, Any] | None = None) -> Path | None:
    """Locate a SNAPHU executable: explicit param → env → snaphu-py bundle → ``PATH``."""
    cfg = cfg or {}
    for raw in (cfg.get("snaphu_executable"), os.environ.get("WINTERSAR_SNAPHU_EXE")):
        if raw:
            p = Path(str(raw)).expanduser()
            if p.is_file():
                return p
    bundled = bundled_snaphu_executable()
    if bundled is not None:
        return bundled
    which = shutil.which("snaphu")
    return Path(which) if which else None


# ---------------------------------------------------------------------------- config file


def _bool_kw(v: bool) -> str:
    return "TRUE" if v else "FALSE"


@dataclass
class SnaphuConfig:
    """Generated SNAPHU configuration file (``snaphu -f <file>``).

    Keyword names/values verified against
    https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/snaphu.conf.full (v2.0.7)
    and the snaphu(1) man page (Ubuntu noble, snaphu 2.0.6). Binary rasters are in the
    machine's native byte order (man page, FILE FORMATS).
    """

    infile: Path
    linelength: int
    outfile: Path
    corrfile: Path | None = None
    conncompfile: Path | None = None
    ncorrlooks: float = 1.0
    statcostmode: str = "DEFO"
    initmethod: str = "MCF"
    bytemaskfile: Path | None = None
    ntilerow: int = 1
    ntilecol: int = 1
    rowovrlp: int = 0
    colovrlp: int = 0
    nproc: int = 1
    tilecostthresh: int = int(SNAPHU_PY_DEFAULTS["tile_cost_thresh"])
    minregionsize: int = int(SNAPHU_PY_DEFAULTS["min_region_size"])
    tiledir: Path | None = None
    rmtmptile: bool | None = None
    assembleonly: bool = False
    singletilereoptimize: bool | None = None
    minconncompfrac: float | None = None
    conncompouttype: str = "UINT"
    costoutfile: Path | None = None
    costinfile: Path | None = None
    logfile: Path | None = None
    verbose: bool = True
    infileformat: str = "COMPLEX_DATA"
    corrfileformat: str = "FLOAT_DATA"
    outfileformat: str = "FLOAT_DATA"
    extra: dict[str, str] = field(default_factory=dict)

    def lines(self) -> list[tuple[str, str]]:
        """Ordered ``(KEYWORD, value)`` pairs (only keywords that are set)."""
        kv: list[tuple[str, str | None]] = [
            ("INFILE", str(self.infile)),
            ("INFILEFORMAT", self.infileformat),
            ("LINELENGTH", str(int(self.linelength))),
            ("OUTFILE", str(self.outfile)),
            ("OUTFILEFORMAT", self.outfileformat),
            ("CORRFILE", str(self.corrfile) if self.corrfile else None),
            ("CORRFILEFORMAT", self.corrfileformat if self.corrfile else None),
            ("NCORRLOOKS", repr(float(self.ncorrlooks))),
            ("STATCOSTMODE", self.statcostmode),
            ("INITMETHOD", self.initmethod),
            ("BYTEMASKFILE", str(self.bytemaskfile) if self.bytemaskfile else None),
            ("CONNCOMPFILE", str(self.conncompfile) if self.conncompfile else None),
            ("CONNCOMPOUTTYPE", self.conncompouttype if self.conncompfile else None),
            (
                "MINCONNCOMPFRAC",
                repr(float(self.minconncompfrac)) if self.minconncompfrac is not None else None,
            ),
            ("NTILEROW", str(int(self.ntilerow))),
            ("NTILECOL", str(int(self.ntilecol))),
            ("ROWOVRLP", str(int(self.rowovrlp))),
            ("COLOVRLP", str(int(self.colovrlp))),
            ("NPROC", str(int(self.nproc))),
            ("TILECOSTTHRESH", str(int(self.tilecostthresh))),
            ("MINREGIONSIZE", str(int(self.minregionsize))),
            ("TILEDIR", str(self.tiledir) if self.tiledir else None),
            ("RMTMPTILE", _bool_kw(self.rmtmptile) if self.rmtmptile is not None else None),
            ("ASSEMBLEONLY", _bool_kw(self.assembleonly) if self.assembleonly else None),
            (
                "SINGLETILEREOPTIMIZE",
                _bool_kw(self.singletilereoptimize)
                if self.singletilereoptimize is not None
                else None,
            ),
            ("COSTOUTFILE", str(self.costoutfile) if self.costoutfile else None),
            ("COSTINFILE", str(self.costinfile) if self.costinfile else None),
            ("LOGFILE", str(self.logfile) if self.logfile else None),
            ("VERBOSE", _bool_kw(self.verbose)),
        ]
        out = [(k, v) for k, v in kv if v is not None]
        out.extend((k.upper(), str(v)) for k, v in self.extra.items())
        return out

    def render(self) -> str:
        head = "# generated by wintersar (snaphu adapter); keywords per snaphu.conf.full v2.0.7\n"
        return head + "".join(f"{k}\t{v}\n" for k, v in self.lines())

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")
        return path

    @classmethod
    def parse(cls, text: str) -> dict[str, str]:
        """Read back ``KEYWORD value`` lines (comment lines start with a non-alnum char)."""
        out: dict[str, str] = {}
        for line in text.splitlines():
            s = line.strip()
            if not s or not s[0].isalnum():
                continue
            parts = s.split(None, 1)
            out[parts[0].upper()] = parts[1].strip() if len(parts) > 1 else ""
        return out


def build_snaphu_config(
    cfg: Mapping[str, Any],
    scratch: Path,
    shape: tuple[int, int],
    nlooks: float,
    tiles: TileSpec,
    has_mask: bool,
) -> SnaphuConfig:
    """Map wintersar ``UnwrapCfg`` (+ adapter extras) onto SNAPHU keywords."""
    _ny, nx = shape
    cost = str(cfg.get("cost", "defo"))
    init = str(cfg.get("init", "mcf"))
    if cost not in _STATCOSTMODE or init not in _INITMETHOD:
        msg = f"unsupported cost/init: {cost!r}/{init!r}"
        raise ValueError(msg)
    assemble_only = bool(cfg.get("assemble_only", False))
    tile_dir = Path(str(cfg["tile_dir"])) if cfg.get("tile_dir") else scratch / "tiles"
    keep = bool(cfg.get("keep_tile_dir", True))
    conf = SnaphuConfig(
        infile=scratch / "igram.c64",
        linelength=nx,
        outfile=scratch / "unw.f32",
        corrfile=scratch / "corr.f32",
        conncompfile=scratch / "conncomp.u32",
        ncorrlooks=float(nlooks),
        statcostmode=_STATCOSTMODE[cost],
        initmethod=_INITMETHOD[init],
        bytemaskfile=(scratch / "mask.i8") if has_mask else None,
        ntilerow=tiles.rows,
        ntilecol=tiles.cols,
        rowovrlp=tiles.overlap_px[0],
        colovrlp=tiles.overlap_px[1],
        nproc=max(int(cfg.get("nproc_per_igram", 1) or 1), 1),
        tilecostthresh=int(cfg.get("tile_cost_thresh", SNAPHU_PY_DEFAULTS["tile_cost_thresh"])),
        minregionsize=int(cfg.get("min_region_size", SNAPHU_PY_DEFAULTS["min_region_size"])),
        minconncompfrac=float(
            cfg.get("min_conncomp_frac", SNAPHU_PY_DEFAULTS["min_conncomp_frac"])
        ),
        logfile=scratch / "snaphu.log",
        verbose=True,
    )
    if tiles.tiled or assemble_only:
        conf.tiledir = tile_dir
        conf.rmtmptile = not keep
        conf.assembleonly = assemble_only
        stro = cfg.get("single_tile_reoptimize", SNAPHU_PY_DEFAULTS["single_tile_reoptimize"])
        conf.singletilereoptimize = bool(stro)
    if cfg.get("save_cost_file"):
        conf.costoutfile = scratch / "costs.bin"
    if cfg.get("cost_in_file"):
        conf.costinfile = Path(str(cfg["cost_in_file"]))
    extra = cfg.get("snaphu_extra_config")
    if isinstance(extra, Mapping):
        conf.extra = {str(k): str(v) for k, v in extra.items()}
    return conf


# ---------------------------------------------------------------------------- engine


@register_engine
class SnaphuEngine(UnwrapEngineBase):
    name: ClassVar[str] = "snaphu"
    version_constraint: ClassVar[str] = SNAPHU_PY_CONSTRAINT
    diagnose_engine: ClassVar[str | None] = "snaphu"
    install_hint: ClassVar[str] = (
        "conda install -c conda-forge snaphu  (snaphu-py; or: pip install snaphu). "
        "A bare `snaphu` executable on PATH is also accepted (subprocess backend)."
    )
    # source: snaphu-py README + LICENSE-Apache-2.0 / LICENSE-BSD-3-Clause;
    # conda-forge snaphu-feedstock license "(Apache-2.0 OR BSD-3-Clause) AND LicenseRef-SNAPHU";
    # SNAPHU README (Stanford): permissive notice, cs2 solver "strictly noncommercial" (ADR-0023)
    license_note: ClassVar[str] = (
        "snaphu-py: BSD-3-Clause OR Apache-2.0; SNAPHU C core: Stanford notice + cs2 MCF "
        "solver non-commercial clause (LicenseRef-SNAPHU) — not bundled, commercial use needs review"
    )

    # ------------------------------------------------------------------ discovery
    def detect_version(self) -> str | None:
        v = python_module_version("snaphu")
        if v:
            return v
        exe = find_snaphu_executable()
        if exe is None:
            return None
        return command_version([str(exe)], ("-h",), parser=parse_snaphu_help_version)

    def check_install(self) -> list[Finding]:
        """ENV-001 when nothing is found; ENV-002 against the backend-specific range."""
        if snaphu_py_available():
            return super().check_install()
        exe = find_snaphu_executable()
        if exe is None:
            return super().check_install()  # -> ENV-001
        version = (
            command_version([str(exe)], ("-h",), parser=parse_snaphu_help_version) or "unknown"
        )
        findings = [
            make_finding("UNW-013", severity="INFO", path=str(exe), version=version),
        ]
        if not version_satisfies(version, SNAPHU_EXE_CONSTRAINT):
            findings.append(
                Finding(
                    rule_id="ENV-002",
                    severity="WARN",
                    message_key="env.ENV-002.cause",
                    fix_key="env.ENV-002.fix",
                    params={
                        "engine": self.name,
                        "found": version,
                        "constraint": SNAPHU_EXE_CONSTRAINT,
                    },
                    evidence={"engine": self.name, "version": version, "executable": str(exe)},
                )
            )
        return findings

    # ------------------------------------------------------------------ backend
    def select_backend(self, cfg: Mapping[str, Any]) -> tuple[str, Path | None]:
        """``("snaphu-py", None)`` or ``("subprocess", exe)``; raises when impossible."""
        requested = str(cfg.get("backend", "auto"))
        cost = str(cfg.get("cost", "defo"))
        needs_exe = (
            cost not in SNAPHU_PY_COST_MODES
            or bool(cfg.get("assemble_only"))
            or bool(cfg.get("save_cost_file"))
            or bool(cfg.get("cost_in_file"))
            or bool(cfg.get("snaphu_extra_config"))
        )
        have_py = snaphu_py_available()
        exe = find_snaphu_executable(cfg)
        if requested == "snaphu-py":
            if not have_py:
                raise EngineNotAvailableError(self._not_available_msg())
            if needs_exe:
                raise EngineRunError.from_rule("UNW-004", cost=cost)
            return "snaphu-py", None
        if requested == "subprocess":
            if exe is None:
                raise EngineNotAvailableError(self._not_available_msg())
            return "subprocess", exe
        if requested != "auto":
            msg = f"unknown snaphu backend {requested!r} (auto | snaphu-py | subprocess)"
            raise ValueError(msg)
        if have_py and not needs_exe:
            return "snaphu-py", None
        if exe is not None:
            return "subprocess", exe
        if have_py:
            raise EngineRunError.from_rule("UNW-004", cost=cost)
        raise EngineNotAvailableError(self._not_available_msg())

    def _not_available_msg(self) -> str:
        return f"engine {self.name!r} is not available: ['ENV-001']"

    # ------------------------------------------------------------------ unwrap
    def unwrap(
        self,
        igram: FloatArray,
        coh: FloatArray,
        mask: BoolArray | None,
        params: Mapping[str, Any],
    ) -> UnwrapResult:
        cfg = unwrap_cfg(params)
        backend, exe = self.select_backend(cfg)
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
        if tiles.capped:
            findings.append(
                make_finding(
                    "UNW-006",
                    severity="WARN",
                    requested=list(tiles.requested_overlap_px),
                    capped=list(tiles.overlap_px),
                    tile=[-(-shape[0] // tiles.rows), -(-shape[1] // tiles.cols)],
                )
            )
        if log is not None:
            for f in findings:
                log.finding(f)
        scratch, scratch_is_temp = self._scratch_dir(cfg)
        t0 = time.perf_counter()
        if backend == "snaphu-py":
            unw, cc, extra = self._unwrap_snaphu_py(
                igram, coh, masked, cfg, nlooks, tiles, scratch, scratch_is_temp, log
            )
        else:
            assert exe is not None
            unw, cc, extra = self._unwrap_subprocess(
                igram, coh, masked, cfg, nlooks, tiles, scratch, exe, log
            )
        wall = time.perf_counter() - t0
        unw_out = nan_masked(unw, masked)
        cc_out = conncomp_masked(cc, masked)
        stats: dict[str, Any] = {
            "engine": self.name,
            "backend": backend,
            "wall_time_s": round(wall, 3),
            "cost": cfg.get("cost"),
            "init": cfg.get("init"),
            "nlooks": nlooks,
            "nlooks_source": nlooks_src,
            "coherence_threshold": float(cfg.get("coherence_threshold", 0.3)),
            "masked_fraction": float(masked.mean()),
            "ntiles": list(tiles.ntiles),
            "tile_overlap_px": list(tiles.overlap_px),
            "nproc": int(cfg.get("nproc_per_igram", 1) or 1),
            "findings": [f.rule_id for f in findings],
            **conncomp_stats(cc_out),
            **extra,
        }
        if scratch_is_temp and bool(cfg.get("delete_scratch", True)):
            shutil.rmtree(scratch, ignore_errors=True)
            stats.pop("tile_dir_kept", None)
            if "tile_dir" in stats:
                stats["tile_dir"] = None
                stats["assemble_only_capable"] = False
        return UnwrapResult(unw=unw_out, conncomp=cc_out, stats=stats)

    @staticmethod
    def _scratch_dir(cfg: Mapping[str, Any]) -> tuple[Path, bool]:
        raw = cfg.get("_scratch_dir") or cfg.get("scratch_dir")
        if raw:
            p = Path(str(raw))
            p.mkdir(parents=True, exist_ok=True)
            return p, False
        return Path(tempfile.mkdtemp(prefix="wintersar-snaphu-")), True

    # ------------------------------------------------------------------ backend: snaphu-py
    def _unwrap_snaphu_py(
        self,
        igram: FloatArray,
        coh: FloatArray,
        masked: BoolArray,
        cfg: Mapping[str, Any],
        nlooks: float,
        tiles: TileSpec,
        scratch: Path,
        scratch_is_temp: bool,
        log: EngineLog | None = None,
    ) -> tuple[NDArray[np.float32], NDArray[np.uint32], dict[str, Any]]:
        snaphu = importlib.import_module("snaphu")
        c64 = to_complex64(igram, coh, masked)
        corr = clean_coherence(coh, masked)
        unw = np.zeros(c64.shape, dtype=np.float32)
        cc = np.zeros(c64.shape, dtype=np.uint32)
        keep = bool(cfg.get("keep_tile_dir", True)) and not scratch_is_temp
        # source: snaphu-py v0.4.1 src/snaphu/_unwrap.py `unwrap()` keyword names (ADR-0023)
        kwargs: dict[str, Any] = {
            "igram": c64,
            "corr": corr,
            "nlooks": float(nlooks),
            "cost": str(cfg.get("cost", "defo")),
            "init": str(cfg.get("init", "mcf")),
            "mask": np.asarray(~masked, dtype=np.bool_),  # zeros = masked out
            "min_conncomp_frac": float(
                cfg.get("min_conncomp_frac", SNAPHU_PY_DEFAULTS["min_conncomp_frac"])
            ),
            "ntiles": tiles.ntiles,
            "tile_overlap": tiles.overlap_px,
            "nproc": max(int(cfg.get("nproc_per_igram", 1) or 1), 1),
            "tile_cost_thresh": int(
                cfg.get("tile_cost_thresh", SNAPHU_PY_DEFAULTS["tile_cost_thresh"])
            ),
            "min_region_size": int(
                cfg.get("min_region_size", SNAPHU_PY_DEFAULTS["min_region_size"])
            ),
            "single_tile_reoptimize": bool(
                cfg.get("single_tile_reoptimize", SNAPHU_PY_DEFAULTS["single_tile_reoptimize"])
            ),
            "regrow_conncomps": bool(
                cfg.get("regrow_conncomps", SNAPHU_PY_DEFAULTS["regrow_conncomps"])
            ),
            "scratchdir": str(scratch),
            "delete_scratch": not keep,
            "unw": unw,
            "conncomp": cc,
        }
        if log is not None:
            log.write(
                "snaphu-py unwrap "
                + " ".join(
                    f"{k}={kwargs[k]}"
                    for k in (
                        "nlooks",
                        "cost",
                        "init",
                        "ntiles",
                        "tile_overlap",
                        "nproc",
                        "tile_cost_thresh",
                        "min_region_size",
                        "scratchdir",
                        "delete_scratch",
                    )
                )
            )
        out_unw, out_cc = snaphu.unwrap(**kwargs)
        unw_arr = np.asarray(out_unw if out_unw is not None else unw, dtype=np.float32)
        cc_arr = np.asarray(out_cc if out_cc is not None else cc, dtype=np.uint32)
        extra: dict[str, Any] = {"snaphu_py_version": python_module_version("snaphu")}
        if tiles.tiled:
            # snaphu-py never sets TILEDIR/RMTMPTILE, so SNAPHU removes its tile files
            # (v2.0 default) — the scratch dir is recorded but cannot feed ASSEMBLEONLY.
            extra["tile_dir"] = str(scratch)
            extra["tile_dir_kept"] = keep
            extra["assemble_only_capable"] = False
        return unw_arr, cc_arr, extra

    # ------------------------------------------------------------------ backend: subprocess
    def _unwrap_subprocess(
        self,
        igram: FloatArray,
        coh: FloatArray,
        masked: BoolArray,
        cfg: Mapping[str, Any],
        nlooks: float,
        tiles: TileSpec,
        scratch: Path,
        exe: Path,
        log: EngineLog | None,
    ) -> tuple[NDArray[np.float32], NDArray[np.uint32], dict[str, Any]]:
        shape = (int(igram.shape[0]), int(igram.shape[1]))
        has_mask = bool(masked.any())
        conf = build_snaphu_config(cfg, scratch, shape, nlooks, tiles, has_mask)
        if conf.assembleonly and (conf.tiledir is None or not conf.tiledir.is_dir()):
            raise EngineRunError.from_rule("UNW-005", tile_dir=str(conf.tiledir))
        scratch.mkdir(parents=True, exist_ok=True)
        # native byte order, COMPLEX_DATA = interleaved float32 re/im (man page FILE FORMATS)
        to_complex64(igram, coh, masked).tofile(conf.infile)
        if conf.corrfile is not None:
            clean_coherence(coh, masked).tofile(conf.corrfile)
        if conf.bytemaskfile is not None:
            # BYTEMASKFILE: signed bytes, 0 = masked out, 1 = valid (snaphu.conf.full)
            np.asarray(~masked, dtype=np.int8).tofile(conf.bytemaskfile)
        conf_path = conf.write(scratch / "snaphu.conf")
        cmd = [str(exe), "-f", str(conf_path)]  # source: snaphu(1) `-f configfile`
        if log is not None:
            log.write(f"snaphu subprocess: {' '.join(cmd)}")
        timeout = cfg.get("timeout_s")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(scratch),
                check=False,
                timeout=float(timeout) if timeout else None,
            )
        except subprocess.TimeoutExpired as e:
            raise EngineRunError.from_rule(
                "UNW-003",
                engine="snaphu",
                returncode="timeout",
                log=str(log.path if log else scratch / "snaphu.log"),
            ) from e
        if log is not None:
            log.write_raw(proc.stdout)
            log.write_raw(proc.stderr)
        if proc.returncode != 0:
            raise EngineRunError.from_rule(
                "UNW-003",
                engine="snaphu",
                returncode=proc.returncode,
                log=str(log.path if log else scratch / "snaphu.log"),
                evidence={"stderr_tail": (proc.stderr or "")[-2000:]},
            )
        unw = self._read_raw(conf.outfile, np.float32, shape)
        cc = (
            self._read_raw(conf.conncompfile, np.uint32, shape)
            if conf.conncompfile is not None and conf.conncompfile.exists()
            else np.where(masked, 0, 1).astype(np.uint32)
        )
        extra: dict[str, Any] = {
            "snaphu_executable": str(exe),
            "config_file": str(conf_path),
            "snaphu_logfile": str(conf.logfile) if conf.logfile else None,
        }
        if conf.tiledir is not None:
            extra["tile_dir"] = str(conf.tiledir)
            extra["tile_dir_kept"] = conf.rmtmptile is False
            extra["assemble_only_capable"] = conf.rmtmptile is False
            extra["assemble_only"] = conf.assembleonly
        if conf.costoutfile is not None:
            extra["cost_file"] = str(conf.costoutfile)
        return unw, cc, extra

    @staticmethod
    def _read_raw(path: Path, dtype: type[np.generic], shape: tuple[int, int]) -> Any:
        expected = int(np.dtype(dtype).itemsize) * shape[0] * shape[1]
        actual = path.stat().st_size if path.exists() else 0
        if actual != expected:
            raise EngineRunError.from_rule(
                "UNW-012", engine="snaphu", path=str(path), expected=expected, actual=actual
            )
        return np.fromfile(path, dtype=dtype).reshape(shape)
