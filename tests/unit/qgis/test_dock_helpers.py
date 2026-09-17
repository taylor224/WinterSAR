"""Pure helpers of ``wintersar_qgis.dock_widget`` (config form, row builders) without Qt."""

from __future__ import annotations

import json
import sys
import typing
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest
import yaml
from wintersar_qgis import dock_widget as dw

from wintersar.pipeline.config import EXAMPLE_CONFIG, Config, load_config


def test_module_imports_without_qt() -> None:
    assert "qgis" not in sys.modules and "PyQt5" not in sys.modules and "PyQt6" not in sys.modules
    dock = dw.WintersarDock(iface=None, client_factory=lambda: None, settings=None)  # type: ignore[arg-type]
    assert dock.widget is None


# ------------------------------------------------------------------ config fields vs pydantic


def _model_field(model: type[Any], path: tuple[str, ...]) -> Any:
    cur: Any = model
    info = None
    for key in path:
        fields = cur.model_fields
        name = next((n for n, f in fields.items() if n == key or f.alias == key), None)
        assert name is not None, f"{'.'.join(path)}: {key} not in {list(fields)}"
        info = fields[name]
        cur = _unwrap_model(info.annotation)
    return info


def _unwrap_model(annotation: Any) -> Any:
    for a in (annotation, *get_args(annotation)):
        if isinstance(a, type) and hasattr(a, "model_fields"):
            return a
    return annotation


def _literal_choices(annotation: Any) -> set[str] | None:
    found: set[str] = set()
    stack = [annotation]
    while stack:
        a = stack.pop()
        if get_origin(a) is typing.Literal:
            found.update(str(x) for x in get_args(a))
        else:
            stack.extend(get_args(a))
    return found or None


def test_config_fields_exist_in_config_model_with_matching_choices() -> None:
    assert len({f.dotted for f in dw.CONFIG_FIELDS}) == len(dw.CONFIG_FIELDS)
    for spec in dw.CONFIG_FIELDS:
        info = _model_field(Config, spec.path)
        if spec.kind == "choice":
            assert set(spec.choices) == _literal_choices(info.annotation), spec.dotted
    assert set(dw.CONFIG_SECTIONS) == {f.path[0] for f in dw.CONFIG_FIELDS}


def test_config_field_labels_exist_in_both_catalogs() -> None:
    from wintersar.i18n import load_catalog

    ko, en = load_catalog("ko"), load_catalog("en")
    for spec in dw.CONFIG_FIELDS:
        assert spec.label_key in ko and spec.label_key in en, spec.label_key
    for section in dw.CONFIG_SECTIONS:
        key = f"qgis.config.section.{section}"
        assert key in ko and key in en, key


@pytest.mark.parametrize(
    ("dotted", "raw", "expected"),
    [
        ("engine.looks", "auto", "auto"),
        ("engine.looks", "5, 1", [5, 1]),
        ("timeseries.reference_point", "", dw.UNSET),
        ("timeseries.reference_point", "auto", "auto"),
        ("timeseries.reference_point", "37.5,127.0", [37.5, 127.0]),
        ("unwrap.tiles", "2x3", {"rows": 2, "cols": 3}),
        ("unwrap.tiles", "AUTO", "auto"),
        ("unwrap.tiles", "", dw.UNSET),
        ("data.relative_orbit", "52", 52),
        ("data.relative_orbit", "", dw.UNSET),
        ("engine.cleanup", "", dw.UNSET),
        ("project.name", "", dw.UNSET),
        ("compute.memory_gb", "16", 16.0),
        ("compute.gpu", "false", False),
        ("compute.gpu", "auto", "auto"),
        ("selection.budget_credits", "", None),
        ("validate.leveling_csv", "", None),
        ("time_range.start", "2024-01-01", "2024-01-01"),
        ("engine.esd", "yes", True),
        ("engine.esd", False, False),
    ],
)
def test_parse_field_value(dotted: str, raw: Any, expected: Any) -> None:
    spec = dw.config_field_by_dotted(dotted)
    assert dw.parse_field_value(spec, raw) == expected


@pytest.mark.parametrize(
    ("dotted", "raw"),
    [
        ("engine.looks", "5"),
        ("unwrap.tiles", "2"),
        ("data.source", "nasa"),
        ("time_range.start", "2024/01/01"),
        ("compute.gpu", "maybe"),
        ("selection.max_perp_baseline_m", "abc"),
        ("aoi", ""),
    ],
)
def test_parse_field_value_rejects(dotted: str, raw: str) -> None:
    with pytest.raises(ValueError):
        dw.parse_field_value(dw.config_field_by_dotted(dotted), raw)


def test_format_field_value_roundtrip() -> None:
    cases = {
        "engine.looks": [5, 1],
        "timeseries.reference_point": [37.5, 127.0],
        "unwrap.tiles": {"rows": 2, "cols": 2},
        "compute.gpu": True,
        "engine.esd": True,
        "selection.budget_credits": None,
    }
    for dotted, value in cases.items():
        spec = dw.config_field_by_dotted(dotted)
        text = dw.format_field_value(spec, value)
        parsed = dw.parse_field_value(spec, text)
        if isinstance(value, dict):
            assert parsed == value
        else:
            assert parsed == value or (value is None and parsed is None)
    assert dw.format_field_value(dw.config_field_by_dotted("engine.esd"), None) is False
    with pytest.raises(KeyError):
        dw.config_field_by_dotted("nope.field")


def test_apply_form_values_roundtrips_example_config(tmp_path: Path) -> None:
    data = yaml.safe_load(EXAMPLE_CONFIG)
    values = {
        f.dotted: dw.format_field_value(f, dw.get_nested(data, f.path)) for f in dw.CONFIG_FIELDS
    }
    values["unwrap.coherence_threshold"] = "0.4"
    values["engine.looks"] = "10,2"
    values["selection.budget_credits"] = ""
    values["validate.gnss.path"] = ""
    values["compute.cache_dir"] = ""
    new, errors = dw.apply_form_values(dict(data), values)
    assert errors == {}
    p = tmp_path / "config.yaml"
    dw.write_config_file(new, p)
    (tmp_path / "aoi.geojson").write_text("{}", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.unwrap.coherence_threshold == 0.4 and cfg.engine.looks == (10, 2)
    assert cfg.selection.budget_credits is None and cfg.validation.gnss is None
    assert dw.read_config_file(p)["project"]["name"] == "site-a-subsidence"
    assert dw.resolve_workdir(p, new) == (tmp_path / "work").resolve()


def test_apply_form_values_reports_errors_per_field() -> None:
    data = yaml.safe_load(EXAMPLE_CONFIG)
    _, errors = dw.apply_form_values(data, {"engine.looks": "x", "data.source": "asf"})
    assert set(errors) == {"engine.looks"}


# ------------------------------------------------------------------ rows


def _finding(rule: str, sev: str, scope: str | None) -> dict[str, Any]:
    return {
        "rule_id": rule,
        "severity": sev,
        "message_key": f"select.{rule}.fail",
        "params": {},
        "scope": scope,
    }


def test_badge_for_stack_scopes() -> None:
    fs = [
        _finding("SEL-01", "FAIL", "T052D_VV"),
        _finding("SEL-06", "WARN", "20240101_20240113"),
        _finding("SEL-07", "INFO", None),
        _finding("SEL-12", "WARN", "T061A_VV:DESCENDING"),
    ]
    assert dw.badge_for_stack("T052D_VV", fs) == "FAIL"
    assert dw.badge_for_stack("T061A_VV", fs) == "WARN"
    assert dw.badge_for_stack("T100A_VV", fs) == "OK"
    assert dw.badge_for_stack("T100A_VV", fs, pair_keys=["20240101_20240113"]) == "WARN"
    assert dw.badge_for_stack("T100A_VV", [_finding("X", "FAIL", None)]) == "FAIL"


def test_candidate_rows_from_precheck_payload() -> None:
    payload = {
        "recommended": "T061A_VV",
        "candidates": [
            {
                "relative_orbit": 61,
                "flight_direction": "ASCENDING",
                "polarization": "VV",
                "dates": ["2024-01-01", "2024-01-13"],
                "coverage_of_aoi": 1.0,
                "pairs": [{"reference": "2024-01-01", "secondary": "2024-01-13"}],
            },
            {
                "relative_orbit": 52,
                "flight_direction": "DESCENDING",
                "polarization": "VV",
                "dates": ["2024-02-01"],
                "coverage_of_aoi": 0.5,
                "pairs": [],
            },
        ],
        "candidate_table": [
            {
                "stack_id": "T061A_VV",
                "relative_orbit": 61,
                "flight_direction": "ASCENDING",
                "polarization": "VV",
                "n_dates": 2,
                "n_pairs": 1,
                "coverage_of_aoi": 1.0,
                "first_date": "2024-01-01",
                "last_date": "2024-01-13",
            },
            {
                "stack_id": "T052D_VV",
                "relative_orbit": 52,
                "flight_direction": "DESCENDING",
                "polarization": "VV",
                "n_dates": 1,
                "n_pairs": 0,
                "coverage_of_aoi": 0.5,
                "first_date": "2024-02-01",
                "last_date": "2024-02-01",
            },
        ],
        "findings": [
            _finding("SEL-06", "WARN", "20240101_20240113"),
            _finding("SEL-04", "FAIL", "T052D_VV"),
        ],
    }
    rows = dw.candidate_rows(payload)
    assert [r["stack_id"] for r in rows] == ["T061A_VV", "T052D_VV"]
    assert rows[0]["badge"] == "WARN" and rows[0]["recommended"] is True
    assert rows[1]["badge"] == "FAIL" and rows[1]["recommended"] is False
    assert rows[0]["n_dates"] == 2 and rows[1]["coverage"] == 0.5
    # without candidate_table the summary is derived from the candidates
    del payload["candidate_table"]
    rows2 = dw.candidate_rows(payload)
    assert [r["stack_id"] for r in rows2] == ["T061A_VV", "T052D_VV"] and rows2[0][
        "last_date"
    ] == "2024-01-13"
    assert dw.candidate_rows(None) == [] and dw.candidate_rows({}) == []


def test_findings_rows_render_cause_fix_in_order() -> None:
    fs = [
        {
            "rule_id": "ENV-001",
            "severity": "INFO",
            "message_key": "env.ENV-001.cause",
            "fix_key": "env.ENV-001.fix",
            "params": {"engine": "snaphu", "install_hint": "conda install snaphu"},
            "scope": "unwrap",
        },
        {
            "rule_id": "CLI_ERROR",
            "severity": "FAIL",
            "message_key": "qgis.error.CLI_ERROR.cause",
            "fix_key": "qgis.error.CLI_ERROR.fix",
            "params": {"command": "run", "exit_code": 1},
        },
    ]
    rows = dw.findings_rows(fs, "en")
    assert [r["severity"] for r in rows] == ["FAIL", "INFO"]
    assert (
        rows[1]["rule_id"] == "ENV-001 [unwrap]"
        and "snaphu" in rows[1]["cause"]
        and "conda install" in rows[1]["fix"]
    )
    assert rows[0]["severity_label"] == "FAIL"
    assert dw.findings_summary(fs, "en") == "FAIL 1 · WARN 0 · INFO 1"


def _run_data() -> dict[str, Any]:
    return {
        "records": [
            {
                "stage": "fetch",
                "engine": "fake",
                "status": "ok",
                "extra": {"cache_hit": True},
                "resources": {"wall_time_s": 0.1},
            },
            {
                "stage": "unwrap",
                "engine": "fake",
                "status": "failed",
                "extra": {},
                "resources": {},
                "log_path": "/w/unwrap/abc/logs/unwrap.log",
            },
        ],
        "artifacts": {
            "velocity": {
                "name": "velocity",
                "path": "/w/geocode/x/out/velocity.npy",
                "kind": "npy",
            },
            "vel_cog": {"name": "vel_cog", "path": "/w/geocode/x/out/velocity.tif", "kind": "cog"},
            "coh": {"name": "coh", "path": "/w/geocode/x/out/coherence.tif", "kind": "file"},
        },
        "plan": {
            "resources": {
                "wall_time_s": 120.0,
                "peak_rss_gb": 2.5,
                "disk_gb": None,
                "credits": None,
            }
        },
    }


def test_diagnose_target_falls_back_to_the_workdir_not_workdir_logs(tmp_path: Path) -> None:
    """``<workdir>/logs`` does not exist (ADR-0032) — diagnose must get the workdir itself."""
    workdir = tmp_path / "work"
    (workdir / "unwrap" / "abc" / "logs").mkdir(parents=True)
    # a failed stage wins: its own node log dir
    assert dw.diagnose_target(_run_data(), workdir) == "/w/unwrap/abc/logs"
    # nothing failed (or no run yet): the workdir, which `wintersar diagnose` walks
    target = dw.diagnose_target(None, workdir)
    assert target == str(workdir)
    assert Path(target).is_dir()
    assert not (workdir / "logs").exists()


def test_stage_rows_failed_log_dir_and_resources_line() -> None:
    rows = dw.stage_rows(_run_data())
    assert [(r["stage"], r["status"]) for r in rows] == [("fetch", "cached"), ("unwrap", "failed")]
    assert rows[0]["wall_s"] == 0.1
    assert dw.failed_log_dir(_run_data()) == "/w/unwrap/abc/logs"
    assert dw.failed_log_dir(None) is None
    line = dw.resources_line(_run_data())
    assert "2.0 min" in line and "2.5 GB" in line
    assert dw.resources_line({"resources": {"wall_time_s": None}}) != ""
    assert dw.resources_line({}) == ""
    plan_rows = dw.stage_rows({"stages": [{"stage": "search", "status": "skipped"}]})
    assert plan_rows[0]["status"] == "skipped" and plan_rows[0]["engine"] == "-"


def test_artifact_rows_and_raster_detection() -> None:
    rows = dw.artifact_rows(_run_data())
    assert {r["name"]: r["raster"] for r in rows} == {
        "velocity": False,
        "vel_cog": True,
        "coh": True,
    }
    assert [r["name"] for r in dw.raster_artifacts(_run_data())] == ["vel_cog", "coh"]
    assert dw.artifact_rows(None) == []


def test_latest_run_summary_reads_newest_json(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "old.json").write_text(json.dumps({"run_id": "old"}), encoding="utf-8")
    (runs / "bad.json").write_text("{", encoding="utf-8")
    import os
    import time

    (runs / "new.json").write_text(json.dumps({"run_id": "new"}), encoding="utf-8")
    now = time.time()
    os.utime(runs / "old.json", (now - 100, now - 100))
    os.utime(runs / "bad.json", (now + 10, now + 10))
    os.utime(runs / "new.json", (now + 5, now + 5))
    data = dw.latest_run_summary(tmp_path)
    assert (
        data is not None and data["run_id"] == "new" and data["summary_path"].endswith("new.json")
    )
    assert dw.latest_run_summary(tmp_path / "none") is None


def test_refpoint_rows() -> None:
    data = {
        "candidates": [
            {"row": 1, "col": 2, "lat": 37.5, "lon": 127.0, "score": 0.9, "coherence": 0.95},
            {"lat": None, "lon": 1.0},
            {"lat": 37.6, "lon": 127.1},
        ]
    }
    rows = dw.refpoint_rows(data)
    assert (
        [r["rank"] for r in rows] == [1, 3] and rows[0]["lat"] == 37.5 and rows[1]["score"] is None
    )
    assert dw.refpoint_rows([{"lat": 1.0, "lon": 2.0}])[0]["rank"] == 1
    assert dw.refpoint_rows(None) == [] and dw.refpoint_rows({"nothing": 1}) == []


def test_validation_rows_convert_metres_to_mm() -> None:
    data = {
        "per_site": [
            {
                "site_id": "BM01",
                "method": "leveling",
                "n": 4,
                "rmse_m": 0.0032,
                "bias_m": -0.001,
                "dates": ["2024-01-01", "2024-01-13"],
                "insar_m": [0.0, -0.002],
                "gt_los_m": [0.0, -0.0025],
            },
            {
                "site_id": "G1",
                "method": "gnss",
                "n": 0,
                "rmse_mm": 5.0,
                "bias_mm": 1.0,
                "dates": [],
            },
        ]
    }
    rows = dw.validation_rows(data)
    assert rows[0]["rmse_mm"] == pytest.approx(3.2) and rows[0]["bias_mm"] == pytest.approx(-1.0)
    assert rows[0]["insar_mm"] == pytest.approx([0.0, -2.0]) and rows[0]["gt_mm"] == pytest.approx(
        [0.0, -2.5]
    )
    assert rows[1]["rmse_mm"] == 5.0 and rows[1]["bias_mm"] == 1.0
    assert dw.validation_rows({"comparison": data})[0]["site_id"] == "BM01"
    assert dw.validation_rows(None) == []


def test_ground_truth_uri_and_geojson_helpers(tmp_path: Path) -> None:
    csv = tmp_path / "lev.csv"
    uri = dw.ground_truth_layer_uri(csv)
    assert uri.startswith("file://") and uri.endswith(
        "?delimiter=,&crs=epsg:4326&xField=lon&yField=lat"
    )
    poly = dw.bbox_polygon(126.9, 37.5, 127.0, 37.6)
    fc = dw.geojson_feature_collection([poly])
    assert fc["type"] == "FeatureCollection" and fc["features"][0]["geometry"]["coordinates"][0][
        0
    ] == [126.9, 37.5]
    assert len(poly["coordinates"][0]) == 5


def test_nested_helpers() -> None:
    d: dict[str, Any] = {}
    dw.set_nested(d, ("a", "b", "c"), 1)
    assert dw.get_nested(d, ("a", "b", "c")) == 1 and dw.get_nested(d, ("a", "x")) is None
    dw.delete_nested(d, ("a", "b", "c"))
    assert d == {"a": {"b": {}}}
    dw.delete_nested(d, ("zzz",))
