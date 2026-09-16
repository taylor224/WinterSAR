"""Fixtures for the ISCE2 topsStack / run_files / burst2safe / dolphin adapter tests.

None of the engines is installed (CLAUDE.md): ``stackSentinel.py``, ``burst2safe`` and
``dolphin run`` are replaced by injected runners that write the *verified* output layout
(ADR-0026/0028), and the run_files jobs are real shell commands (``fake_job.sh``) so the
PERF-07 executor is exercised end to end.
"""

from __future__ import annotations

import os
import shlex
import stat
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from wintersar.engines import burst2safe, dolphin, isce2_topsstack
from wintersar.io.schemas import Pair, StackCandidate

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "isce2"
DATES = ["20240101", "20240113", "20240125", "20240206"]
PAIRS = [
    "20240101_20240113",
    "20240101_20240125",
    "20240113_20240125",
    "20240113_20240206",
    "20240125_20240206",
]


@pytest.fixture
def run_files_dir() -> Path:
    return FIXTURES / "run_files"


@pytest.fixture
def job_env(tmp_path: Path) -> dict[str, str]:
    """PATH with the fake job wrapper + a directory the wrapper touches per job."""
    script = FIXTURES / "bin" / "fake_job.sh"
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = dict(os.environ)
    env["PATH"] = f"{script.parent}{os.pathsep}{env.get('PATH', '')}"
    env["WINTERSAR_FAKE_JOB_TOUCH"] = str(tmp_path / "touched")
    return env


@pytest.fixture
def engines_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(isce2_topsstack, "find_executable", lambda: None)
    monkeypatch.setattr(dolphin, "find_executable", lambda: None)
    monkeypatch.setattr(burst2safe, "find_executable", lambda: None)
    monkeypatch.setattr(burst2safe, "python_module_version", lambda *_a, **_k: None)


@pytest.fixture
def isce_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        isce2_topsstack, "find_executable", lambda: "/opt/isce2/topsStack/stackSentinel.py"
    )


# ------------------------------------------------------------------ stack.json


def make_stack(dates: list[str] | None = None, with_granules: bool = True) -> StackCandidate:
    ds = [date.fromisoformat(f"{d[:4]}-{d[4:6]}-{d[6:]}") for d in (dates or DATES)]
    pairs = []
    for i, a in enumerate(ds):
        for b in ds[i + 1 : i + 3]:
            pairs.append(
                Pair(
                    reference=a,
                    secondary=b,
                    temporal_baseline_days=(b - a).days,
                    perp_baseline_m=20.0,
                )
            )
    notes: dict[str, Any] = {
        "aoi_wkt": "POLYGON((126.9 37.5,127.0 37.5,127.0 37.6,126.9 37.6,126.9 37.5))"
    }
    if with_granules:
        notes["granules"] = {
            d.isoformat(): {
                "052_109903_IW2": f"S1_109903_IW2_{d:%Y%m%d}T092000_VV_AB12-BURST",
                "052_109904_IW2": f"S1_109904_IW2_{d:%Y%m%d}T092000_VV_CD34-BURST",
            }
            for d in ds
        }
    return StackCandidate(
        relative_orbit=52,
        flight_direction="DESCENDING",
        polarization="VV",
        subswaths=["IW2"],
        burst_ids=["052_109903_IW2", "052_109904_IW2"],
        dates=ds,
        reference_date=ds[0],
        coverage_of_aoi=1.0,
        pairs=pairs,
        notes=notes,
    )


@pytest.fixture
def stack_json(tmp_path: Path) -> Path:
    p = tmp_path / "stack.json"
    p.write_text(make_stack().model_dump_json(indent=2), encoding="utf-8")
    return p


@pytest.fixture
def dem_file(tmp_path: Path) -> Path:
    dem = tmp_path / "dem" / "demLat_N37_N38_Lon_E126_E128.dem.wgs84"
    dem.parent.mkdir(parents=True)
    dem.write_bytes(b"\0" * 64)
    dem.with_name(dem.name + ".xml").write_text("<imageFile/>", encoding="utf-8")
    return dem


# ------------------------------------------------------------------ fake burst2safe


class FakeBurst2Safe:
    """Runner for ``burst2safe`` argv: creates ``S1A_IW_SLC__1SDV_<date>T…SAFE`` dirs."""

    def __init__(self, fail_dates: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_dates = fail_dates or set()

    def __call__(self, argv: list[str], cwd: Path, log_path: Path) -> int:
        self.calls.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake burst2safe\n", encoding="utf-8")
        granules = [a for a in argv[1:] if a.endswith("-BURST")]
        ymd = granules[0].split("_")[3][:8]
        if ymd in self.fail_dates:
            return 1
        out = Path(argv[argv.index("--output-dir") + 1])
        safe = out / f"S1A_IW_SLC__1SDV_{ymd}T092000_{ymd}T092027_051234_062E5F_ABCD.SAFE"
        (safe / "annotation").mkdir(parents=True, exist_ok=True)
        (safe / "manifest.safe").write_text("<xfdu/>", encoding="utf-8")
        return 0


# ------------------------------------------------------------------ fake stackSentinel.py


def _sh(*parts: str) -> str:
    return " && ".join(parts)


class FakeStackSentinel:
    """Runner for ``stackSentinel.py`` argv: writes ``run_files`` whose jobs create the verified
    topsStack product layout (ADR-0026) with plain shell commands.

    ``fail_step`` makes that step's first job exit 1 (retry + stop semantics).
    """

    def __init__(
        self,
        dates: list[str] | None = None,
        pairs: list[str] | None = None,
        fail_step: str | None = None,
        rc: int = 0,
    ) -> None:
        self.dates = dates or DATES
        self.pairs = pairs or PAIRS
        self.fail_step = fail_step
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], cwd: Path, log_path: Path) -> int:
        self.calls.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("# fake stackSentinel.py\n" + " ".join(argv) + "\n", encoding="utf-8")
        if self.rc != 0:
            return self.rc
        workdir = Path(argv[argv.index("-w") + 1])
        ref = argv[argv.index("-m") + 1] if "-m" in argv else self.dates[0]
        secondaries = [d for d in self.dates if d != ref]
        run_dir = workdir / "run_files"
        run_dir.mkdir(parents=True, exist_ok=True)
        (workdir / "configs").mkdir(exist_ok=True)
        steps: list[tuple[str, list[str]]] = [
            (
                "unpack_topo_reference",
                [
                    _sh(
                        "mkdir -p reference geom_reference",
                        ": > reference/IW2.xml",
                        ": > geom_reference/hgt_01.rdr",
                    )
                ],
            ),
            (
                "unpack_secondary_slc",
                [
                    _sh(f"mkdir -p secondarys/{d}", f": > secondarys/{d}/IW2.xml")
                    for d in secondaries
                ],
            ),
            (
                "average_baseline",
                [
                    _sh(
                        f"mkdir -p baselines/{ref}_{d}",
                        f"printf 'Bperp (average): 12.5\\n' > baselines/{ref}_{d}/{ref}_{d}.txt",
                    )
                    for d in secondaries
                ],
            ),
            ("fullBurst_geo2rdr", [f"mkdir -p coreg_secondarys/{d}" for d in secondaries]),
            ("fullBurst_resample", [f": > coreg_secondarys/{d}/IW2.xml" for d in secondaries]),
            ("extract_stack_valid_region", ["mkdir -p stack"]),
            (
                "merge_reference_secondary_slc",
                [
                    _sh(
                        "mkdir -p merged/geom_reference",
                        *[
                            f": > merged/geom_reference/{f}"
                            for f in (
                                "hgt.rdr",
                                "lat.rdr",
                                "lon.rdr",
                                "los.rdr",
                                "shadowMask.rdr",
                                "incLocal.rdr",
                            )
                        ],
                    )
                ]
                + [
                    _sh(f"mkdir -p merged/SLC/{d}", f": > merged/SLC/{d}/{d}.slc.full.vrt")
                    for d in self.dates
                ],
            ),
            (
                "generate_burst_igram",
                [
                    _sh(f"mkdir -p interferograms/{p}", f": > interferograms/{p}/burst_01.int")
                    for p in self.pairs
                ],
            ),
            (
                "merge_burst_igram",
                [
                    _sh(
                        f"mkdir -p merged/interferograms/{p}",
                        f"printf 'ISCEDATA' > merged/interferograms/{p}/fine.int",
                        f": > merged/interferograms/{p}/fine.int.xml",
                        f": > merged/interferograms/{p}/fine.int.vrt",
                    )
                    for p in self.pairs
                ],
            ),
            (
                "filter_coherence",
                [
                    _sh(
                        f": > merged/interferograms/{p}/filt_fine.int",
                        f": > merged/interferograms/{p}/filt_fine.cor",
                        f": > merged/interferograms/{p}/filt_fine.cor.xml",
                    )
                    for p in self.pairs
                ],
            ),
            (
                "unwrap",
                [
                    _sh(
                        f": > merged/interferograms/{p}/filt_fine.unw",
                        f": > merged/interferograms/{p}/filt_fine.unw.conncomp",
                    )
                    for p in self.pairs
                ],
            ),
        ]
        for i, (name, jobs) in enumerate(steps, start=1):
            if name == self.fail_step:
                jobs = ["false", *jobs[1:]]
            (run_dir / f"run_{i:02d}_{name}").write_text("\n".join(jobs) + "\n", encoding="utf-8")
        return 0


@pytest.fixture
def fake_stacksentinel() -> FakeStackSentinel:
    return FakeStackSentinel()


# ------------------------------------------------------------------ fake dolphin


def write_geotiff(
    path: Path, data: np.ndarray, *, crs: str | None = "EPSG:4326", nodata: float | None = None
) -> Path:
    import rasterio
    from rasterio.transform import from_origin

    path.parent.mkdir(parents=True, exist_ok=True)
    transform = from_origin(127.0, 37.6, 0.001, 0.001)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform if crs is not None else None,
        nodata=nodata,
    ) as dst:
        dst.write(data.astype(np.float32), 1)
    return path


class FakeDolphin:
    """Runner for ``dolphin run <config>``: writes ``timeseries/<ref>_<date>.tif`` + ``velocity.tif``
    (+ ``unwrapped/*.unw.tif``) following the verified naming (ADR-0028)."""

    def __init__(
        self,
        dates: list[str] | None = None,
        shape: tuple[int, int] = (6, 8),
        rc: int = 0,
        wavelength_in_config: bool = True,
    ) -> None:
        self.dates = dates or DATES
        self.shape = shape
        self.rc = rc
        self.calls: list[list[str]] = []
        self.config_seen: dict[str, Any] = {}

    def __call__(self, argv: list[str], cwd: Path, log_path: Path) -> int:
        import yaml

        self.calls.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake dolphin run\n", encoding="utf-8")
        if self.rc != 0:
            return self.rc
        cfg_path = Path(argv[2])
        self.config_seen = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        work = Path(self.config_seen["work_directory"])
        ny, nx = self.shape
        ref = self.dates[0]
        for i, d in enumerate(self.dates[1:], start=1):
            data = np.full((ny, nx), 0.01 * i, dtype=np.float32)
            write_geotiff(work / "timeseries" / f"{ref}_{d}.tif", data)
        write_geotiff(
            work / "timeseries" / "velocity.tif", np.full((ny, nx), 0.3, dtype=np.float32)
        )
        write_geotiff(
            work / "interferograms" / "temporal_coherence_average.tif",
            np.full((ny, nx), 0.9, dtype=np.float32),
        )
        (work / "unwrapped").mkdir(exist_ok=True)
        write_geotiff(
            work / "unwrapped" / f"{ref}_{self.dates[1]}.unw.tif",
            np.zeros((ny, nx), dtype=np.float32),
        )
        return 0


def shlex_join(argv: list[str]) -> str:
    return shlex.join(argv)
