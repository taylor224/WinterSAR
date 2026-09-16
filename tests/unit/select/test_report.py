"""Plan section 5.1.6: precheck report files, JSON round trip, recommendation."""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.select.conftest import make_candidate
from wintersar.io.schemas import Finding, Resources
from wintersar.select.looks import compute_looks
from wintersar.select.report import (
    findings_for_stack,
    load_precheck_json,
    precheck_payload,
    recommend_stack,
    stack_summary,
    write_precheck_report,
)


def fnd(rule: str, sev: str, scope: str | None) -> Finding:
    kind = sev.lower()
    return Finding(
        rule_id=rule,
        severity=sev,  # type: ignore[arg-type]
        message_key=f"select.{rule}.{kind}",
        params={"stack": scope, "pair": "x", "perp_m": 1.0, "max_perp_m": 1.0},
        fix_key=f"select.{rule}.fix",
        scope=scope,
    )


def test_findings_for_stack_includes_pair_scopes() -> None:
    fs = [
        fnd("SEL-06", "WARN", "T052D_VV:20240101_20240113"),
        fnd("SEL-04", "WARN", "T052D_VV"),
        fnd("SEL-01", "INFO", None),
        fnd("SEL-04", "FAIL", "T134A_VV"),
    ]
    assert [f.scope for f in findings_for_stack("T052D_VV", fs)] == [
        "T052D_VV:20240101_20240113",
        "T052D_VV",
    ]


def test_recommend_stack_rules() -> None:
    a = make_candidate(relative_orbit=52, coverage=0.9)
    b = make_candidate(relative_orbit=134, coverage=1.0)
    c = make_candidate(relative_orbit=61, coverage=1.0, dates=["2024-01-01", "2024-01-13"])
    fs = [fnd("SEL-05", "FAIL", "T134A_VV"), fnd("SEL-01", "INFO", None)]
    assert recommend_stack([a, b, c], []) is not None
    assert recommend_stack([a, b, c], []).stack_id == "T134D_VV"  # type: ignore[union-attr]
    # a FAIL scoped to the best candidate excludes it; global INFO does not exclude anyone
    assert (
        recommend_stack([a, b.model_copy(update={"flight_direction": "ASCENDING"}), c], fs).stack_id
        == "T061D_VV"
    )  # type: ignore[union-attr]
    assert recommend_stack([b], [fnd("SEL-04", "FAIL", "T134D_VV")]) is None
    assert recommend_stack([], []) is None
    # equal coverage and dates -> fewer WARN wins
    d = make_candidate(relative_orbit=1, coverage=1.0)
    e = make_candidate(relative_orbit=2, coverage=1.0)
    assert recommend_stack([d, e], [fnd("SEL-06", "WARN", "T001D_VV:x")]).stack_id == "T002D_VV"  # type: ignore[union-attr]


def test_stack_summary_baseline_stats() -> None:
    c = make_candidate()
    s = stack_summary(c)
    assert s["n_pairs"] == 4 and s["temporal_days"] == {"min": 12.0, "median": 12.0, "max": 12.0}
    assert (
        s["perp_m"] is None and s["first_date"] == "2024-01-01" and s["last_date"] == "2024-02-18"
    )


def test_write_report_files_and_json_roundtrip(tmp_path: Path, monkeypatch) -> None:
    home = str(Path.home())
    c = make_candidate(notes={"source": f"{home}/data/candidates.json"})
    fs = [
        fnd("SEL-04", "WARN", c.stack_id),
        fnd("SEL-06", "WARN", f"{c.stack_id}:20240101_20240113"),
    ]
    looks = {c.stack_id: compute_looks(2.33, 14.1, 39.0, 40.0)}
    res = {c.stack_id: Resources(n_jobs=8)}
    for lang in ("ko", "en"):
        out = tmp_path / lang
        paths = write_precheck_report(
            [c], fs, out, lang, looks=looks, resources=res, recommended=c.stack_id
        )
        assert set(paths) == {"json", "md", "html"}
        for p in paths.values():
            assert p.exists() and p.stat().st_size > 0
        payload = json.loads(paths["json"].read_text(encoding="utf-8"))
        assert payload["recommended"] == "T052D_VV" and payload["summary"]["WARN"] == 2
        assert payload["looks"]["T052D_VV"]["rg_looks"] == 10
        assert payload["resources"]["T052D_VV"]["n_jobs"] == 8
        assert home not in paths["json"].read_text(encoding="utf-8")  # masked (rule 11.11)
        loaded = load_precheck_json(paths["json"])
        assert loaded["candidates"][0] == c.model_copy(
            update={"notes": {"source": "~/data/candidates.json"}}
        )
        assert [f.rule_id for f in loaded["findings"]] == ["SEL-04", "SEL-06"]
        md = paths["md"].read_text(encoding="utf-8")
        html = paths["html"].read_text(encoding="utf-8")
        assert "T052D_VV" in md and "| 10 x 3 |" in md
        assert "<html" in html and "T052D_VV" in html and f'lang="{lang}"' in html
        title = "사전검증 리포트" if lang == "ko" else "Precheck report"
        assert title in md and title in html


def test_payload_is_json_serialisable_without_optional_parts() -> None:
    c = make_candidate()
    payload = precheck_payload([c], [], "en")
    json.dumps(payload)
    assert payload["summary"] == {"FAIL": 0, "WARN": 0, "INFO": 0, "n_candidates": 1, "ok": True}
    assert payload["looks"] == {} and payload["resources"] == {}
    single = precheck_payload([c], [], "en", resources=Resources(credits=3.0))
    assert single["resources"] == {"*": Resources(credits=3.0).model_dump(mode="json")}


def test_alternatives_only_listed_when_something_is_dropped() -> None:
    from wintersar.select.report import alternative_rows, render_markdown

    complete = [
        {
            "name": "drop_bursts",
            "coverage_of_aoi": 1.0,
            "n_dates": 5,
            "n_bursts": 2,
            "n_dates_dropped": 0,
            "n_bursts_dropped": 0,
        },
        {
            "name": "drop_dates",
            "coverage_of_aoi": 1.0,
            "n_dates": 5,
            "n_bursts": 2,
            "n_dates_dropped": 0,
            "n_bursts_dropped": 0,
        },
    ]
    partial = [
        dict(complete[0], coverage_of_aoi=0.5, n_bursts=1, n_bursts_dropped=1),
        dict(complete[1], n_dates=4, n_dates_dropped=1),
    ]
    a = make_candidate(relative_orbit=52, notes={"alternatives": complete})
    b = make_candidate(relative_orbit=61, notes={"alternatives": partial})
    payload = precheck_payload([a, b], [], "en")
    rows = alternative_rows(payload)
    assert [sid for sid, _ in rows] == ["T061D_VV", "T061D_VV"]
    md = render_markdown(precheck_payload([a], [], "en"), [], "en")
    assert "Burst/date options" not in md
    md2 = render_markdown(payload, [], "en")
    assert "Burst/date options" in md2 and "Drop incomplete dates" in md2
