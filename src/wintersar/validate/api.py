"""Python API of the validate module (plan §5.6, R-09/R-10/R-11).

* :func:`load_timeseries` — :class:`~wintersar.io.timeseries.TimeSeries` from a file. ``.h5``
  (MintPy, via h5py) is delegated to ``wintersar.io.formats.load_timeseries`` when that module
  is importable; the fake-engine ``.npz`` (``dates``, ``displacement_m``, ``velocity_m_per_yr``)
  is read here with a synthesised lat/lon grid and optional geometry overrides.
* :func:`validate_timeseries` — CSV ground truth → LOS → comparison → report files.
* :func:`run_validate` — pipeline stage entry point
  (``fn(cfg, inputs, params, out_dir, log_dir) -> (Artifacts, list[Finding])``,
  # source: src/wintersar/pipeline/stages.py::PYTHON_STAGE_ENTRYPOINTS / _as_artifacts).
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from wintersar.io.schemas import Artifact, Artifacts, Finding, GroundTruthRecord
from wintersar.io.timeseries import TimeSeries
from wintersar.util.masking import mask_text
from wintersar.validate.closure import ClosureResult
from wintersar.validate.ground_truth import GroundTruthError, load_csv, make_finding
from wintersar.validate.los import (
    S1_HEADING_DESC_DEG,
    S1_INCIDENCE_MID_DEG,
    default_heading,
)
from wintersar.validate.metrics import (
    DEFAULT_MAX_GAP_DAYS,
    DEFAULT_RADIUS_M,
    EARTH_RADIUS_M,
    Aggregate,
    Align,
    ComparisonResult,
    compare,
)
from wintersar.validate.refpoint import RefPointCandidate
from wintersar.validate.report import write_report

if TYPE_CHECKING:
    from wintersar.pipeline.config import Config

FloatArray = NDArray[np.float64]

# Synthetic geometry used when a time series carries no lat/lon (fake engine / tests):
# the ~10 km AOI near Seoul used by tests/conftest.py::aoi_geojson, 40 m pixels
# (engine.target_pixel_m default), row 0 = north.
SYNTHETIC_LAT0_DEG: float = 37.60
SYNTHETIC_LON0_DEG: float = 126.90
SYNTHETIC_PIXEL_M: float = 40.0

NPZ_REQUIRED = ("dates", "displacement_m")
NPZ_OPTIONAL = (
    "velocity_m_per_yr",
    "lat",
    "lon",
    "incidence_deg",
    "coherence",
    "dem_m",
    "conncomp",
)


class ValidateError(ValueError):
    """Validation input problem; ``finding`` carries the VAL-0xx id (cause → fix)."""

    def __init__(self, finding: Finding) -> None:
        self.finding = finding
        super().__init__(finding.message_key)


def synthetic_latlon_grid(
    shape: tuple[int, int],
    lat0_deg: float = SYNTHETIC_LAT0_DEG,
    lon0_deg: float = SYNTHETIC_LON0_DEG,
    pixel_m: float = SYNTHETIC_PIXEL_M,
) -> tuple[FloatArray, FloatArray]:
    """Regular lat/lon grid whose top-left pixel centre is ``(lat0, lon0)`` (row 0 = north)."""
    ny, nx = shape
    deg_per_m = 180.0 / (np.pi * EARTH_RADIUS_M)
    dlat = pixel_m * deg_per_m
    dlon = dlat / np.cos(np.deg2rad(lat0_deg))
    lat = lat0_deg - np.arange(ny, dtype=np.float64) * dlat
    lon = lon0_deg + np.arange(nx, dtype=np.float64) * dlon
    lon2, lat2 = np.meshgrid(lon, lat)
    return np.asarray(lat2, dtype=np.float64), np.asarray(lon2, dtype=np.float64)


def _parse_dates(raw: Any) -> list[date]:
    out: list[date] = []
    for d in np.asarray(raw).tolist():
        s = str(d)
        if len(s) == 8 and s.isdigit():
            out.append(date(int(s[:4]), int(s[4:6]), int(s[6:])))
        else:
            out.append(date.fromisoformat(s[:10]))
    return out


def load_timeseries_npz(
    path: Path | str,
    *,
    lat0_deg: float | None = None,
    lon0_deg: float | None = None,
    pixel_m: float | None = None,
    heading_deg: float | None = None,
    incidence_deg: float | None = None,
) -> TimeSeries:
    """Read the fake-engine / interchange ``.npz`` (keys as written by
    ``wintersar.engines.fake.FakeEngine._stage_timeseries``: ``dates``, ``displacement_m``,
    ``velocity_m_per_yr``; optional ``lat``, ``lon``, ``incidence_deg``, ``coherence``,
    ``dem_m``, ``conncomp`` and scalar attrs ``lat0``, ``lon0``, ``pixel_m``, ``heading_deg``).
    """
    p = Path(path)
    attrs: dict[str, Any] = {"source": mask_text(str(p))}
    with np.load(p, allow_pickle=False) as z:
        missing = [k for k in NPZ_REQUIRED if k not in z.files]
        if missing:
            raise ValidateError(
                make_finding(
                    "VAL-011", path=mask_text(str(p)), suffix=f"npz without {', '.join(missing)}"
                )
            )
        dates = _parse_dates(z["dates"])
        disp = np.asarray(z["displacement_m"], dtype=np.float32)
        opt: dict[str, NDArray[Any]] = {k: z[k] for k in NPZ_OPTIONAL if k in z.files}
        scalars = {
            k: float(z[k]) for k in ("lat0", "lon0", "pixel_m", "heading_deg") if k in z.files
        }
    shape = (int(disp.shape[1]), int(disp.shape[2]))
    if "lat" in opt and "lon" in opt:
        lat, lon = (
            np.asarray(opt["lat"], dtype=np.float64),
            np.asarray(opt["lon"], dtype=np.float64),
        )
    else:
        lat, lon = synthetic_latlon_grid(
            shape,
            lat0_deg if lat0_deg is not None else scalars.get("lat0", SYNTHETIC_LAT0_DEG),
            lon0_deg if lon0_deg is not None else scalars.get("lon0", SYNTHETIC_LON0_DEG),
            pixel_m if pixel_m is not None else scalars.get("pixel_m", SYNTHETIC_PIXEL_M),
        )
        attrs["synthetic_geometry"] = True
    inc: NDArray[np.floating] | float
    if incidence_deg is not None:
        inc = float(incidence_deg)
    elif "incidence_deg" in opt:
        arr = np.asarray(opt["incidence_deg"], dtype=np.float64)
        inc = float(arr) if arr.ndim == 0 else arr
    else:
        inc = S1_INCIDENCE_MID_DEG
        attrs["synthetic_incidence"] = True
    heading = (
        heading_deg if heading_deg is not None else scalars.get("heading_deg", S1_HEADING_DESC_DEG)
    )
    return TimeSeries(
        dates=dates,
        displacement_m=disp,
        lat=lat,
        lon=lon,
        incidence_deg=inc,
        heading_deg=float(heading),
        coherence=opt.get("coherence"),
        velocity_m_per_yr=opt.get("velocity_m_per_yr"),
        dem_m=opt.get("dem_m"),
        conncomp=opt.get("conncomp"),
        attrs=attrs,
    )


def _formats_loader() -> Callable[[Path], Any] | None:
    """``wintersar.io.formats.load_timeseries`` when that (io-module owned) file exists.

    # source: src/wintersar/io/formats.py::load_timeseries(path, **kwargs) -> TimeSeries
    #   dispatches .npz -> read_timeseries_npz, .h5/.hdf5/.he5 -> read_timeseries_h5 (MintPy,
    #   h5py) and raises ValueError for other suffixes. validate keeps its own .npz reader
    #   because it accepts explicit lat0/lon0/pixel_m/heading/incidence overrides.
    """
    try:
        mod = importlib.import_module("wintersar.io.formats")
    except ImportError:
        return None
    fn = getattr(mod, "load_timeseries", None)
    return fn if callable(fn) else None


def load_timeseries(path: Path | str, **geometry: Any) -> TimeSeries:
    """Load a time series; prefers ``wintersar.io.formats.load_timeseries`` (lazy import).

    ``geometry`` (``lat0_deg``, ``lon0_deg``, ``pixel_m``, ``heading_deg``, ``incidence_deg``)
    only applies to the ``.npz`` fallback.
    """
    p = Path(path)
    if not p.exists():
        raise ValidateError(make_finding("VAL-006", path=mask_text(str(p))))
    loader = _formats_loader()
    if loader is not None and p.suffix.lower() != ".npz":
        try:
            ts = loader(p)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValidateError(
                make_finding(
                    "VAL-011",
                    path=mask_text(str(p)),
                    suffix=f"{p.suffix or '?'}: {mask_text(str(exc))[:200]}",
                )
            ) from exc
        if isinstance(ts, TimeSeries):
            return ts
    if p.suffix.lower() == ".npz":
        return load_timeseries_npz(p, **geometry)
    raise ValidateError(make_finding("VAL-011", path=mask_text(str(p)), suffix=p.suffix or "?"))


def load_ground_truth(
    leveling_csv: Path | str | None = None, gnss_csv: Path | str | None = None
) -> list[GroundTruthRecord]:
    """Concatenate the levelling and GNSS CSVs (either may be omitted, not both)."""
    if leveling_csv is None and gnss_csv is None:
        raise ValidateError(make_finding("VAL-014"))
    records: list[GroundTruthRecord] = []
    if leveling_csv is not None:
        records.extend(load_csv(leveling_csv))
    if gnss_csv is not None:
        records.extend(load_csv(gnss_csv))
    return records


def summary_finding(result: ComparisonResult) -> Finding:
    """``VAL-013`` INFO line carried into pipeline reports and ``--json`` output."""
    return make_finding(
        "VAL-013",
        "INFO",
        rmse_mm=round(result.rmse_m * 1000.0, 1) if np.isfinite(result.rmse_m) else "-",
        bias_mm=round(result.bias_m * 1000.0, 1) if np.isfinite(result.bias_m) else "-",
        n_sites=result.n_sites,
        n_points=result.n_points,
        radius_m=result.radius_m,
    )


def validate_timeseries(
    ts_path: Path | str | TimeSeries,
    leveling_csv: Path | str | None = None,
    gnss_csv: Path | str | None = None,
    out_dir: Path | str | None = None,
    lang: str | None = None,
    *,
    radius_m: float = DEFAULT_RADIUS_M,
    method: Aggregate = "mean",
    align: Align = "nearest",
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
    heading_deg: float | None = None,
    incidence_deg: float | None = None,
    closure: ClosureResult | None = None,
    refpoints: Sequence[RefPointCandidate] | None = None,
    plots: bool = True,
    geometry: dict[str, Any] | None = None,
) -> tuple[ComparisonResult, dict[str, Path]]:
    """Compare a time series with levelling/GNSS CSVs and write the report.

    Returns the :class:`ComparisonResult` and the written paths (``markdown``, ``html``,
    ``json``, optional ``plots``). Raises :class:`GroundTruthError` / :class:`ValidateError`
    for input problems (each carries a ``VAL-0xx`` finding).
    """
    if isinstance(ts_path, TimeSeries):
        ts, source = ts_path, str(ts_path.attrs.get("source", "<memory>"))
    else:
        ts = load_timeseries(ts_path, **(geometry or {}))
        source = str(ts_path)
    gt = load_ground_truth(leveling_csv, gnss_csv)
    result = compare(
        ts,
        gt,
        radius_m=radius_m,
        method=method,
        align=align,
        max_gap_days=max_gap_days,
        heading_deg=heading_deg,
        incidence_deg=incidence_deg,
    )
    result.findings.append(summary_finding(result))
    out = Path(out_dir) if out_dir is not None else _default_out_dir(ts_path)
    paths = write_report(
        result, out, lang, source=source, closure=closure, refpoints=refpoints, plots=plots
    )
    return result, paths


def _default_out_dir(ts_path: Path | str | TimeSeries) -> Path:
    if isinstance(ts_path, TimeSeries):
        return Path.cwd() / "validate"
    return Path(ts_path).resolve().parent / "validate"


# ------------------------------------------------------------------ pipeline stage


def _aoi_origin(aoi_path: Path | None) -> tuple[float, float] | None:
    """North-west corner of the AOI GeoJSON (lon/lat) → ``(lat0, lon0)`` for the synthetic grid."""
    if aoi_path is None or not Path(aoi_path).exists():
        return None
    try:
        data = json.loads(Path(aoi_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    coords: list[tuple[float, float]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if "coordinates" in obj:
                walk(obj["coordinates"])
            for k in ("features", "geometry", "geometries"):
                if k in obj:
                    walk(obj[k])
        elif isinstance(obj, list):
            if len(obj) >= 2 and all(isinstance(v, int | float) for v in obj[:2]):
                coords.append((float(obj[0]), float(obj[1])))
            else:
                for v in obj:
                    walk(v)

    walk(data)
    if not coords:
        return None
    lons, lats = zip(*coords, strict=True)
    return max(lats), min(lons)


def run_validate(
    cfg: Config,
    inputs: Artifacts,
    params: dict[str, Any],
    out_dir: Path,
    log_dir: Path,
) -> tuple[Artifacts, list[Finding]]:
    """Pipeline ``validate`` stage: ground truth from ``cfg.validation``, time series from the
    optional ``timeseries`` input artifact (fake-engine ``.npz`` or any format
    ``wintersar.io.formats`` understands)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "validate.log"
    findings: list[Finding] = []
    vcfg = cfg.validation
    vparams = params.get("validate", {}) if isinstance(params.get("validate"), dict) else {}
    radius = float(vparams.get("radius_m", vcfg.radius_m))
    leveling = vcfg.leveling_csv
    gnss = vcfg.gnss.path if vcfg.gnss is not None and vcfg.gnss.source == "csv" else None
    ts_art = inputs.items.get("timeseries")
    if ts_art is None:
        f = make_finding("VAL-012", "WARN")
        findings.append(f)
        empty = ComparisonResult(
            per_site=[],
            rmse_m=float("nan"),
            bias_m=float("nan"),
            n_sites=0,
            n_points=0,
            radius_m=radius,
            method="mean",
            align="nearest",
            max_gap_days=DEFAULT_MAX_GAP_DAYS,
            findings=[f],
        )
        paths = write_report(empty, out_dir, cfg.project.language, plots=False)
        log.write_text("no timeseries artifact\n", encoding="utf-8")
        return _artifacts(paths, empty), findings
    origin = _aoi_origin(cfg.aoi)
    geometry: dict[str, Any] = {
        "pixel_m": float(cfg.engine.target_pixel_m),
        "heading_deg": default_heading(
            None if cfg.data.orbit_direction == "auto" else cfg.data.orbit_direction
        ),
    }
    if origin is not None:
        geometry["lat0_deg"], geometry["lon0_deg"] = origin
    try:
        result, paths = validate_timeseries(
            ts_art.path,
            leveling,
            gnss,
            out_dir,
            cfg.project.language,
            radius_m=radius,
            plots=True,
            geometry=geometry,
        )
    except (GroundTruthError, ValidateError) as exc:
        findings.append(exc.finding)
        log.write_text(f"validate failed: {exc.finding.rule_id}\n", encoding="utf-8")
        from wintersar.pipeline.stages import StageFailureError

        raise StageFailureError(exc.finding.rule_id, findings) from exc
    findings.extend(result.findings)
    log.write_text(
        f"sites={result.n_sites} points={result.n_points} rmse_m={result.rmse_m} bias_m={result.bias_m}\n",
        encoding="utf-8",
    )
    return _artifacts(paths, result), findings


def _artifacts(paths: dict[str, Path], result: ComparisonResult) -> Artifacts:
    meta = {
        "rmse_m": None if not np.isfinite(result.rmse_m) else float(result.rmse_m),
        "bias_m": None if not np.isfinite(result.bias_m) else float(result.bias_m),
        "n_sites": result.n_sites,
        "n_points": result.n_points,
        "html": mask_text(str(paths["html"])),
        "json": mask_text(str(paths["json"])),
    }
    arts = Artifacts().add(
        Artifact(name="validation_report", path=paths["markdown"], kind="md", meta=meta)
    )
    arts.add(Artifact(name="validation_json", path=paths["json"], kind="json", meta=dict(meta)))
    return arts


__all__ = [
    "SYNTHETIC_LAT0_DEG",
    "SYNTHETIC_LON0_DEG",
    "SYNTHETIC_PIXEL_M",
    "ValidateError",
    "load_ground_truth",
    "load_timeseries",
    "load_timeseries_npz",
    "run_validate",
    "summary_finding",
    "synthetic_latlon_grid",
    "validate_timeseries",
]
