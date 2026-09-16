"""``wintersar_qgis.settings``: JSON persistence without QGIS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from wintersar_qgis import settings as st


def test_roundtrip_save_load(tmp_path: Path) -> None:
    s = st.PluginSettings(
        python_exe="/envs/w/bin/python", env_hint="conda:w", lang="en", timeout_s=120.0
    )
    s.remember_config("/a/config.yaml")
    p = st.save_settings(s, tmp_path / "x" / "settings.json")
    assert p.exists() and json.loads(p.read_text(encoding="utf-8"))["lang"] == "en"
    back = st.load_settings(p)
    assert back == s and back.recent_configs == ["/a/config.yaml"]


def test_default_path_uses_env_override(tmp_path: Path) -> None:
    assert st.default_settings_path().parent == tmp_path / "settings"
    s = st.PluginSettings(lang="ko")
    st.save_settings(s)
    assert st.load_settings().lang == "ko"
    assert (tmp_path / "settings" / st.SETTINGS_FILENAME).exists()


def test_missing_or_corrupt_file_yields_defaults(tmp_path: Path) -> None:
    assert st.load_settings(tmp_path / "none.json") == st.PluginSettings()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert st.load_settings(bad) == st.PluginSettings()
    bad.write_text("[1, 2]", encoding="utf-8")
    assert st.load_settings(bad) == st.PluginSettings()


def test_from_dict_is_tolerant() -> None:
    s = st.PluginSettings.from_dict(
        {"lang": "fr", "timeout_s": "abc", "unknown": 1, "recent_configs": "x", "python_exe": ""}
    )
    assert s.lang == "ko" and s.timeout_s == st.DEFAULT_TIMEOUT_S
    assert s.recent_configs == [] and s.python_exe is None
    s2 = st.PluginSettings.from_dict({"timeout_s": -5})
    assert s2.timeout_s == st.DEFAULT_TIMEOUT_S


def test_remember_config_orders_and_limits() -> None:
    s = st.PluginSettings()
    for i in range(st.MAX_RECENT + 3):
        s.remember_config(f"/c{i}.yaml")
    s.remember_config("/c5.yaml")
    assert s.recent_configs[0] == "/c5.yaml" and s.last_config == "/c5.yaml"
    assert len(s.recent_configs) == st.MAX_RECENT and len(set(s.recent_configs)) == st.MAX_RECENT


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_default_dir_per_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str, tmp_path: Path
) -> None:
    monkeypatch.delenv("WINTERSAR_QGIS_SETTINGS_DIR", raising=False)
    monkeypatch.setattr(st.sys, "platform", platform)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    d = st.default_settings_dir()
    assert d.name == "wintersar"
    if platform == "win32":
        assert d.parent == tmp_path / "appdata"
    elif platform == "linux":
        assert d.parent == tmp_path / "xdg"
    else:
        assert "Application Support" in str(d)
