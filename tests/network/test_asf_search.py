"""Live asf_search checks (R-01, SEL-06). Run with ``pytest -m network`` / ``--run-network``.

They skip cleanly when no Earthdata credentials are configured (EARTHDATA_TOKEN or ~/.netrc),
even though CMR search and the stack API themselves are public.
Hand-checked values were recorded on 2026-09-16 with asf_search 14.0.0 and independently
reproduced by the orbit self-computation (ADR-0011).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from wintersar.io.schemas import Platform
from wintersar.pipeline.config import Config
from wintersar.select import baseline as bl
from wintersar.select.auth import find_credentials
from wintersar.select.search import aoi_to_wkt, search_bursts

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        find_credentials() is None, reason="no Earthdata credentials (EARTHDATA_TOKEN / ~/.netrc)"
    ),
]

REFERENCE = "S1_270859_IW2_20240107T093233_VV_0C4A-BURST"
# asf_search stack API values (integer metres) relative to REFERENCE, recorded 2026-09-16;
# orbit self-computation gave -15.3 / 129.7 / 154.6 on the same day.
HAND_CHECKED = {
    "S1_270859_IW2_20240119T093233_VV_938E-BURST": -15,
    "S1_270859_IW2_20231226T093234_VV_86AF-BURST": 130,
    "S1_270859_IW2_20231108T093236_VV_F8E9-BURST": 155,
}


@pytest.fixture
def seoul_config(tmp_path: Path) -> Config:
    aoi = tmp_path / "aoi.geojson"
    aoi.write_text(
        '{"type":"Polygon","coordinates":[[[126.90,37.50],[127.00,37.50],[127.00,37.60],[126.90,37.60],[126.90,37.50]]]}',
        encoding="utf-8",
    )
    return Config.model_validate(
        {
            "project": {"name": "seoul-net", "workdir": str(tmp_path / "work")},
            "aoi": str(aoi),
            "time_range": {"start": "2024-01-01", "end": "2024-01-14"},
            "data": {"polarization": "VV", "platform": ["S1A"]},
        }
    )


def test_seoul_two_week_burst_search(seoul_config: Config) -> None:
    res = search_bursts(seoul_config, aoi_to_wkt(seoul_config.aoi))
    assert res.ok and res.product_type == "BURST"
    assert res.records, "expected Sentinel-1A bursts over Seoul in the first two weeks of 2024"
    rec = next(r for r in res.records if r.granule_id == REFERENCE)
    assert rec.platform is Platform.S1A and rec.mode == "IW" and rec.subswath == "IW2"
    assert rec.full_burst_id == "127_270859_IW2" and rec.relative_orbit == 127
    assert rec.flight_direction == "ASCENDING" and rec.polarization == "VV"
    assert rec.acquisition_date == date(2024, 1, 7) and rec.absolute_orbit == 51999
    assert rec.ipf_version == "3.71"
    assert rec.url.startswith("https://sentinel1-burst.asf.alaska.edu/")
    assert rec.extra["metadata_url"].endswith(".xml")
    assert rec.range_pixel_spacing_m is None  # not in the search response
    assert "state_vectors" in rec.extra
    assert 127 in res.relative_orbits


def test_stack_baseline_hand_checked(seoul_config: Config) -> None:
    cfg = seoul_config.model_copy(deep=True)
    cfg.time_range.start, cfg.time_range.end = date(2023, 11, 1), date(2024, 2, 29)
    res = search_bursts(cfg, aoi_to_wkt(cfg.aoi))
    recs = [r for r in res.records if r.full_burst_id == "127_270859_IW2"]
    ref = next(r for r in recs if r.granule_id == REFERENCE)
    asf_values = bl.perpendicular_baselines_asf(recs, ref)
    assert asf_values[REFERENCE] == 0.0
    for granule, expected in HAND_CHECKED.items():
        assert asf_values[granule] == pytest.approx(expected, abs=1.0)
    orbit_values = bl.perpendicular_baselines_orbit(recs, ref)
    for granule in HAND_CHECKED:
        assert orbit_values[granule] == pytest.approx(asf_values[granule], abs=1.5)
