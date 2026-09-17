"""Shared pydantic data models (plan §4.3).

Every module exchanges data through these models so that the CLI ``--json`` output,
the QGIS plugin, the DAG cache manifests and the reports agree on one schema.

Conventions
-----------
* Dates are ``datetime.date`` (acquisition day) unless a full timestamp is needed.
* Distances are metres, angles are degrees, phase is radians.
* ``Finding`` carries only *keys* for user-facing text; the actual sentences live in
  ``wintersar/i18n`` (rule 11.6).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["FAIL", "WARN", "INFO"]
FlightDirection = Literal["ASCENDING", "DESCENDING"]


def _plain(value: Any) -> Any:
    """Recursively replace numpy scalars/arrays with built-ins.

    The free-form ``dict[str, Any]`` fields below (``params``, ``evidence``, ``meta``,
    ``extra``, ``notes``) are filled by rules and engine adapters that compute with numpy.
    Pydantic accepts such a value but every serializer downstream (``emit_json``,
    ``cache.write_record``, the run summary) then raises ``PydanticSerializationError``,
    turning one stray ``np.float32`` into a crash *while reporting a failure*. Normalising
    on the way in keeps the models JSON-serialisable by construction.
    """
    if value is None or isinstance(value, str | bytes):
        return value
    tolist = getattr(value, "tolist", None)
    if callable(tolist) and hasattr(value, "dtype"):  # np.ndarray / np.generic
        return _plain(tolist())
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain(v) for v in value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


class Platform(StrEnum):
    S1A = "S1A"
    S1B = "S1B"
    S1C = "S1C"
    S1D = "S1D"


class BurstRecord(BaseModel):
    """One Sentinel-1 burst (or, for SLC fallback, one scene) as returned by search.

    Field provenance (which values come from the search API and which only exist after
    download) is documented in ``wintersar/select/metadata.py``.
    """

    model_config = ConfigDict(frozen=True)

    granule_id: str
    platform: Platform
    mode: Literal["IW", "EW", "SM"]
    subswath: str = Field(description="IW1 | IW2 | IW3 (or swath name for SM/EW)")
    full_burst_id: str = Field(
        description="ASF Full Burst ID, e.g. '052_109903_IW2'. For SLC fallback: scene-level id.",
    )
    relative_orbit: int = Field(ge=1, le=175, description="Sentinel-1 track number")
    absolute_orbit: int
    flight_direction: FlightDirection
    polarization: str = Field(description="VV | VH | HH | HV")
    acquisition_time: datetime
    ipf_version: str | None = None
    range_pixel_spacing_m: float | None = None
    azimuth_pixel_spacing_m: float | None = None
    incidence_near_deg: float | None = None
    incidence_far_deg: float | None = None
    footprint_wkt: str
    url: str
    product_type: Literal["BURST", "SLC"] = "BURST"
    extra: dict[str, Any] = Field(default_factory=dict, description="Raw provider fields")

    @field_validator("extra", mode="before")
    @classmethod
    def _plain_values(cls, v: Any) -> Any:
        return _plain(v)

    @property
    def acquisition_date(self) -> date:
        return self.acquisition_time.date()


class Pair(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: date
    secondary: date
    temporal_baseline_days: int
    perp_baseline_m: float | None = Field(
        default=None, description="asf_search stack value or self-computed from POEORB"
    )

    @field_validator("temporal_baseline_days")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            msg = "temporal_baseline_days must be > 0 (reference < secondary)"
            raise ValueError(msg)
        return v

    @property
    def key(self) -> str:
        return f"{self.reference:%Y%m%d}_{self.secondary:%Y%m%d}"


class StackCandidate(BaseModel):
    """A coherent set of acquisitions that can be interfered together (plan §5.1.2)."""

    relative_orbit: int
    flight_direction: FlightDirection
    polarization: str
    subswaths: list[str] = Field(default_factory=list)
    burst_ids: list[str] = Field(
        description="AOI-intersecting bursts present on *every* date of the stack"
    )
    dates: list[date]
    reference_date: date | None = None
    coverage_of_aoi: float = Field(ge=0.0, le=1.0)
    pairs: list[Pair] = Field(default_factory=list)
    n_dates_dropped: int = 0
    n_bursts_dropped: int = 0
    product_type: Literal["BURST", "SLC"] = "BURST"
    notes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("notes", mode="before")
    @classmethod
    def _plain_values(cls, v: Any) -> Any:
        return _plain(v)

    @property
    def stack_id(self) -> str:
        d = "A" if self.flight_direction == "ASCENDING" else "D"
        return f"T{self.relative_orbit:03d}{d}_{self.polarization}"


class Finding(BaseModel):
    """Result of a precheck rule (SEL-xx), diagnosis (KB-xx) or install check (ENV-xx)."""

    rule_id: str
    severity: Severity
    message_key: str = Field(description="i18n key, e.g. 'select.sel01.fail'")
    params: dict[str, Any] = Field(default_factory=dict, description="format params for message")
    evidence: dict[str, Any] = Field(
        default_factory=dict, description="values used for the verdict"
    )
    fix_key: str | None = None
    refs: list[str] = Field(default_factory=list)
    scope: str | None = Field(
        default=None, description="stack_id / pair key / stage the finding is about"
    )

    @field_validator("params", "evidence", mode="before")
    @classmethod
    def _plain_values(cls, v: Any) -> Any:
        return _plain(v)

    @property
    def is_fail(self) -> bool:
        return self.severity == "FAIL"


SEVERITY_ORDER: dict[str, int] = {"FAIL": 0, "WARN": 1, "INFO": 2}


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.rule_id))


class Resources(BaseModel):
    """Estimated (or measured) resource usage of a stage / plan."""

    wall_time_s: float | None = None
    peak_rss_gb: float | None = None
    disk_gb: float | None = None
    network_gb: float | None = None
    credits: float | None = Field(default=None, description="HyP3 credits (None for local)")
    n_jobs: int | None = None
    notes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("notes", mode="before")
    @classmethod
    def _plain_values(cls, v: Any) -> Any:
        return _plain(v)

    def __add__(self, other: Resources) -> Resources:
        def _add(a: float | None, b: float | None) -> float | None:
            if a is None and b is None:
                return None
            return (a or 0.0) + (b or 0.0)

        def _max(a: float | None, b: float | None) -> float | None:
            if a is None and b is None:
                return None
            return max(a or 0.0, b or 0.0)

        return Resources(
            wall_time_s=_add(self.wall_time_s, other.wall_time_s),
            peak_rss_gb=_max(self.peak_rss_gb, other.peak_rss_gb),
            disk_gb=_add(self.disk_gb, other.disk_gb),
            network_gb=_add(self.network_gb, other.network_gb),
            credits=_add(self.credits, other.credits),
            n_jobs=int(_add(self.n_jobs, other.n_jobs) or 0),
        )


class Artifact(BaseModel):
    """One file/directory produced or consumed by a stage."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="logical name, e.g. 'unw', 'coh', 'timeseries'")
    path: Path
    kind: str = Field(default="file", description="file | dir | zarr | h5 | cog | json")
    sha256: str | None = Field(default=None, description="content hash (files) or manifest hash")
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("meta", mode="before")
    @classmethod
    def _plain_values(cls, v: Any) -> Any:
        return _plain(v)


class Artifacts(BaseModel):
    """Named collection of artifacts flowing between stages."""

    items: dict[str, Artifact] = Field(default_factory=dict)

    def __getitem__(self, name: str) -> Artifact:
        return self.items[name]

    def __contains__(self, name: object) -> bool:
        return name in self.items

    def add(self, artifact: Artifact) -> Artifacts:
        self.items[artifact.name] = artifact
        return self

    def paths(self) -> dict[str, Path]:
        return {k: v.path for k, v in self.items.items()}

    def merged(self, other: Artifacts) -> Artifacts:
        return Artifacts(items={**self.items, **other.items})


class StageRecord(BaseModel):
    """Manifest written to ``work/<stage>/<hash>/manifest.json`` (plan §5.3)."""

    stage: str
    node_hash: str
    engine: str | None = None
    engine_version: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict, description="artifact name -> hash")
    outputs: dict[str, str] = Field(default_factory=dict, description="artifact name -> path")
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: Literal["pending", "running", "ok", "failed", "skipped"] = "pending"
    resources: Resources = Field(default_factory=Resources)
    findings: list[Finding] = Field(default_factory=list)
    log_path: Path | None = None
    extra: dict[str, Any] = Field(default_factory=dict, description="e.g. snaphu tile dir for -A")

    @field_validator("params", "extra", mode="before")
    @classmethod
    def _plain_values(cls, v: Any) -> Any:
        return _plain(v)


class Plan(BaseModel):
    """Dry-run output of ``wintersar plan``."""

    stages: list[StageRecord]
    to_run: list[str] = Field(description="node hashes that will actually execute")
    cached: list[str] = Field(default_factory=list)
    resources: Resources = Field(default_factory=Resources)
    findings: list[Finding] = Field(default_factory=list)


class GroundTruthRecord(BaseModel):
    """One row of the ground-truth CSV (plan §5.6).

    ``site_id, lat, lon, elev_m, date, up_m, east_m, north_m, method, sigma_mm``
    Levelling supplies only ``up_m``; GNSS supplies ENU.
    """

    site_id: str
    lat: float
    lon: float
    elev_m: float | None = None
    date: date
    up_m: float | None = None
    east_m: float | None = None
    north_m: float | None = None
    method: Literal["leveling", "gnss"]
    sigma_mm: float | None = None
