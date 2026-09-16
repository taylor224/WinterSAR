"""R-01 / plan section 5.1.2: grouping, coverage alternatives, network, reference (ADR-0016)."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import make_burst
from tests.unit.select.conftest import (
    BURST_E,
    BURST_FAR,
    BURST_W,
    DATES_12D,
    FOOT_FAR,
    FOOT_W,
    burst_pair,
    full_stack,
)
from wintersar.io.schemas import Pair
from wintersar.pipeline.config import Config
from wintersar.select.network import (
    build_network,
    coverage_fraction,
    group_candidates,
    network_components,
    orbit_direction_filter,
    perp_by_date_from_pairs,
    polarization_filter,
    recommend_reference,
    with_baselines,
)

D = [date.fromisoformat(d) for d in DATES_12D]


def test_orbit_direction_filter() -> None:
    recs = [make_burst("2024-01-01"), make_burst("2024-01-01", flight_direction="ASCENDING")]
    assert len(orbit_direction_filter(recs, "auto")) == 2
    assert [r.flight_direction for r in orbit_direction_filter(recs, "desc")] == ["DESCENDING"]
    assert [r.flight_direction for r in orbit_direction_filter(recs, "asc")] == ["ASCENDING"]
    with pytest.raises(ValueError):
        orbit_direction_filter(recs, "sideways")


def test_polarization_filter_handles_dual_pol_and_fallback() -> None:
    recs = [
        make_burst("2024-01-01", polarization="VV+VH"),
        make_burst("2024-01-01", polarization="VH"),
    ]
    assert [r.polarization for r in polarization_filter(recs, "VV")] == ["VV+VH"]
    assert len(polarization_filter(recs, "HH")) == 2  # nothing matches -> keep all for SEL-05


def test_coverage_fraction(aoi_wkt: str) -> None:
    assert coverage_fraction([FOOT_W], aoi_wkt) == pytest.approx(0.5, abs=1e-6)
    assert coverage_fraction([FOOT_W, FOOT_FAR], aoi_wkt) == pytest.approx(0.5, abs=1e-6)
    assert coverage_fraction([], aoi_wkt) == 0.0


def test_group_candidates_full_stack(cfg: Config, aoi_wkt: str) -> None:
    recs = full_stack() + [
        make_burst(d, full_burst_id=BURST_FAR, subswath="IW3", footprint_wkt=FOOT_FAR)
        for d in DATES_12D
    ]
    cands = group_candidates(recs, aoi_wkt, cfg.selection, cfg.data)
    assert len(cands) == 1
    c = cands[0]
    assert c.stack_id == "T052D_VV"
    assert c.burst_ids == [BURST_W, BURST_E]  # sorted; the far burst does not intersect the AOI
    assert c.dates == D
    assert c.coverage_of_aoi == pytest.approx(1.0, abs=1e-6)
    assert c.n_dates_dropped == 0 and c.n_bursts_dropped == 0
    assert c.subswaths == ["IW2"]
    assert c.notes["selected_alternative"] == "drop_bursts"
    assert len(c.notes["granule_ids"]) == 10
    # sbas with 48-day threshold on 12-day sampling: pairs with dt in {12,24,36,48}
    assert len(c.pairs) == 4 + 3 + 2 + 1
    assert c.reference_date == D[2]
    assert c.notes["network"]["n_components"] == 1


def test_group_candidates_missing_burst_gives_alternatives(cfg: Config, aoi_wkt: str) -> None:
    # date 3 lacks the east burst -> common = {W} (coverage 0.5) vs drop that date (coverage 1.0)
    recs = [
        *full_stack(DATES_12D[:2]),
        make_burst(DATES_12D[2], full_burst_id=BURST_W, footprint_wkt=FOOT_W),
        *full_stack(DATES_12D[3:]),
    ]
    c = group_candidates(recs, aoi_wkt, cfg.selection, cfg.data)[0]
    alts = {a["name"]: a for a in c.notes["alternatives"]}
    assert alts["drop_bursts"]["coverage_of_aoi"] == pytest.approx(0.5, abs=1e-6)
    assert alts["drop_bursts"]["n_dates"] == 5 and alts["drop_bursts"]["n_bursts_dropped"] == 1
    assert alts["drop_bursts"]["bursts_dropped"] == [BURST_E]
    assert alts["drop_dates"]["coverage_of_aoi"] == pytest.approx(1.0, abs=1e-6)
    assert alts["drop_dates"]["n_dates"] == 4 and alts["drop_dates"]["n_dates_dropped"] == 1
    assert alts["drop_dates"]["dates_dropped"] == [DATES_12D[2]]
    # 0.5 < min_coverage (0.95) -> the better-covering alternative is selected
    assert c.notes["selected_alternative"] == "drop_dates"
    assert c.n_dates_dropped == 1 and c.n_bursts_dropped == 0
    assert len(c.dates) == 4 and D[2] not in c.dates
    assert c.burst_ids == [BURST_W, BURST_E]


def test_group_candidates_prefers_all_dates_when_coverage_ok(aoi_wkt: str, cfg: Config) -> None:
    # min_coverage 0.5 -> keep every date with the common burst instead of dropping a date
    cfg2 = cfg.model_copy(deep=True)
    cfg2.selection.min_coverage = 0.5
    recs = [
        *full_stack(DATES_12D[:2]),
        make_burst(DATES_12D[2], full_burst_id=BURST_W, footprint_wkt=FOOT_W),
    ]
    c = group_candidates(recs, aoi_wkt, cfg2.selection, cfg2.data)[0]
    assert c.notes["selected_alternative"] == "drop_bursts"
    assert c.burst_ids == [BURST_W] and len(c.dates) == 3 and c.n_bursts_dropped == 1


def test_group_candidates_splits_track_direction_polarization(cfg: Config, aoi_wkt: str) -> None:
    recs = (
        burst_pair("2024-01-01")
        + burst_pair("2024-01-13")
        + burst_pair("2024-01-01", relative_orbit=134, flight_direction="ASCENDING")
        + burst_pair("2024-01-13", relative_orbit=134, flight_direction="ASCENDING")
        + burst_pair("2024-01-01", relative_orbit=61)
    )
    cands = group_candidates(recs, aoi_wkt, cfg.selection, cfg.data)
    assert [c.stack_id for c in cands] == ["T052D_VV", "T061D_VV", "T134A_VV"]
    assert cands[1].pairs == [] and cands[1].reference_date == date(2024, 1, 1)


def test_group_candidates_honours_data_filters(cfg: Config, aoi_wkt: str) -> None:
    recs = burst_pair("2024-01-01") + burst_pair(
        "2024-01-01", relative_orbit=134, flight_direction="ASCENDING"
    )
    cfg2 = cfg.model_copy(deep=True)
    cfg2.data.orbit_direction = "asc"
    assert [c.stack_id for c in group_candidates(recs, aoi_wkt, cfg2.selection, cfg2.data)] == [
        "T134A_VV"
    ]
    cfg3 = cfg.model_copy(deep=True)
    cfg3.data.relative_orbit = 52
    assert [c.stack_id for c in group_candidates(recs, aoi_wkt, cfg3.selection, cfg3.data)] == [
        "T052D_VV"
    ]
    vh = burst_pair("2024-01-01", polarization="VH")
    assert [c.polarization for c in group_candidates(vh, aoi_wkt, cfg.selection, cfg.data)] == [
        "VH"
    ]


def test_group_candidates_no_intersection(cfg: Config, aoi_wkt: str) -> None:
    recs = [make_burst("2024-01-01", full_burst_id=BURST_FAR, footprint_wkt=FOOT_FAR)]
    assert group_candidates(recs, aoi_wkt, cfg.selection, cfg.data) == []


# ------------------------------------------------------------------------- network


def test_build_network_sbas_thresholds() -> None:
    pairs = build_network(D, "sbas", 24, 150.0)
    assert [(p.reference, p.secondary) for p in pairs] == [
        (D[0], D[1]),
        (D[0], D[2]),
        (D[1], D[2]),
        (D[1], D[3]),
        (D[2], D[3]),
        (D[2], D[4]),
        (D[3], D[4]),
    ]
    assert all(p.perp_baseline_m is None for p in pairs)
    perp = {D[0]: 0.0, D[1]: 50.0, D[2]: 400.0, D[3]: 60.0, D[4]: 70.0}
    pairs = build_network(D, "sbas", 24, 150.0, perp)
    keys = {p.key for p in pairs}
    assert "20240101_20240125" not in keys and "20240113_20240125" not in keys
    assert "20240101_20240113" in keys
    p01 = next(p for p in pairs if p.key == "20240101_20240113")
    assert p01.perp_baseline_m == pytest.approx(50.0)


def test_build_network_sequential_and_single_reference() -> None:
    seq = build_network(D, "sequential", 48, 150.0, None, connections=2)
    assert [p.key for p in seq] == [
        "20240101_20240113",
        "20240101_20240125",
        "20240113_20240125",
        "20240113_20240206",
        "20240125_20240206",
        "20240125_20240218",
        "20240206_20240218",
    ]
    single = build_network(D, "single_reference", 48, 150.0)
    assert len(single) == 4
    assert all(D[2] in (p.reference, p.secondary) for p in single)
    assert all(p.reference < p.secondary for p in single)
    assert build_network(D[:1], "sbas", 48, 150.0) == []
    with pytest.raises(ValueError):
        build_network(D, "star", 48, 150.0)


def test_recommend_reference_temporal_only() -> None:
    assert recommend_reference(D, None) == D[2]
    assert recommend_reference(D[:4], None) == D[1]  # tie between 01-13 and 01-25 -> earlier
    assert recommend_reference([D[0]], None) == D[0]
    with pytest.raises(ValueError):
        recommend_reference([], None)


def test_recommend_reference_penalises_large_baseline() -> None:
    # central date has a 300 m offset from everyone else -> one of its neighbours wins.
    # D[1] and D[3] tie temporally (12 days from the centre); sum|dB| is 330 m for D[1]
    # (10,290,10,20) and 320 m for D[3] (20,10,280,10) -> D[3].
    perp = {D[0]: 0.0, D[1]: 10.0, D[2]: 300.0, D[3]: 20.0, D[4]: 30.0}
    assert recommend_reference(D, perp) == D[3]
    perp2 = {D[0]: 0.0, D[1]: 20.0, D[2]: 300.0, D[3]: 10.0, D[4]: 30.0}
    assert recommend_reference(D, perp2) == D[1]
    flat = {d: 0.0 for d in D}
    assert recommend_reference(D, flat) == D[2]


def test_perp_by_date_from_pairs_and_components() -> None:
    pairs = [
        Pair(reference=D[0], secondary=D[1], temporal_baseline_days=12, perp_baseline_m=10.0),
        Pair(reference=D[1], secondary=D[2], temporal_baseline_days=12, perp_baseline_m=-25.0),
        Pair(reference=D[3], secondary=D[4], temporal_baseline_days=12, perp_baseline_m=5.0),
    ]
    perp = perp_by_date_from_pairs(pairs)
    assert perp == {D[0]: 0.0, D[1]: 10.0, D[2]: -15.0}  # D[3], D[4] not connected to the origin
    assert network_components(D, pairs) == 2
    assert perp_by_date_from_pairs([]) == {}


def test_with_baselines_reapplies_threshold(cfg: Config) -> None:
    from tests.unit.select.conftest import make_candidate

    c = make_candidate(pairs=[])
    pairs = [
        Pair(reference=D[0], secondary=D[1], temporal_baseline_days=12, perp_baseline_m=20.0),
        Pair(reference=D[1], secondary=D[2], temporal_baseline_days=12, perp_baseline_m=500.0),
    ]
    c2 = with_baselines(c, pairs, cfg.selection)
    assert [p.key for p in c2.pairs] == ["20240101_20240113"]
    assert c2.notes["perp_by_date"] == {"2024-01-01": 0.0, "2024-01-13": 20.0, "2024-01-25": 520.0}
    assert c2.notes["network"]["n_pairs"] == 1
