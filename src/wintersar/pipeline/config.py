"""Project configuration (plan §4.4) as pydantic models.

``load_config(path)`` reads ``config.yaml``; relative paths are resolved against the
config file's directory. ``Config.normalized_params(stage)`` returns the *canonical*
parameter mapping of a stage (defaults filled, keys sorted) which the DAG hashes
(PERF-03: parameter normalisation before hashing).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCfg(_Strict):
    name: str
    workdir: Path = Path("./work")
    language: Literal["ko", "en"] = "ko"


class TimeRange(_Strict):
    start: date
    end: date

    @model_validator(mode="after")
    def _order(self) -> TimeRange:
        if self.end < self.start:
            msg = "time_range.end must be >= time_range.start"
            raise ValueError(msg)
        return self


class DataCfg(_Strict):
    source: Literal["asf", "cdse"] = "asf"
    product: Literal["burst", "slc"] = "burst"
    polarization: Literal["VV", "HH", "VH", "HV"] = "VV"
    orbit_direction: Literal["asc", "desc", "auto"] = "auto"
    relative_orbit: int | Literal["auto"] = "auto"
    credentials: str = Field(
        default="env:EARTHDATA_TOKEN",
        description="'env:VAR' reads the token from an environment variable; 'netrc' uses ~/.netrc",
    )
    platform: list[str] = Field(default_factory=lambda: ["S1A", "S1B", "S1C", "S1D"])

    def resolve_token(self) -> str | None:
        if self.credentials.startswith("env:"):
            return os.environ.get(self.credentials[4:]) or None
        return None


class SelectionCfg(_Strict):
    network: Literal["sbas", "sequential", "single_reference"] = "sbas"
    max_perp_baseline_m: float = 150.0
    max_temporal_baseline_days: int = 48
    sequential_connections: int = 3
    min_coverage: float = Field(default=0.95, ge=0.0, le=1.0)
    max_layover_shadow_fraction: float = Field(default=0.10, ge=0.0, le=1.0)
    pixel_spacing_tolerance: float = 0.01
    budget_credits: float | None = None


class FilterCfg(_Strict):
    type: Literal["goldstein", "none"] = "goldstein"
    alpha: float = 0.6
    window: int = 64


class EngineCfg(_Strict):
    interferogram: Literal["hyp3", "isce2_topsstack", "compass_isce3", "fake"] = "hyp3"
    looks: Literal["auto"] | tuple[int, int] = "auto"
    target_pixel_m: float = 40.0
    filter: FilterCfg = Field(default_factory=FilterCfg)
    esd: bool = True
    cleanup: Literal["none", "stage", "aggressive"] = "stage"
    hyp3: dict[str, Any] = Field(
        default_factory=dict,
        description="hyp3 adapter options (apply_water_mask, looks snapping, batch size); see engines/hyp3.py",
    )
    isce2: dict[str, Any] = Field(
        default_factory=dict,
        description="isce2_topsstack options: slc_dir, orbit_dir, aux_dir, dem, bbox [S,N,W,E], "
        "reference_date, num_connections, esd_coherence_threshold, num_overlap_connections, "
        "unwrap_in_isce, workdir, max_parallel_per_step, retries, regenerate_run_files",
    )

    @field_validator("looks", mode="before")
    @classmethod
    def _looks(cls, v: Any) -> Any:
        if isinstance(v, list):
            if len(v) != 2:
                msg = "engine.looks must be 'auto' or [rg, az]"
                raise ValueError(msg)
            return (int(v[0]), int(v[1]))
        return v


class MaskCfg(_Strict):
    water: bool = True
    layover: bool = True
    coherence: bool = True


class TilesCfg(_Strict):
    rows: int = Field(ge=1)
    cols: int = Field(ge=1)
    overlap: float = Field(default=0.25, ge=0.0, lt=1.0)
    min_overlap_px: int = 200


class UnwrapCfg(_Strict):
    method: Literal["snaphu", "tophu", "spurt", "auto"] = "auto"
    cost: Literal["defo", "smooth", "topo"] = "defo"
    init: Literal["mst", "mcf"] = "mcf"
    coherence_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    mask: MaskCfg = Field(default_factory=MaskCfg)
    tiles: Literal["auto"] | TilesCfg = "auto"
    memory_mb_per_mpixel: float = Field(
        default=100.0,
        description="SNAPHU single-tile memory model constant c (MB per 1e6 pixels); "
        "initial value from the SNAPHU man page, to be re-fitted by `wintersar bench`",
    )
    nproc_per_igram: int = 1
    save_cost_file: bool = False


class TimeseriesCfg(_Strict):
    engine: Literal["mintpy", "dolphin", "fake"] = "mintpy"
    reference_point: Literal["auto_recommend", "auto"] | tuple[float, float] = "auto_recommend"
    troposphere: Literal["era5", "gacos", "height_correlation", "none"] = "era5"
    deramp: Literal["linear", "quadratic", "no"] = "linear"
    unwrap_error_correction: Literal["phase_closure", "bridging", "no"] = "phase_closure"
    coherence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    dolphin: dict[str, Any] = Field(
        default_factory=dict,
        description="dolphin options: cslc_files|cslc_glob|cslc_dir, subdataset, wavelength_m, "
        "ministack_size, half_window, max_bandwidth, unwrap_method, ntiles, reference_point_rowcol, "
        "strides, workdir",
    )

    @field_validator("reference_point", mode="before")
    @classmethod
    def _refpt(cls, v: Any) -> Any:
        if isinstance(v, list):
            if len(v) != 2:
                msg = "timeseries.reference_point must be 'auto_recommend' or [lat, lon]"
                raise ValueError(msg)
            return (float(v[0]), float(v[1]))
        return v


class GnssCfg(_Strict):
    source: Literal["csv", "ngii"] = "csv"
    path: Path | None = None


class ValidateCfg(_Strict):
    leveling_csv: Path | None = None
    gnss: GnssCfg | None = None
    radius_m: float = 100.0


class ComputeCfg(_Strict):
    cores: Literal["auto"] | int = "auto"
    memory_gb: Literal["auto"] | float = "auto"
    gpu: Literal["auto"] | bool = "auto"
    cache_dir: Path | None = Field(
        default=None,
        description="content-addressed cache for orbits/DEM/weather (PERF-02); default ~/.cache/wintersar",
    )


class Config(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: ProjectCfg
    aoi: Path
    time_range: TimeRange
    data: DataCfg = Field(default_factory=DataCfg)
    selection: SelectionCfg = Field(default_factory=SelectionCfg)
    engine: EngineCfg = Field(default_factory=EngineCfg)
    unwrap: UnwrapCfg = Field(default_factory=UnwrapCfg)
    timeseries: TimeseriesCfg = Field(default_factory=TimeseriesCfg)
    validation: ValidateCfg = Field(default_factory=ValidateCfg, alias="validate")
    compute: ComputeCfg = Field(default_factory=ComputeCfg)

    # populated by load_config
    config_path: Path | None = Field(default=None, exclude=True)

    # ---------------------------------------------------------------- paths
    @property
    def workdir(self) -> Path:
        return self.project.workdir

    @property
    def cache_dir(self) -> Path:
        if self.compute.cache_dir is not None:
            return self.compute.cache_dir
        env = os.environ.get("WINTERSAR_CACHE")
        return Path(env) if env else Path.home() / ".cache" / "wintersar"

    # ---------------------------------------------------------------- hashing
    def stage_params(self, stage: str) -> dict[str, Any]:
        """Canonical parameters relevant to ``stage`` (used by the DAG hash).

        Only the sections that influence a stage are included, so changing
        e.g. ``unwrap.coherence_threshold`` invalidates ``unwrap`` and downstream but not
        ``interferogram`` (PERF-03).
        """
        sections: dict[str, list[str]] = {
            "search": ["aoi", "time_range", "data"],
            "precheck": ["aoi", "time_range", "data", "selection"],
            "fetch": ["data"],
            "coregister": ["engine"],
            "interferogram": ["engine"],
            "multilook": ["engine"],
            "unwrap": ["unwrap"],
            "timeseries": ["timeseries"],
            "corrections": ["timeseries"],
            "geocode": ["engine"],
            "validate": ["validate"],
        }
        picked = sections.get(stage, [])
        dumped = self.model_dump(mode="json", by_alias=True, exclude={"config_path"})
        params = {k: dumped[k] for k in picked if k in dumped}
        return cast(dict[str, Any], _sort_recursive(params))


def _sort_recursive(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sort_recursive(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list | tuple):
        return [_sort_recursive(x) for x in obj]
    return obj


def _resolve_paths(cfg: Config, base: Path) -> Config:
    def rp(p: Path | None) -> Path | None:
        if p is None:
            return None
        return p if p.is_absolute() else (base / p).resolve()

    cfg.project.workdir = rp(cfg.project.workdir) or cfg.project.workdir
    cfg.aoi = rp(cfg.aoi) or cfg.aoi
    cfg.validation.leveling_csv = rp(cfg.validation.leveling_csv)
    if cfg.validation.gnss is not None:
        cfg.validation.gnss.path = rp(cfg.validation.gnss.path)
    cfg.compute.cache_dir = rp(cfg.compute.cache_dir)
    return cfg


def load_config(path: str | Path) -> Config:
    p = Path(path)
    with p.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = Config.model_validate(raw)
    cfg.config_path = p.resolve()
    return _resolve_paths(cfg, p.resolve().parent)


def dump_config(cfg: Config) -> str:
    data = cfg.model_dump(mode="json", by_alias=True, exclude={"config_path"}, exclude_none=True)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


EXAMPLE_CONFIG = """\
project:
  name: site-a-subsidence
  workdir: ./work
  language: ko
aoi: aoi.geojson
time_range: { start: 2023-01-01, end: 2025-12-31 }
data:
  source: asf
  product: burst
  polarization: VV
  orbit_direction: auto
  relative_orbit: auto
  credentials: env:EARTHDATA_TOKEN
selection:
  network: sbas
  max_perp_baseline_m: 150
  max_temporal_baseline_days: 48
  min_coverage: 0.95
engine:
  interferogram: hyp3
  looks: auto
  target_pixel_m: 40
  filter: { type: goldstein, alpha: 0.6, window: 64 }
unwrap:
  method: auto
  cost: defo
  coherence_threshold: 0.3
  mask: { water: true, layover: true }
  tiles: auto
timeseries:
  engine: mintpy
  reference_point: auto_recommend
  troposphere: era5
  deramp: linear
  unwrap_error_correction: phase_closure
validate:
  leveling_csv: data/leveling.csv
compute:
  cores: auto
  memory_gb: auto
  gpu: auto
"""
