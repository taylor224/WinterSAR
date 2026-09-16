"""MintPy ``smallbaselineApp.py`` adapter (plan §5.2 ``mintpy.py``; Phase 2 step ②; PERF-09;
ADR-0021).

MintPy is GPL-3 and is **never imported** (rule 11.2, ADR-0001): every step runs as
``smallbaselineApp.py <template> --dostep <step> --dir <workdir>`` through subprocess and
the HDF5 results are read with ``h5py`` (:func:`read_timeseries_h5`).

Stage → MintPy steps (order verified from ``mintpy/defaults/template.py`` STEP_LIST, 1.6.4):

* ``timeseries``  : load_data → modify_network → reference_point → quick_overview →
  correct_unwrap_error → invert_network
* ``corrections`` : correct_LOD → correct_SET → correct_ionosphere → correct_troposphere →
  deramp → correct_topography → residual_RMS → reference_date → velocity
* ``geocode``     : geocode → google_earth → hdfeos5

# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/cli/smallbaselineApp.py
#   (positional ``customTemplateFile``; ``--dir/--work-dir``; ``--dostep STEP``;
#    ``-v/--version`` prints ``mintpy.version.version_description`` =
#    "MintPy version {v}, date {d}")
# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/objects/stack.py
#   (timeseries.h5: datasets ``timeseries`` float32 (n_date, length, width) in metres,
#    ``date`` bytes 'YYYYMMDD', optional ``bperp``; all metadata as root attrs (str))
# source: https://mintpy.readthedocs.io/en/latest/api/attributes/ (X_FIRST/Y_FIRST/X_STEP/
#   Y_STEP/X_UNIT/Y_UNIT/REF_LAT/REF_LON/REF_X/REF_Y/REF_DATE/HEADING/UNIT/FILE_TYPE/EPSG)
# source: mintpy/utils/utils0.py get_lat_lon: pixel centre = Y_FIRST + Y_STEP * (row + 0.5)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from numpy.typing import NDArray

from wintersar.engines import aux_cache
from wintersar.engines import mintpy_template as tpl
from wintersar.engines.base import Engine, executable_version, register_engine
from wintersar.io.schemas import Artifact, Artifacts, Finding, Severity
from wintersar.io.timeseries import TimeSeries
from wintersar.util import sysinfo
from wintersar.util.masking import mask_mapping, mask_text

EXECUTABLE = "smallbaselineApp.py"
TEMPLATE_FILENAME = "wintersar_mintpy.cfg"  # must not be named smallbaselineApp.cfg (CLI help)
TEMPLATE_JSON = "wintersar_template.json"
VERSION_RE = re.compile(r"MintPy version\s+([0-9][0-9A-Za-z.+\-]*)\s*,")

STAGE_STEPS: dict[str, tuple[str, ...]] = {
    "timeseries": tpl.STEP_LIST[0:6],
    "corrections": tpl.STEP_LIST[6:15],
    "geocode": tpl.STEP_LIST[15:18],
}
assert STAGE_STEPS["timeseries"][-1] == "invert_network"
assert STAGE_STEPS["corrections"][-1] == "velocity"
assert STAGE_STEPS["geocode"] == ("geocode", "google_earth", "hdfeos5")

# log patterns for diagnose (own KB ids MP-00x; cause -> fix texts in i18n)
_OOM_RE = re.compile(r"MemoryError|Cannot allocate memory|Killed|out of memory|OOM", re.I)
_MISSING_RE = re.compile(
    r"No such file|FileNotFoundError|no input .*file|ERROR: no .*found|not found", re.I
)
_ERROR_RE = re.compile(r"^(Traceback|.*Error:|ERROR)", re.I)

Runner = Callable[[list[str], Path, Path], int]


class MintPyStepError(RuntimeError):
    """A ``smallbaselineApp.py --dostep`` call failed (findings already written)."""

    def __init__(
        self, step: str, returncode: int, log_path: Path | None, findings: list[Finding]
    ) -> None:
        super().__init__(f"MintPy step {step!r} failed with exit code {returncode}")
        self.step = step
        self.returncode = returncode
        self.log_path = log_path
        self.findings = findings


def parse_version(text: str) -> str | None:
    m = VERSION_RE.search(text)
    return m.group(1) if m else None


def find_executable() -> str | None:
    return shutil.which(EXECUTABLE)


def _subprocess_runner(cmd: list[str], cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"# {datetime.now(UTC).isoformat(timespec='seconds')} $ {mask_text(' '.join(cmd))}\n"
        )
        fh.flush()
        proc = subprocess.run(
            cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, check=False, env=dict(os.environ)
        )
    return int(proc.returncode)


def _default_cache_dir(params: Mapping[str, Any]) -> Path:
    v = params.get("_cache_dir")
    if v:
        return Path(v)
    env = os.environ.get("WINTERSAR_CACHE")
    return Path(env) if env else Path.home() / ".cache" / "wintersar"


def _finding(rule: str, severity: Severity, scope: str, **params: Any) -> Finding:
    return Finding(
        rule_id=rule,
        severity=severity,
        message_key=f"engines.mintpy.{rule}.cause",
        fix_key=f"engines.mintpy.{rule}.fix",
        params=params,
        evidence=dict(params),
        scope=scope,
    )


# ---------------------------------------------------------------------- engine


@register_engine
class MintPyEngine(Engine):
    name: ClassVar[str] = "mintpy"
    version_constraint: ClassVar[str] = ">=1.5,<2"  # STEP_LIST/template keys verified on 1.6.4
    stages: ClassVar[tuple[str, ...]] = ("timeseries", "corrections", "geocode")
    install_hint: ClassVar[str] = (
        "conda install -c conda-forge mintpy  (or pip install mintpy); GPL-3, subprocess only"
    )
    license_note: ClassVar[str] = (
        "GPL-3.0 — never imported; invoked via smallbaselineApp.py subprocess"
    )

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner: Runner = runner or _subprocess_runner
        self.findings: list[Finding] = []

    # ------------------------------------------------------------------ discovery
    def detect_version(self) -> str | None:
        if find_executable() is None:
            return None
        return executable_version(EXECUTABLE, ("--version",), parser=parse_version)

    def check_install(self) -> list[Finding]:
        findings = super().check_install()
        if self.detect_version() == "unknown":
            findings.append(
                _finding(
                    "MP-005",
                    "WARN",
                    "mintpy",
                    output="(unparseable)",
                    constraint=self.version_constraint,
                )
            )
        return findings

    # ------------------------------------------------------------------ run
    def run(
        self, stage: str, inputs: Artifacts, params: dict[str, Any], log_dir: Path
    ) -> Artifacts:
        if stage not in STAGE_STEPS:
            msg = f"mintpy engine does not implement stage {stage!r}; stages: {self.stages}"
            raise ValueError(msg)
        out = Path(params.get("_out_dir") or log_dir.parent)
        out.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        stage_log = log_dir / f"{stage}.log"
        findings: list[Finding] = []

        if stage == "timeseries":
            workdir = Path(params.get("workdir") or out / "mintpy")
            workdir.mkdir(parents=True, exist_ok=True)
            entries = self._build_entries(inputs, params, workdir, findings)
            cfg = tpl.write_template(workdir / TEMPLATE_FILENAME, entries)
            (workdir / TEMPLATE_JSON).write_text(json.dumps(entries, indent=2), encoding="utf-8")
        else:
            inherited = self._inherit_workdir(inputs, params)
            if inherited is None:
                findings.append(_finding("MP-004", "FAIL", stage, stage=stage))
                self._write_findings(log_dir, stage, findings)
                raise MintPyStepError("inputs", 2, None, findings)
            workdir = inherited
            cfg = workdir / TEMPLATE_FILENAME
            entries = self._load_entries(workdir)
        if findings and any(f.is_fail for f in findings):
            self._write_findings(log_dir, stage, findings)
            raise MintPyStepError("template", 2, None, findings)

        exe = find_executable()
        if exe is None:
            if self._runner is _subprocess_runner:
                self.require_available()  # raises EngineNotAvailableError (ENV-001)
            exe = EXECUTABLE  # injected runner (tests / dry-run) receives the bare name
        steps = self._steps_for(stage, params, entries)
        timings: dict[str, float] = {}
        _append(stage_log, f"START mintpy stage={stage} workdir={workdir} steps={list(steps)}")
        for step in steps:
            step_log = log_dir / f"mintpy_{step}.log"
            cmd = [exe, str(cfg), "--dostep", step, "--dir", str(workdir)]
            t0 = time.monotonic()
            rc = self._runner(cmd, workdir, step_log)
            timings[step] = time.monotonic() - t0
            _append(
                stage_log, f"STEP {step} rc={rc} elapsed_s={timings[step]:.1f} log={step_log.name}"
            )
            if rc != 0:
                findings.append(
                    _finding(
                        "MP-001",
                        "FAIL",
                        stage,
                        step=step,
                        returncode=rc,
                        log=mask_text(str(step_log)),
                    )
                )
                findings.extend(self.parse_log(step_log, entries=entries))
                self._write_findings(log_dir, stage, findings)
                raise MintPyStepError(step, rc, step_log, findings)

        arts, missing = self._collect_artifacts(stage, workdir, entries, cfg)
        for path in missing:
            findings.append(
                _finding(
                    "MP-007",
                    "FAIL",
                    stage,
                    step=steps[-1],
                    path=mask_text(str(path)),
                    log=mask_text(str(stage_log)),
                )
            )
        self._write_findings(log_dir, stage, findings)
        manifest = {
            "engine": self.name,
            "version": self.detect_version(),
            "stage": stage,
            "workdir": str(workdir),
            "steps": list(steps),
            "timings_s": timings,
            "outputs": {k: str(v.path) for k, v in arts.items.items()},
            "template": entries,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        (out / f"mintpy_{stage}.json").write_text(
            json.dumps(mask_mapping(manifest), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if missing:
            raise MintPyStepError(steps[-1], 0, stage_log, findings)
        _append(stage_log, f"END mintpy stage={stage} outputs={list(arts.items)}")
        return arts

    # ------------------------------------------------------------------ helpers
    def _build_entries(
        self, inputs: Artifacts, params: Mapping[str, Any], workdir: Path, findings: list[Finding]
    ) -> dict[str, str]:
        src: Artifact | None = None
        for name in ("unw", "igrams"):
            if name in inputs:
                src = inputs[name]
                break
        if src is None:
            findings.append(
                _finding("MP-003", "FAIL", "timeseries", pattern="<no unw/igrams artifact>")
            )
            return {}
        processor = str(params.get("processor") or src.meta.get("processor") or "hyp3")
        patterns = (
            src.meta.get("patterns") if isinstance(src.meta.get("patterns"), Mapping) else None
        )
        ts_raw = params.get("timeseries")
        ts: Mapping[str, Any] = ts_raw if isinstance(ts_raw, Mapping) else params
        sel_raw = params.get("selection")
        sel: Mapping[str, Any] | None = sel_raw if isinstance(sel_raw, Mapping) else None
        compute_raw = params.get("compute")
        compute: Mapping[str, Any] = compute_raw if isinstance(compute_raw, Mapping) else {}
        cores = params.get("_cores", compute.get("cores"))
        memory = params.get("_memory_gb", compute.get("memory_gb"))
        machine = sysinfo.detect()
        weather = aux_cache.weather_dir(_default_cache_dir(params), "ERA5")
        gacos = params.get("gacos_dir")
        if ts.get("reference_point") in (None, "auto", "auto_recommend"):
            findings.append(_finding("MP-006", "WARN", "timeseries"))
        return tpl.build_template(
            processor=processor,
            data_dir=Path(src.path),
            timeseries=ts,
            selection=sel,
            machine=machine,
            cores=None if cores in (None, "auto") else int(cores),
            memory_gb=None if memory in (None, "auto") else float(memory),
            patterns=patterns,
            clip_suffix=tpl_clip(src.meta),
            weather_dir=weather,
            gacos_dir=Path(gacos) if gacos else None,
            extra=params.get("template_extra"),
        )

    @staticmethod
    def _inherit_workdir(inputs: Artifacts, params: Mapping[str, Any]) -> Path | None:
        v = params.get("workdir")
        if v is None and "mintpy_workdir" in inputs:
            v = inputs["mintpy_workdir"].path
        if v is None:
            return None
        wd = Path(v)
        return wd if (wd / TEMPLATE_FILENAME).exists() else None

    @staticmethod
    def _load_entries(workdir: Path) -> dict[str, str]:
        j = workdir / TEMPLATE_JSON
        if j.exists():
            return {str(k): str(v) for k, v in json.loads(j.read_text(encoding="utf-8")).items()}
        return tpl.parse_template((workdir / TEMPLATE_FILENAME).read_text(encoding="utf-8"))

    @staticmethod
    def _steps_for(
        stage: str, params: Mapping[str, Any], entries: Mapping[str, str]
    ) -> tuple[str, ...]:
        override = params.get("steps")
        if override:
            steps = tuple(str(s) for s in override)
            bad = [s for s in steps if s not in tpl.STEP_LIST]
            if bad:
                msg = f"unknown MintPy steps {bad}; known: {tpl.STEP_LIST}"
                raise ValueError(msg)
            return steps
        steps = STAGE_STEPS[stage]
        if stage == "geocode" and entries.get("mintpy.save.hdfEos5", "no") in ("no", "auto"):
            steps = tuple(s for s in steps if s != "hdfeos5")
        return steps

    @staticmethod
    def _collect_artifacts(
        stage: str, workdir: Path, entries: Mapping[str, str], cfg: Path
    ) -> tuple[Artifacts, list[Path]]:
        arts = Artifacts()
        missing: list[Path] = []
        arts.add(Artifact(name="mintpy_workdir", path=workdir, kind="dir", meta={"stage": stage}))
        arts.add(Artifact(name="mintpy_template", path=cfg, kind="file"))
        geom = _first_existing(
            workdir / "inputs" / "geometryGeo.h5", workdir / "inputs" / "geometryRadar.h5"
        )
        if geom is not None:
            arts.add(Artifact(name="geometry", path=geom, kind="h5"))
        stack = workdir / "inputs" / "ifgramStack.h5"
        if stack.exists():
            arts.add(Artifact(name="ifgram_stack", path=stack, kind="h5"))
        if stage == "timeseries":
            ts = workdir / "timeseries.h5"
            if ts.exists():
                arts.add(Artifact(name="timeseries", path=ts, kind="h5", meta=_h5_meta(ts)))
            else:
                missing.append(ts)
            for name, fn in (
                ("temporal_coherence", "temporalCoherence.h5"),
                ("mask", "maskTempCoh.h5"),
            ):
                p = workdir / fn
                if p.exists():
                    arts.add(Artifact(name=name, path=p, kind="h5"))
            return arts, missing
        expected = workdir / tpl.expected_timeseries_filename(entries)
        ts_final = expected if expected.exists() else _newest(workdir.glob("timeseries*.h5"))
        if ts_final is None:
            missing.append(expected)
            return arts, missing
        geocoded = "Y_FIRST" in read_h5_attrs(ts_final)
        if stage == "corrections":
            arts.add(Artifact(name="timeseries", path=ts_final, kind="h5", meta=_h5_meta(ts_final)))
            vel = workdir / "velocity.h5"
            if vel.exists():
                arts.add(Artifact(name="velocity", path=vel, kind="h5", meta=_h5_meta(vel)))
            else:
                missing.append(vel)
            return arts, missing
        # geocode: MintPy skips geocoding when inputs are already geocoded (Y_FIRST present)
        if geocoded:
            ts_geo, vel_geo = ts_final, workdir / "velocity.h5"
            tcoh, mask = workdir / "temporalCoherence.h5", workdir / "maskTempCoh.h5"
            geom_geo = geom
        else:
            geo = workdir / "geo"
            ts_geo, vel_geo = geo / f"geo_{ts_final.name}", geo / "geo_velocity.h5"
            tcoh, mask = geo / "geo_temporalCoherence.h5", geo / "geo_maskTempCoh.h5"
            geom_geo = geo / f"geo_{geom.name}" if geom is not None else None
        for name, p in (("timeseries", ts_geo), ("velocity", vel_geo)):
            if p.exists():
                arts.add(
                    Artifact(name=name, path=p, kind="h5", meta={**_h5_meta(p), "geocoded": True})
                )
            else:
                missing.append(p)
        for name, opt in (("temporal_coherence", tcoh), ("mask", mask), ("geometry", geom_geo)):
            if opt is not None and opt.exists():
                arts.add(Artifact(name=name, path=opt, kind="h5", meta={"geocoded": True}))
        kmz = _newest((workdir / "geo").glob("*.kmz")) if (workdir / "geo").is_dir() else None
        if kmz is not None:
            arts.add(Artifact(name="kmz", path=kmz, kind="file"))
        return arts, missing

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

    # ------------------------------------------------------------------ logs
    def parse_log(self, log_path: Path, entries: Mapping[str, str] | None = None) -> list[Finding]:
        """MintPy step log → findings (OOM → MP-002, missing inputs → MP-003, else MP-001)."""
        if not log_path.exists():
            return []
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        entries = entries or {}
        findings: list[Finding] = []
        oom = next((ln for ln in lines if _OOM_RE.search(ln)), None)
        if oom:
            findings.append(
                _finding(
                    "MP-002",
                    "FAIL",
                    "mintpy",
                    max_memory_gb=entries.get("mintpy.compute.maxMemory", "auto"),
                    num_worker=entries.get("mintpy.compute.numWorker", "auto"),
                    line=mask_text(oom.strip()),
                )
            )
            return findings
        missing = next((ln for ln in lines if _MISSING_RE.search(ln)), None)
        if missing:
            findings.append(
                _finding(
                    "MP-003",
                    "FAIL",
                    "mintpy",
                    pattern=entries.get("mintpy.load.unwFile", mask_text(missing.strip())),
                    line=mask_text(missing.strip()),
                )
            )
            return findings
        err = next((ln for ln in reversed(lines) if _ERROR_RE.search(ln)), None)
        if err:
            m = re.search(r"--dostep\s+(\S+)", text)
            findings.append(
                _finding(
                    "MP-001",
                    "FAIL",
                    "mintpy",
                    step=m.group(1) if m else "?",
                    returncode="?",
                    log=mask_text(str(log_path)),
                    line=mask_text(err.strip()),
                )
            )
        return findings


def tpl_clip(meta: Mapping[str, Any]) -> str:
    return "_clip" if meta.get("clipped") else ""


def _append(path: Path, msg: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {mask_text(msg)}\n")


def _first_existing(*paths: Path) -> Path | None:
    return next((p for p in paths if p.exists()), None)


def _newest(paths: Any) -> Path | None:
    files = [p for p in paths if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def _h5_meta(path: Path) -> dict[str, Any]:
    try:
        a = read_h5_attrs(path)
    except OSError:
        return {}
    keep = (
        "FILE_TYPE",
        "UNIT",
        "REF_DATE",
        "REF_LAT",
        "REF_LON",
        "LENGTH",
        "WIDTH",
        "EPSG",
        "X_UNIT",
    )
    return {k: a[k] for k in keep if k in a}


# ---------------------------------------------------------------------- HDF5 readers (h5py only)


def _decode(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, np.ndarray):
        return " ".join(_decode(x) for x in v.tolist())
    return str(v)


def read_h5_attrs(path: Path) -> dict[str, str]:
    """Root-level attributes of a MintPy HDF5 file as ``{str: str}`` (MintPy stores str)."""
    import h5py

    with h5py.File(path, "r") as f:
        return {str(k): _decode(v) for k, v in f.attrs.items()}


def _parse_yyyymmdd(v: Any) -> date:
    s = _decode(v).strip()[:8]
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _grid_latlon(
    attrs: Mapping[str, str], ny: int, nx: int, geometry_path: Path | None
) -> tuple[NDArray[np.floating], NDArray[np.floating]]:
    if "Y_FIRST" in attrs and "X_FIRST" in attrs:
        y0, dy = float(attrs["Y_FIRST"]), float(attrs["Y_STEP"])
        x0, dx = float(attrs["X_FIRST"]), float(attrs["X_STEP"])
        # pixel centres, as in mintpy.utils.utils0.get_lat_lon
        ys = y0 + dy * (np.arange(ny, dtype=np.float64) + 0.5)
        xs = x0 + dx * (np.arange(nx, dtype=np.float64) + 0.5)
        unit = attrs.get("Y_UNIT", attrs.get("X_UNIT", "degrees")).lower()
        epsg = attrs.get("EPSG")
        if unit.startswith("deg") or not epsg or epsg == "4326":
            return ys, xs
        from pyproj import Transformer

        tr = Transformer.from_crs(f"EPSG:{int(float(epsg))}", "EPSG:4326", always_xy=True)
        xx, yy = np.meshgrid(xs, ys)
        lon, lat = tr.transform(xx, yy)
        return np.asarray(lat, dtype=np.float64), np.asarray(lon, dtype=np.float64)
    if geometry_path is not None:
        import h5py

        with h5py.File(geometry_path, "r") as g:
            if "latitude" in g and "longitude" in g:
                return np.asarray(g["latitude"][:], dtype=np.float64), np.asarray(
                    g["longitude"][:], dtype=np.float64
                )
    msg = "timeseries.h5 is in radar coordinates (no Y_FIRST/X_FIRST); pass geometry_path=geometryRadar.h5 with latitude/longitude datasets"
    raise ValueError(msg)


def _read_dataset(path: Path | None, name: str) -> NDArray[Any] | None:
    if path is None or not Path(path).exists():
        return None
    import h5py

    with h5py.File(path, "r") as f:
        if name not in f:
            return None
        return np.asarray(f[name][:])


def read_timeseries_h5(
    path: Path,
    *,
    geometry_path: Path | None = None,
    velocity_path: Path | None = None,
    temporal_coherence_path: Path | None = None,
    mask_path: Path | None = None,
) -> TimeSeries:
    """Load MintPy ``timeseries*.h5`` (+ optional sidecars) into :class:`TimeSeries`.

    Datasets ``timeseries`` (n_date, ny, nx, float32, metres) and ``date`` (bytes YYYYMMDD);
    attributes are root attrs. Sidecars default to the siblings ``velocity.h5``
    (``velocity``, m/year), ``temporalCoherence.h5`` (``temporalCoherence``),
    ``maskTempCoh.h5`` (``mask``) and ``inputs/geometry*.h5`` (``height``, ``incidenceAngle``
    in degrees, ``latitude``/``longitude`` for radar-coded stacks).
    """
    import h5py

    path = Path(path)
    with h5py.File(path, "r") as f:
        attrs = {str(k): _decode(v) for k, v in f.attrs.items()}
        data = np.asarray(f["timeseries"][:], dtype=np.float32)
        dates = [_parse_yyyymmdd(d) for d in f["date"][:]]
        bperp = np.asarray(f["bperp"][:]) if "bperp" in f else None
    if data.ndim != 3 or data.shape[0] != len(dates):
        msg = f"timeseries dataset shape {data.shape} does not match {len(dates)} dates"
        raise ValueError(msg)
    ny, nx = int(data.shape[1]), int(data.shape[2])
    wd = path.parent
    prefix = "geo_" if path.name.startswith("geo_") else ""
    if geometry_path is None:
        geometry_path = _first_existing(
            wd / f"{prefix}geometryGeo.h5",
            wd / f"{prefix}geometryRadar.h5",
            wd / "inputs" / "geometryGeo.h5",
            wd / "inputs" / "geometryRadar.h5",
            wd.parent / "inputs" / "geometryGeo.h5",
            wd.parent / "inputs" / "geometryRadar.h5",
        )
    lat, lon = _grid_latlon(attrs, ny, nx, geometry_path)
    inc = _read_dataset(geometry_path, "incidenceAngle")
    dem = _read_dataset(geometry_path, "height")
    vel = _read_dataset(velocity_path or wd / f"{prefix}velocity.h5", "velocity")
    coh = _read_dataset(
        temporal_coherence_path or wd / f"{prefix}temporalCoherence.h5", "temporalCoherence"
    )
    mask = _read_dataset(mask_path or wd / f"{prefix}maskTempCoh.h5", "mask")
    heading = float(attrs["HEADING"]) if "HEADING" in attrs else None
    ref: tuple[float, float] | None = None
    if "REF_LAT" in attrs and "REF_LON" in attrs:
        ref = (float(attrs["REF_LAT"]), float(attrs["REF_LON"]))
    extra: dict[str, Any] = {
        **attrs,
        "source": str(path),
        "REF_DATE": attrs.get("REF_DATE", f"{dates[0]:%Y%m%d}"),
        "geometry_path": None if geometry_path is None else str(geometry_path),
    }
    if bperp is not None:
        extra["bperp"] = bperp.astype(np.float32)
    if mask is not None:
        extra["mask"] = mask.astype(bool)
    return TimeSeries(
        dates=dates,
        displacement_m=data,
        lat=lat,
        lon=lon,
        incidence_deg=None if inc is None else inc.astype(np.float32),
        heading_deg=heading,
        coherence=None if coh is None else coh.astype(np.float32),
        velocity_m_per_yr=None if vel is None else vel.astype(np.float32),
        reference_latlon=ref,
        dem_m=None if dem is None else dem.astype(np.float32),
        attrs=extra,
    )
