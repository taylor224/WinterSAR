"""Phase 0 foundation tests: schemas, i18n, hashing, masking, config, CLI skeleton."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tests.conftest import make_burst
from wintersar import i18n
from wintersar.io.schemas import Finding, Pair, Resources, StackCandidate, sort_findings
from wintersar.pipeline.config import EXAMPLE_CONFIG, Config, load_config
from wintersar.util import hashing, masking

# ------------------------------------------------------------------ schemas


def test_burst_record_roundtrip() -> None:
    b = make_burst("2024-01-01")
    d = b.model_dump(mode="json")
    b2 = type(b).model_validate(d)
    assert b2 == b
    assert b.acquisition_date == date(2024, 1, 1)


def test_pair_requires_positive_temporal_baseline() -> None:
    with pytest.raises(ValueError):
        Pair(reference=date(2024, 1, 13), secondary=date(2024, 1, 1), temporal_baseline_days=-12)
    p = Pair(reference=date(2024, 1, 1), secondary=date(2024, 1, 13), temporal_baseline_days=12)
    assert p.key == "20240101_20240113"


def test_stack_candidate_id() -> None:
    s = StackCandidate(
        relative_orbit=52,
        flight_direction="DESCENDING",
        polarization="VV",
        burst_ids=["052_109903_IW2"],
        dates=[date(2024, 1, 1)],
        coverage_of_aoi=1.0,
    )
    assert s.stack_id == "T052D_VV"


def test_sort_findings_fail_first() -> None:
    fs = [
        Finding(rule_id="SEL-07", severity="INFO", message_key="x"),
        Finding(rule_id="SEL-01", severity="FAIL", message_key="x"),
        Finding(rule_id="SEL-06", severity="WARN", message_key="x"),
    ]
    assert [f.severity for f in sort_findings(fs)] == ["FAIL", "WARN", "INFO"]


def test_resources_add() -> None:
    r = Resources(wall_time_s=10, peak_rss_gb=2, credits=1) + Resources(
        wall_time_s=5, peak_rss_gb=4
    )
    assert r.wall_time_s == 15 and r.peak_rss_gb == 4 and r.credits == 1


# ------------------------------------------------------------------ i18n


def test_i18n_common_keys_exist_in_both_languages() -> None:
    ko = set(i18n.all_keys("ko"))
    en = set(i18n.all_keys("en"))
    assert ko == en, f"ko-only: {sorted(ko - en)[:10]} en-only: {sorted(en - ko)[:10]}"


def test_i18n_translate_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "엔진" in i18n.t("env.ENV-001.cause", "ko", engine="snaphu", install_hint="x")
    assert "Engine" in i18n.t("env.ENV-001.cause", "en", engine="snaphu", install_hint="x")
    assert i18n.t("does.not.exist") == "does.not.exist"
    # missing params do not raise
    assert "{engine}" in i18n.t("env.ENV-001.cause", "en")
    monkeypatch.setenv("WINTERSAR_LANG", "en")
    assert i18n.current_lang() == "en"


def test_i18n_module_files_are_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ko.yaml").write_text("a: {b: 'x'}\n", encoding="utf-8")
    (tmp_path / "ko").mkdir()
    (tmp_path / "ko" / "mod.yaml").write_text("a: {c: 'y'}\nm: {k: '{v}!'}\n", encoding="utf-8")
    monkeypatch.setattr(i18n, "_I18N_DIR", tmp_path)
    i18n.load_catalog.cache_clear()
    try:
        cat = i18n.load_catalog("ko")
        assert cat == {"a.b": "x", "a.c": "y", "m.k": "{v}!"}
        assert i18n.t("m.k", "ko", v=1) == "1!"
    finally:
        i18n.load_catalog.cache_clear()


# ------------------------------------------------------------------ hashing / masking


def test_hash_params_is_order_independent() -> None:
    a = hashing.hash_params({"b": 1, "a": [1, 2], "d": date(2024, 1, 1)})
    b = hashing.hash_params({"a": [1, 2], "d": date(2024, 1, 1), "b": 1})
    assert a == b and len(a) == 16
    assert hashing.hash_params({"b": 2, "a": [1, 2]}) != a


def test_hash_file_and_tree(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc" * 1000)
    h1 = hashing.hash_file(f)
    assert h1 == hashing.hash_file(f)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "y.bin").write_bytes(b"z")
    t1 = hashing.hash_tree(tmp_path)
    (tmp_path / "sub" / "y.bin").write_bytes(b"zz")
    assert hashing.hash_tree(tmp_path) != t1


def test_masking_hides_tokens_and_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARTHDATA_TOKEN", "supersecrettoken123")
    home = str(Path.home())
    txt = f"token=supersecrettoken123 path={home}/work Bearer abcdefghijklmnopqrstuvwxyz0123"
    out = masking.mask_text(txt)
    assert "supersecrettoken123" not in out
    assert home not in out
    assert "abcdefghijklmnopqrstuvwxyz0123" not in out
    assert masking.mask_mapping({"a": [f"{home}/x", {"password": "p"}]})["a"][0] == "~/x"


# ------------------------------------------------------------------ config


def test_example_config_loads(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.project.name == "site-a-subsidence"
    assert cfg.aoi == (tmp_path / "aoi.geojson").resolve()
    assert cfg.workdir == (tmp_path / "work").resolve()
    assert cfg.engine.looks == "auto"
    assert cfg.unwrap.tiles == "auto"


def test_config_stage_params_isolate_sections(tmp_path: Path) -> None:
    raw = yaml.safe_load(EXAMPLE_CONFIG)
    cfg = Config.model_validate(raw)
    unw1 = hashing.hash_params(cfg.stage_params("unwrap"))
    ig1 = hashing.hash_params(cfg.stage_params("interferogram"))
    raw["unwrap"]["coherence_threshold"] = 0.4
    cfg2 = Config.model_validate(raw)
    assert hashing.hash_params(cfg2.stage_params("unwrap")) != unw1
    assert hashing.hash_params(cfg2.stage_params("interferogram")) == ig1


def test_config_rejects_unknown_keys() -> None:
    raw = yaml.safe_load(EXAMPLE_CONFIG)
    raw["typo_section"] = {}
    with pytest.raises(ValueError):
        Config.model_validate(raw)


def test_config_looks_and_refpoint_lists() -> None:
    raw = yaml.safe_load(EXAMPLE_CONFIG)
    raw["engine"]["looks"] = [5, 1]
    raw["timeseries"]["reference_point"] = [37.5, 127.0]
    cfg = Config.model_validate(raw)
    assert cfg.engine.looks == (5, 1)
    assert cfg.timeseries.reference_point == (37.5, 127.0)


# ------------------------------------------------------------------ CLI


def test_cli_help_and_version() -> None:
    from wintersar.cli import app

    r = CliRunner().invoke(app, ["--help"])
    assert r.exit_code == 0, r.output
    assert "check-install" in r.output
    r = CliRunner().invoke(app, ["--json", "version"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["data"]["version"]


def test_cli_check_install_reports_missing_engines_as_findings() -> None:
    from wintersar.cli import app

    r = CliRunner().invoke(app, ["--json", "check-install"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["command"] == "check-install"
    assert "engines" in payload["data"]
    ids = {f["rule_id"] for f in payload["findings"]}
    # In this environment no external engine is installed -> ENV-001 must appear
    # once the engine adapters are registered (fake engine is always available).
    assert all(f["message_key"] for f in payload["findings"])
    assert ids <= {"ENV-001", "ENV-002", "ENV-003", "ENV-004", "ENV-005", "ENV-006"}


def test_cli_init_writes_config(tmp_path: Path) -> None:
    from wintersar.cli import app

    p = tmp_path / "config.yaml"
    r = CliRunner().invoke(app, ["init", str(p)])
    assert r.exit_code == 0, r.output
    assert load_config(p).project.name
