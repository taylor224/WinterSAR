"""Fixtures for the select core tests (rules, looks, network, report, CLI)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from itertools import pairwise
from pathlib import Path

import pytest

from tests.conftest import make_burst
from wintersar.io.schemas import BurstRecord, Pair, StackCandidate
from wintersar.pipeline.config import Config

# The AOI of tests/conftest.py::aoi_geojson (10 km box near Seoul).
AOI_WKT = "POLYGON((126.90 37.50,127.00 37.50,127.00 37.60,126.90 37.60,126.90 37.50))"

# Two bursts that each cover exactly one half of the AOI (west / east).
BURST_W = "052_109903_IW2"
BURST_E = "052_109904_IW2"
FOOT_W = "POLYGON((126.80 37.40,126.95 37.40,126.95 37.70,126.80 37.70,126.80 37.40))"
FOOT_E = "POLYGON((126.95 37.40,127.10 37.40,127.10 37.70,126.95 37.70,126.95 37.40))"
# A burst far away from the AOI.
BURST_FAR = "052_109950_IW3"
FOOT_FAR = "POLYGON((128.00 38.00,128.20 38.00,128.20 38.20,128.00 38.20,128.00 38.00))"

DATES_12D = ["2024-01-01", "2024-01-13", "2024-01-25", "2024-02-06", "2024-02-18"]


def burst_pair(date_str: str, **kw: object) -> list[BurstRecord]:
    """West + east burst for one date (full AOI coverage together)."""
    return [
        make_burst(date_str, full_burst_id=BURST_W, footprint_wkt=FOOT_W, **kw),
        make_burst(date_str, full_burst_id=BURST_E, footprint_wkt=FOOT_E, **kw),
    ]


def full_stack(dates: Sequence[str] = DATES_12D, **kw: object) -> list[BurstRecord]:
    out: list[BurstRecord] = []
    for d in dates:
        out.extend(burst_pair(d, **kw))
    return out


def make_candidate(
    dates: Sequence[str] = DATES_12D,
    burst_ids: Sequence[str] = (BURST_W, BURST_E),
    relative_orbit: int = 52,
    flight_direction: str = "DESCENDING",
    polarization: str = "VV",
    coverage: float = 1.0,
    pairs: Sequence[Pair] | None = None,
    **kw: object,
) -> StackCandidate:
    ds = [date.fromisoformat(d) for d in dates]
    if pairs is None:
        pairs = [
            Pair(reference=a, secondary=b, temporal_baseline_days=(b - a).days)
            for a, b in pairwise(ds)
        ]
    return StackCandidate(
        relative_orbit=relative_orbit,
        flight_direction=flight_direction,  # type: ignore[arg-type]
        polarization=polarization,
        subswaths=["IW2"],
        burst_ids=list(burst_ids),
        dates=ds,
        coverage_of_aoi=coverage,
        pairs=list(pairs),
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture
def aoi_wkt() -> str:
    return AOI_WKT


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "project": {"name": "t", "workdir": str(tmp_path / "work"), "language": "ko"},
            "aoi": str(tmp_path / "aoi.geojson"),
            "time_range": {"start": "2024-01-01", "end": "2024-12-31"},
            "data": {"polarization": "VV", "orbit_direction": "auto"},
            "selection": {
                "network": "sbas",
                "max_perp_baseline_m": 150,
                "max_temporal_baseline_days": 48,
                "min_coverage": 0.95,
            },
            "engine": {"interferogram": "fake", "looks": "auto", "target_pixel_m": 40},
        }
    )


@pytest.fixture
def records() -> list[BurstRecord]:
    return full_stack()


@pytest.fixture
def candidate() -> StackCandidate:
    return make_candidate()
