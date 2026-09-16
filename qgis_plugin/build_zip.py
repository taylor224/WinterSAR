#!/usr/bin/env python3
"""Package ``wintersar_qgis/`` as an installable QGIS plugin ZIP (Phase 7, ADR-0070/0072).

    .venv/bin/python qgis_plugin/build_zip.py [--out DIR] [--i18n-dir SRC_I18N]

Steps
-----
1. Read the version from ``wintersar_qgis/metadata.txt``.
2. Flatten the message catalogues (``src/wintersar/i18n/{ko,en}.yaml`` deep-merged with
   ``{ko,en}/qgis.yaml``) to ``wintersar_qgis/i18n/{ko,en}.json`` inside the ZIP so the
   plugin needs neither ``wintersar`` nor PyYAML at runtime (only the ``qgis.*`` and
   ``common.*`` namespaces are shipped).
3. Zip the package with the top-level folder ``wintersar_qgis/`` (QGIS "Install from ZIP"
   expects the plugin directory at the root of the archive).
   # source: https://docs.qgis.org/latest/en/docs/user_manual/plugins/plugins.html
   #   "The Install from ZIP tab provides a file selector widget to import plugins in a
   #   zipped format"; "Installed external python plugins are placed under the python/plugins
   #   folder of the active user profile path."

Stdlib + PyYAML only (PyYAML is a core dependency of wintersar).
"""

from __future__ import annotations

import argparse
import configparser
import json
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PLUGIN_NAME = "wintersar_qgis"
HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE / PLUGIN_NAME
REPO_I18N_DIR = HERE.parent / "src" / "wintersar" / "i18n"
SHIPPED_NAMESPACES: tuple[str, ...] = ("qgis.", "common.")
EXCLUDE_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
)
EXCLUDE_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo"})
LANGS: tuple[str, ...] = ("ko", "en")


def read_version(metadata: Path = PLUGIN_DIR / "metadata.txt") -> str:
    cp = configparser.ConfigParser()
    cp.read(metadata, encoding="utf-8")
    return cp.get("general", "version")


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        elif v is not None:
            flat[key] = str(v)
    return flat


def build_catalog(
    lang: str, i18n_dir: Path = REPO_I18N_DIR, namespaces: Iterable[str] = SHIPPED_NAMESPACES
) -> dict[str, str]:
    """Flattened ``{key: template}`` of the namespaces the plugin uses (same merge as core)."""
    import yaml

    merged: dict[str, Any] = {}
    files = [i18n_dir / f"{lang}.yaml", *sorted((i18n_dir / lang).glob("*.yaml"))]
    for f in files:
        if not f.exists():
            continue
        with f.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if isinstance(data, dict):
            merged = _deep_merge(merged, data)
    flat = _flatten(merged)
    ns = tuple(namespaces)
    return {k: v for k, v in sorted(flat.items()) if k.startswith(ns)}


def iter_plugin_files(plugin_dir: Path = PLUGIN_DIR) -> list[Path]:
    files: list[Path] = []
    for p in sorted(plugin_dir.rglob("*")):
        if not p.is_file() or p.suffix in EXCLUDE_SUFFIXES:
            continue
        if any(part in EXCLUDE_DIRS for part in p.relative_to(plugin_dir).parts):
            continue
        if p.relative_to(plugin_dir).parts[0] == "i18n":
            continue  # generated below
        files.append(p)
    return files


def build_zip(out_dir: Path, plugin_dir: Path = PLUGIN_DIR, i18n_dir: Path = REPO_I18N_DIR) -> Path:
    version = read_version(plugin_dir / "metadata.txt")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{PLUGIN_NAME}-{version}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in iter_plugin_files(plugin_dir):
            zf.write(f, f"{PLUGIN_NAME}/{f.relative_to(plugin_dir).as_posix()}")
        for lang in LANGS:
            catalog = build_catalog(lang, i18n_dir)
            zf.writestr(
                f"{PLUGIN_NAME}/i18n/{lang}.json",
                json.dumps(catalog, ensure_ascii=False, indent=1, sort_keys=True),
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=HERE / "dist",
        help="output directory (default: qgis_plugin/dist)",
    )
    parser.add_argument(
        "--i18n-dir", type=Path, default=REPO_I18N_DIR, help="src/wintersar/i18n directory"
    )
    args = parser.parse_args(argv)
    out = build_zip(args.out, i18n_dir=args.i18n_dir)
    sys.stdout.write(f"{out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
