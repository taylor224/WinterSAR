"""Fixtures for the hyp3/mintpy adapter tests (no network, no external engines).

* :class:`FakeHyp3Client` implements the :class:`wintersar.engines.hyp3.Hyp3Client` protocol
  and writes real (tiny) GeoTIFF products with the verified HyP3 file suffixes.
* ``fake_mintpy_exe`` puts a fake ``smallbaselineApp.py`` on PATH that logs the step order and
  creates the MintPy output files (HDF5 via h5py) the adapter expects.
* ``make_timeseries_h5`` writes a MintPy-compatible ``timeseries.h5`` with documented attrs.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import zipfile
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from wintersar.engines.hyp3 import (
    JOB_TYPE_MULTI_BURST,
    LOOKS_PIXEL_M,
    RECOMMENDED_SUFFIXES,
    REQUIRED_SUFFIXES,
    JobInfo,
    JobSpec,
    job_credits,
)
from wintersar.io.schemas import Pair, StackCandidate

BURST_IDS = ["052_109903_IW2", "052_109904_IW2"]
DATES = [date(2024, 1, 1), date(2024, 1, 13), date(2024, 1, 25)]


def granule_name(burst_id: str, day: date) -> str:
    # burst granule naming, e.g. S1_136231_IW2_20200604T022312_VV_7C85-BURST (product guide)
    _, num, sw = burst_id.split("_")
    return f"S1_{num}_{sw}_{day:%Y%m%d}T092000_VV_{day.day:02d}AB-BURST"


def make_stack(n_bursts: int = 1, n_dates: int = 3, with_granules: bool = True) -> StackCandidate:
    dates = DATES[:n_dates]
    bursts = (
        BURST_IDS[:n_bursts]
        if n_bursts <= len(BURST_IDS)
        else [f"052_{109903 + i:06d}_IW2" for i in range(n_bursts)]
    )
    pairs = [
        Pair(reference=a, secondary=b, temporal_baseline_days=(b - a).days, perp_baseline_m=10.0)
        for i, a in enumerate(dates)
        for b in dates[i + 1 :]
    ]
    notes: dict[str, Any] = {}
    if with_granules:
        notes["granules"] = {d.isoformat(): {b: granule_name(b, d) for b in bursts} for d in dates}
    return StackCandidate(
        relative_orbit=52,
        flight_direction="DESCENDING",
        polarization="VV",
        subswaths=["IW2"],
        burst_ids=bursts,
        dates=dates,
        reference_date=dates[0],
        coverage_of_aoi=1.0,
        pairs=pairs,
        notes=notes,
    )


def write_candidates(
    path: Path, n_bursts: int = 1, n_dates: int = 3, with_granules: bool = True
) -> Path:
    stack = make_stack(n_bursts, n_dates, with_granules)
    path.write_text(
        json.dumps({"stacks": [stack.model_dump(mode="json")], "recommended": stack.stack_id}),
        encoding="utf-8",
    )
    return path


def product_name_for(spec: JobSpec, pair: str) -> str:
    ref, sec = pair.split("_")
    px = LOOKS_PIXEL_M[spec.looks]
    if spec.n_bursts == 1:
        return f"S1_109903_IW2_{ref}_{sec}_VV_INT{px}_A1B2"
    return f"S1_052_000000s1n00-109903s2n{spec.n_bursts:02d}-000000s3n00_IW_{ref}_{sec}_VV_INT{px}_C3D4"


METADATA_TXT = """Reference Granule: {ref}
Secondary Granule: {sec}
Reference Pass Direction: DESCENDING
Reference Orbit Number: 50000
Secondary Orbit Number: 50175
Baseline: -23.4
UTC time: 33600.0
Heading: -167.9
Spacecraft height: 693000.0
Earth radius at nadir: 6371000.0
Slant range near: 800000.0
Slant range center: 850000.0
Slant range far: 900000.0
Range looks: {rg}
Azimuth looks: {az}
InSAR phase filter: yes
Phase filter parameter: 0.6
DEM source: GLO-30
Unwrapping type: snaphu_mcf
Water mask: no
"""


def write_geotiff(
    path: Path, west: float, north: float, size: int = 6, px: float = 0.001, dtype: str = "float32"
) -> None:
    import rasterio
    from rasterio.transform import from_origin

    data = np.arange(size * size, dtype=dtype).reshape(size, size)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype=dtype,
        crs="EPSG:4326",
        transform=from_origin(west, north, px, px),
    ) as dst:
        dst.write(data, 1)


def write_product(
    product_dir: Path,
    name: str,
    spec: JobSpec,
    *,
    skip: Sequence[str] = (),
    west: float = 126.9,
    north: float = 37.6,
) -> None:
    product_dir.mkdir(parents=True, exist_ok=True)
    for suffix in (*REQUIRED_SUFFIXES, *RECOMMENDED_SUFFIXES):
        if suffix in skip:
            continue
        dtype = "uint8" if suffix in ("_conncomp.tif", "_water_mask.tif") else "float32"
        write_geotiff(product_dir / f"{name}{suffix}", west, north, dtype=dtype)
    rg, az = spec.looks.split("x")
    if ".txt" not in skip:
        (product_dir / f"{name}.txt").write_text(
            METADATA_TXT.format(ref=spec.reference[0], sec=spec.secondary[0], rg=rg, az=az),
            encoding="utf-8",
        )
    (product_dir / f"{name}.README.md.txt").write_text(
        "HyP3 burst InSAR product\n", encoding="utf-8"
    )
    (product_dir / f"{name}_unw_phase.png").write_bytes(b"\x89PNG")


class FakeHyp3Client:
    """Deterministic stand-in for the hyp3_sdk-backed client (records every call)."""

    def __init__(
        self,
        *,
        credits: float | None = 8000.0,
        fail_pairs: Sequence[str] = (),
        skip_files: Sequence[str] = (),
        never_finish: bool = False,
        expired_pairs: Sequence[str] = (),
        shift_pairs: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self.credits = credits
        self.fail_pairs = set(fail_pairs)
        self.skip_files = tuple(skip_files)
        self.never_finish = never_finish
        self.expired_pairs = set(expired_pairs)
        self.shift_pairs = shift_pairs or {}
        self.submitted: list[list[JobSpec]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.refresh_calls = 0
        self.download_calls = 0
        self._n = 0

    # -- protocol
    def submit(self, specs: Sequence[JobSpec]) -> list[JobInfo]:
        self.submitted.append(list(specs))
        infos = []
        for spec in specs:
            self._n += 1
            jid = f"job-{self._n:04d}"
            info = JobInfo(
                job_id=jid,
                status_code="PENDING",
                job_type=spec.job_type,
                name=spec.name,
                credit_cost=job_credits(spec.looks, spec.n_bursts),
                job_parameters=spec.to_prepared()["job_parameters"],
                request_time="2026-09-16T00:00:00+00:00",
            )
            self.jobs[jid] = {"spec": spec, "info": info, "polls": 0}
            infos.append(info)
        return infos

    def refresh(self, job_ids: Sequence[str]) -> list[JobInfo]:
        self.refresh_calls += 1
        out = []
        for jid in job_ids:
            rec = self.jobs[jid]
            rec["polls"] += 1
            info: JobInfo = rec["info"]
            spec: JobSpec = rec["spec"]
            if not info.complete:
                if self.never_finish or rec["polls"] == 1:
                    info.status_code = "RUNNING"
                elif spec.pair in self.fail_pairs:
                    info.status_code = "FAILED"
                    info.logs = [f"https://example.invalid/logs/{jid}.log"]
                else:
                    info.status_code = "SUCCEEDED"
                    name = product_name_for(spec, spec.pair)
                    info.files = [
                        {
                            "filename": f"{name}.zip",
                            "url": f"https://example.invalid/{name}.zip",
                            "size": 1234,
                        }
                    ]
                    info.expiration_time = (
                        "2000-01-01T00:00:00+00:00"
                        if spec.pair in self.expired_pairs
                        else "2999-01-01T00:00:00+00:00"
                    )
            out.append(JobInfo(**info.to_dict()))
        return out

    def download(self, job: JobInfo, dest: Path) -> list[Path]:
        self.download_calls += 1
        spec: JobSpec = self.jobs[job.job_id]["spec"]
        name = product_name_for(spec, spec.pair)
        west, north = self.shift_pairs.get(spec.pair, (126.9, 37.6))
        staging = dest / "_staging" / name
        write_product(staging, name, spec, skip=self.skip_files, west=west, north=north)
        zip_path = dest / f"{name}.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            for f in sorted(staging.iterdir()):
                z.write(f, arcname=f"{name}/{f.name}")
        for f in staging.iterdir():
            f.unlink()
        staging.rmdir()
        (dest / "_staging").rmdir()
        return [zip_path]

    def check_credits(self) -> float | None:
        return self.credits

    def costs(self) -> dict[str, Any]:
        return {"source": "fake"}


@pytest.fixture
def fake_client() -> FakeHyp3Client:
    return FakeHyp3Client()


@pytest.fixture
def candidates_single(tmp_path: Path) -> Path:
    return write_candidates(tmp_path / "candidates.json", n_bursts=1)


@pytest.fixture
def candidates_multi(tmp_path: Path) -> Path:
    return write_candidates(tmp_path / "candidates_multi.json", n_bursts=2)


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))


# ---------------------------------------------------------------------- fake MintPy

FAKE_MINTPY = '''#!{python}
"""Fake smallbaselineApp.py for wintersar tests: logs steps, creates expected outputs."""
import os, sys, json
from pathlib import Path
import numpy as np

args = sys.argv[1:]
if "-v" in args or "--version" in args:
    print("MintPy version 1.6.4, date 2026-07-25")
    sys.exit(0)
template = Path(args[0])
workdir = Path(args[args.index("--dir") + 1]) if "--dir" in args else Path(".")
step = args[args.index("--dostep") + 1]
workdir.mkdir(parents=True, exist_ok=True)
with (workdir / "steps.log").open("a") as fh:
    fh.write(step + "\\n")
print("MintPy version 1.6.4, date 2026-07-25")
print("Run routine processing with smallbaselineApp.py on steps: ['%s']" % step)
if os.environ.get("FAKE_MINTPY_FAIL_STEP") == step:
    print("ERROR: simulated failure in step " + step)
    sys.exit(1)
if os.environ.get("FAKE_MINTPY_OOM_STEP") == step:
    print("numpy.core._exceptions.MemoryError: Unable to allocate 12.0 GiB for an array")
    sys.exit(1)
cfg = {{}}
for line in template.read_text().splitlines():
    line = line.split("#", 1)[0].strip()
    if "=" in line:
        k, v = (s.strip() for s in line.split("=", 1))
        cfg[k] = v
import h5py
sys.path.insert(0, {tests_dir!r})
from unit.engines_hyp3_mintpy.conftest import make_timeseries_h5  # noqa: E402

def chain():
    name = "timeseries"
    m = cfg.get("mintpy.troposphericDelay.method", "pyaps")
    if m in ("auto", "pyaps"):
        name += "_" + cfg.get("mintpy.troposphericDelay.weatherModel", "ERA5")
    elif m == "height_correlation":
        name += "_tropHgt"
    elif m == "gacos":
        name += "_GACOS"
    if cfg.get("mintpy.deramp", "no") not in ("no", "auto"):
        name += "_ramp"
    if cfg.get("mintpy.topographicResidual", "yes") in ("yes", "auto"):
        name += "_demErr"
    return name + ".h5"

if step == "load_data":
    (workdir / "inputs").mkdir(exist_ok=True)
    with h5py.File(workdir / "inputs" / "ifgramStack.h5", "w") as f:
        f.create_dataset("unwrapPhase", data=np.zeros((3, 4, 5), np.float32))
        f.attrs["FILE_TYPE"] = "ifgramStack"
    with h5py.File(workdir / "inputs" / "geometryGeo.h5", "w") as f:
        f.create_dataset("height", data=np.full((4, 5), 50.0, np.float32))
        f.create_dataset("incidenceAngle", data=np.full((4, 5), 39.0, np.float32))
        f.attrs["FILE_TYPE"] = "geometry"
elif step == "invert_network":
    make_timeseries_h5(workdir / "timeseries.h5", sidecars=True)
elif step == "correct_troposphere":
    make_timeseries_h5(workdir / ("timeseries_" + cfg.get("mintpy.troposphericDelay.weatherModel", "ERA5") + ".h5"))
elif step == "deramp":
    make_timeseries_h5(workdir / chain().replace("_demErr", ""))
elif step == "correct_topography":
    make_timeseries_h5(workdir / chain())
elif step == "velocity":
    make_timeseries_h5(workdir / "velocity.h5", velocity_only=True)
elif step == "google_earth":
    (workdir / "geo").mkdir(exist_ok=True)
    (workdir / "geo" / "geo_velocity.kmz").write_bytes(b"PK")
sys.exit(0)
'''


@pytest.fixture
def fake_mintpy_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "smallbaselineApp.py"
    tests_dir = str(Path(__file__).resolve().parents[2])
    exe.write_text(FAKE_MINTPY.format(python=sys.executable, tests_dir=tests_dir), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("FAKE_MINTPY_FAIL_STEP", raising=False)
    monkeypatch.delenv("FAKE_MINTPY_OOM_STEP", raising=False)
    return exe


@pytest.fixture
def no_mintpy_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


# ---------------------------------------------------------------------- MintPy HDF5 factory

TS_ATTRS: dict[str, str] = {
    # source: https://mintpy.readthedocs.io/en/latest/api/attributes/ + prep_hyp3.py
    "FILE_TYPE": "timeseries",
    "UNIT": "m",
    "PROCESSOR": "hyp3",
    "PLATFORM": "Sen",
    "ORBIT_DIRECTION": "DESCENDING",
    "WAVELENGTH": "0.05546576",
    "HEADING": "-167.9",
    "X_FIRST": "126.9",
    "Y_FIRST": "37.6",
    "X_STEP": "0.001",
    "Y_STEP": "-0.001",
    "X_UNIT": "degrees",
    "Y_UNIT": "degrees",
    "REF_LAT": "37.5985",
    "REF_LON": "126.9025",
    "REF_Y": "1",
    "REF_X": "2",
    "REF_DATE": "20240101",
    "EARTH_RADIUS": "6371000.0",
    "HEIGHT": "693000.0",
    "CENTER_LINE_UTC": "33600.0",
    "ALOOKS": "4",
    "RLOOKS": "20",
    "STARTING_RANGE": "800000.0",
    "RANGE_PIXEL_SIZE": "46.6",
    "AZIMUTH_PIXEL_SIZE": "56.4",
}


def make_timeseries_h5(
    path: Path,
    n_dates: int = 3,
    ny: int = 4,
    nx: int = 5,
    *,
    geocoded: bool = True,
    sidecars: bool = False,
    velocity_only: bool = False,
) -> Path:
    """Write a MintPy-compatible HDF5 (stack.py timeseries.write2hdf5 layout)."""
    import h5py

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    dates = [DATES[0] + timedelta(days=12 * i) for i in range(n_dates)]
    attrs = {**TS_ATTRS, "LENGTH": str(ny), "WIDTH": str(nx)}
    if not geocoded:
        for k in ("X_FIRST", "Y_FIRST", "X_STEP", "Y_STEP", "X_UNIT", "Y_UNIT"):
            attrs.pop(k)
    if velocity_only:
        with h5py.File(path, "w") as f:
            f.create_dataset("velocity", data=np.full((ny, nx), -0.012, np.float32))
            for k, v in {**attrs, "FILE_TYPE": "velocity", "UNIT": "m/year"}.items():
                f.attrs[k] = str(v)
        return path
    data = np.zeros((n_dates, ny, nx), np.float32)
    for i in range(n_dates):
        data[i] = -0.001 * i  # 1 mm per epoch away from satellite
    with h5py.File(path, "w") as f:
        f.create_dataset("timeseries", data=data, chunks=True)
        f.create_dataset("date", data=np.array([f"{d:%Y%m%d}" for d in dates], dtype=np.bytes_))
        f.create_dataset("bperp", data=np.linspace(0, 50, n_dates).astype(np.float32))
        for k, v in attrs.items():
            f.attrs[k] = str(v)
    if sidecars:
        wd = path.parent
        make_timeseries_h5(
            wd / "velocity.h5", n_dates, ny, nx, geocoded=geocoded, velocity_only=True
        )
        with h5py.File(wd / "temporalCoherence.h5", "w") as f:
            f.create_dataset("temporalCoherence", data=np.full((ny, nx), 0.9, np.float32))
            f.attrs["FILE_TYPE"] = "temporalCoherence"
        with h5py.File(wd / "maskTempCoh.h5", "w") as f:
            f.create_dataset("mask", data=np.ones((ny, nx), bool))
            f.attrs["FILE_TYPE"] = "mask"
        (wd / "inputs").mkdir(exist_ok=True)
        geom = wd / "inputs" / ("geometryGeo.h5" if geocoded else "geometryRadar.h5")
        with h5py.File(geom, "w") as f:
            f.create_dataset("height", data=np.full((ny, nx), 50.0, np.float32))
            f.create_dataset("incidenceAngle", data=np.full((ny, nx), 39.0, np.float32))
            if not geocoded:
                lat = 37.6 - 0.001 * (np.arange(ny) + 0.5)
                lon = 126.9 + 0.001 * (np.arange(nx) + 0.5)
                lo, la = np.meshgrid(lon, lat)
                f.create_dataset("latitude", data=la.astype(np.float32))
                f.create_dataset("longitude", data=lo.astype(np.float32))
            f.attrs["FILE_TYPE"] = "geometry"
    return path


__all__ = [
    "BURST_IDS",
    "DATES",
    "JOB_TYPE_MULTI_BURST",
    "FakeHyp3Client",
    "granule_name",
    "make_stack",
    "make_timeseries_h5",
    "product_name_for",
    "write_candidates",
    "write_geotiff",
    "write_product",
]
