"""ADR-0010 / open question #1: asf_search burst field provenance (R-01, SEL-08, SEL-09)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tests.unit.select_search.conftest import FIXTURE_DIR, FakeProduct
from wintersar.io.schemas import BurstRecord, Platform
from wintersar.select import metadata as md


def _burst(products: list[FakeProduct], name: str) -> FakeProduct:
    return next(p for p in products if p.properties["sceneName"] == name)


def test_burst_record_search_time_fields(burst_products: list[FakeProduct]) -> None:
    p = _burst(burst_products, "S1_270859_IW2_20240107T093233_VV_0C4A-BURST")
    rec = md.burst_record_from_asf(p)
    assert rec.granule_id == "S1_270859_IW2_20240107T093233_VV_0C4A-BURST"
    assert rec.platform is Platform.S1A
    assert rec.mode == "IW"
    assert rec.subswath == "IW2"
    assert rec.full_burst_id == "127_270859_IW2"
    assert rec.relative_orbit == 127
    assert rec.absolute_orbit == 51999
    assert rec.flight_direction == "ASCENDING"
    assert rec.polarization == "VV"
    assert rec.product_type == "BURST"
    # pgeVersion '003.71' (CMR PGEVersionClass) is the IPF version and is pre-download
    assert rec.ipf_version == "3.71"
    assert rec.extra["pge_version"] == "003.71"
    # microsecond start time comes from the raw UMM, not the second-rounded property
    assert rec.acquisition_time == datetime(2024, 1, 7, 9, 32, 34, 976057)
    assert rec.acquisition_time.tzinfo is None
    assert rec.url.startswith("https://sentinel1-burst.asf.alaska.edu/") and rec.url.endswith(
        "/IW2/VV/2.tiff"
    )
    assert rec.extra["metadata_url"].endswith("/IW2/VV/2.xml")
    assert rec.footprint_wkt.startswith("POLYGON ((126.093612 37.464618")
    # not available from the search API (filled after download)
    assert rec.range_pixel_spacing_m is None
    assert rec.azimuth_pixel_spacing_m is None
    assert rec.incidence_near_deg is None and rec.incidence_far_deg is None
    # raw UMM attributes asf_search does not map
    assert rec.extra["lines_per_burst"] == 1506
    assert rec.extra["azimuth_time_interval_s"] == pytest.approx(0.0020555563)
    assert rec.extra["burst"]["relative_burst_id"] == 270859
    assert rec.extra["burst"]["burst_index"] == 2
    sv = rec.extra["state_vectors"]
    assert sv["pre"]["time"] == "2024-01-07T09:32:32Z"
    assert sv["post"]["position"] == pytest.approx([-2908969.328569, 4845819.23838, 4248609.053406])
    assert (
        rec.extra["ascending_node_time"]
        if "ascending_node_time" in rec.extra
        else sv["ascending_node_time"]
    )


def test_burst_record_without_umm_falls_back_to_properties(
    burst_products: list[FakeProduct],
) -> None:
    p = _burst(burst_products, "S1_270858_IW2_20240107T093231_VV_0C4A-BURST")
    assert p.umm is None  # captured from the plain search, no raw UMM kept
    rec = md.burst_record_from_asf(p)
    assert rec.full_burst_id == "127_270858_IW2"
    assert rec.acquisition_time == datetime(2024, 1, 7, 9, 32, 32)
    assert rec.extra["lines_per_burst"] is None
    assert rec.ipf_version == "3.71"


def test_slc_record(slc_products: list[FakeProduct]) -> None:
    rec = md.burst_record_from_asf(slc_products[0], preferred_polarization="VV")
    assert rec.product_type == "SLC"
    assert rec.granule_id == "S1A_IW_SLC__1SDV_20240107T093228_20240107T093254_051999_0648A4_0C4A"
    assert rec.full_burst_id == rec.granule_id
    assert rec.subswath == "IW1+IW2+IW3"
    assert rec.platform is Platform.S1A  # 'Sentinel-1A' mixed case for SLC
    assert rec.polarization == "VV" and rec.extra["polarizations"] == ["VV", "VH"]
    assert rec.extra["frame_number"] == 120
    assert rec.url.endswith(".zip")
    rec_vh = md.burst_record_from_asf(slc_products[0], preferred_polarization="VH")
    assert rec_vh.polarization == "VH"
    rec_hh = md.burst_record_from_asf(slc_products[0], preferred_polarization="HH")
    assert rec_hh.polarization == "VV"  # not available -> first channel


def test_records_from_asf_dedupes_and_sorts(burst_products: list[FakeProduct]) -> None:
    recs = md.records_from_asf(burst_products + burst_products[:2])
    assert len(recs) == len(burst_products)
    times = [r.acquisition_time for r in recs]
    assert times == sorted(times)
    same_day = [r.full_burst_id for r in recs if r.acquisition_date.isoformat() == "2024-01-07"]
    assert same_day == ["127_270858_IW2", "127_270859_IW2"]


def test_field_provenance_table_covers_schema() -> None:
    fields = set(BurstRecord.model_fields) - {"extra"}
    assert set(md.FIELD_PROVENANCE) == fields
    assert {k for k, v in md.FIELD_PROVENANCE.items() if v == "xml"} == {
        "range_pixel_spacing_m",
        "azimuth_pixel_spacing_m",
        "incidence_near_deg",
        "incidence_far_deg",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("SENTINEL-1A", Platform.S1A),
        ("Sentinel-1B", Platform.S1B),
        ("S1C", Platform.S1C),
        ("sentinel-1d", Platform.S1D),
    ],
)
def test_normalize_platform(value: str, expected: Platform) -> None:
    assert md.normalize_platform(value) is expected


def test_normalize_platform_unknown() -> None:
    with pytest.raises(ValueError, match="ALOS"):
        md.normalize_platform("ALOS")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("003.71", "3.71"), ("3.20", "3.20"), ("002.36", "2.36"), (None, None), ("", None)],
)
def test_normalize_ipf_version(value: str | None, expected: str | None) -> None:
    assert md.normalize_ipf_version(value) == expected


@pytest.mark.parametrize(("value", "expected"), [("IW", "IW"), ("EW", "EW"), ("S3", "SM")])
def test_normalize_mode(value: str, expected: str) -> None:
    assert md.normalize_mode(value) == expected


def test_split_polarizations() -> None:
    assert md.split_polarizations("VV+VH") == ["VV", "VH"]
    assert md.split_polarizations("HH") == ["HH"]
    assert md.split_polarizations("DUAL VV") == ["VV"]


def test_parse_time_variants() -> None:
    assert md.parse_time("2024-01-07T09:32:34Z") == datetime(2024, 1, 7, 9, 32, 34)
    assert md.parse_time("2024-01-07T09:32:34.123456+00:00") == datetime(
        2024, 1, 7, 9, 32, 34, 123456
    )
    assert md.parse_time("2024-01-07T18:32:34+09:00") == datetime(2024, 1, 7, 9, 32, 34)


# ---------------------------------------------------------------- post-download XML


def test_parse_safe_annotation() -> None:
    meta = md.parse_safe_annotation(FIXTURE_DIR / "annotation_iw1_sample.xml")
    assert meta["swath"] == "IW1" and meta["polarisation"] == "VV" and meta["mode"] == "IW"
    assert meta["range_pixel_spacing_m"] == pytest.approx(2.329562)
    assert meta["azimuth_pixel_spacing_m"] == pytest.approx(13.97051)
    assert meta["incidence_mid_deg"] == pytest.approx(33.9265, abs=1e-3)
    assert meta["incidence_near_deg"] == pytest.approx((30.6925 + 30.718) / 2, abs=1e-3)
    assert meta["incidence_far_deg"] == pytest.approx((36.767 + 36.773) / 2, abs=1e-3)
    assert meta["lines_per_burst"] == 1497 and meta["samples_per_burst"] == 21577
    assert meta["n_bursts"] == 10
    assert meta["range_sampling_rate_hz"] == pytest.approx(64345238.12571428)
    assert meta["absolute_orbit"] == 53336 and meta["pass"] == "Ascending"
    assert meta["ipf_version"] is None  # only in manifest.safe / burst XML manifest


def test_parse_manifest_ipf_version() -> None:
    assert md.parse_manifest_ipf_version(FIXTURE_DIR / "manifest_sample.safe") == "003.20"


def test_parse_burst_xml_metadata_selects_swath() -> None:
    path = FIXTURE_DIR / "burst_metadata_sample.xml"
    iw2 = md.parse_burst_xml_metadata(path, subswath="IW2", polarization="VV")
    assert iw2["swath"] == "IW2"
    assert iw2["azimuth_pixel_spacing_m"] == pytest.approx(13.95)
    assert iw2["incidence_near_deg"] == pytest.approx(36.5) and iw2[
        "incidence_far_deg"
    ] == pytest.approx(41.9)
    assert iw2["lines_per_burst"] == 1506 and iw2["n_bursts"] == 9
    assert iw2["ipf_version"] == "003.71"
    assert iw2["available_swaths"] == ["IW1", "IW2"]
    assert iw2["source_filename"].startswith("s1a-iw2-slc-vv")
    first = md.parse_burst_xml_metadata(path)  # no selector -> first product entry
    assert first["swath"] == "IW1" and first["range_pixel_spacing_m"] == pytest.approx(2.329562)
    with pytest.raises(ValueError, match="IW3"):
        md.parse_burst_xml_metadata(path, subswath="IW3", polarization="VV")


def test_parse_xml_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.xml"
    bad.write_text("<product><adsHeader>", encoding="utf-8")
    with pytest.raises(ValueError):
        md.parse_safe_annotation(bad)
    with pytest.raises(ValueError):
        md.parse_safe_annotation(tmp_path / "missing.xml")


def test_enrich_record(burst_products: list[FakeProduct]) -> None:
    rec = md.burst_record_from_asf(
        _burst(burst_products, "S1_270859_IW2_20240107T093233_VV_0C4A-BURST")
    )
    meta = md.parse_burst_xml_metadata(FIXTURE_DIR / "burst_metadata_sample.xml", "IW2", "VV")
    out = md.enrich_record(rec, meta)
    assert out.range_pixel_spacing_m == pytest.approx(2.329562)
    assert out.azimuth_pixel_spacing_m == pytest.approx(13.95)
    assert out.incidence_near_deg == pytest.approx(36.5)
    assert out.incidence_far_deg == pytest.approx(41.9)
    assert out.ipf_version == "3.71"
    assert out.extra["annotation"]["lines_per_burst"] == 1506
    assert (
        out.granule_id == rec.granule_id and rec.range_pixel_spacing_m is None
    )  # frozen original untouched
