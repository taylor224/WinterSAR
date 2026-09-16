"""``qgis_plugin/build_zip.py`` packages the plugin folder with generated catalogues."""

from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

from tests.unit.qgis.conftest import QGIS_PLUGIN_DIR


def _load_build_zip():
    spec = importlib.util.spec_from_file_location("build_zip", QGIS_PLUGIN_DIR / "build_zip.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_zip_contains_plugin_and_catalogues(tmp_path: Path) -> None:
    bz = _load_build_zip()
    out = bz.build_zip(tmp_path / "dist")
    assert out.name == f"wintersar_qgis-{bz.read_version()}.zip"
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        for required in (
            "metadata.txt",
            "__init__.py",
            "plugin.py",
            "dock_widget.py",
            "cli_client.py",
            "settings.py",
            "algorithms.py",
            "processing_provider.py",
            "i18n.py",
            "i18n/ko.json",
            "i18n/en.json",
        ):
            assert f"wintersar_qgis/{required}" in names, required
        assert not any(".pyc" in n or "__pycache__" in n for n in names)
        assert all(n.startswith("wintersar_qgis/") for n in names)
        ko = json.loads(zf.read("wintersar_qgis/i18n/ko.json"))
        en = json.loads(zf.read("wintersar_qgis/i18n/en.json"))
    assert set(ko) == set(en)
    assert "qgis.plugin.name" in ko and "common.severity.FAIL" in ko
    assert not any(k.startswith("select.") for k in ko)


def test_build_catalog_matches_core_namespace() -> None:
    from wintersar.i18n import load_catalog

    bz = _load_build_zip()
    cat = bz.build_catalog("en")
    core = {k: v for k, v in load_catalog("en").items() if k.startswith(("qgis.", "common."))}
    assert cat == core


def test_main_writes_zip_path(tmp_path: Path, capsys) -> None:
    bz = _load_build_zip()
    assert bz.main(["--out", str(tmp_path)]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed.endswith(".zip") and Path(printed).exists()
