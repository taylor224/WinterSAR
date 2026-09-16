"""SEL-01..SEL-13: one positive and one negative case per rule (plan section 5.1.3, ADR-0014)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from types import SimpleNamespace

import pytest

from tests.conftest import make_burst
from tests.unit.select.conftest import (
    BURST_E,
    BURST_W,
    DATES_12D,
    FOOT_E,
    FOOT_W,
    full_stack,
    make_candidate,
)
from wintersar.i18n import load_catalog
from wintersar.io.schemas import BurstRecord, Finding, Pair, Resources, StackCandidate
from wintersar.pipeline.config import Config
from wintersar.select import rules
from wintersar.select.rules import RULES, RuleContext, all_message_keys, rule_table, run_rules

D = [date.fromisoformat(d) for d in DATES_12D]


def ctx(
    candidate: StackCandidate, records: list[BurstRecord], cfg: Config, **kw: object
) -> RuleContext:
    return RuleContext(
        candidate=candidate, records=rules.records_for_candidate(candidate, records), cfg=cfg, **kw
    )  # type: ignore[arg-type]


def by_rule(findings: list[Finding], rule_id: str, severity: str | None = None) -> list[Finding]:
    return [
        f for f in findings if f.rule_id == rule_id and (severity is None or f.severity == severity)
    ]


def test_registry_and_rule_table() -> None:
    assert list(RULES) == [f"SEL-{i:02d}" for i in range(1, 14)]
    rows = rule_table()
    assert [r["id"] for r in rows] == list(RULES)
    assert rows[0]["severities"] == ["FAIL", "INFO"] and rows[3]["severities"] == ["FAIL", "WARN"]
    ko, en = load_catalog("ko"), load_catalog("en")
    for key in all_message_keys():
        assert key in ko, key
        assert key in en, key


def test_clean_stack_has_no_fail_or_warn(
    candidate: StackCandidate, records: list[BurstRecord], cfg: Config
) -> None:
    fs = run_rules([candidate], records, cfg)
    assert not [f for f in fs if f.severity in ("FAIL", "WARN")]
    # informational only: baselines not computed yet (SEL-06) and the auto looks (SEL-09)
    assert [f.rule_id for f in fs] == ["SEL-06", "SEL-09"]
    for f in fs:
        assert f.message_key.startswith("select.") and f.fix_key == f"select.{f.rule_id}.fix"


# SEL-01 -------------------------------------------------------------------------------
def test_sel01_fail_on_mixed_tracks(cfg: Config) -> None:
    other = "134_200001_IW2"
    recs = [
        make_burst("2024-01-01"),
        make_burst("2024-01-13", full_burst_id=other, relative_orbit=134),
    ]
    c = make_candidate(dates=DATES_12D[:2], burst_ids=[BURST_W, other])
    fs = rules.sel_01(ctx(c, recs, cfg))
    assert len(fs) == 1 and fs[0].severity == "FAIL" and fs[0].message_key == "select.SEL-01.fail"
    assert fs[0].evidence["tracks"] == [52, 134] and fs[0].scope == "T052D_VV"
    assert fs[0].params["tracks"] == "52, 134"


def test_sel01_pass_and_cross_candidate_info(
    candidate: StackCandidate, records: list[BurstRecord], cfg: Config
) -> None:
    assert rules.sel_01(ctx(candidate, records, cfg)) == []
    other = make_candidate(relative_orbit=134)
    info = rules.sel_01_cross([candidate, other])
    assert len(info) == 1 and info[0].severity == "INFO" and info[0].scope is None
    assert info[0].params == {"direction": "DESCENDING", "n": 2, "tracks": "52, 134"}
    assert (
        rules.sel_01_cross(
            [candidate, make_candidate(relative_orbit=134, flight_direction="ASCENDING")]
        )
        == []
    )


# SEL-02 -------------------------------------------------------------------------------
def test_sel02_fail_on_mixed_direction(cfg: Config) -> None:
    recs = [make_burst("2024-01-01"), make_burst("2024-01-13", flight_direction="ASCENDING")]
    c = make_candidate(dates=DATES_12D[:2], burst_ids=[BURST_W])
    fs = rules.sel_02(ctx(c, recs, cfg))
    assert len(fs) == 1 and fs[0].severity == "FAIL"
    assert fs[0].evidence["directions"] == ["ASCENDING", "DESCENDING"]


def test_sel02_pass(candidate: StackCandidate, records: list[BurstRecord], cfg: Config) -> None:
    assert rules.sel_02(ctx(candidate, records, cfg)) == []


# SEL-03 -------------------------------------------------------------------------------
def test_sel03_fail_on_mode_or_subswath_mismatch(cfg: Config) -> None:
    recs = [make_burst("2024-01-01"), make_burst("2024-01-13").model_copy(update={"mode": "EW"})]
    c = make_candidate(dates=DATES_12D[:2], burst_ids=[BURST_W])
    fs = rules.sel_03(ctx(c, recs, cfg))
    assert len(fs) == 1 and fs[0].severity == "FAIL" and fs[0].evidence["modes"] == ["EW", "IW"]
    recs2 = [make_burst("2024-01-01", subswath="IW1"), make_burst("2024-01-13", subswath="IW2")]
    fs2 = rules.sel_03(ctx(c, recs2, cfg))
    assert len(fs2) == 1 and "2024-01-01: IW1" in fs2[0].params["subswaths"]


def test_sel03_pass(candidate: StackCandidate, records: list[BurstRecord], cfg: Config) -> None:
    assert rules.sel_03(ctx(candidate, records, cfg)) == []


# SEL-04 -------------------------------------------------------------------------------
def test_sel04_fail_without_common_bursts_and_warn_below_coverage(
    cfg: Config, records: list[BurstRecord]
) -> None:
    empty = make_candidate(burst_ids=[], coverage=0.0)
    fs = rules.sel_04(ctx(empty, records, cfg))
    assert len(fs) == 1 and fs[0].severity == "FAIL" and fs[0].message_key == "select.SEL-04.fail"
    alts = [
        {
            "name": "drop_bursts",
            "coverage_of_aoi": 0.5,
            "n_dates_dropped": 0,
            "n_bursts_dropped": 1,
        },
        {"name": "drop_dates", "coverage_of_aoi": 1.0, "n_dates_dropped": 1, "n_bursts_dropped": 0},
    ]
    partial = make_candidate(
        burst_ids=[BURST_W], coverage=0.5, n_bursts_dropped=1, notes={"alternatives": alts}
    )
    fs = rules.sel_04(ctx(partial, records, cfg))
    assert len(fs) == 1 and fs[0].severity == "WARN"
    assert fs[0].params["cov_drop_dates"] == 1.0 and fs[0].params["cov_drop_bursts"] == 0.5
    assert fs[0].params["n_dates_dropped_alt"] == 1 and fs[0].params["n_bursts_dropped"] == 1


def test_sel04_pass(candidate: StackCandidate, records: list[BurstRecord], cfg: Config) -> None:
    assert rules.sel_04(ctx(candidate, records, cfg)) == []
    assert rules.sel_04(ctx(make_candidate(coverage=0.95), records, cfg)) == []


# SEL-05 -------------------------------------------------------------------------------
def test_sel05_fail_on_cross_pol(cfg: Config) -> None:
    recs = full_stack(DATES_12D[:2], polarization="VH")
    c = make_candidate(dates=DATES_12D[:2], polarization="VH")
    fs = rules.sel_05(ctx(c, recs, cfg))
    assert len(fs) == 1 and fs[0].severity == "FAIL" and fs[0].params["polarization"] == "VH"


def test_sel05_pass_co_pol_and_dual(
    cfg: Config, candidate: StackCandidate, records: list[BurstRecord]
) -> None:
    assert rules.sel_05(ctx(candidate, records, cfg)) == []
    dual = full_stack(DATES_12D[:2], polarization="VV+VH")
    assert (
        rules.sel_05(ctx(make_candidate(dates=DATES_12D[:2], polarization="VV+VH"), dual, cfg))
        == []
    )
    hh = full_stack(DATES_12D[:2], polarization="HH")
    assert rules.sel_05(ctx(make_candidate(dates=DATES_12D[:2], polarization="HH"), hh, cfg)) == []


# SEL-06 -------------------------------------------------------------------------------
def pair(i: int, j: int, perp: float | None) -> Pair:
    return Pair(
        reference=D[i],
        secondary=D[j],
        temporal_baseline_days=(D[j] - D[i]).days,
        perp_baseline_m=perp,
    )


def test_sel06_warn_per_pair_and_info_when_unknown(cfg: Config, records: list[BurstRecord]) -> None:
    c = make_candidate(pairs=[pair(0, 1, 200.0), pair(1, 2, -160.0), pair(2, 3, 100.0)])
    fs = rules.sel_06(ctx(c, records, cfg))
    assert [(f.severity, f.scope) for f in fs] == [
        ("WARN", "T052D_VV:20240101_20240113"),
        ("WARN", "T052D_VV:20240113_20240125"),
    ]
    assert fs[1].params["perp_m"] == 160.0 and fs[1].params["max_perp_m"] == 150.0
    unknown = make_candidate(pairs=[pair(0, 1, None)])
    fs2 = rules.sel_06(ctx(unknown, records, cfg))
    assert (
        len(fs2) == 1 and fs2[0].severity == "INFO" and fs2[0].message_key == "select.SEL-06.info"
    )


def test_sel06_pass(cfg: Config, records: list[BurstRecord]) -> None:
    c = make_candidate(pairs=[pair(0, 1, 149.0), pair(1, 2, -20.0)])
    assert rules.sel_06(ctx(c, records, cfg)) == []


# SEL-07 -------------------------------------------------------------------------------
def test_sel07_warn_on_long_temporal_and_info_on_season_crossing(
    cfg: Config, records: list[BurstRecord]
) -> None:
    jan, jul = date(2024, 1, 10), date(2024, 7, 8)
    c = make_candidate(
        dates=["2024-01-10", "2024-07-08"],
        pairs=[Pair(reference=jan, secondary=jul, temporal_baseline_days=(jul - jan).days)],
    )
    fs = rules.sel_07(ctx(c, records, cfg))
    assert [f.severity for f in fs] == ["WARN", "INFO"]
    assert fs[0].params["days"] == 180 and fs[0].params["max_days"] == 48
    assert fs[1].params["n_pairs"] == 1 and fs[1].params["example"] == "20240110_20240708"
    # southern hemisphere: Jan (summer, leaf-on) vs Jul (winter, snow) still crosses
    assert len(rules.sel_07(ctx(c, records, cfg, aoi_lat=-33.0))) == 2
    # Jan <-> Apr: transition month -> no seasonal info (only the 48-day WARN)
    apr = date(2024, 4, 9)
    c2 = make_candidate(
        dates=["2024-01-10", "2024-04-09"],
        pairs=[Pair(reference=jan, secondary=apr, temporal_baseline_days=(apr - jan).days)],
    )
    assert [f.severity for f in rules.sel_07(ctx(c2, records, cfg))] == ["WARN"]


def test_sel07_pass(cfg: Config, records: list[BurstRecord]) -> None:
    c = make_candidate(pairs=[pair(0, 1, None), pair(0, 4, None)])  # 12 and 48 days, all winter
    assert rules.sel_07(ctx(c, records, cfg)) == []
    assert rules.season_of(date(2024, 12, 1)) == "snow"
    assert rules.season_of(date(2024, 6, 1)) == "leaf_on"
    assert rules.season_of(date(2024, 6, 1), northern=False) == "snow"
    assert rules.season_of(date(2024, 10, 1)) == "transition"


# SEL-08 -------------------------------------------------------------------------------
def test_sel08_info_on_major_ipf_difference(cfg: Config) -> None:
    recs = [
        make_burst("2024-01-01", ipf_version="2.91"),
        make_burst("2024-01-13", ipf_version="3.71"),
    ]
    c = make_candidate(dates=DATES_12D[:2], burst_ids=[BURST_W])
    fs = rules.sel_08(ctx(c, recs, cfg))
    assert len(fs) == 1 and fs[0].severity == "INFO" and fs[0].params["versions"] == "2.91, 3.71"


def test_sel08_pass_same_major(cfg: Config) -> None:
    recs = [
        make_burst("2024-01-01", ipf_version="3.61"),
        make_burst("2024-01-13", ipf_version="3.71"),
    ]
    c = make_candidate(dates=DATES_12D[:2], burst_ids=[BURST_W])
    assert rules.sel_08(ctx(c, recs, cfg)) == []


# SEL-09 -------------------------------------------------------------------------------
def test_sel09_warn_on_spacing_deviation_plus_info(cfg: Config) -> None:
    recs = [
        make_burst("2024-01-01"),
        make_burst("2024-01-13", azimuth_pixel_spacing_m=14.5),
    ]  # 2.8 %
    c = make_candidate(dates=DATES_12D[:2], burst_ids=[BURST_W])
    fs = rules.sel_09(ctx(c, recs, cfg))
    assert [f.severity for f in fs] == ["WARN", "INFO"]
    assert fs[0].params["deviation"] == pytest.approx(0.4 / 14.3)
    assert fs[0].params["tolerance"] == 0.01


def test_sel09_info_always_reports_auto_looks(
    cfg: Config, candidate: StackCandidate, records: list[BurstRecord]
) -> None:
    fs = rules.sel_09(ctx(candidate, records, cfg))
    assert len(fs) == 1 and fs[0].severity == "INFO"
    p = fs[0].params
    assert (p["rg_looks"], p["az_looks"]) == (10, 3) and p["target"] == 40.0
    assert p["rg"] == 2.33 and p["az"] == 14.1 and p["aspect"] <= 1.2
    assert fs[0].evidence["auto_looks"]["rg_looks"] == 10
    cfg2 = cfg.model_copy(deep=True)
    cfg2.engine.looks = (10, 2)
    fs2 = rules.sel_09(ctx(candidate, records, cfg2))
    assert fs2[0].evidence["configured_looks"]["az_looks"] == 2


# SEL-10 -------------------------------------------------------------------------------
def test_sel10_warn_on_missing_burst_or_lines(cfg: Config, candidate: StackCandidate) -> None:
    recs = [
        *full_stack(DATES_12D[:4]),
        make_burst(DATES_12D[4], full_burst_id=BURST_W, footprint_wkt=FOOT_W),
    ]
    fs = rules.sel_10(ctx(candidate, recs, cfg))
    assert len(fs) == 1 and fs[0].severity == "WARN"
    assert fs[0].params["dates"] == "2024-02-18" and fs[0].params["expected"] == 2
    assert fs[0].evidence["burst_count_by_date"] == {"2024-02-18": 1}
    recs2 = [
        *full_stack(DATES_12D[:4]),
        make_burst(DATES_12D[4], full_burst_id=BURST_W, footprint_wkt=FOOT_W, missing_lines=12),
        make_burst(DATES_12D[4], full_burst_id=BURST_E, footprint_wkt=FOOT_E),
    ]
    fs2 = rules.sel_10(ctx(candidate, recs2, cfg))
    assert len(fs2) == 1 and fs2[0].evidence["missing_lines_dates"] == ["2024-02-18"]


def test_sel10_pass(cfg: Config, candidate: StackCandidate, records: list[BurstRecord]) -> None:
    assert rules.sel_10(ctx(candidate, records, cfg)) == []


# SEL-11 -------------------------------------------------------------------------------
def test_sel11_info_when_poeorb_missing(
    cfg: Config, candidate: StackCandidate, records: list[BurstRecord]
) -> None:
    hook: Callable[[date], bool | None] = lambda d: d != D[4]  # noqa: E731
    fs = rules.sel_11(ctx(candidate, records, cfg, poeorb_available=hook))
    assert len(fs) == 1 and fs[0].severity == "INFO" and fs[0].params["dates"] == "2024-02-18"


def test_sel11_pass_or_skip(
    cfg: Config, candidate: StackCandidate, records: list[BurstRecord]
) -> None:
    assert rules.sel_11(ctx(candidate, records, cfg)) == []  # no hook -> skipped
    assert rules.sel_11(ctx(candidate, records, cfg, poeorb_available=lambda d: True)) == []
    assert rules.sel_11(ctx(candidate, records, cfg, poeorb_available=lambda d: None)) == []


# SEL-12 -------------------------------------------------------------------------------
def test_sel12_warn_above_threshold(
    cfg: Config, candidate: StackCandidate, records: list[BurstRecord]
) -> None:
    geom = {
        "DESCENDING": {
            "layover_fraction": 0.08,
            "shadow_fraction": 0.05,
            "foreshortening_mean": 0.3,
        }
    }
    fs = rules.sel_12(ctx(candidate, records, cfg, geometry=geom))
    assert len(fs) == 1 and fs[0].severity == "WARN"
    assert fs[0].params["total"] == pytest.approx(0.13) and fs[0].params["max_fraction"] == 0.10
    # GeometryMaskResult-like object with .stats / .flight_direction
    obj = SimpleNamespace(
        stats={"layover_fraction": 0.2, "shadow_fraction": 0.0}, flight_direction="DESCENDING"
    )
    assert (
        rules.sel_12(ctx(candidate, records, cfg, geometry={"T052D_VV": obj}))[0].severity == "WARN"
    )


def test_sel12_info_below_threshold_or_skip(
    cfg: Config, candidate: StackCandidate, records: list[BurstRecord]
) -> None:
    geom = {"layover_fraction": 0.02, "shadow_fraction": 0.01}
    fs = rules.sel_12(ctx(candidate, records, cfg, geometry=geom))
    assert len(fs) == 1 and fs[0].severity == "INFO" and fs[0].params["foreshortening"] == 0.0
    assert rules.sel_12(ctx(candidate, records, cfg, geometry=None)) == []
    assert rules.sel_12(ctx(candidate, records, cfg, geometry={"ASCENDING": geom})) == []


# SEL-13 -------------------------------------------------------------------------------
def test_sel13_warn_over_budget(
    cfg: Config, candidate: StackCandidate, records: list[BurstRecord]
) -> None:
    cfg2 = cfg.model_copy(deep=True)
    cfg2.selection.budget_credits = 100.0
    fs = rules.sel_13(ctx(candidate, records, cfg2, resources=Resources(credits=120.0, n_jobs=12)))
    assert (
        len(fs) == 1
        and fs[0].severity == "WARN"
        and fs[0].params == {"credits": 120.0, "budget": 100.0, "n_jobs": 12}
    )


def test_sel13_info_within_budget_or_unknown(
    cfg: Config, candidate: StackCandidate, records: list[BurstRecord]
) -> None:
    cfg2 = cfg.model_copy(deep=True)
    cfg2.selection.budget_credits = 100.0
    fs = rules.sel_13(ctx(candidate, records, cfg2, resources=Resources(credits=50.0, n_jobs=5)))
    assert len(fs) == 1 and fs[0].severity == "INFO" and fs[0].message_key == "select.SEL-13.info"
    fs2 = rules.sel_13(ctx(candidate, records, cfg2, resources=Resources(n_jobs=5)))
    assert fs2[0].message_key == "select.SEL-13.info_unknown"
    assert rules.sel_13(ctx(candidate, records, cfg2)) == []
    # run_rules: single Resources -> one global finding; mapping -> per stack
    fs3 = run_rules([candidate], records, cfg2, resources=Resources(credits=500.0, n_jobs=1))
    assert [f.scope for f in by_rule(fs3, "SEL-13")] == [None]
    fs4 = run_rules([candidate], records, cfg2, resources={"T052D_VV": Resources(credits=1.0)})
    assert [f.scope for f in by_rule(fs4, "SEL-13")] == ["T052D_VV"]


def test_records_for_candidate_matching(
    candidate: StackCandidate, records: list[BurstRecord]
) -> None:
    assert len(rules.records_for_candidate(candidate, records)) == 10
    tagged = candidate.model_copy(update={"notes": {"granule_ids": [records[0].granule_id]}})
    assert rules.records_for_candidate(tagged, records) == [records[0]]
    assert (
        rules.burst_track("017_034465_IW2") == 17 and rules.burst_track("S1A_IW_SLC__1SDV") is None
    )


def test_every_message_key_renders_in_both_languages(
    cfg: Config, records: list[BurstRecord]
) -> None:
    """Trigger every rule/severity once and render it: no '{placeholder}' may survive."""
    from wintersar.i18n import t as tr
    from wintersar.util.output import render_finding

    cfg2 = cfg.model_copy(deep=True)
    cfg2.selection.budget_credits = 100.0
    other = "134_200001_IW2"
    mixed = [
        make_burst("2024-01-01"),
        make_burst(
            "2024-01-13",
            full_burst_id=other,
            relative_orbit=134,
            flight_direction="ASCENDING",
            ipf_version="2.91",
            azimuth_pixel_spacing_m=14.6,
        ),
    ]
    mixed[1] = mixed[1].model_copy(update={"mode": "EW"})
    c_mixed = make_candidate(
        dates=DATES_12D[:2], burst_ids=[BURST_W, other], coverage=0.5, notes={"alternatives": []}
    )
    jan, jul = date(2024, 1, 10), date(2024, 7, 8)
    c_season = make_candidate(
        dates=["2024-01-10", "2024-07-08"],
        pairs=[
            Pair(reference=jan, secondary=jul, temporal_baseline_days=180, perp_baseline_m=300.0)
        ],
        relative_orbit=61,
    )
    vh = make_candidate(polarization="VH", relative_orbit=77, pairs=[pair(0, 1, None)])
    empty = make_candidate(burst_ids=[], coverage=0.0, relative_orbit=78)
    geom = {
        "T052D_VV": {"layover_fraction": 0.2, "shadow_fraction": 0.0},
        "T061D_VV": {"layover_fraction": 0.01, "shadow_fraction": 0.0},
    }
    res = {
        "T052D_VV": Resources(credits=500.0, n_jobs=3),
        "T061D_VV": Resources(credits=5.0, n_jobs=3),
        "T077D_VH": Resources(n_jobs=3),
    }
    recs = [
        *records,
        *mixed,
        *full_stack(DATES_12D[:4]),
        make_burst(DATES_12D[4], full_burst_id=BURST_W, footprint_wkt=FOOT_W),
    ]
    fs = run_rules(
        [
            c_mixed,
            c_season,
            vh,
            empty,
            make_candidate(relative_orbit=134, flight_direction="ASCENDING"),
        ],
        recs,
        cfg2,
        geometry=geom,
        resources=res,
        poeorb_available=lambda d: False,
    )
    seen = {f.message_key for f in fs}
    expected = {k for k in all_message_keys() if not k.endswith((".desc", ".fix"))}
    assert expected <= seen, sorted(expected - seen)
    for f in fs:
        for lang in ("ko", "en"):
            _, _, cause, fix = render_finding(f, lang)
            assert "{" not in cause and "}" not in cause, (f.rule_id, lang, cause)
            assert "{" not in fix and "}" not in fix, (f.rule_id, lang, fix)
            assert tr(f"select.{f.rule_id}.desc", lang) != f"select.{f.rule_id}.desc"
