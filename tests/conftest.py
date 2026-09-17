"""Shared pytest fixtures.

Network tests are deselected unless ``--run-network`` (or ``-m network``) is given.
``engine``-marked tests are adapter *contract* tests driven by stubs/mocks and run
everywhere; only ``engine_real`` needs the external engine installed (see the marker
descriptions in ``pyproject.toml``).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from wintersar.io.schemas import BurstRecord


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-network", action="store_true", default=False, help="run network tests")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-network") or config.getoption("-m"):
        return
    skip = pytest.mark.skip(reason="needs --run-network or -m network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _lang_ko(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINTERSAR_LANG", "ko")


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(42)


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setenv("WINTERSAR_CACHE", str(d))
    return d


@pytest.fixture
def aoi_geojson(tmp_path: Path) -> Path:
    """Small AOI near Seoul (~10 km box)."""
    p = tmp_path / "aoi.geojson"
    p.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
        '"geometry":{"type":"Polygon","coordinates":[[[126.90,37.50],[127.00,37.50],'
        "[127.00,37.60],[126.90,37.60],[126.90,37.50]]]}}]}",
        encoding="utf-8",
    )
    return p


def make_burst(
    date: str,
    full_burst_id: str = "052_109903_IW2",
    relative_orbit: int = 52,
    flight_direction: str = "DESCENDING",
    polarization: str = "VV",
    subswath: str = "IW2",
    platform: str = "S1A",
    absolute_orbit: int = 50000,
    footprint_wkt: str = "POLYGON((126.8 37.4,127.1 37.4,127.1 37.7,126.8 37.7,126.8 37.4))",
    **extra: object,
) -> BurstRecord:
    """Factory used across select tests (importable: ``from tests.conftest import make_burst``)."""
    return BurstRecord(
        granule_id=f"S1_{full_burst_id.replace('_', '')}_{date}T092000_{polarization}",
        platform=platform,  # type: ignore[arg-type]
        mode="IW",
        subswath=subswath,
        full_burst_id=full_burst_id,
        relative_orbit=relative_orbit,
        absolute_orbit=absolute_orbit,
        flight_direction=flight_direction,  # type: ignore[arg-type]
        polarization=polarization,
        acquisition_time=datetime.fromisoformat(f"{date}T09:20:00"),
        ipf_version=str(extra.pop("ipf_version", "3.71")),
        range_pixel_spacing_m=float(extra.pop("range_pixel_spacing_m", 2.33)),
        azimuth_pixel_spacing_m=float(extra.pop("azimuth_pixel_spacing_m", 14.1)),
        incidence_near_deg=float(extra.pop("incidence_near_deg", 36.5)),
        incidence_far_deg=float(extra.pop("incidence_far_deg", 41.9)),
        footprint_wkt=footprint_wkt,
        url=f"https://example.invalid/{full_burst_id}/{date}",
        extra={k: v for k, v in extra.items()},
    )


@pytest.fixture
def make_burst_factory() -> type:
    return type("F", (), {"__call__": staticmethod(make_burst)})


# Keep ``os`` referenced for environments that patch env vars in fixtures.
_ = os
