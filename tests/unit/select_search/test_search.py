"""R-01 / PERF-01: asf_search query construction, BURST -> SLC fallback, candidates.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import shapely

from tests.unit.select_search.conftest import SEOUL_WKT, FakeProduct, FakeResults
from wintersar.pipeline.config import Config
from wintersar.select import search as sr

# ---------------------------------------------------------------- AOI


def _wkt_area(wkt: str) -> float:
    return float(shapely.from_wkt(wkt).area)


def test_aoi_geojson_feature(config: Config) -> None:
    wkt = sr.aoi_to_wkt(config.aoi)
    geom = shapely.from_wkt(wkt)
    assert geom.geom_type == "Polygon" and geom.is_valid
    assert geom.exterior.is_ccw
    assert geom.bounds == pytest.approx((126.9, 37.5, 127.0, 37.6))


def test_aoi_geojson_feature_collection_and_geometry(tmp_path: Path) -> None:
    fc = tmp_path / "fc.geojson"
    fc.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[0.5, 0.5], [2, 0.5], [2, 2], [0.5, 2], [0.5, 0.5]]],
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    merged = shapely.from_wkt(sr.aoi_to_wkt(fc))
    assert merged.geom_type == "Polygon" and merged.area == pytest.approx(1 + 2.25 - 0.25)
    geom = tmp_path / "geom.json"
    geom.write_text(
        json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}),
        encoding="utf-8",
    )
    g = shapely.from_wkt(sr.aoi_to_wkt(geom))
    assert g.exterior.is_ccw  # clockwise input re-oriented


def test_aoi_multipolygon_uses_convex_hull(tmp_path: Path) -> None:
    mp = tmp_path / "mp.geojson"
    mp.write_text(
        json.dumps(
            {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                    [[[3, 3], [4, 3], [4, 4], [3, 4], [3, 3]]],
                ],
            }
        ),
        encoding="utf-8",
    )
    hull = shapely.from_wkt(sr.aoi_to_wkt(mp))
    assert hull.geom_type == "Polygon" and hull.contains(shapely.Point(2, 2))


def test_aoi_wkt_file_and_holes_dropped(tmp_path: Path) -> None:
    p = tmp_path / "aoi.wkt"
    p.write_text("POLYGON((0 0,10 0,10 10,0 10,0 0),(4 4,6 4,6 6,4 6,4 4))\n", encoding="utf-8")
    assert _wkt_area(sr.aoi_to_wkt(p)) == pytest.approx(100.0)


def test_aoi_errors(tmp_path: Path) -> None:
    empty = tmp_path / "empty.geojson"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        sr.aoi_to_wkt(empty)
    bad = tmp_path / "bad.geojson"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        sr.aoi_to_wkt(bad)
    point = tmp_path / "pt.geojson"
    point.write_text(json.dumps({"type": "Point", "coordinates": [1, 2]}), encoding="utf-8")
    with pytest.raises(ValueError):
        sr.aoi_to_wkt(point)
    with pytest.raises(ValueError):
        sr.aoi_to_wkt(tmp_path / "missing.geojson")


# ---------------------------------------------------------------- query


def test_build_query_burst(config: Config) -> None:
    q = sr.build_query(config, SEOUL_WKT, "BURST")
    assert q["processingLevel"] == "BURST" and q["beamMode"] == "IW"
    assert q["polarization"] == ["VV"]
    assert q["platform"] == ["Sentinel-1A", "Sentinel-1B", "Sentinel-1C", "Sentinel-1D"]
    assert q["start"] == "2023-11-01T00:00:00Z" and q["end"] == "2024-02-29T23:59:59Z"
    assert q["intersectsWith"] == SEOUL_WKT
    assert "flightDirection" not in q and "relativeOrbit" not in q
    json.dumps(q)  # serialisable


def test_build_query_slc_filters(config: Config) -> None:
    cfg = config.model_copy(deep=True)
    cfg.data.orbit_direction = "desc"
    cfg.data.relative_orbit = 61
    cfg.data.polarization = "HH"
    cfg.data.platform = ["S1A"]
    q = sr.build_query(cfg, SEOUL_WKT, "SLC")
    assert q["polarization"] == ["HH", "HH+HV"]
    assert q["flightDirection"] == "DESCENDING" and q["relativeOrbit"] == 61
    assert q["platform"] == ["Sentinel-1A"]


def test_build_query_accepts_asf_search_options(config: Config) -> None:
    """Every keyword we send must be a valid ASFSearchOptions key with a passing validator."""
    import asf_search

    for product_type in ("BURST", "SLC"):
        opts = asf_search.ASFSearchOptions(**sr.build_query(config, SEOUL_WKT, product_type))
        assert dict(opts)["processingLevel"] == [product_type]


# ---------------------------------------------------------------- search + fallback


def test_search_bursts_burst_path(
    config: Config, burst_products: list[FakeProduct], patch_search
) -> None:
    calls = patch_search(lambda **kw: FakeResults(burst_products))
    res = sr.search_bursts(config, SEOUL_WKT)
    assert res.product_type == "BURST" and res.ok
    assert len(res.records) == 11 and len(res.dates) == 10
    assert res.relative_orbits == [127]
    assert res.burst_ids == ["127_270858_IW2", "127_270859_IW2"]
    assert len(calls) == 1 and calls[0]["processingLevel"] == "BURST"
    ids = [f.rule_id for f in res.findings]
    assert ids == ["SEL-SEARCH-01"]
    assert res.findings[0].params["n_records"] == 11 and res.findings[0].params["tracks"] == "127A"
    assert res.query["processingLevel"] == "BURST"


def test_search_bursts_falls_back_to_slc(
    config: Config, slc_products: list[FakeProduct], patch_search
) -> None:
    def handler(**kw):
        return FakeResults(slc_products if kw["processingLevel"] == "SLC" else [])

    calls = patch_search(handler)
    res = sr.search_bursts(config, SEOUL_WKT)
    assert [c["processingLevel"] for c in calls] == ["BURST", "SLC"]
    assert res.product_type == "SLC" and len(res.records) == 1
    assert res.records[0].product_type == "SLC" and res.records[0].polarization == "VV"
    ids = [f.rule_id for f in res.findings]
    assert "SEL-SEARCH-02" in ids and "SEL-SEARCH-01" in ids
    fb = next(f for f in res.findings if f.rule_id == "SEL-SEARCH-02")
    assert fb.severity == "INFO" and fb.params["n_slc"] == 1


def test_search_slc_configured_skips_burst(
    config: Config, slc_products: list[FakeProduct], patch_search
) -> None:
    config.data.product = "slc"
    calls = patch_search(lambda **kw: FakeResults(slc_products))
    res = sr.search_bursts(config, SEOUL_WKT)
    assert [c["processingLevel"] for c in calls] == ["SLC"]
    assert res.product_type == "SLC"
    assert all(f.rule_id != "SEL-SEARCH-02" for f in res.findings)


def test_search_no_results(config: Config, patch_search) -> None:
    calls = patch_search(lambda **kw: FakeResults([]))
    res = sr.search_bursts(config, SEOUL_WKT)
    assert res.records == [] and len(calls) == 2
    f = res.findings[-1]
    assert f.rule_id == "SEL-SEARCH-03" and f.severity == "WARN"
    assert f.params["product_type"] == "BURST/SLC" and f.params["start"] == "2023-11-01"


def test_search_incomplete_results(
    config: Config, burst_products: list[FakeProduct], patch_search
) -> None:
    patch_search(lambda **kw: FakeResults(burst_products, complete=False))
    res = sr.search_bursts(config, SEOUL_WKT)
    assert any(f.rule_id == "SEL-SEARCH-04" and f.severity == "WARN" for f in res.findings)
    assert len(res.records) == 11


def test_search_error_becomes_fail_finding(config: Config, patch_search) -> None:
    import asf_search

    def boom(**kw):
        raise asf_search.ASFSearchError("CMR 503 at https://cmr.earthdata.nasa.gov/search")

    patch_search(boom)
    res = sr.search_bursts(config, SEOUL_WKT)
    assert res.records == [] and not res.ok
    f = res.findings[0]
    assert f.rule_id == "SEL-SEARCH-05" and f.severity == "FAIL"
    assert f.params["error_type"] == "ASFSearchError" and "503" in f.params["error"]


def test_search_filters_records_outside_aoi(
    config: Config, burst_products: list[FakeProduct], patch_search
) -> None:
    far = FakeProduct(
        {
            "properties": burst_products[0].properties,
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]],
            },
            "baseline": None,
        }
    )
    far.properties = dict(far.properties, sceneName="FAR-BURST", fileID="FAR-BURST")
    patch_search(lambda **kw: FakeResults([*burst_products, far]))
    res = sr.search_bursts(config, SEOUL_WKT)
    assert all(r.granule_id != "FAR-BURST" for r in res.records)
    assert res.findings[-1].params["n_dropped"] == 1


def test_search_passes_session(
    config: Config, burst_products: list[FakeProduct], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asf_search

    seen: dict[str, object] = {}

    def _search(**kwargs):
        seen["opts"] = kwargs.get("opts")
        return FakeResults(burst_products)

    monkeypatch.setattr(asf_search, "search", _search)
    session = asf_search.ASFSession()
    sr.search_bursts(config, SEOUL_WKT, session=session)
    assert seen["opts"] is not None and seen["opts"].session is session


# ---------------------------------------------------------------- search_from_config


def test_search_from_config_warns_without_credentials(
    config: Config,
    burst_products: list[FakeProduct],
    patch_search,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))  # no ~/.netrc
    patch_search(lambda **kw: FakeResults(burst_products))
    res = sr.search_from_config(config)
    assert [f.rule_id for f in res.findings] == ["KB-AUTH-001", "SEL-SEARCH-01"]
    assert (
        res.findings[0].severity == "WARN"
        and res.findings[0].params["env_var"] == "EARTHDATA_TOKEN"
    )
    assert res.ok


def test_search_from_config_with_token_has_no_auth_finding(
    config: Config, burst_products: list[FakeProduct], patch_search, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "EARTHDATA_TOKEN",
        "eyJ0eXAiOiJKV1QiLCJvcmlnaW4iOiJFYXJ0aGRhdGEgTG9naW4i.eyJ0eXBlIjoiVXNlciJ9.signaturesignature",
    )
    patch_search(lambda **kw: FakeResults(burst_products))
    res = sr.search_from_config(config)
    assert all(f.rule_id != "KB-AUTH-001" for f in res.findings)


def test_search_from_config_bad_aoi(config: Config, tmp_path: Path, patch_search) -> None:
    calls = patch_search(lambda **kw: FakeResults([]))
    config.aoi.write_text("garbage", encoding="utf-8")
    res = sr.search_from_config(config)
    assert calls == [] and not res.ok
    assert res.findings[-1].rule_id == "SEL-SEARCH-07" and res.findings[-1].severity == "FAIL"


# ---------------------------------------------------------------- persistence


def test_save_and_load_records_roundtrip(
    config: Config,
    burst_products: list[FakeProduct],
    patch_search,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "EARTHDATA_TOKEN",
        "eyJ0eXAiOiJKV1QiLCJvcmlnaW4iOiJFYXJ0aGRhdGEgTG9naW4i.eyJ0eXBlIjoiVXNlciJ9.signaturesignature",
    )
    patch_search(lambda **kw: FakeResults(burst_products))
    res = sr.search_from_config(config)
    out = sr.save_records(res, tmp_path / "work" / "select" / "candidates.json")
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == {"schema_version", "product_type", "query", "records", "findings"}
    assert data["product_type"] == "BURST" and len(data["records"]) == 11
    assert "eyJ0eXAi" not in out.read_text(
        encoding="utf-8"
    )  # secrets never land in candidates.json
    back = sr.load_records(out)
    assert back.product_type == "BURST" and back.query == res.query
    assert back.records == res.records
    assert [f.rule_id for f in back.findings] == [f.rule_id for f in res.findings]
    assert back.summary()["n_dates"] == 10


def test_load_records_bad_file(tmp_path: Path) -> None:
    p = tmp_path / "candidates.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        sr.load_records(p)
    with pytest.raises(ValueError):
        sr.load_records(tmp_path / "nope.json")
