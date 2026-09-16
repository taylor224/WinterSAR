"""Fixtures for the unwrap adapters: none of snaphu-py / tophu / spurt is installed, so the
tests inject fake modules into ``sys.modules`` (exposing the *verified* upstream signatures,
ADR-0023..0025) and fake executables that speak the SNAPHU config-file / spurt-emcf CLI."""

from __future__ import annotations

import importlib.machinery
import json
import os
import shutil
import stat
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from wintersar.io.igrams import IgramStack, save_igram_stack
from wintersar.research import synth

# ------------------------------------------------------------------ synthetic inputs


@dataclass
class PairData:
    wrapped: np.ndarray
    coherence: np.ndarray
    mask: np.ndarray
    unw_true: np.ndarray


@pytest.fixture
def pair() -> PairData:
    ig = synth.make_interferogram(
        shape=(40, 48),
        rng=np.random.default_rng(3),
        atmosphere_std_rad=0.2,
        coherence_base=0.85,
        water_fraction=0.1,
    )
    wrapped = ig.wrapped.astype(np.float32)
    wrapped[0, 0] = np.nan  # an invalid pixel must be masked too
    return PairData(
        wrapped=wrapped,
        coherence=ig.coherence.astype(np.float32),
        mask=ig.mask,
        unw_true=ig.unw_true.astype(np.float32),
    )


@pytest.fixture
def igrams_npz(tmp_path: Path) -> Path:
    stack = synth.make_stack(n_dates=4, shape=(24, 32), rng=np.random.default_rng(1))
    keys = [p.key for p in stack.pairs]
    ig = IgramStack(
        wrapped=np.stack([stack.igrams[k].wrapped for k in keys]).astype(np.float32),
        coherence=np.stack([stack.igrams[k].coherence for k in keys]).astype(np.float32),
        pairs=keys,
        dates=stack.dates,
        mask=np.stack([stack.igrams[k].mask for k in keys]),
        truth={"unw_true": np.stack([stack.igrams[k].unw_true for k in keys]).astype(np.float32)},
    )
    return save_igram_stack(ig, tmp_path / "igrams.npz")


# ------------------------------------------------------------------ absence


@pytest.fixture
def engines_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No python module, no executable, no env override for any of the three engines."""
    for mod in ("snaphu", "tophu", "spurt"):
        monkeypatch.setitem(sys.modules, mod, None)
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    for var in ("WINTERSAR_SNAPHU_EXE", "WINTERSAR_SPURT_EXE"):
        monkeypatch.delenv(var, raising=False)


def _module(name: str, version: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__version__ = version  # type: ignore[attr-defined]
    mod.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    return mod


# ------------------------------------------------------------------ fake snaphu-py


@dataclass
class FakeSnaphu:
    module: types.ModuleType
    calls: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def fake_snaphu(monkeypatch: pytest.MonkeyPatch, engines_absent: None) -> FakeSnaphu:
    """``snaphu.unwrap`` with the exact v0.4.1 keyword signature, recording every call."""
    mod = _module("snaphu", "0.4.1")
    rec = FakeSnaphu(module=mod)

    def unwrap(
        igram: Any,
        corr: Any,
        nlooks: float,
        cost: str = "smooth",
        init: str = "mcf",
        *,
        mask: Any = None,
        min_conncomp_frac: float = 0.01,
        phase_grad_window: tuple[int, int] = (7, 7),
        ntiles: tuple[int, int] = (1, 1),
        tile_overlap: Any = 0,
        nproc: int = 1,
        tile_cost_thresh: int = 500,
        min_region_size: int = 100,
        single_tile_reoptimize: bool = True,
        regrow_conncomps: bool = True,
        scratchdir: Any = None,
        delete_scratch: bool = True,
        unw: Any = None,
        conncomp: Any = None,
    ) -> tuple[Any, Any]:
        # source: snaphu-py v0.4.1 _check.py check_cost_mode
        if cost == "topo":
            msg = "'topo' cost mode is not currently supported"
            raise NotImplementedError(msg)
        if cost not in {"defo", "smooth"}:
            raise ValueError(cost)
        if init not in {"mst", "mcf"}:
            raise ValueError(init)
        rec.calls.append(
            {
                "igram": igram,
                "corr": corr,
                "nlooks": nlooks,
                "cost": cost,
                "init": init,
                "mask": mask,
                "min_conncomp_frac": min_conncomp_frac,
                "phase_grad_window": phase_grad_window,
                "ntiles": ntiles,
                "tile_overlap": tile_overlap,
                "nproc": nproc,
                "tile_cost_thresh": tile_cost_thresh,
                "min_region_size": min_region_size,
                "single_tile_reoptimize": single_tile_reoptimize,
                "regrow_conncomps": regrow_conncomps,
                "scratchdir": scratchdir,
                "delete_scratch": delete_scratch,
                "unw": unw,
                "conncomp": conncomp,
            }
        )
        if scratchdir is not None:
            Path(scratchdir).mkdir(parents=True, exist_ok=True)
        if unw is None:
            unw = np.zeros(igram.shape, dtype=np.float32)
        if conncomp is None:
            conncomp = np.zeros(igram.shape, dtype=np.uint32)
        unw[...] = np.angle(igram).astype(np.float32) + 2 * np.pi  # "unwrapped" = phase + offset
        valid = np.ones(igram.shape, dtype=bool) if mask is None else np.asarray(mask, bool)
        conncomp[...] = np.where(valid, 1, 0).astype(np.uint32)
        return unw, conncomp

    mod.unwrap = unwrap  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "snaphu", mod)
    return rec


# ------------------------------------------------------------------ fake tophu


@dataclass
class FakeTophu:
    module: types.ModuleType
    calls: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def fake_tophu(monkeypatch: pytest.MonkeyPatch, engines_absent: None) -> FakeTophu:
    """``tophu.multiscale_unwrap`` + ``SnaphuUnwrap``/``ICUUnwrap``/``PhassUnwrap`` (v0.2.1)."""
    mod = _module("tophu", "0.2.1")
    rec = FakeTophu(module=mod)

    class SnaphuUnwrap:
        def __init__(
            self, cost: str = "smooth", cost_params: Any = None, init_method: str = "mcf"
        ) -> None:
            self.cost, self.cost_params, self.init_method = cost, cost_params, init_method

        def __call__(self, igram: Any, coherence: Any, nlooks: float, scratchdir: Path) -> Any:
            return np.angle(igram).astype(np.float32), np.ones(igram.shape, np.uint32)

    class ICUUnwrap(SnaphuUnwrap):
        def __init__(self) -> None:
            super().__init__()

    class PhassUnwrap(SnaphuUnwrap):
        def __init__(self) -> None:
            super().__init__()

    def multiscale_unwrap(
        unwrapped: Any,
        conncomp: Any,
        igram: Any,
        coherence: Any,
        nlooks: float,
        unwrap_func: Any,
        downsample_factor: tuple[int, int],
        ntiles: tuple[int, int],
        min_conncomp_overlap: float = 0.5,
        scratchdir: Any = None,
        *,
        do_lowpass_filter: bool = True,
        shape_factor: float = 1.5,
        overhang: float = 0.5,
        ripple: float = 0.01,
        attenuation: float = 40.0,
    ) -> None:
        rec.calls.append(
            {
                "unwrapped": unwrapped,
                "conncomp": conncomp,
                "igram": igram,
                "coherence": coherence,
                "nlooks": nlooks,
                "unwrap_func": unwrap_func,
                "downsample_factor": downsample_factor,
                "ntiles": ntiles,
                "min_conncomp_overlap": min_conncomp_overlap,
                "scratchdir": scratchdir,
                "do_lowpass_filter": do_lowpass_filter,
            }
        )
        scratch = Path(scratchdir) if scratchdir else None
        if scratch is not None:
            scratch.mkdir(parents=True, exist_ok=True)
        u, c = unwrap_func(igram, coherence, nlooks, scratch or Path.cwd())
        unwrapped[...] = u
        conncomp[...] = c

    mod.multiscale_unwrap = multiscale_unwrap  # type: ignore[attr-defined]
    mod.SnaphuUnwrap = SnaphuUnwrap  # type: ignore[attr-defined]
    mod.ICUUnwrap = ICUUnwrap  # type: ignore[attr-defined]
    mod.PhassUnwrap = PhassUnwrap  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tophu", mod)
    return rec


# ------------------------------------------------------------------ fake executables

_FAKE_SNAPHU_EXE = '''#!{python}
"""Stand-in for the SNAPHU executable: understands `-h` and `-f <config>` (keywords from
snaphu.conf.full) and writes OUTFILE (FLOAT_DATA) / CONNCOMPFILE (UINT) / LOGFILE."""
import os, sys, pathlib
import numpy as np

args = sys.argv[1:]
if args == ["-h"]:
    print("snaphu v2.0.7")
    print("usage: snaphu [options] [infile] [linelength] [options]")
    sys.exit(0)
if len(args) != 2 or args[0] != "-f":
    sys.stderr.write("fake snaphu: expected -f <config>\\n")
    sys.exit(64)
conf = {{}}
for line in open(args[1], encoding="utf-8"):
    s = line.strip()
    if not s or not s[0].isalnum():
        continue
    k, v = s.split(None, 1)
    conf[k.upper()] = v.strip()
if os.environ.get("FAKE_SNAPHU_FAIL"):
    sys.stderr.write("snaphu: Exceeded maximum number of secondary arcs\\n")
    sys.exit(2)
nx = int(conf["LINELENGTH"])
ig = np.fromfile(conf["INFILE"], dtype=np.complex64).reshape(-1, nx)
print("fake snaphu: shape", ig.shape, "mode", conf.get("STATCOSTMODE"), "init", conf.get("INITMETHOD"))
if conf.get("ASSEMBLEONLY") == "TRUE":
    td = pathlib.Path(conf["TILEDIR"])
    if not (td / "marker").exists():
        sys.stderr.write("fake snaphu: no tiles to assemble in %s\\n" % td)
        sys.exit(3)
    print("fake snaphu: assemble-only from", td)
elif conf.get("TILEDIR") and conf.get("RMTMPTILE") == "FALSE":
    td = pathlib.Path(conf["TILEDIR"])
    td.mkdir(parents=True, exist_ok=True)
    (td / "marker").write_text("tiles kept")
np.angle(ig).astype(np.float32).tofile(conf["OUTFILE"])
if "CONNCOMPFILE" in conf:
    valid = np.ones(ig.shape, dtype=bool)
    if "BYTEMASKFILE" in conf:
        valid = np.fromfile(conf["BYTEMASKFILE"], dtype=np.int8).reshape(ig.shape) == 1
    np.where(valid, 1, 0).astype(np.uint32).tofile(conf["CONNCOMPFILE"])
if "COSTOUTFILE" in conf:
    np.zeros(ig.size, dtype=np.int16).tofile(conf["COSTOUTFILE"])
if "LOGFILE" in conf:
    with open(conf["LOGFILE"], "w", encoding="utf-8") as fh:
        for k, v in conf.items():
            fh.write(f"{{k}} {{v}}\\n")
sys.exit(0)
'''

_FAKE_SPURT_EXE = '''#!{python}
"""Stand-in for `spurt-emcf` (argparse flags from spurt v0.1.1 _cli.py)."""
import argparse, json, pathlib, sys

if sys.argv[1:] == ["--version"]:
    print("spurt-emcf 0.1.1")
    sys.exit(0)
p = argparse.ArgumentParser()
p.add_argument("-i", "--inputdir", required=True)
p.add_argument("-o", "--outputdir", default="./emcf")
p.add_argument("--tempdir", default="./emcf_tmp")
p.add_argument("-w", "--t-workers", type=int, default=0)
p.add_argument("--s-workers", type=int, default=1)
p.add_argument("-b", "--batchsize", type=int, default=150000)
p.add_argument("-c", "--coh", type=float, default=0.6)
p.add_argument("--t-cost-type", choices=["constant", "distance", "centroid"], default="constant")
p.add_argument("--t-cost-scale", type=int, default=100)
p.add_argument("--pts-per-tile", type=int, default=800000)
p.add_argument("--max-tiles", type=int, default=49)
p.add_argument("--merge-parallel-ifgs", type=int, default=1)
p.add_argument("--unwrap-parallel-tiles", type=int, default=1)
p.add_argument("--singletile", action="store_true")
p.add_argument("--log-file", default=None)
a = p.parse_args()
inp = pathlib.Path(a.inputdir)
if not inp.is_dir():
    sys.stderr.write("no input dir\\n")
    sys.exit(2)
out = pathlib.Path(a.outputdir); out.mkdir(parents=True, exist_ok=True)
pathlib.Path(a.tempdir).mkdir(parents=True, exist_ok=True)
(out / "argv.json").write_text(json.dumps(sys.argv[1:]))
(out / "20240101_20240113.unw.tif").write_bytes(b"fake")
if a.log_file:
    pathlib.Path(a.log_file).write_text("spurt-emcf fake log\\n")
print("fake spurt-emcf done")
'''


def _write_exe(path: Path, template: str) -> Path:
    path.write_text(template.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_snaphu_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines_absent: None) -> Path:
    (tmp_path / "bin").mkdir(exist_ok=True)
    exe = _write_exe(tmp_path / "bin" / "snaphu", _FAKE_SNAPHU_EXE)
    monkeypatch.setenv("WINTERSAR_SNAPHU_EXE", str(exe))
    monkeypatch.delenv("FAKE_SNAPHU_FAIL", raising=False)
    return exe


@pytest.fixture
def fake_spurt_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engines_absent: None) -> Path:
    (tmp_path / "bin").mkdir(exist_ok=True)
    exe = _write_exe(tmp_path / "bin" / "spurt-emcf", _FAKE_SPURT_EXE)
    monkeypatch.setenv("WINTERSAR_SPURT_EXE", str(exe))
    return exe


@pytest.fixture
def phase_linked_dir(tmp_path: Path) -> Path:
    d = tmp_path / "linked"
    d.mkdir()
    for name in ("20240101_20240113.int.tif", "20240101_20240125.int.tif"):
        (d / name).write_bytes(b"x")
    (d / "temporal_coherence.tif").write_bytes(b"x")
    return d


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


_ = os  # keep os imported for fixtures that toggle env vars
