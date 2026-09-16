"""ISCE2 topsStack adapter (plan §5.2 ``isce2_topsstack.py``; Phase 2 step ⑤; PERF-07 / PERF-11;
ADR-0026 (flags + run_files facts), ADR-0027 (parallel safety + cleanup), ADR-0029 (reference
geometry reuse)).

``stackSentinel.py`` only *generates* ``configs/`` and ``run_files/run_NN_<step>`` shell scripts;
this adapter runs it through subprocess, parses the run files with :mod:`wintersar.engines.runfiles`
and executes the steps of each pipeline stage in order (jobs of one step concurrently):

* ``fetch``         → SAFE products (``burst2safe`` from the selected burst granules, or a
  user-provided ``slc_dir`` with ``S1*_IW_SLC*.zip|.SAFE``), orbit/aux directories, DEM →
  ``slc_manifest``.
* ``coregister``    → ``stackSentinel.py`` argv from the stage params, run files up to
  ``merge_reference_secondary_slc`` (unpack, baselines, NESD/ESD, geo2rdr, resample, merge) →
  ``coreg_manifest``.
* ``interferogram`` → ``generate_burst_igram`` … ``filter_coherence`` (``unwrap`` only when
  ``unwrap_in_isce`` is set; the default path unwraps with :mod:`wintersar.unwrap`) →
  ``igrams`` (``merged/interferograms``, ISCE flat binaries + ``.xml``/``.vrt``) with a
  MintPy ``prep_isce``-compatible listing.
* ``multilook``     → pass-through: topsStack applies ``-r/-z`` looks in ``merge_burst_igram``.

Verified facts (``# source`` comments; ADR-0026):

* CLI of ``stackSentinel.py`` (argparse ``createParser``): ``-s/--slc_directory``,
  ``-o/--orbit_directory``, ``-a/--aux_directory``, ``-w/--working_directory`` ('./'),
  ``-d/--dem``, ``-p/--polarization`` ('vv'), ``-W/--workflow`` (slc|correlation|
  interferogram|offset, default interferogram), ``-n/--swath_num`` ('1 2 3'), ``-b/--bbox``
  ("Lat/Lon Bounding SNWE"), ``-x/--exclude_dates`` / ``-i/--include_dates`` ('20141007,20141031'),
  ``--start_date``/``--stop_date`` (YYYY-MM-DD), ``-C/--coregistration`` (geometry|NESD, default
  NESD), ``-m/--reference_date``, ``--snr_misreg_threshold`` ('10'),
  ``-e/--esd_coherence_threshold`` ('0.85'), ``-O/--num_overlap_connections`` ('3'),
  ``-c/--num_connections`` ('1'), ``-z/--azimuth_looks`` ('3'), ``-r/--range_looks`` ('9'),
  ``-f/--filter_strength`` ('0.5'), ``-u/--unw_method`` (icu|snaphu, default snaphu),
  ``-rmFilter/--rmFilter``, ``--param_ion``, ``--num_connections_ion`` ('3'),
  ``-useGPU/--useGPU``, ``--num_proc`` (int, 1), ``--num_proc4topo`` (int, 1),
  ``-t/--text_cmd`` (''), ``-V/--virtual_merge`` (True|False).
  # source: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/stackSentinel.py
* run files are named ``'run_{:02d}_<step>'``; the interferogram workflow (NESD) writes
  unpack_topo_reference, unpack_secondary_slc, average_baseline, extract_burst_overlaps,
  overlap_geo2rdr, overlap_resample, pairs_misreg, timeseries_misreg, fullBurst_geo2rdr,
  fullBurst_resample, extract_stack_valid_region, merge_reference_secondary_slc,
  generate_burst_igram, merge_burst_igram, filter_coherence, unwrap.
  "run_files folder exists. … Please remove or rename this folder and try again."
  SAFE discovery: ``glob('S1*_IW_SLC*zip')`` then ``glob('S1*_IW_SLC*SAFE')`` in ``-s``.
  Reference date default: "The reference date was not chosen. The first date is considered
  as reference date."  # source: stackSentinel.py (get_dates, main)
* Update mode (PERF-11, ADR-0029): ``checkCurrentStatus`` looks for ``coreg_secondarys/``;
  with existing coregistered dates it re-processes the last ``2*num_overlap_connections``
  (NESD) or ``num_connections`` dates plus the new ones and sets ``stackUpdate=True``, which
  skips ``run_NN_unpack_topo_reference`` and ``run_NN_extract_burst_overlaps``
  ("The original SAFE files for latest {0} coregistered SLCs is needed").
* Orbits: ``sentinelSLC.get_orbit`` searches ``*.EOF`` in ``-o`` and otherwise runs
  ``fetchOrbit.py -i <safe> -o <work_dir>/orbits`` itself.  # source: Stack.py
* Products (Stack.py config lines): ``merged/interferograms/<ref>_<sec>/fine.int`` (looked),
  ``filt_fine.int``, ``filt_fine.cor`` (FilterAndCoherence), ``filt_fine.unw`` (+ ``.conncomp``,
  unwrap); ``merged/geom_reference/{lat,lon,los,hgt,shadowMask,incLocal}.rdr``;
  ``merged/SLC/<date>/<date>.slc`` (+ ``.full`` suffix unless 1x1 looks, mergeBursts.py);
  ``baselines/<ref>_<sec>/<ref>_<sec>.txt`` (computeBaseline, "Bperp (average):").
  # source: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/Stack.py
  # source: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/mergeBursts.py
* MintPy ``prep_isce.py -f "./merged/interferograms/*/filt_*.unw" -m ./reference/IW1.xml
  -b ./baselines/ -g ./merged/geom_reference/`` and the ``mintpy.load.*`` keys for topsStack.
  # source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/cli/prep_isce.py
  # source: https://github.com/insarlab/MintPy/blob/main/docs/dir_structure.md
* ``isce.__version__ = release_version`` (isce2/__init__.py); importing ``isce`` prints
  "Using default ISCE Path: …" when ``ISCE_HOME`` is unset — hence the marker-based probe.
  # source: https://github.com/isce-framework/isce2/blob/main/__init__.py
* ISCE2 licence: Apache-2.0 (Caltech).  # source: https://github.com/isce-framework/isce2/blob/main/LICENSE
* ``gdal2isce_xml.py -i <file>`` writes the ISCE ``.xml`` for a GDAL raster (DEM sidecar).
  # source: https://github.com/isce-framework/isce2/blob/main/applications/gdal2isce_xml.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

from wintersar.engines import burst2safe, runfiles
from wintersar.engines.base import Engine, register_engine
from wintersar.io.schemas import Artifact, Artifacts, Finding, Severity, StackCandidate
from wintersar.util.hashing import hash_params
from wintersar.util.masking import mask_mapping, mask_text

EXECUTABLE = "stackSentinel.py"
DEM_XML_TOOL = "gdal2isce_xml.py"  # source: isce2/applications/gdal2isce_xml.py
ISCE_VERSION_PROBE = "import isce; print('ISCE_VERSION=' + str(isce.__version__))"
VERSION_RE = re.compile(r"ISCE_VERSION=([0-9][0-9A-Za-z.+\-]*)")
SAFE_GLOBS: tuple[str, ...] = ("S1*_IW_SLC*zip", "S1*_IW_SLC*SAFE")  # source: get_dates()
SAFE_DATE_RE = re.compile(r"_(\d{8})T\d{6}_")

WORKFLOWS: tuple[str, ...] = ("slc", "correlation", "interferogram", "offset")
COREGISTRATIONS: tuple[str, ...] = ("NESD", "geometry")
UNWRAP_METHODS: tuple[str, ...] = ("snaphu", "icu")

#: last run step of each pipeline stage (ADR-0026); ``unwrap`` is appended to
#: ``interferogram`` only when ``unwrap_in_isce`` is true.
STAGE_WINDOWS: dict[str, tuple[str | None, str]] = {
    "coregister": (None, "merge_reference_secondary_slc"),
    "interferogram": ("generate_burst_igram", "filter_coherence"),
}
MERGED_IGRAM_FILES: dict[str, str] = {  # logical name -> file in merged/interferograms/<pair>/
    "int": "fine.int",
    "filt_int": "filt_fine.int",
    "cor": "filt_fine.cor",
    "unw": "filt_fine.unw",
    "conncomp": "filt_fine.unw.conncomp",
}
GEOMETRY_FILES: tuple[str, ...] = (
    "hgt.rdr",
    "lat.rdr",
    "lon.rdr",
    "los.rdr",
    "shadowMask.rdr",
    "incLocal.rdr",
)
#: MintPy ``mintpy.load.*`` keys for topsStack (relative to the topsStack work dir)
# source: MintPy docs/dir_structure.md (ISCE-2 topsStack section)
MINTPY_LOAD_PATTERNS: dict[str, str] = {
    "metaFile": "reference/IW*.xml",
    "baselineDir": "baselines",
    "unwFile": "merged/interferograms/*/filt_*.unw",
    "corFile": "merged/interferograms/*/filt_*.cor",
    "connCompFile": "merged/interferograms/*/filt_*.unw.conncomp",
    "demFile": "merged/geom_reference/hgt.rdr",
    "lookupYFile": "merged/geom_reference/lat.rdr",
    "lookupXFile": "merged/geom_reference/lon.rdr",
    "incAngleFile": "merged/geom_reference/los.rdr",
    "azAngleFile": "merged/geom_reference/los.rdr",
    "shadowMaskFile": "merged/geom_reference/shadowMask.rdr",
}
ISCE_RASTER_READER = "wintersar.io.formats.read_isce_raster"  # owned by the io module
DEFAULT_NUM_CONNECTIONS = 3  # wintersar selection.sequential_connections default (config.py)
SLC_MANIFEST = "slc_manifest.json"
COREG_MANIFEST = "coreg_manifest.json"
IGRAMS_MANIFEST = "igrams_manifest.json"
PREP_ISCE_LISTING = "prep_isce.json"

Runner = Callable[[list[str], Path, Path], int]
JobRunner = Callable[[str, Path | None, Mapping[str, str] | None, Any], int]


class TopsStackError(RuntimeError):
    """A topsStack stage failed; ``findings`` carry the cause → fix keys and the log path."""

    def __init__(self, message: str, findings: list[Finding]) -> None:
        super().__init__(message)
        self.findings = findings


# ---------------------------------------------------------------------- argv builder


@dataclass(frozen=True)
class TopsStackArgs:
    """Explicit ``stackSentinel.py`` options (names mirror the upstream ``dest`` values)."""

    slc_dir: Path
    orbit_dir: Path
    aux_dir: Path
    work_dir: Path
    dem: Path
    polarization: str = "vv"
    workflow: str = "interferogram"
    swaths: tuple[int, ...] = (1, 2, 3)
    bbox: tuple[float, float, float, float] | None = None  # S N W E
    reference_date: str | None = None  # YYYYMMDD
    coregistration: str = "NESD"
    esd_coherence_threshold: float = 0.85
    num_overlap_connections: int = 3
    num_connections: int = 1
    range_looks: int = 9
    azimuth_looks: int = 3
    filter_strength: float = 0.5
    unwrap_method: str = "snaphu"
    rm_filter: bool = False
    exclude_dates: tuple[str, ...] = ()
    include_dates: tuple[str, ...] = ()
    start_date: str | None = None  # YYYY-MM-DD
    stop_date: str | None = None
    use_gpu: bool = False
    num_proc: int = 1
    num_proc4topo: int = 1
    text_cmd: str | None = None
    virtual_merge: bool | None = None
    snr_misreg_threshold: float | None = None
    param_ion: Path | None = None
    num_connections_ion: int | None = None

    def validate(self) -> None:
        if self.workflow not in WORKFLOWS:
            msg = f"workflow must be one of {WORKFLOWS}, got {self.workflow!r}"
            raise ValueError(msg)
        if self.coregistration not in COREGISTRATIONS:
            msg = f"coregistration must be one of {COREGISTRATIONS}, got {self.coregistration!r}"
            raise ValueError(msg)
        if self.unwrap_method not in UNWRAP_METHODS:
            msg = f"unwrap_method must be one of {UNWRAP_METHODS}, got {self.unwrap_method!r}"
            raise ValueError(msg)
        if self.range_looks < 1 or self.azimuth_looks < 1:
            msg = f"looks must be >= 1, got ({self.range_looks}, {self.azimuth_looks})"
            raise ValueError(msg)
        if self.num_connections < 1:
            msg = f"num_connections must be >= 1, got {self.num_connections}"
            raise ValueError(msg)
        if not self.swaths or any(s not in (1, 2, 3) for s in self.swaths):
            msg = f"swaths must be a non-empty subset of (1, 2, 3), got {self.swaths}"
            raise ValueError(msg)
        if self.bbox is not None:
            s, n, w, e = self.bbox
            if not (s < n and w < e):
                msg = f"bbox must be S N W E with S<N and W<E, got {self.bbox}"
                raise ValueError(msg)
        for d in (*self.exclude_dates, *self.include_dates, self.reference_date or "20000101"):
            if not re.fullmatch(r"\d{8}", d):
                msg = f"dates must be YYYYMMDD, got {d!r}"
                raise ValueError(msg)

    def to_argv(self, executable: str = EXECUTABLE) -> list[str]:
        """Exact upstream flags (ADR-0026); only non-default optional flags are emitted."""
        self.validate()
        argv = [
            executable,
            "-s",
            str(self.slc_dir),
            "-o",
            str(self.orbit_dir),
            "-a",
            str(self.aux_dir),
            "-w",
            str(self.work_dir),
            "-d",
            str(self.dem),
            "-p",
            self.polarization.lower(),
            "-W",
            self.workflow,
            "-n",
            " ".join(str(s) for s in self.swaths),
            "-C",
            self.coregistration,
            "-c",
            str(self.num_connections),
            "-r",
            str(self.range_looks),
            "-z",
            str(self.azimuth_looks),
            "-f",
            f"{self.filter_strength:g}",
            "-u",
            self.unwrap_method,
        ]
        if self.bbox is not None:
            argv += ["-b", " ".join(f"{v:g}" for v in self.bbox)]
        if self.reference_date:
            argv += ["-m", self.reference_date]
        if self.coregistration == "NESD":
            argv += ["-e", f"{self.esd_coherence_threshold:g}"]
            argv += ["-O", str(self.num_overlap_connections)]
        if self.snr_misreg_threshold is not None:
            argv += ["--snr_misreg_threshold", f"{self.snr_misreg_threshold:g}"]
        if self.exclude_dates:
            argv += ["-x", ",".join(self.exclude_dates)]
        if self.include_dates:
            argv += ["-i", ",".join(self.include_dates)]
        if self.start_date:
            argv += ["--start_date", self.start_date]
        if self.stop_date:
            argv += ["--stop_date", self.stop_date]
        if self.rm_filter:
            argv.append("--rmFilter")
        if self.param_ion is not None:
            argv += ["--param_ion", str(self.param_ion)]
            if self.num_connections_ion is not None:
                argv += ["--num_connections_ion", str(self.num_connections_ion)]
        if self.use_gpu:
            argv.append("--useGPU")
        argv += ["--num_proc", str(max(1, self.num_proc))]
        argv += ["--num_proc4topo", str(max(1, self.num_proc4topo))]
        if self.text_cmd:
            argv += ["-t", self.text_cmd]
        if self.virtual_merge is not None:
            argv += ["-V", "True" if self.virtual_merge else "False"]
        return argv


# ---------------------------------------------------------------------- helpers


def _finding(rule: str, severity: Severity, scope: str, **params: Any) -> Finding:
    return Finding(
        rule_id=rule,
        severity=severity,
        message_key=f"engines.isce2.{rule}.cause",
        fix_key=f"engines.isce2.{rule}.fix",
        params=params,
        evidence=mask_mapping(dict(params)),
        scope=scope,
    )


def _append(log: Path, text: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {mask_text(text)}\n")


def _write_json(path: Path, data: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(mask_mapping(dict(data)), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"expected a JSON object in {mask_text(str(path))}"
        raise ValueError(msg)
    return data


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


def find_executable() -> str | None:
    return shutil.which(EXECUTABLE)


def parse_isce_version(text: str) -> str | None:
    m = VERSION_RE.search(text)
    return m.group(1) if m else None


def probe_isce_version(python: str | None = None, timeout: float = 60.0) -> str | None:
    """``python -c 'import isce; print(isce.__version__)'`` in a subprocess (never in-process).

    The interpreter is the ``python3`` on PATH (the one topsStack's ``#!/usr/bin/env python3``
    shebang would use), falling back to :data:`sys.executable`.
    """
    exe = python or shutil.which("python3") or sys.executable
    try:
        proc = subprocess.run(
            [exe, "-c", ISCE_VERSION_PROBE],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(os.environ),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_isce_version((proc.stdout or "") + (proc.stderr or ""))


def safe_date(path: Path) -> str | None:
    """``YYYYMMDD`` of a SAFE name (first ``_YYYYMMDDTHHMMSS_`` = sensing start)."""
    m = SAFE_DATE_RE.search(Path(path).name)
    return m.group(1) if m else None


def list_safe_products(slc_dir: Path) -> list[Path]:
    """SAFE zips (preferred, like ``get_dates``) or SAFE directories in ``slc_dir``."""
    slc_dir = Path(slc_dir)
    if not slc_dir.is_dir():
        return []
    for pattern in SAFE_GLOBS:
        found = sorted(slc_dir.glob(pattern))
        if found:
            return found
    return []


def load_stack(inputs: Artifacts, params: Mapping[str, Any]) -> StackCandidate | None:
    """``stack.json`` (precheck output) from ``inputs['stack']`` or ``params['stack']``."""
    path: Path | None = None
    if "stack" in inputs:
        path = Path(inputs["stack"].path)
    elif params.get("stack"):
        path = Path(str(params["stack"]))
    if path is None or not path.exists():
        return None
    return StackCandidate.model_validate_json(path.read_text(encoding="utf-8"))


def granules_per_date(stack: StackCandidate | None) -> dict[str, list[str]]:
    """``{date_iso: [granule, ...]}`` from ``StackCandidate.notes.granules`` (ADR-0020 contract)."""
    if stack is None:
        return {}
    raw = stack.notes.get("granules")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, list[str]] = {}
    for day, per_burst in raw.items():
        if isinstance(per_burst, Mapping):
            out[str(day)] = sorted(str(g) for g in per_burst.values())
        elif isinstance(per_burst, list | tuple):
            out[str(day)] = sorted(str(g) for g in per_burst)
    return out


def _ymd(d: date | str) -> str:
    if isinstance(d, date):
        return f"{d:%Y%m%d}"
    return str(d).replace("-", "")[:8]


def bbox_snwe(
    params: Mapping[str, Any], stack: StackCandidate | None
) -> tuple[float, float, float, float] | None:
    """``(S, N, W, E)`` from ``params['bbox']`` (SNWE list), ``notes.bbox_snwe`` or an AOI WKT."""
    raw = params.get("bbox")
    if raw is None and stack is not None:
        raw = stack.notes.get("bbox_snwe")
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if isinstance(raw, list | tuple) and len(raw) == 4:
        s, n, w, e = (float(v) for v in raw)
        return (s, n, w, e)
    wkt = params.get("aoi_wkt") or (stack.notes.get("aoi_wkt") if stack is not None else None)
    if isinstance(wkt, str) and wkt.strip():
        from shapely import wkt as shapely_wkt  # core dependency

        minx, miny, maxx, maxy = shapely_wkt.loads(wkt).bounds
        return (float(miny), float(maxy), float(minx), float(maxx))
    return None


def derive_num_connections(
    stack: StackCandidate | None, default: int = DEFAULT_NUM_CONNECTIONS
) -> int:
    """Smallest nearest-N that covers every selected pair (``selectNeighborPairs`` builds
    ``(date[i], date[j])`` for ``j`` up to ``i + num_connections``)."""
    if stack is None or not stack.pairs or not stack.dates:
        return default
    order = {d: i for i, d in enumerate(sorted(stack.dates))}
    spans = [
        order[p.secondary] - order[p.reference]
        for p in stack.pairs
        if p.reference in order and p.secondary in order
    ]
    return max(1, max(spans)) if spans else default


def resolve_looks(
    params: Mapping[str, Any],
    *,
    range_pixel_spacing_m: float | None = None,
    azimuth_pixel_spacing_m: float | None = None,
    incidence_deg: float | None = None,
) -> tuple[tuple[int, int], dict[str, Any]]:
    """``(rg, az)`` looks: explicit ``looks: [rg, az]`` or ``'auto'`` via
    :func:`wintersar.select.looks.compute_looks` (lazy import; SEL-09, ADR-0015)."""
    raw = params.get("looks", "auto")
    if isinstance(raw, list | tuple) and len(raw) == 2:
        rg, az = int(raw[0]), int(raw[1])
        return (rg, az), {"mode": "explicit"}
    from wintersar.select import looks as looks_mod

    res = looks_mod.compute_looks(
        range_pixel_spacing_m or looks_mod.IW_NOMINAL_RANGE_SPACING_M,
        azimuth_pixel_spacing_m or looks_mod.IW_NOMINAL_AZIMUTH_SPACING_M,
        incidence_deg or looks_mod.IW_NOMINAL_INCIDENCE_DEG,
        target_pixel_m=float(params.get("target_pixel_m", looks_mod.DEFAULT_TARGET_PIXEL_M)),
    )
    info = {"mode": "auto", **res.as_dict()}
    return (res.rg_looks, res.az_looks), info


def merged_pairs(workdir: Path) -> list[str]:
    root = Path(workdir) / "merged" / "interferograms"
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d{8}_\d{8}", p.name)
    )


def _dates_from_pairs(pairs: Sequence[str]) -> list[str]:
    out: set[str] = set()
    for p in pairs:
        a, b = p.split("_")
        out.update((a, b))
    return sorted(out)


def _isce_files(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "xml": str(path.with_name(path.name + ".xml"))
        if path.with_name(path.name + ".xml").exists()
        else None,
        "vrt": str(path.with_name(path.name + ".vrt"))
        if path.with_name(path.name + ".vrt").exists()
        else None,
        "bytes": path.stat().st_size if path.is_file() else None,
    }


def write_igrams_manifest(
    workdir: Path, out_path: Path, *, looks: tuple[int, int]
) -> dict[str, Any]:
    """Listing of the ISCE flat binaries (``merged/interferograms``, ``merged/geom_reference``).

    Reading the rasters is the io module's job (:data:`ISCE_RASTER_READER`); this manifest
    only records what exists so that downstream stages and ``wintersar diagnose`` can check.
    """
    workdir = Path(workdir)
    pairs = merged_pairs(workdir)
    per_pair: list[dict[str, Any]] = []
    for pair in pairs:
        d = workdir / "merged" / "interferograms" / pair
        per_pair.append(
            {
                "pair": pair,
                "dir": str(d),
                "files": {k: _isce_files(d / v) for k, v in MERGED_IGRAM_FILES.items()},
            }
        )
    geom_dir = workdir / "merged" / "geom_reference"
    manifest: dict[str, Any] = {
        "format": "isce2_flat_binary",
        "reader": ISCE_RASTER_READER,
        "workdir": str(workdir),
        "looks": [looks[0], looks[1]],
        "pairs": pairs,
        "dates": _dates_from_pairs(pairs),
        "interferograms": per_pair,
        "geometry": {f: _isce_files(geom_dir / f) for f in GEOMETRY_FILES},
        "coreg_slc_dir": str(workdir / "merged" / "SLC"),
        "baseline_dir": str(workdir / "baselines"),
        "reference_dir": str(workdir / "reference"),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _write_json(out_path, manifest)
    return manifest


def prep_isce_listing(workdir: Path) -> dict[str, Any]:
    """MintPy ``prep_isce`` / ``mintpy.load.*`` inputs for a topsStack work dir (ADR-0026)."""
    workdir = Path(workdir)
    patterns = {k: str(workdir / v) for k, v in MINTPY_LOAD_PATTERNS.items()}
    return {
        "processor": "isce",
        "patterns": patterns,
        "prep_isce_cmd": [
            "prep_isce.py",
            "-f",
            patterns["unwFile"],
            "-m",
            patterns["metaFile"],
            "-b",
            patterns["baselineDir"],
            "-g",
            str(workdir / "merged" / "geom_reference"),
        ],
        "baseline_file_pattern": str(workdir / "baselines" / "<ref>_<sec>" / "<ref>_<sec>.txt"),
        "baseline_keyword": "Bperp (average):",  # source: mintpy/utils/isce_utils.py read_tops_baseline
    }


# ---------------------------------------------------------------------- engine


@dataclass
class _StageCtx:
    stage: str
    params: dict[str, Any]
    out: Path
    log_dir: Path
    stage_log: Path
    findings: list[Finding] = field(default_factory=list)


@register_engine
class Isce2TopsStackEngine(Engine):
    name: ClassVar[str] = "isce2_topsstack"
    version_constraint: ClassVar[str] = ">=2.6,<3"  # flags/run_files verified on main (2.6.x)
    stages: ClassVar[tuple[str, ...]] = ("fetch", "coregister", "interferogram", "multilook")
    install_hint: ClassVar[str] = (
        "conda install -c conda-forge isce2; export ISCE_STACK=<isce2>/contrib/stack; "
        "export PATH=$PATH:$ISCE_STACK/topsStack (only ONE stack processor on PATH)"
    )
    license_note: ClassVar[str] = "Apache-2.0 (ISCE2, Caltech) — invoked via subprocess only"

    def __init__(
        self,
        runner: Runner | None = None,
        job_runner: JobRunner | None = None,
        version_probe: Callable[[], str | None] | None = None,
    ) -> None:
        self._runner: Runner = runner or _subprocess_runner
        self._job_runner = job_runner
        self._probe = version_probe or probe_isce_version
        self.findings: list[Finding] = []

    # ------------------------------------------------------------------ discovery
    def detect_version(self) -> str | None:
        if find_executable() is None:
            return None
        return self._probe() or "unknown"

    def check_install(self) -> list[Finding]:
        findings = super().check_install()
        if find_executable() is not None and self._probe() is None:
            findings.append(_finding("ISCE2-016", "WARN", "install", executable=EXECUTABLE))
        return findings

    # ------------------------------------------------------------------ run
    def run(
        self, stage: str, inputs: Artifacts, params: dict[str, Any], log_dir: Path
    ) -> Artifacts:
        if stage not in self.stages:
            msg = f"isce2_topsstack does not implement stage {stage!r}; stages: {self.stages}"
            raise ValueError(msg)
        if self._runner is _subprocess_runner and stage != "multilook":
            self.require_available()  # EngineNotAvailableError (ENV-001)
        merged = dict(params)
        sub = params.get("isce2")
        if isinstance(sub, Mapping):
            merged.update(sub)
        out = Path(params.get("_out_dir") or Path(log_dir).parent)
        out.mkdir(parents=True, exist_ok=True)
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        ctx = _StageCtx(stage, merged, out, log_dir, log_dir / f"{stage}.log")
        _append(ctx.stage_log, f"START isce2_topsstack stage={stage}")
        handler = {
            "fetch": self._stage_fetch,
            "coregister": self._stage_coregister,
            "interferogram": self._stage_interferogram,
            "multilook": self._stage_multilook,
        }[stage]
        try:
            arts = handler(inputs, ctx)
        finally:
            self._write_findings(ctx)
        _append(ctx.stage_log, f"END isce2_topsstack stage={stage} outputs={list(arts.items)}")
        return arts

    # ------------------------------------------------------------------ fetch
    def _stage_fetch(self, inputs: Artifacts, ctx: _StageCtx) -> Artifacts:
        p = ctx.params
        stack = load_stack(inputs, p)
        workdir = self.topsstack_workdir(p, stack)
        workdir.mkdir(parents=True, exist_ok=True)
        safes: dict[str, Path] = {}
        slc_dir_raw = p.get("slc_dir")
        if slc_dir_raw:
            slc_dir = Path(str(slc_dir_raw))
            for safe in list_safe_products(slc_dir):
                d = safe_date(safe)
                if d:
                    safes.setdefault(d, safe)
            if not safes:
                ctx.findings.append(
                    _finding(
                        "ISCE2-007",
                        "FAIL",
                        "fetch",
                        slc_dir=str(slc_dir),
                        reason="no_safe_in_slc_dir",
                    )
                )
        else:
            slc_dir = workdir / "SLC"
            granules = granules_per_date(stack)
            if stack is not None and stack.product_type == "SLC":
                ctx.findings.append(
                    _finding(
                        "ISCE2-007",
                        "FAIL",
                        "fetch",
                        slc_dir=str(slc_dir),
                        reason="slc_product_needs_slc_dir",
                    )
                )
            elif not granules:
                ctx.findings.append(
                    _finding(
                        "ISCE2-007", "FAIL", "fetch", slc_dir=str(slc_dir), reason="no_granules"
                    )
                )
            else:
                b2s = burst2safe.check_install()
                ctx.findings.extend(b2s)
                if any(f.rule_id == "ENV-006" for f in b2s) and self._runner is _subprocess_runner:
                    ctx.findings.append(
                        _finding(
                            "ISCE2-007",
                            "FAIL",
                            "fetch",
                            slc_dir=str(slc_dir),
                            reason="burst2safe_missing",
                        )
                    )
                else:
                    built, f2 = burst2safe.build_safes(
                        granules,
                        slc_dir,
                        ctx.log_dir,
                        polarizations=[
                            str(
                                p.get("polarization") or (stack.polarization if stack else "VV")
                            ).upper()
                        ],
                        swaths=[str(s) for s in (stack.subswaths if stack else [])] or None,
                        runner=self._b2s_runner(),
                    )
                    ctx.findings.extend(f2)
                    safes = {_ymd(k): v for k, v in built.items()}
        self._raise_if_fail(ctx, "fetch")

        orbit_dir = Path(str(p.get("orbit_dir") or workdir / "orbits"))
        aux_dir = Path(str(p.get("aux_dir") or workdir / "aux_cal"))
        orbit_dir.mkdir(parents=True, exist_ok=True)
        aux_dir.mkdir(parents=True, exist_ok=True)
        n_eof = len(list(orbit_dir.glob("*.EOF")))
        if n_eof == 0:
            ctx.findings.append(_finding("ISCE2-014", "INFO", "fetch", orbit_dir=str(orbit_dir)))

        dem = self._resolve_dem(ctx, stack, workdir)
        bbox = bbox_snwe(p, stack)
        if bbox is None:
            ctx.findings.append(_finding("ISCE2-006", "WARN", "fetch"))
        reference = p.get("reference_date") or (stack.reference_date if stack else None)
        manifest: dict[str, Any] = {
            "engine": self.name,
            "stack_id": stack.stack_id if stack else None,
            "workdir": str(workdir),
            "slc_dir": str(slc_dir),
            "safes": {k: str(v) for k, v in sorted(safes.items())},
            "dates": sorted(safes),
            "orbit_dir": str(orbit_dir),
            "aux_dir": str(aux_dir),
            "dem": str(dem) if dem else None,
            "bbox_snwe": list(bbox) if bbox else None,
            "swaths": [int(s[-1]) for s in (stack.subswaths if stack else []) if s[-1].isdigit()]
            or [1, 2, 3],
            "polarization": str(
                p.get("polarization") or (stack.polarization if stack else "VV")
            ).lower(),
            "reference_date": _ymd(reference) if reference else None,
            "pairs": [pr.key for pr in stack.pairs] if stack else [],
            "n_dates": len(safes),
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        path = _write_json(ctx.out / SLC_MANIFEST, manifest)
        return Artifacts().add(
            Artifact(
                name="slc_manifest",
                path=path,
                kind="json",
                meta={"n_dates": len(safes), "workdir": str(workdir), "engine": self.name},
            )
        )

    def _b2s_runner(self) -> burst2safe.Runner | None:
        return None if self._runner is _subprocess_runner else self._runner

    def _resolve_dem(
        self, ctx: _StageCtx, stack: StackCandidate | None, workdir: Path
    ) -> Path | None:
        p = ctx.params
        dem_raw = p.get("dem")
        dem: Path | None = Path(str(dem_raw)) if dem_raw else None
        if dem is None:
            wkt = p.get("aoi_wkt") or (stack.notes.get("aoi_wkt") if stack else None)
            if isinstance(wkt, str) and wkt.strip():
                try:
                    from wintersar.select import dem as dem_mod

                    cache = Path(str(p.get("_cache_dir") or Path.home() / ".cache" / "wintersar"))
                    dem = dem_mod.get_dem(wkt, cache)
                except Exception as e:  # DemNotInstalledError / DemFetchError / import errors
                    finding = getattr(e, "finding", None)
                    if isinstance(finding, Finding):
                        ctx.findings.append(finding)
                    ctx.findings.append(
                        _finding("ISCE2-002", "FAIL", "fetch", reason=mask_text(str(e)))
                    )
                    self._raise_if_fail(ctx, "fetch")
            else:
                ctx.findings.append(
                    _finding("ISCE2-002", "FAIL", "fetch", reason="no dem parameter and no AOI")
                )
                self._raise_if_fail(ctx, "fetch")
        assert dem is not None
        if not dem.exists():
            ctx.findings.append(
                _finding("ISCE2-002", "FAIL", "fetch", reason=f"not found: {mask_text(str(dem))}")
            )
            self._raise_if_fail(ctx, "fetch")
        xml = dem.with_name(dem.name + ".xml")
        if not xml.exists():
            tool = shutil.which(DEM_XML_TOOL)
            if tool is None and self._runner is _subprocess_runner:
                ctx.findings.append(
                    _finding(
                        "ISCE2-002",
                        "FAIL",
                        "fetch",
                        reason=f"missing ISCE xml {mask_text(str(xml))}",
                    )
                )
                self._raise_if_fail(ctx, "fetch")
            rc = self._runner(
                [tool or DEM_XML_TOOL, "-i", str(dem)], workdir, ctx.log_dir / "gdal2isce_xml.log"
            )
            if rc != 0 or not xml.exists():
                ctx.findings.append(
                    _finding("ISCE2-002", "FAIL", "fetch", reason=f"{DEM_XML_TOOL} rc={rc}")
                )
                self._raise_if_fail(ctx, "fetch")
        return dem

    # ------------------------------------------------------------------ coregister
    def _stage_coregister(self, inputs: Artifacts, ctx: _StageCtx) -> Artifacts:
        p = ctx.params
        if "slc_manifest" not in inputs:
            ctx.findings.append(
                _finding(
                    "ISCE2-012", "FAIL", "coregister", stage="coregister", needed="slc_manifest"
                )
            )
            self._raise_if_fail(ctx, "coregister")
        m = _read_json(Path(inputs["slc_manifest"].path))
        stack = load_stack(inputs, p)
        workdir = Path(str(p.get("workdir") or m["workdir"]))
        workdir.mkdir(parents=True, exist_ok=True)
        looks, looks_info = resolve_looks(p)
        if looks_info.get("mode") == "auto":
            ctx.findings.append(
                _finding(
                    "ISCE2-010",
                    "INFO",
                    "coregister",
                    rg=looks[0],
                    az=looks[1],
                    target_m=looks_info.get("target_m"),
                    pixel_m=round(float(looks_info.get("pixel_m", 0.0)), 1),
                )
            )
        n_conn = int(p.get("num_connections") or derive_num_connections(stack))
        if not p.get("num_connections"):
            ctx.findings.append(_finding("ISCE2-013", "INFO", "coregister", n=n_conn))
        filt_raw = p.get("filter")
        filt: Mapping[str, Any] = filt_raw if isinstance(filt_raw, Mapping) else {}
        filter_strength = float(
            p.get(
                "filter_strength",
                filt.get("alpha", 0.5) if filt.get("type", "goldstein") != "none" else 0.0,
            )
        )
        cores = int(p.get("_cores") or 1)
        bbox_raw = m.get("bbox_snwe")
        args = TopsStackArgs(
            slc_dir=Path(m["slc_dir"]),
            orbit_dir=Path(m["orbit_dir"]),
            aux_dir=Path(m["aux_dir"]),
            work_dir=workdir,
            dem=Path(str(m["dem"])),
            polarization=str(m.get("polarization") or "vv"),
            workflow=str(p.get("workflow") or "interferogram"),
            swaths=tuple(int(s) for s in m.get("swaths") or (1, 2, 3)),
            bbox=tuple(float(v) for v in bbox_raw) if bbox_raw else None,  # type: ignore[arg-type]
            reference_date=m.get("reference_date"),
            coregistration="NESD" if bool(p.get("esd", True)) else "geometry",
            esd_coherence_threshold=float(p.get("esd_coherence_threshold", 0.85)),
            num_overlap_connections=int(p.get("num_overlap_connections", 3)),
            num_connections=n_conn,
            range_looks=looks[0],
            azimuth_looks=looks[1],
            filter_strength=filter_strength,
            unwrap_method=str(p.get("unwrap_method") or "snaphu"),
            rm_filter=bool(p.get("rm_filter", False)),
            include_dates=tuple(_ymd(d) for d in m.get("dates") or ()),
            use_gpu=bool(p.get("use_gpu", p.get("_gpu", False))),
            num_proc=1,  # wintersar schedules the jobs itself (PERF-07)
            num_proc4topo=int(p.get("num_proc4topo") or cores),
            text_cmd=p.get("text_cmd"),
            virtual_merge=p.get("virtual_merge"),
        )
        argv = args.to_argv(find_executable() or EXECUTABLE)
        run_dir = workdir / "run_files"
        update_mode = (workdir / "coreg_secondarys").is_dir() and any(
            (workdir / "coreg_secondarys").iterdir()
        )
        regenerate = bool(p.get("regenerate_run_files", False)) or not run_dir.is_dir()
        if update_mode and not regenerate:
            known = {_ymd(d.name) for d in (workdir / "coreg_secondarys").iterdir() if d.is_dir()}
            new_dates = sorted(
                set(_ymd(d) for d in m.get("dates") or ()) - known - {str(m.get("reference_date"))}
            )
            regenerate = bool(new_dates)
        if regenerate:
            if update_mode:
                ctx.findings.append(
                    _finding("ISCE2-008", "INFO", "coregister", workdir=str(workdir))
                )
            self._archive(run_dir, ctx)
            self._archive(workdir / "configs", ctx)
            runfiles.save_state(workdir, {"completed": []})
            log = ctx.log_dir / "stackSentinel.log"
            _append(ctx.stage_log, f"stackSentinel argv={' '.join(argv)}")
            rc = self._runner(argv, workdir, log)
            if rc != 0 or not run_dir.is_dir():
                ctx.findings.append(
                    _finding("ISCE2-003", "FAIL", "coregister", returncode=rc, log=str(log))
                )
                self._raise_if_fail(ctx, "coregister")
        steps = runfiles.parse_run_files(run_dir)
        results = self._run_window(ctx, steps, workdir, STAGE_WINDOWS["coregister"])
        manifest: dict[str, Any] = {
            "engine": self.name,
            "version": self.detect_version(),
            "workdir": str(workdir),
            "run_files_dir": str(run_dir),
            "argv": argv,
            "looks": [looks[0], looks[1]],
            "looks_info": looks_info,
            "num_connections": n_conn,
            "coregistration": args.coregistration,
            "steps": [s.name for s in steps],
            "ran": [r.step.name for r in results],
            "completed": sorted(runfiles.completed_steps(workdir)),
            "update_mode": update_mode,
            "slc_manifest": str(inputs["slc_manifest"].path),
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        path = _write_json(ctx.out / COREG_MANIFEST, manifest)
        return Artifacts().add(
            Artifact(
                name="coreg_manifest",
                path=path,
                kind="json",
                meta={"workdir": str(workdir), "looks": [looks[0], looks[1]], "engine": self.name},
            )
        )

    # ------------------------------------------------------------------ interferogram
    def _stage_interferogram(self, inputs: Artifacts, ctx: _StageCtx) -> Artifacts:
        p = ctx.params
        if "coreg_manifest" not in inputs:
            ctx.findings.append(
                _finding(
                    "ISCE2-012",
                    "FAIL",
                    "interferogram",
                    stage="interferogram",
                    needed="coreg_manifest",
                )
            )
            self._raise_if_fail(ctx, "interferogram")
        m = _read_json(Path(inputs["coreg_manifest"].path))
        workdir = Path(str(p.get("workdir") or m["workdir"]))
        run_dir = workdir / "run_files"
        if not run_dir.is_dir():
            ctx.findings.append(
                _finding(
                    "ISCE2-003", "FAIL", "interferogram", returncode=-1, log=str(ctx.stage_log)
                )
            )
            self._raise_if_fail(ctx, "interferogram")
        looks_raw = m.get("looks") or [9, 3]
        looks = (int(looks_raw[0]), int(looks_raw[1]))
        start, until = STAGE_WINDOWS["interferogram"]
        if bool(p.get("unwrap_in_isce", False)):
            until = "unwrap"
        steps = runfiles.parse_run_files(run_dir)
        self._run_window(ctx, steps, workdir, (start, until))
        pairs = merged_pairs(workdir)
        if not pairs:
            ctx.findings.append(
                _finding(
                    "ISCE2-009",
                    "FAIL",
                    "interferogram",
                    path=str(workdir / "merged" / "interferograms"),
                    step=until,
                )
            )
            self._raise_if_fail(ctx, "interferogram")
        missing = [
            f"{pair}/{MERGED_IGRAM_FILES['cor']}"
            for pair in pairs
            if not (
                workdir / "merged" / "interferograms" / pair / MERGED_IGRAM_FILES["cor"]
            ).exists()
        ]
        if missing:
            ctx.findings.append(
                _finding(
                    "ISCE2-009", "FAIL", "interferogram", path=", ".join(missing[:5]), step=until
                )
            )
            self._raise_if_fail(ctx, "interferogram")
        manifest = write_igrams_manifest(workdir, ctx.out / IGRAMS_MANIFEST, looks=looks)
        listing = prep_isce_listing(workdir)
        listing_path = _write_json(ctx.out / PREP_ISCE_LISTING, listing)
        igram_dir = workdir / "merged" / "interferograms"
        meta = {
            "engine": self.name,
            "processor": "isce",
            "format": "isce2_flat_binary",
            "reader": ISCE_RASTER_READER,
            "n_pairs": len(pairs),
            "pairs": pairs,
            "dates": manifest["dates"],
            "looks": [looks[0], looks[1]],
            "unwrapped": until == "unwrap",
            "manifest": str(ctx.out / IGRAMS_MANIFEST),
            "prep_isce": str(listing_path),
            "patterns": listing["patterns"],
            "workdir": str(workdir),
            "coreg_slc_dir": str(workdir / "merged" / "SLC"),
            "geometry_dir": str(workdir / "merged" / "geom_reference"),
            "baseline_dir": str(workdir / "baselines"),
            "meta_file": listing["patterns"]["metaFile"],
        }
        arts = Artifacts()
        arts.add(Artifact(name="igrams", path=igram_dir, kind="dir", meta=meta))
        arts.add(Artifact(name="igrams_manifest", path=ctx.out / IGRAMS_MANIFEST, kind="json"))
        arts.add(
            Artifact(name="prep_isce", path=listing_path, kind="json", meta={"processor": "isce"})
        )
        if until == "unwrap":
            arts.add(
                Artifact(
                    name="unw",
                    path=igram_dir,
                    kind="dir",
                    meta={**meta, "unw_file": MERGED_IGRAM_FILES["unw"]},
                )
            )
        return arts

    # ------------------------------------------------------------------ multilook
    def _stage_multilook(self, inputs: Artifacts, ctx: _StageCtx) -> Artifacts:
        if "igrams" not in inputs:
            ctx.findings.append(
                _finding("ISCE2-012", "FAIL", "multilook", stage="multilook", needed="igrams")
            )
            self._raise_if_fail(ctx, "multilook")
        src = inputs["igrams"]
        meta = {**src.meta, "multilook": "applied by topsStack merge_burst_igram (-r/-z)"}
        arts = Artifacts().add(Artifact(name="igrams", path=src.path, kind=src.kind, meta=meta))
        for name in ("igrams_manifest", "prep_isce", "unw"):
            if name in inputs:
                arts.add(inputs[name])
        _write_json(
            ctx.out / "multilook.json", {"looks": meta.get("looks"), "source": str(src.path)}
        )
        return arts

    # ------------------------------------------------------------------ shared
    def topsstack_workdir(self, params: Mapping[str, Any], stack: StackCandidate | None) -> Path:
        """Persistent topsStack work dir (stable across DAG node hashes so that update mode /
        reference-geometry reuse works, ADR-0029): ``<workdir>/isce2_topsstack/<stack>_<geomkey>``."""
        explicit = params.get("workdir")
        if explicit:
            return Path(str(explicit))
        base = params.get("_workdir")
        root = (
            Path(str(base)) / "isce2_topsstack"
            if base
            else Path(str(params.get("_out_dir") or ".")) / "topsStack"
        )
        key_src = {
            "stack_id": stack.stack_id if stack else None,
            "reference_date": _ymd(
                str(params.get("reference_date") or (stack.reference_date if stack else "") or "")
            ),
            "bbox": bbox_snwe(params, stack),
            "swaths": list(stack.subswaths) if stack else None,
            "polarization": params.get("polarization") or (stack.polarization if stack else None),
            "esd": bool(params.get("esd", True)),
            "dem": str(params.get("dem") or ""),
        }
        name = f"{stack.stack_id if stack else 'stack'}_{hash_params(key_src, length=8)}"
        return root / name

    def _archive(self, path: Path, ctx: _StageCtx) -> None:
        if path.is_dir():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            target = path.with_name(f"{path.name}.bak-{stamp}")
            path.rename(target)
            ctx.findings.append(
                _finding("ISCE2-015", "INFO", ctx.stage, path=str(path), backup=str(target))
            )

    def _run_window(
        self,
        ctx: _StageCtx,
        steps: list[runfiles.RunStep],
        workdir: Path,
        window: tuple[str | None, str],
    ) -> list[runfiles.StepResult]:
        p = ctx.params
        names = [s.name for s in steps]
        start, until = window
        start_key = start if start in names else None
        until_key = until if until in names else (names[-1] if names else None)
        wanted = runfiles.select_steps(steps, start_key, until_key) if steps else []
        pending = runfiles.pending_steps(wanted, workdir)
        skipped = [s.name for s in wanted if s not in pending]
        if skipped:
            _append(ctx.stage_log, f"skip completed steps: {skipped}")
        policy = str(p.get("cleanup") or "stage")
        if policy not in ("none", "stage", "aggressive"):
            policy = "stage"
        cores = int(p.get("_cores") or 1)
        caps_raw = p.get("max_parallel_per_step")
        caps = (
            {str(k): (None if v is None else int(v)) for k, v in caps_raw.items()}
            if isinstance(caps_raw, Mapping)
            else None
        )
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (p.get("env") or {}).items()})

        def _on_step(res: runfiles.StepResult) -> None:
            _append(
                ctx.stage_log,
                f"STEP {res.step.filename} status={res.status} n_jobs={res.step.n_jobs} n_parallel={res.n_parallel} elapsed_s={(res.duration_s or 0):.1f}",
            )
            if res.status == "ok":
                runfiles.mark_completed(workdir, res.step)
                if res.cleanup_removed:
                    ctx.findings.append(
                        _finding(
                            "ISCE2-011",
                            "INFO",
                            ctx.stage,
                            policy=policy,
                            step=res.step.name,
                            n=len(res.cleanup_removed),
                        )
                    )

        t0 = time.monotonic()
        results = runfiles.run_steps(
            pending,
            cores,
            ctx.log_dir,
            max_parallel_per_step=caps,
            retries=int(p.get("retries", 1)),
            dry_run=bool(p.get("dry_run", False)),
            cleanup=runfiles.make_cleanup(policy, workdir),  # type: ignore[arg-type]
            cwd=workdir,
            env=env,
            timeout_s=p.get("job_timeout_s"),
            on_step=_on_step,
            runner=self._job_runner,
        )
        _append(
            ctx.stage_log,
            f"run_steps done n_steps={len(results)} elapsed_s={time.monotonic() - t0:.1f}",
        )
        fail = runfiles.first_failure(results)
        if fail is not None:
            ctx.findings.append(
                _finding(
                    "ISCE2-001",
                    "FAIL",
                    ctx.stage,
                    step=fail.step.name,
                    run_file=fail.step.filename,
                    job_index=fail.job.index,
                    command=mask_text(fail.job.command),
                    returncode=fail.job.returncode,
                    attempts=fail.job.attempts,
                    log=str(fail.log_path),
                )
            )
            self._raise_if_fail(ctx, ctx.stage)
        return results

    def _raise_if_fail(self, ctx: _StageCtx, stage: str) -> None:
        if any(f.is_fail for f in ctx.findings):
            self._write_findings(ctx)
            ids = [f.rule_id for f in ctx.findings if f.is_fail]
            msg = f"isce2_topsstack stage {stage!r} failed: {ids}"
            raise TopsStackError(msg, list(ctx.findings))

    def _write_findings(self, ctx: _StageCtx) -> None:
        self.findings = list(ctx.findings)
        (ctx.log_dir / f"{ctx.stage}.findings.json").write_text(
            json.dumps(
                mask_mapping([f.model_dump(mode="json") for f in ctx.findings]),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
