"""Benchmark site definitions ``benchmarks/sites/*.yaml`` (plan §5.9 / §6.3, ADR-0054).

A site is a pydantic :class:`Site`: which engine path to run (``runner``), the config
overrides, per-stage parameter overrides (fake-engine ``n_dates``/``shape`` …), an optional
ground-truth CSV (plan §5.6 schema), the number of repeats (median is reported) and the
stages to measure. Real-site YAMLs are *templates*: their placeholders (``<AOI_GEOJSON>``)
make :meth:`Site.is_template` true and the runner refuses to run them (BENCH-004).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wintersar.pipeline.config import Config, load_config

# source: src/wintersar/pipeline/stages.py STAGE_ORDER (engine stages the fake engine runs)
ENGINE_STAGES: tuple[str, ...] = (
    "fetch",
    "coregister",
    "interferogram",
    "multilook",
    "unwrap",
    "timeseries",
    "corrections",
    "geocode",
)
ALL_STAGES: tuple[str, ...] = ("search", "precheck", *ENGINE_STAGES, "validate")
METRICS: tuple[str, ...] = ("closure_rms", "unwrap_error_fraction", "gt_rmse")
PLACEHOLDER_PREFIX = "<"
DEFAULT_REPEATS = 3
DEFAULT_REGRESSION_THRESHOLD = 0.15  # plan §6.3 item 5: fail when > 15 % slower


class TimeRangeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date | str
    end: date | str


class Site(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    size: Literal["S", "M", "L"]
    description: str = ""
    synthetic: bool = False
    network: bool = False
    runner: Literal["pipeline", "fake"] = "pipeline"
    aoi: str | None = None
    time_range: TimeRangeSpec | None = None
    config: dict[str, Any] = Field(default_factory=dict, description="config.yaml overrides")
    param_overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="stage -> params (pipeline.api.run param_overrides)"
    )
    ground_truth: str | None = Field(default=None, description="ground-truth CSV (plan §5.6)")
    repeats: int = Field(default=DEFAULT_REPEATS, ge=1)
    stages: list[str] = Field(default_factory=lambda: list(ENGINE_STAGES))
    metrics: list[str] = Field(default_factory=lambda: list(METRICS))
    max_wall_time_s: float | None = Field(default=None, gt=0)
    regression_threshold: float = Field(default=DEFAULT_REGRESSION_THRESHOLD, ge=0)
    baseline: str | None = None
    notes: dict[str, Any] = Field(default_factory=dict)
    path: Path | None = Field(default=None, exclude=True)

    @field_validator("stages")
    @classmethod
    def _stages_known(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if s not in ALL_STAGES]
        if bad:
            msg = f"unknown stages {bad}; known: {list(ALL_STAGES)}"
            raise ValueError(msg)
        if not v:
            msg = "stages must not be empty"
            raise ValueError(msg)
        return v

    @field_validator("metrics")
    @classmethod
    def _metrics_known(cls, v: list[str]) -> list[str]:
        bad = [m for m in v if m not in METRICS]
        if bad:
            msg = f"unknown metrics {bad}; known: {list(METRICS)}"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _consistent(self) -> Site:
        if self.synthetic and self.network:
            msg = "a synthetic site cannot require the network"
            raise ValueError(msg)
        if not self.synthetic and self.aoi is None:
            msg = "real sites need an 'aoi' (path or <PLACEHOLDER>)"
            raise ValueError(msg)
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def placeholders(self) -> list[str]:
        vals = [self.aoi, self.ground_truth, self.baseline]
        if self.time_range is not None:
            vals.extend([str(self.time_range.start), str(self.time_range.end)])
        return [v for v in vals if isinstance(v, str) and v.startswith(PLACEHOLDER_PREFIX)]

    @property
    def is_template(self) -> bool:
        return bool(self.placeholders)

    def resolve(self, p: str | None) -> Path | None:
        """Resolve a site-relative path against the YAML directory (``None`` for placeholders)."""
        if p is None or p.startswith(PLACEHOLDER_PREFIX):
            return None
        q = Path(p)
        if q.is_absolute() or self.path is None:
            return q
        return (self.path.parent / q).resolve()

    @property
    def ground_truth_path(self) -> Path | None:
        return self.resolve(self.ground_truth)

    @property
    def baseline_path(self) -> Path | None:
        return self.resolve(self.baseline)

    def summary(self) -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude={"notes"})
        d["path"] = None if self.path is None else str(self.path)
        d["is_template"] = self.is_template
        return d


def load_site(path: Path | str) -> Site:
    p = Path(path)
    with p.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        msg = f"{p}: top level must be a mapping"
        raise ValueError(msg)
    site = Site.model_validate(raw)
    site.path = p.resolve()
    return site


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def site_config_mapping(site: Site, workdir: Path) -> dict[str, Any]:
    """The ``config.yaml`` mapping for ``site`` (plan §4.4) with ``site.config`` merged in."""
    if site.synthetic:
        base: dict[str, Any] = {
            "project": {"name": site.name, "workdir": str(workdir / "work"), "language": "ko"},
            "aoi": site.aoi or str(workdir / "aoi.geojson"),
            "time_range": {"start": "2024-01-01", "end": "2024-12-31"},
            "engine": {"interferogram": "fake"},
            "timeseries": {"engine": "fake"},
            "unwrap": {"method": "auto"},
            "compute": {"cores": 2, "memory_gb": 4},
        }
    else:
        base = {
            "project": {"name": site.name, "workdir": str(workdir / "work"), "language": "ko"},
            "aoi": site.aoi,
            "time_range": {"start": "2024-01-01", "end": "2024-12-31"},
        }
    if site.time_range is not None:
        base["time_range"] = {
            "start": str(site.time_range.start),
            "end": str(site.time_range.end),
        }
    if site.ground_truth_path is not None:
        base = _deep_merge(base, {"validate": {"leveling_csv": str(site.ground_truth_path)}})
    return _deep_merge(base, site.config)


def build_config(site: Site, workdir: Path) -> Config:
    """Write ``<workdir>/config.yaml`` (and an empty AOI for synthetic sites) and load it."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mapping = site_config_mapping(site, workdir)
    if site.synthetic:
        aoi = Path(str(mapping["aoi"]))
        if not aoi.is_absolute():
            aoi = workdir / aoi
        if not aoi.exists():
            aoi.parent.mkdir(parents=True, exist_ok=True)
            aoi.write_text(
                json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
            )
        mapping["aoi"] = str(aoi)
    cfg_path = workdir / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(mapping, allow_unicode=True), encoding="utf-8")
    return load_config(cfg_path)
