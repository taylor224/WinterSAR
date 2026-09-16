"""metadata.txt: required keys per the PyQGIS cookbook (ADR-0070)."""

from __future__ import annotations

import configparser

from tests.unit.qgis.conftest import PLUGIN_PKG_DIR

# source: https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/plugins/plugins.html
REQUIRED = (
    "name",
    "qgisMinimumVersion",
    "description",
    "about",
    "version",
    "author",
    "email",
    "repository",
)


def _meta() -> configparser.SectionProxy:
    cp = configparser.ConfigParser()
    cp.read(PLUGIN_PKG_DIR / "metadata.txt", encoding="utf-8")
    assert cp.has_section("general")
    return cp["general"]


def test_required_keys_present_and_non_empty() -> None:
    meta = _meta()
    for key in REQUIRED:
        assert meta.get(key, "").strip(), key


def test_version_constraints_and_flags() -> None:
    meta = _meta()
    assert meta["qgisMinimumVersion"] == "3.44"  # current LTR line (qgis.org/download, 2026-09-16)
    assert meta["qgisMaximumVersion"] == "4.99"  # Qt6 line, plugins.qgis.org/docs/migrate-qgis4
    assert meta.getboolean("experimental") is True
    assert meta["hasProcessingProvider"].lower() == "yes"
    assert "supportsQt6" not in meta  # deprecated flag (QGIS wiki)
    from wintersar_qgis import __version__

    assert meta["version"] == __version__


def test_package_init_exposes_class_factory_lazily() -> None:
    import sys

    import wintersar_qgis

    assert callable(wintersar_qgis.classFactory)
    assert "qgis" not in sys.modules  # importing the package must not need QGIS
