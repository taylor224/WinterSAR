"""Fixtures for select/search tests: fake asf_search products built from real responses.

``tests/fixtures/asf/burst_products.json`` holds ``S1BurstProduct.properties`` / ``geometry`` /
``baseline`` captured from asf_search 14.0.0 on 2026-09-16 (Seoul AOI, burst 127_270859_IW2,
Nov 2023 - Feb 2024) plus the stack API's perpendicular/temporal baselines for those products.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wintersar.pipeline.config import Config

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "asf"
SEOUL_WKT = "POLYGON((126.90 37.50,127.00 37.50,127.00 37.60,126.90 37.60,126.90 37.50))"


class FakeProduct:
    """Duck-typed stand-in for ``asf_search.ASFProduct`` subclasses."""

    def __init__(self, item: dict[str, Any], class_name: str | None = None) -> None:
        self.properties: dict[str, Any] = json.loads(json.dumps(item["properties"]))
        self.geometry: dict[str, Any] = item.get("geometry") or {}
        self.baseline: dict[str, Any] | None = item.get("baseline")
        self.meta: dict[str, Any] | None = item.get("meta")
        self.umm: dict[str, Any] | None = item.get("umm")
        self._class_name = class_name or item.get("class", "S1BurstProduct")

    @property
    def __class__(self):  # type: ignore[override]
        return type(self._class_name, (FakeProduct,), {})

    def geojson(self) -> dict[str, Any]:
        return {"type": "Feature", "geometry": self.geometry, "properties": self.properties}


class FakeResults(list):
    """``asf_search.ASFSearchResults`` look-alike (a list with ``searchComplete``)."""

    def __init__(self, items: list[Any], complete: bool = True) -> None:
        super().__init__(items)
        self.searchComplete = complete
        self.searchOptions = None


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def burst_fixture() -> dict[str, Any]:
    return load_fixture("burst_products.json")


@pytest.fixture(scope="session")
def slc_fixture() -> dict[str, Any]:
    return load_fixture("slc_products.json")


@pytest.fixture
def burst_products(burst_fixture: dict[str, Any]) -> list[FakeProduct]:
    return [FakeProduct(item) for item in burst_fixture["products"]]


@pytest.fixture
def slc_products(slc_fixture: dict[str, Any]) -> list[FakeProduct]:
    return [FakeProduct(item) for item in slc_fixture["products"]]


@pytest.fixture
def stack_expected(burst_fixture: dict[str, Any]) -> dict[str, dict[str, int | None]]:
    return burst_fixture["stack"]["values"]


@pytest.fixture
def stack_reference_id(burst_fixture: dict[str, Any]) -> str:
    return burst_fixture["stack"]["reference"]


@pytest.fixture
def fake_stack(burst_products: list[FakeProduct], stack_expected: dict[str, Any]):
    """Products of the fixture stack with the stack API's baseline properties attached."""

    def _make() -> FakeResults:
        out = []
        for p in burst_products:
            name = p.properties["sceneName"]
            if name not in stack_expected:
                continue
            q = FakeProduct(
                {"properties": p.properties, "geometry": p.geometry, "baseline": p.baseline}
            )
            q.properties["perpendicularBaseline"] = stack_expected[name]["perpendicularBaseline"]
            q.properties["temporalBaseline"] = stack_expected[name]["temporalBaseline"]
            out.append(q)
        out.sort(key=lambda q: q.properties["temporalBaseline"])
        return FakeResults(out)

    return _make


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Config for the Seoul AOI, Nov 2023 - Feb 2024, no credentials in the environment."""
    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
    aoi = tmp_path / "aoi.geojson"
    aoi.write_text(
        '{"type":"Feature","properties":{},"geometry":{"type":"Polygon","coordinates":'
        "[[[126.90,37.50],[127.00,37.50],[127.00,37.60],[126.90,37.60],[126.90,37.50]]]}}",
        encoding="utf-8",
    )
    return Config.model_validate(
        {
            "project": {"name": "seoul-test", "workdir": str(tmp_path / "work")},
            "aoi": str(aoi),
            "time_range": {"start": "2023-11-01", "end": "2024-02-29"},
            "data": {"polarization": "VV", "orbit_direction": "auto"},
        }
    )


@pytest.fixture
def patch_search(monkeypatch: pytest.MonkeyPatch):
    """``patch_search(handler)`` installs ``handler(**kwargs)`` as ``asf_search.search``."""
    import asf_search

    calls: list[dict[str, Any]] = []

    def _install(handler):
        def _search(**kwargs):
            calls.append(kwargs)
            return handler(**kwargs)

        monkeypatch.setattr(asf_search, "search", _search)
        return calls

    return _install
