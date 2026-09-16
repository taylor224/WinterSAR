"""dolphin adapter (plan §5.2 ``dolphin.py``; PERF-05 A/B time-series path; ADR-0028 facts,
ADR-0029 output normalisation).

`dolphin <https://github.com/isce-framework/dolphin>`_ (Caltech/JPL, **BSD-3-Clause OR
Apache-2.0**; PyPI ``dolphin`` 0.42.7, 2026-06-15) runs phase linking (PS + DS) on a
coregistered SLC stack, forms the nearest-N interferogram network, unwraps (N-1 or more
interferograms) and inverts a displacement time series. It therefore covers the wintersar
``timeseries`` stage as an alternative to MintPy — but its *inputs* are the coregistered SLCs
(``merged/SLC`` of topsStack, COMPASS CSLC HDF5 or any GDAL-readable SLC stack), not the
unwrapped interferograms of the ``unwrap`` stage.

Verified facts (``# source`` comments; ADR-0028):

* CLI (``dolphin/cli.py``, tyro): ``dolphin run <config_file> [--debug]`` loads
  ``DisplacementWorkflow.from_yaml(config_file)``; ``dolphin config …`` (``ConfigCli``, default
  ``outfile = dolphin_config.yaml``); ``dolphin --version`` prints ``__version__``.
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/cli.py
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/workflows/_cli_config.py
* YAML keys = pydantic field names (``YamlModel.to_yaml(..., by_alias=True)``;
  ``from_yaml`` → ``cls(**data)``): ``cslc_file_list``, ``input_options.{subdataset,
  cslc_date_fmt('%Y%m%d'), wavelength}``, ``work_directory``, ``keep_paths_relative``,
  ``worker_settings.{gpu_enabled, threads_per_worker, n_parallel_bursts, block_shape}``,
  ``phase_linking.{ministack_size(15), max_num_compressed(10), half_window{x(14),y(7)},
  use_evd, beta, shp_method('glrt'|'ks'|'rect'), shp_alpha, …}``,
  ``interferogram_network.{reference_idx, max_bandwidth, max_temporal_baseline, indexes}``,
  ``unwrap_options.{run_unwrap, run_goldstein, run_interpolation, unwrap_method,
  n_parallel_jobs(-1), zero_where_masked, snaphu_options{ntiles, tile_overlap,
  n_parallel_tiles, init_method('mcf'|'mst'), cost('defo'|'smooth')}, tophu_options,
  spurt_options}``, ``timeseries_options.{run_inversion, method('L1'|'L2'), reference_point
  (row, col), run_velocity, correlation_threshold(0.2), …}``, ``output_options.{strides{x,y},
  epsg, bounds, bounds_epsg, bounds_wkt}``, ``mask_file``, ``log_file``.
  ``UnwrapMethod`` values: ``snaphu``, ``icu``, ``phass``, ``spurt``, ``whirlwind`` (no
  ``tophu`` value; ``tophu_options`` exist separately).
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/workflows/config/_common.py
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/workflows/config/_displacement.py
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/workflows/config/_unwrap_options.py
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/workflows/config/_enums.py
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/workflows/config/_yaml_model.py
* Output directories (``PrivateAttr`` defaults): ``PS``, ``linked_phase``, ``interferograms``,
  ``unwrapped`` (``*.unw.tif`` / ``*.unw.conncomp.tif``), ``timeseries``
  (``<ref>_<date>.tif`` per date, ``velocity.tif``).
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/unwrap/_constants.py
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/timeseries.py
* Units/sign: with ``input_options.wavelength`` set, time series are converted with
  ``-1 * (wavelength / (4 * pi))`` → metres, "Positive values indicate motion *toward* the
  radar"; without it they stay in radians. Velocity = ``units / year`` (365.25 d).
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/timeseries.py
* ``SENTINEL_1_WAVELENGTH = SPEED_OF_LIGHT / 5.405e9``.
  # source: https://github.com/isce-framework/dolphin/blob/main/src/dolphin/constants.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import yaml
from numpy.typing import NDArray

from wintersar.engines.base import (
    Engine,
    executable_version,
    python_module_version,
    register_engine,
)
from wintersar.io.schemas import Artifact, Artifacts, Finding, Severity
from wintersar.io.timeseries import TimeSeries
from wintersar.util.masking import mask_mapping, mask_text

EXECUTABLE = "dolphin"
CONFIG_FILENAME = "dolphin_config.yaml"  # source: ConfigCli.outfile default
DOLPHIN_VERIFIED_VERSION = "0.42.7"  # source: https://pypi.org/pypi/dolphin/json (2026-09-16)
SPEED_OF_LIGHT = 299_792_458.0  # source: dolphin/constants.py
SENTINEL_1_FREQUENCY_HZ = 5.405e9  # source: dolphin/constants.py
SENTINEL_1_WAVELENGTH_M = SPEED_OF_LIGHT / SENTINEL_1_FREQUENCY_HZ
UNWRAP_METHODS: tuple[str, ...] = ("snaphu", "icu", "phass", "spurt", "whirlwind")  # _enums.py
TIMESERIES_DIR = "timeseries"
UNWRAPPED_DIR = "unwrapped"
INTERFEROGRAMS_DIR = "interferograms"
LINKED_PHASE_DIR = "linked_phase"
VELOCITY_FILE = "velocity.tif"
UNW_SUFFIX = ".unw.tif"  # source: dolphin/unwrap/_constants.py
CONNCOMP_SUFFIX = ".unw.conncomp.tif"
TS_FILE_RE = re.compile(r"^(\d{8})_(\d{8})\.tif$")
CSLC_GLOBS: tuple[str, ...] = (  # topsStack merged SLCs (Stack.py / mergeBursts.py naming)
    "*/*.slc.full.vrt",
    "*/*.slc.full",
    "*/*.slc.vrt",
    "*/*.slc",
    "*.h5",
    "*.tif",
)

Runner = Callable[[list[str], Path, Path], int]


class DolphinRunError(RuntimeError):
    def __init__(self, message: str, findings: list[Finding]) -> None:
        super().__init__(message)
        self.findings = findings


def _finding(rule: str, severity: Severity, scope: str, **params: Any) -> Finding:
    return Finding(
        rule_id=rule,
        severity=severity,
        message_key=f"engines.dolphin.{rule}.cause",
        fix_key=f"engines.dolphin.{rule}.fix",
        params=params,
        evidence=mask_mapping(dict(params)),
        scope=scope,
    )


def find_executable() -> str | None:
    return shutil.which(EXECUTABLE)


def _subprocess_runner(argv: list[str], cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"# {datetime.now(UTC).isoformat(timespec='seconds')} $ {mask_text(' '.join(argv))}\n"
        )
        fh.flush()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                stdout=fh,
                stderr=subprocess.STDOUT,
                check=False,
                env=dict(os.environ),
            )
        except OSError as e:
            fh.write(f"# could not start {argv[0]}: {mask_text(str(e))}\n")
            return 127
        fh.write(f"# exit={proc.returncode}\n")
        return int(proc.returncode)


# ---------------------------------------------------------------------- config generation


def discover_cslc_files(directory: Path) -> list[Path]:
    """Coregistered SLC files under ``directory`` (first matching pattern of :data:`CSLC_GLOBS`)."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    for pattern in CSLC_GLOBS:
        found = sorted(p for p in directory.glob(pattern) if p.is_file())
        if found:
            return found
    return []


def build_config(
    cslc_files: Sequence[Path],
    work_dir: Path,
    params: Mapping[str, Any],
    *,
    cores: int = 1,
    gpu: bool = False,
    log_file: Path | None = None,
) -> tuple[dict[str, Any], list[Finding]]:
    """``DisplacementWorkflow`` YAML mapping (verified field names) from stage params.

    Only keys the user set (or wintersar decides) are written; everything else keeps the
    dolphin default so that the file stays valid across minor versions.
    """
    findings: list[Finding] = []
    p = params
    wavelength = p.get("wavelength_m", SENTINEL_1_WAVELENGTH_M)
    cfg: dict[str, Any] = {
        "cslc_file_list": [str(f) for f in cslc_files],
        "input_options": {
            "cslc_date_fmt": str(p.get("cslc_date_fmt", "%Y%m%d")),
            "wavelength": None if wavelength is None else float(wavelength),
        },
        "work_directory": str(work_dir),
        "keep_paths_relative": False,
        "worker_settings": {
            "gpu_enabled": bool(p.get("gpu_enabled", gpu)),
            "threads_per_worker": int(p.get("threads_per_worker", max(1, cores))),
            "n_parallel_bursts": int(p.get("n_parallel_bursts", 1)),
        },
        "unwrap_options": {"run_unwrap": True},
        "timeseries_options": {
            "run_inversion": True,
            "run_velocity": True,
            "method": str(p.get("inversion_method", "L1")),
        },
    }
    if p.get("subdataset"):
        cfg["input_options"]["subdataset"] = str(p["subdataset"])
    if wavelength is None:
        findings.append(_finding("DOL-006", "WARN", "timeseries"))

    pl: dict[str, Any] = {}
    if p.get("ministack_size") is not None:
        pl["ministack_size"] = int(p["ministack_size"])
    hw = p.get("half_window")
    if isinstance(hw, Mapping):
        pl["half_window"] = {"x": int(hw["x"]), "y": int(hw["y"])}
    elif isinstance(hw, list | tuple) and len(hw) == 2:
        pl["half_window"] = {"x": int(hw[0]), "y": int(hw[1])}
    if p.get("shp_method"):
        pl["shp_method"] = str(p["shp_method"])
    if p.get("use_evd") is not None:
        pl["use_evd"] = bool(p["use_evd"])
    if pl:
        cfg["phase_linking"] = pl

    net: dict[str, Any] = {}
    if p.get("max_bandwidth") is not None:
        net["max_bandwidth"] = int(p["max_bandwidth"])
    if p.get("max_temporal_baseline_days") is not None:
        net["max_temporal_baseline"] = int(p["max_temporal_baseline_days"])
    if p.get("reference_idx") is not None:
        net["reference_idx"] = int(p["reference_idx"])
    if net:
        cfg["interferogram_network"] = net

    method = str(p.get("unwrap_method") or "snaphu").lower()
    unw = cfg["unwrap_options"]
    if method == "tophu":
        findings.append(_finding("DOL-005", "WARN", "timeseries", requested=method, used="snaphu"))
        method = "snaphu"
    elif method == "auto":
        method = "snaphu"
    if method not in UNWRAP_METHODS:
        findings.append(_finding("DOL-005", "WARN", "timeseries", requested=method, used="snaphu"))
        method = "snaphu"
    unw["unwrap_method"] = method
    if p.get("n_parallel_jobs") is not None:
        unw["n_parallel_jobs"] = int(p["n_parallel_jobs"])
    snaphu_opts: dict[str, Any] = {}
    ntiles = p.get("ntiles")
    if isinstance(ntiles, list | tuple) and len(ntiles) == 2:
        snaphu_opts["ntiles"] = [int(ntiles[0]), int(ntiles[1])]
        if p.get("n_parallel_tiles") is not None:
            snaphu_opts["n_parallel_tiles"] = int(p["n_parallel_tiles"])
    if p.get("cost") in ("defo", "smooth"):
        snaphu_opts["cost"] = str(p["cost"])
    if p.get("init") in ("mcf", "mst"):
        snaphu_opts["init_method"] = str(p["init"])
    if snaphu_opts:
        unw["snaphu_options"] = snaphu_opts
    if p.get("run_goldstein") is not None:
        unw["run_goldstein"] = bool(p["run_goldstein"])

    ts = cfg["timeseries_options"]
    rp = p.get("reference_point_rowcol")
    if isinstance(rp, list | tuple) and len(rp) == 2:
        ts["reference_point"] = [int(rp[0]), int(rp[1])]
    else:
        findings.append(_finding("DOL-004", "WARN", "timeseries"))
    if p.get("correlation_threshold") is not None:
        ts["correlation_threshold"] = float(p["correlation_threshold"])

    strides = p.get("strides")
    if isinstance(strides, Mapping):
        cfg["output_options"] = {"strides": {"x": int(strides["x"]), "y": int(strides["y"])}}
    elif isinstance(strides, list | tuple) and len(strides) == 2:
        cfg["output_options"] = {"strides": {"x": int(strides[0]), "y": int(strides[1])}}
    if p.get("mask_file"):
        cfg["mask_file"] = str(p["mask_file"])
    if log_file is not None:
        cfg["log_file"] = str(log_file)
    return cfg, findings


def write_config(cfg: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# dolphin DisplacementWorkflow config generated by wintersar (edit config.yaml instead)\n"
    )
    path.write_text(
        header + yaml.safe_dump(dict(cfg), sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return path


def build_run_argv(
    config_path: Path, executable: str | None = None, debug: bool = False
) -> list[str]:
    """``dolphin run <config> [--debug]``  # source: dolphin/cli.py run_cli"""
    argv = [executable or EXECUTABLE, "run", str(config_path)]
    if debug:
        argv.append("--debug")
    return argv


# ---------------------------------------------------------------------- output normalisation


def _parse_yyyymmdd(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def list_timeseries_files(work_dir: Path) -> tuple[str | None, list[tuple[str, Path]]]:
    """``(reference_date, [(secondary_date, path), ...])`` of ``timeseries/<ref>_<date>.tif``."""
    ts_dir = Path(work_dir) / TIMESERIES_DIR
    if not ts_dir.is_dir():
        return None, []
    refs: set[str] = set()
    files: list[tuple[str, Path]] = []
    for p in sorted(ts_dir.iterdir()):
        m = TS_FILE_RE.match(p.name)
        if m is None:
            continue
        refs.add(m.group(1))
        files.append((m.group(2), p))
    if not files:
        return None, []
    ref = sorted(refs)[0]
    return ref, [(d, p) for d, p in files if p.name.startswith(ref + "_")]


def _read_raster(path: Path) -> tuple[NDArray[np.floating], Any, Any, float | None]:
    import rasterio

    with rasterio.open(path) as ds:
        data = np.asarray(ds.read(1), dtype=np.float32)
        nodata = ds.nodata
        return data, ds.crs, ds.transform, (None if nodata is None else float(nodata))


def _latlon_grids(
    crs: Any, transform: Any, ny: int, nx: int
) -> tuple[NDArray[np.floating], NDArray[np.floating], str]:
    cols = np.arange(nx, dtype=np.float64) + 0.5
    rows = np.arange(ny, dtype=np.float64) + 0.5
    if crs is None or transform is None:
        return rows, cols, "pixel"
    a, b, c, d, e, f = transform.a, transform.b, transform.c, transform.d, transform.e, transform.f
    if crs.is_geographic and b == 0 and d == 0:
        lon = a * cols + c
        lat = e * rows + f
        return lat.astype(np.float64), lon.astype(np.float64), "geographic"
    cc, rr = np.meshgrid(cols, rows)
    x = a * cc + b * rr + c
    y = d * cc + e * rr + f
    if crs.is_geographic:
        return y, x, "geographic"
    from pyproj import Transformer

    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon2, lat2 = tr.transform(x, y)
    return (
        np.asarray(lat2, dtype=np.float64),
        np.asarray(lon2, dtype=np.float64),
        f"epsg:{crs.to_epsg()}",
    )


def read_dolphin_timeseries(
    work_dir: Path,
    *,
    wavelength_m: float | str | None = "auto",
    lat_file: Path | None = None,
    lon_file: Path | None = None,
) -> TimeSeries:
    """Normalise a dolphin work directory into :class:`TimeSeries` (PERF-05 A/B parity).

    * ``displacement_m[0]`` is zero (reference date = first date of ``<ref>_<date>.tif``);
    * metres, positive **toward** the satellite (dolphin applies ``-λ/4π`` when
      ``input_options.wavelength`` is set — same convention as MintPy / the fake engine);
      ``wavelength_m='auto'`` reads it from ``dolphin_config.yaml`` and converts radians →
      metres itself when the run had no wavelength (``attrs['units_converted_by']``);
    * ``lat``/``lon`` from the raster georeferencing (1-D vectors for north-up geographic
      grids, 2-D otherwise; projected CRS → pyproj), or from ``lat_file``/``lon_file`` for
      radar-coded stacks, else pixel indices (``attrs['coords'] == 'pixel'``).
    """
    work_dir = Path(work_dir)
    ref, files = list_timeseries_files(work_dir)
    if ref is None or not files:
        msg = f"no dolphin timeseries/<ref>_<date>.tif files in {mask_text(str(work_dir))}"
        raise FileNotFoundError(msg)
    cfg_wavelength: float | None = None
    cfg_path = work_dir / CONFIG_FILENAME
    if cfg_path.exists():
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            io = raw.get("input_options") or {}
            if io.get("wavelength") is not None:
                cfg_wavelength = float(io["wavelength"])
        except (OSError, ValueError, AttributeError):
            cfg_wavelength = None
    layers: list[NDArray[np.floating]] = []
    crs = transform = None
    nodata: float | None = None
    for _, path in files:
        arr, crs, transform, nodata = _read_raster(path)
        layers.append(arr)
    stack = np.stack(layers).astype(np.float32)
    if nodata is not None:
        stack[stack == nodata] = np.nan
    ny, nx = stack.shape[1], stack.shape[2]
    disp = np.concatenate([np.zeros((1, ny, nx), dtype=np.float32), stack])
    dates = [_parse_yyyymmdd(ref)] + [_parse_yyyymmdd(d) for d, _ in files]
    attrs: dict[str, Any] = {
        "engine": "dolphin",
        "source": str(work_dir / TIMESERIES_DIR),
        "REF_DATE": ref,
        "sign": "positive = toward satellite",
        "units": "m",
    }
    scale = 1.0
    if cfg_wavelength is None:
        if wavelength_m == "auto":
            wl: float | None = SENTINEL_1_WAVELENGTH_M
        elif wavelength_m is None:
            wl = None
        else:
            wl = float(wavelength_m)
        if wl is None:
            attrs["units"] = "rad"
        else:
            scale = -1.0 * (wl / (4.0 * np.pi))  # source: dolphin/timeseries.py
            attrs["units_converted_by"] = "wintersar"
            attrs["wavelength_m"] = wl
    else:
        attrs["wavelength_m"] = cfg_wavelength
    if scale != 1.0:
        disp = (disp * scale).astype(np.float32)
    vel: NDArray[np.floating] | None = None
    vpath = work_dir / TIMESERIES_DIR / VELOCITY_FILE
    if vpath.exists():
        v, _, _, vnod = _read_raster(vpath)
        if vnod is not None:
            v[v == vnod] = np.nan
        vel = (v * scale).astype(np.float32) if scale != 1.0 else v
    lat: NDArray[np.floating]
    lon: NDArray[np.floating]
    if lat_file is not None and lon_file is not None:
        lat = _read_raster(lat_file)[0].astype(np.float64)
        lon = _read_raster(lon_file)[0].astype(np.float64)
        attrs["coords"] = "radar(lat/lon files)"
    else:
        lat, lon, kind = _latlon_grids(crs, transform, ny, nx)
        attrs["coords"] = kind
    coh: NDArray[np.floating] | None = None
    for sub in (INTERFEROGRAMS_DIR, LINKED_PHASE_DIR):
        cands = (
            sorted((work_dir / sub).glob("temporal_coherence*.tif"))
            if (work_dir / sub).is_dir()
            else []
        )
        if cands:
            coh = _read_raster(cands[-1])[0]
            attrs["temporal_coherence_file"] = str(cands[-1])
            break
    return TimeSeries(
        dates=dates,
        displacement_m=disp,
        lat=lat,
        lon=lon,
        coherence=coh,
        velocity_m_per_yr=vel,
        attrs=attrs,
    )


def save_timeseries_npz(ts: TimeSeries, path: Path) -> Path:
    """Fake-engine-compatible ``timeseries.npz`` (``dates``, ``displacement_m``,
    ``velocity_m_per_yr``) plus ``lat``/``lon``/``coherence``."""
    data: dict[str, Any] = {
        "dates": np.array([d.isoformat() for d in ts.dates]),
        "displacement_m": ts.displacement_m.astype(np.float32),
        "lat": np.asarray(ts.lat),
        "lon": np.asarray(ts.lon),
    }
    if ts.velocity_m_per_yr is not None:
        data["velocity_m_per_yr"] = ts.velocity_m_per_yr.astype(np.float32)
    if ts.coherence is not None:
        data["coherence"] = ts.coherence.astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **data)
    return path


# ---------------------------------------------------------------------- engine


@register_engine
class DolphinEngine(Engine):
    name: ClassVar[str] = "dolphin"
    version_constraint: ClassVar[str] = ">=0.40,<1"  # config keys verified on main / 0.42.7
    stages: ClassVar[tuple[str, ...]] = ("timeseries",)
    install_hint: ClassVar[str] = (
        "mamba install -c conda-forge dolphin  (or pip install dolphin; BSD-3-Clause OR Apache-2.0)"
    )
    license_note: ClassVar[str] = (
        "BSD-3-Clause OR Apache-2.0 (Caltech) — invoked via 'dolphin run' subprocess"
    )

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner: Runner = runner or _subprocess_runner
        self.findings: list[Finding] = []

    # ------------------------------------------------------------------ discovery
    def detect_version(self) -> str | None:
        if find_executable() is None:
            return None
        v = executable_version(EXECUTABLE, ("--version",))
        if v in (None, "unknown"):
            v = python_module_version("dolphin") or v
        return v or "unknown"

    # ------------------------------------------------------------------ run
    def run(
        self, stage: str, inputs: Artifacts, params: dict[str, Any], log_dir: Path
    ) -> Artifacts:
        if stage not in self.stages:
            msg = f"dolphin engine does not implement stage {stage!r}; stages: {self.stages}"
            raise ValueError(msg)
        if self._runner is _subprocess_runner:
            self.require_available()  # EngineNotAvailableError (ENV-001)
        p = dict(params)
        sub = params.get("dolphin")
        if isinstance(sub, Mapping):
            p.update(sub)
        ts_raw = params.get("timeseries")
        if isinstance(ts_raw, Mapping):  # config section (stage_params) → flat keys
            for k, v in ts_raw.items():
                p.setdefault(k, v)
        out = Path(params.get("_out_dir") or Path(log_dir).parent)
        out.mkdir(parents=True, exist_ok=True)
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        stage_log = log_dir / f"{stage}.log"
        findings: list[Finding] = []
        work_dir = Path(str(p.get("workdir") or out / "dolphin"))
        work_dir.mkdir(parents=True, exist_ok=True)

        cslc = self._cslc_files(inputs, p)
        if not cslc:
            findings.append(
                _finding("DOL-002", "FAIL", stage, inputs=", ".join(sorted(inputs.items)) or "-")
            )
            self._fail(log_dir, stage, findings, "no CSLC inputs")
        cfg, cfg_findings = build_config(
            cslc,
            work_dir,
            p,
            cores=int(p.get("_cores") or 1),
            gpu=bool(p.get("_gpu", False)),
            log_file=log_dir / "dolphin.log",
        )
        findings.extend(cfg_findings)
        cfg_path = write_config(cfg, work_dir / CONFIG_FILENAME)
        (out / "dolphin_config.json").write_text(
            json.dumps(mask_mapping(cfg), indent=2), encoding="utf-8"
        )
        exe = find_executable() or EXECUTABLE
        argv = build_run_argv(cfg_path, exe, debug=bool(p.get("debug", False)))
        run_log = log_dir / "dolphin_run.log"
        self._append(
            stage_log, f"START dolphin n_cslc={len(cslc)} workdir={work_dir} argv={' '.join(argv)}"
        )
        rc = self._runner(argv, work_dir, run_log)
        self._append(stage_log, f"dolphin run rc={rc} log={run_log.name}")
        if rc != 0:
            findings.append(_finding("DOL-001", "FAIL", stage, returncode=rc, log=str(run_log)))
            self._fail(log_dir, stage, findings, f"dolphin run failed rc={rc}")
        try:
            ts = read_dolphin_timeseries(
                work_dir,
                lat_file=Path(str(p["lat_file"])) if p.get("lat_file") else None,
                lon_file=Path(str(p["lon_file"])) if p.get("lon_file") else None,
            )
        except FileNotFoundError as e:
            findings.append(
                _finding(
                    "DOL-003", "FAIL", stage, path=str(work_dir / TIMESERIES_DIR), log=str(run_log)
                )
            )
            self._fail(log_dir, stage, findings, str(e))
        if "units_converted_by" in ts.attrs:
            findings.append(_finding("DOL-006", "WARN", stage))
        npz = save_timeseries_npz(ts, out / "timeseries.npz")
        meta = {
            "engine": self.name,
            "version": self.detect_version(),
            "n_dates": ts.n_dates,
            "shape": list(ts.shape),
            "units": ts.attrs.get("units"),
            "sign": ts.attrs.get("sign"),
            "coords": ts.attrs.get("coords"),
            "REF_DATE": ts.attrs.get("REF_DATE"),
            "dolphin_dir": str(work_dir),
            "config": str(cfg_path),
        }
        arts = Artifacts()
        arts.add(Artifact(name="timeseries", path=npz, kind="npz", meta=meta))
        arts.add(
            Artifact(
                name="dolphin_workdir", path=work_dir, kind="dir", meta={"config": str(cfg_path)}
            )
        )
        vel = work_dir / TIMESERIES_DIR / VELOCITY_FILE
        if vel.exists():
            arts.add(
                Artifact(
                    name="velocity",
                    path=vel,
                    kind="cog",
                    meta={"units": f"{ts.attrs.get('units')}/yr"},
                )
            )
        unw_dir = work_dir / UNWRAPPED_DIR
        if unw_dir.is_dir():
            arts.add(
                Artifact(
                    name="unw_dolphin",
                    path=unw_dir,
                    kind="dir",
                    meta={"suffix": UNW_SUFFIX, "conncomp_suffix": CONNCOMP_SUFFIX},
                )
            )
        self._write_findings(log_dir, stage, findings)
        self._append(stage_log, f"END dolphin outputs={list(arts.items)}")
        return arts

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _cslc_files(inputs: Artifacts, p: Mapping[str, Any]) -> list[Path]:
        explicit = p.get("cslc_files")
        if isinstance(explicit, list | tuple) and explicit:
            return [Path(str(f)) for f in explicit]
        glob_raw = p.get("cslc_glob")
        if glob_raw:
            g = str(glob_raw)
            root = Path(g).anchor or "."
            files = (
                sorted(Path(root).glob(str(Path(g).relative_to(root))))
                if Path(g).is_absolute()
                else sorted(Path().glob(g))
            )
            if files:
                return files
        cslc_dir = p.get("cslc_dir")
        if cslc_dir:
            found = discover_cslc_files(Path(str(cslc_dir)))
            if found:
                return found
        for name in ("igrams", "unw", "coreg_manifest"):
            if name in inputs:
                d = inputs[name].meta.get("coreg_slc_dir")
                if d:
                    found = discover_cslc_files(Path(str(d)))
                    if found:
                        return found
        return []

    @staticmethod
    def _append(log: Path, text: str) -> None:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {mask_text(text)}\n")

    def _write_findings(self, log_dir: Path, stage: str, findings: list[Finding]) -> None:
        self.findings = findings
        (log_dir / f"{stage}.findings.json").write_text(
            json.dumps(
                mask_mapping([f.model_dump(mode="json") for f in findings]),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _fail(self, log_dir: Path, stage: str, findings: list[Finding], msg: str) -> None:
        self._write_findings(log_dir, stage, findings)
        raise DolphinRunError(f"dolphin stage {stage!r} failed: {msg}", list(findings))
