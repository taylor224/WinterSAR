"""Rule 11.6 for the plugin: every ``qgis.*`` key exists in both languages and every key the
plugin source uses is in the catalogue; the plugin's own loader reads the same catalogue."""

from __future__ import annotations

import re

from wintersar_qgis import i18n as plugin_i18n
from wintersar_qgis.cli_client import CLIENT_ERROR_IDS

from tests.unit.qgis.conftest import PLUGIN_PKG_DIR, REPO_I18N_DIR
from wintersar.i18n import load_catalog

_KEY_RE = re.compile(r"""t\(\s*f?["'](qgis\.[A-Za-z0-9_.\-{}]+)["']""")


def _ns(cat: dict[str, str], prefix: str = "qgis.") -> set[str]:
    return {k for k in cat if k.startswith(prefix)}


def test_qgis_namespace_identical_in_ko_and_en() -> None:
    ko, en = _ns(load_catalog("ko")), _ns(load_catalog("en"))
    assert ko == en, f"ko-only: {sorted(ko - en)[:10]} en-only: {sorted(en - ko)[:10]}"
    assert ko, "qgis namespace must not be empty"


def test_every_literal_key_in_plugin_source_exists() -> None:
    ko, en = load_catalog("ko"), load_catalog("en")
    used: set[str] = set()
    for py in sorted(PLUGIN_PKG_DIR.glob("*.py")):
        for m in _KEY_RE.finditer(py.read_text(encoding="utf-8")):
            key = m.group(1)
            if "{" in key:  # f-string: check the static prefix only
                key = key.split("{", 1)[0].rstrip(".")
                assert any(k.startswith(key) for k in ko), key
                continue
            used.add(key)
    assert used
    missing = sorted(k for k in used if k not in ko or k not in en)
    assert not missing, missing


def test_error_status_badge_keys_exist() -> None:
    ko, en = load_catalog("ko"), load_catalog("en")
    for error_id in CLIENT_ERROR_IDS:
        for suffix in ("cause", "fix"):
            k = f"qgis.error.{error_id}.{suffix}"
            assert k in ko and k in en, k
    for badge in ("ok", "warn", "fail"):
        assert f"qgis.badge.{badge}" in ko and f"qgis.badge.{badge}" in en
    for sev in ("FAIL", "WARN", "INFO"):
        assert f"common.severity.{sev}" in ko and f"common.severity.{sev}" in en


def test_plugin_loader_matches_core_catalogue() -> None:
    plugin_i18n.load_catalog.cache_clear()
    core_ko = load_catalog("ko")
    plugin_ko = plugin_i18n.load_catalog("ko", repo_i18n_dir=str(REPO_I18N_DIR))
    assert _ns(plugin_ko) == _ns(core_ko)
    for key in _ns(core_ko):
        assert plugin_ko[key] == core_ko[key]
    assert plugin_i18n.t("qgis.status.running", "en", command="run") == "Running: run"
    assert (
        plugin_i18n.t("qgis.error.TIMEOUT.cause", "ko", command="run", timeout_s=30)
        == "'run'이(가) 30초 안에 끝나지 않았습니다."
    )
    assert plugin_i18n.t("does.not.exist") == "does.not.exist"
    assert "{command}" in plugin_i18n.t("qgis.status.running", "en")
    sev, rid, cause, fix = plugin_i18n.render_finding(
        {
            "rule_id": "TIMEOUT",
            "severity": "FAIL",
            "message_key": "qgis.error.TIMEOUT.cause",
            "fix_key": "qgis.error.TIMEOUT.fix",
            "params": {"command": "run", "timeout_s": 5},
        },
        "en",
    )
    assert (sev, rid) == ("FAIL", "TIMEOUT") and "5 s" in cause and fix


def test_plugin_loader_prefers_packaged_json(tmp_path) -> None:
    import json

    (tmp_path / "i18n").mkdir()
    (tmp_path / "i18n" / "en.json").write_text(
        json.dumps({"qgis.plugin.name": "packaged"}), encoding="utf-8"
    )
    plugin_i18n.load_catalog.cache_clear()
    try:
        cat = plugin_i18n.load_catalog("en", plugin_dir=str(tmp_path))
        assert cat == {"qgis.plugin.name": "packaged"}
        assert plugin_i18n.load_catalog("fr") == {}
        assert (
            plugin_i18n.load_catalog(
                "ko", plugin_dir=str(tmp_path), repo_i18n_dir=str(tmp_path / "none")
            )
            == {}
        )
    finally:
        plugin_i18n.load_catalog.cache_clear()
    plugin_i18n.set_lang("xx")
    assert plugin_i18n.current_lang() == "ko"
    plugin_i18n.set_lang("en")
    assert plugin_i18n.current_lang() == "en"
    plugin_i18n.set_lang("ko")
