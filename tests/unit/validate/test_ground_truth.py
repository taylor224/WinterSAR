"""Ground-truth CSV import (R-10): schema validation, units, LOS projection."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from wintersar.i18n import t
from wintersar.io.timeseries import TimeSeries
from wintersar.validate.ground_truth import (
    COLUMNS,
    GroundTruthError,
    group_by_site,
    load_csv,
    parse_rows,
    to_los,
    write_csv,
)
from wintersar.validate.los import enu_to_los, vertical_to_los

HEADER = ",".join(COLUMNS)


def _csv(tmp_path: Path, body: str, header: str = HEADER) -> Path:
    p = tmp_path / "gt.csv"
    p.write_text(header + "\n" + body, encoding="utf-8")
    return p


def test_fixture_csvs_load(leveling_csv, gnss_csv):
    lev = load_csv(leveling_csv)
    gn = load_csv(gnss_csv)
    assert {r.method for r in lev} == {"leveling"} and {r.method for r in gn} == {"gnss"}
    assert all(r.up_m is not None and r.east_m is None for r in lev)
    assert all(r.east_m is not None and r.north_m is not None for r in gn)
    sites = group_by_site(lev)
    assert set(sites) == {"L01-center", "L02-edge", "L03-slope", "L99-outside"}
    for rs in sites.values():
        assert rs == sorted(rs, key=lambda r: r.date)
    assert len(sites["L01-center"]) == 10 and len(sites["L02-edge"]) == 5


def test_round_trip_write_csv(tmp_path, leveling_csv):
    recs = load_csv(leveling_csv)
    p = write_csv(recs, tmp_path / "copy.csv")
    again = load_csv(p)
    assert [r.site_id for r in again] == [r.site_id for r in recs]
    assert np.allclose([r.up_m for r in again], [r.up_m for r in recs], atol=1e-6)


def test_aliases_date_formats_and_optional_columns(tmp_path):
    p = _csv(
        tmp_path,
        "S1,37.5,127.0,,20240101,0.001,,,Levelling,\n"
        "S1,37.5,127.0,12.5,2024-01-13,0.002,,,level,1.5\n"
        "G1,37.5,127.0,,2024-01-01,0.001,0.0,0.0,GPS,\n",
    )
    recs = load_csv(p)
    assert [r.method for r in recs] == ["leveling", "leveling", "gnss"]
    assert recs[0].date == date(2024, 1, 1) and recs[0].elev_m is None and recs[0].sigma_mm is None
    assert recs[1].elev_m == 12.5 and recs[1].sigma_mm == 1.5


def test_header_case_and_bom_and_blank_lines(tmp_path):
    p = tmp_path / "gt.csv"
    p.write_text(
        "﻿Site_ID, Lat ,LON,date,up_m,method\nA,37.5,127.0,2024-01-01,0.0,leveling\n\n",
        encoding="utf-8",
    )
    assert len(load_csv(p)) == 1


@pytest.mark.parametrize(
    ("body", "header", "rule"),
    [
        ("A,37.5,127.0,2024-01-01,0.0", "site_id,lat,lon,date,up_m", "VAL-001"),  # no method
        ("A,37.5,127.0,,2024/01/01,0.0,,,leveling,", HEADER, "VAL-002"),  # bad date
        ("A,99.0,127.0,,2024-01-01,0.0,,,leveling,", HEADER, "VAL-002"),  # lat range
        ("A,37.5,127.0,,2024-01-01,abc,,,leveling,", HEADER, "VAL-002"),  # not a number
        ("A,37.5,127.0,,2024-01-01,,,,leveling,", HEADER, "VAL-003"),  # leveling without up
        ("A,37.5,127.0,,2024-01-01,0.0,0.0,,gnss,", HEADER, "VAL-004"),  # gnss without north
        ("A,37.5,127.0,,2024-01-01,0.0,,,laser,", HEADER, "VAL-005"),  # unknown method
        ("A,37.5,127.0,,2024-01-01,12.0,,,leveling,", HEADER, "VAL-007"),  # mm in a m column
        ("", HEADER, "VAL-006"),  # no rows
    ],
)
def test_errors_carry_findings_with_cause_and_fix(tmp_path, body, header, rule):
    p = _csv(tmp_path, body, header)
    with pytest.raises(GroundTruthError) as exc:
        load_csv(p)
    f = exc.value.finding
    assert f.rule_id == rule and f.severity == "FAIL"
    assert f.message_key == f"validate.{rule}.cause" and f.fix_key == f"validate.{rule}.fix"
    for lang in ("ko", "en"):
        cause = t(f.message_key, lang, **f.params)
        fix = t(f.fix_key, lang, **f.params)
        assert cause != f.message_key and fix != f.fix_key and "{" not in cause


def test_missing_file_is_val_006(tmp_path):
    with pytest.raises(GroundTruthError) as exc:
        load_csv(tmp_path / "nope.csv")
    assert exc.value.finding.rule_id == "VAL-006"


def test_parse_rows_line_numbers_start_after_header():
    with pytest.raises(GroundTruthError) as exc:
        parse_rows(
            [
                {
                    "site_id": "A",
                    "lat": "1",
                    "lon": "2",
                    "date": "x",
                    "method": "gnss",
                    "up_m": "0",
                    "east_m": "0",
                    "north_m": "0",
                }
            ]
        )
    assert exc.value.finding.params["line"] == 2


def test_to_los_leveling_and_gnss(synth_ts, leveling_csv, gnss_csv):
    lev = load_csv(leveling_csv)[:3]
    samples = to_los(lev, synth_ts)
    for r, s in zip(lev, samples, strict=True):
        assert s.incidence_deg == pytest.approx(39.0) and s.heading_deg is None
        assert s.los_m == pytest.approx(float(vertical_to_los(r.up_m, 39.0)))
        assert s.row == 16 and s.col == 16  # site L01 sits on pixel (16, 16)
    gn = load_csv(gnss_csv)[:2]
    samples = to_los(gn, synth_ts)
    for r, s in zip(gn, samples, strict=True):
        assert s.heading_deg == pytest.approx(synth_ts.heading_deg)
        assert s.los_m == pytest.approx(
            float(enu_to_los(r.east_m, r.north_m, r.up_m, 39.0, synth_ts.heading_deg))
        )
    # overrides win over the time series geometry
    s2 = to_los(gn[:1], synth_ts, heading_deg=-12.0, incidence_deg=33.0)[0]
    assert s2.incidence_deg == 33.0 and s2.heading_deg == -12.0
    assert s2.to_dict()["date"] == gn[0].date.isoformat()


def test_to_los_requires_geometry(synth_ts, leveling_csv, gnss_csv):
    bare = TimeSeries(
        dates=synth_ts.dates,
        displacement_m=synth_ts.displacement_m,
        lat=synth_ts.lat,
        lon=synth_ts.lon,
    )
    with pytest.raises(GroundTruthError) as exc:
        to_los(load_csv(leveling_csv)[:1], bare)
    assert (
        exc.value.finding.rule_id == "VAL-008"
        and exc.value.finding.params["what"] == "incidence_deg"
    )
    no_heading = TimeSeries(
        dates=synth_ts.dates,
        displacement_m=synth_ts.displacement_m,
        lat=synth_ts.lat,
        lon=synth_ts.lon,
        incidence_deg=39.0,
    )
    assert len(to_los(load_csv(leveling_csv)[:1], no_heading)) == 1  # levelling needs no heading
    with pytest.raises(GroundTruthError) as exc:
        to_los(load_csv(gnss_csv)[:1], no_heading)
    assert exc.value.finding.params["what"] == "heading_deg"
