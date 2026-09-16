"""``wintersar_qgis.algorithms``: Processing parameter mapping (pure functions)."""

from __future__ import annotations

import json

import pytest
from wintersar_qgis import algorithms as alg
from wintersar_qgis.cli_client import STAGE_ORDER, CliResponse

from wintersar.i18n import load_catalog


def test_algorithm_specs_are_unique_and_labelled() -> None:
    names = [a.name for a in alg.ALGORITHMS]
    assert names == ["search", "precheck", "run", "validate"] and len(set(names)) == 4
    ko, en = load_catalog("ko"), load_catalog("en")
    for a in alg.ALGORITHMS:
        for key in (a.display_key, a.help_key, *(p.label_key for p in a.params)):
            assert key in ko and key in en, key
    for name in alg.OUTPUT_NAMES:
        key = f"qgis.processing.out_{name.lower()}"
        assert key in ko and key in en, key


def test_build_cli_args_search_precheck() -> None:
    assert alg.build_cli_args("search", {"CONFIG": "/p/config.yaml"}) == [
        "search",
        "--config",
        "/p/config.yaml",
    ]
    assert alg.build_cli_args(
        "precheck", {"CANDIDATES": "c.json", "CONFIG": "c.yaml", "OUT": "", "NO_FAIL": True}
    ) == ["precheck", "c.json", "--config", "c.yaml", "--no-fail"]
    assert alg.build_cli_args(
        "precheck", {"CANDIDATES": "c.json", "CONFIG": "c.yaml", "OUT": "rep"}
    ) == ["precheck", "c.json", "--config", "c.yaml", "--out", "rep"]


def test_build_cli_args_run_maps_enum_indices_and_force_list() -> None:
    idx_unwrap = alg.STAGE_OPTIONS.index("unwrap")
    idx_fetch = alg.STAGE_OPTIONS.index("fetch")
    args = alg.build_cli_args(
        "run",
        {
            "CONFIG": "c.yaml",
            "UNTIL": idx_unwrap,
            "FROM": idx_fetch,
            "FORCE": "unwrap, timeseries",
            "DRY_RUN": True,
        },
    )
    assert args == [
        "run",
        "--config",
        "c.yaml",
        "--until",
        "unwrap",
        "--from",
        "fetch",
        "--force",
        "unwrap",
        "--force",
        "timeseries",
        "--dry-run",
    ]
    assert alg.build_cli_args("run", {"CONFIG": "c.yaml", "UNTIL": 0, "FROM": 0}) == [
        "run",
        "--config",
        "c.yaml",
    ]
    assert alg.build_cli_args("run", {"CONFIG": "c.yaml", "UNTIL": "geocode"}) == [
        "run",
        "--config",
        "c.yaml",
        "--until",
        "geocode",
    ]
    assert ("", *STAGE_ORDER) == alg.STAGE_OPTIONS


def test_build_cli_args_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        alg.build_cli_args("run", {"CONFIG": "c.yaml", "FORCE": "nope"})
    with pytest.raises(ValueError, match="out of range"):
        alg.build_cli_args("run", {"CONFIG": "c.yaml", "UNTIL": 99})
    with pytest.raises(KeyError):
        alg.build_cli_args("nope", {})
    with pytest.raises(KeyError):
        alg.build_cli_args("search", {})


def test_build_cli_args_validate() -> None:
    assert alg.build_cli_args("validate", {"TS": "ts.h5", "LEVELING": "lev.csv", "GNSS": ""}) == [
        "validate",
        "--ts",
        "ts.h5",
        "--leveling",
        "lev.csv",
    ]
    with pytest.raises(ValueError):
        alg.build_cli_args("validate", {"TS": "ts.h5", "LEVELING": "", "GNSS": ""})


def test_parse_force() -> None:
    assert alg.parse_force(None) == [] and alg.parse_force("") == []
    assert alg.parse_force(["unwrap", "geocode"]) == ["unwrap", "geocode"]
    assert alg.parse_force(" unwrap ,timeseries,, ") == ["unwrap", "timeseries"]


def test_outputs_and_findings_text() -> None:
    resp = CliResponse(
        ok=False,
        command="precheck",
        data={"recommended": None},
        findings=[
            {
                "rule_id": "CLI_ERROR",
                "severity": "FAIL",
                "message_key": "qgis.error.CLI_ERROR.cause",
                "fix_key": "qgis.error.CLI_ERROR.fix",
                "params": {"command": "precheck", "exit_code": 1},
            },
            {"rule_id": "X", "severity": "WARN", "message_key": "missing.key", "params": {}},
        ],
    )
    out = alg.outputs_from_response(resp)
    assert set(out) == set(alg.OUTPUT_NAMES)
    assert out["OK"] is False and out["N_FAIL"] == 1 and out["N_WARN"] == 1
    assert (
        json.loads(out["DATA"]) == {"recommended": None} and len(json.loads(out["FINDINGS"])) == 2
    )
    lines = alg.findings_text(resp, "en")
    assert (
        lines[0].startswith("[FAIL] CLI_ERROR: 'precheck' failed with exit code 1.")
        and " -> " in lines[0]
    )
    assert lines[1] == "[WARN] X: missing.key"
