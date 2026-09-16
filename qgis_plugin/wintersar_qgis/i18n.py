"""Message catalogue for the plugin (rule 11.6: no hard-coded user-facing strings).

The plugin runs inside the QGIS Python where ``wintersar`` is *not* importable, so it
cannot call :func:`wintersar.i18n.t`. Two sources are supported:

1. ``<plugin dir>/i18n/<lang>.json`` — flattened ``{dotted.key: template}`` written by
   ``qgis_plugin/build_zip.py`` from ``src/wintersar/i18n/{ko,en}.yaml`` +
   ``{ko,en}/qgis.yaml`` (the packaged plugin).
2. The repository checkout (``<repo>/src/wintersar/i18n``) read with PyYAML when the plugin
   directory is a symlink into a checkout (developer mode).

Keys and semantics are identical to the core catalogue (``qgis.*`` namespace plus the
``common.*`` keys used for severity labels).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

SUPPORTED = ("ko", "en")
DEFAULT_LANG = "ko"
FALLBACK_LANG = "en"

_PLUGIN_DIR = Path(__file__).resolve().parent
_REPO_I18N_DIR = _PLUGIN_DIR.parents[1] / "src" / "wintersar" / "i18n"
# catalogue files copied by build_zip.py (relative to the plugin directory)
PACKAGED_CATALOG_DIR = "i18n"

_current_lang: str = os.environ.get("WINTERSAR_LANG", DEFAULT_LANG)


def set_lang(lang: str) -> None:
    global _current_lang
    _current_lang = lang if lang in SUPPORTED else DEFAULT_LANG


def current_lang() -> str:
    return _current_lang


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Nested mapping -> ``{dotted.key: str}`` (same rule as ``wintersar.i18n._flatten``)."""
    flat: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            flat.update(flatten(v, key))
        elif v is None:
            continue
        else:
            flat[key] = str(v)
    return flat


def _read_yaml_catalog(lang: str, i18n_dir: Path) -> dict[str, str]:
    try:
        import yaml
    except ImportError:  # PyYAML missing in this QGIS Python
        return {}
    merged: dict[str, str] = {}
    files = [i18n_dir / f"{lang}.yaml", i18n_dir / lang / "qgis.yaml"]
    for f in files:
        if not f.exists():
            continue
        with f.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if isinstance(data, dict):
            merged.update(flatten(data))
    return merged


@lru_cache(maxsize=4)
def load_catalog(
    lang: str, plugin_dir: str | None = None, repo_i18n_dir: str | None = None
) -> dict[str, str]:
    """Flattened catalogue for ``lang`` (packaged JSON first, then checkout YAML)."""
    if lang not in SUPPORTED:
        return {}
    pdir = Path(plugin_dir) if plugin_dir else _PLUGIN_DIR
    packaged = pdir / PACKAGED_CATALOG_DIR / f"{lang}.json"
    if packaged.exists():
        try:
            data = json.loads(packaged.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError):
            pass
    rdir = Path(repo_i18n_dir) if repo_i18n_dir else _REPO_I18N_DIR
    if rdir.is_dir():
        return _read_yaml_catalog(lang, rdir)
    return {}


def has_key(key: str, lang: str | None = None) -> bool:
    return key in load_catalog(lang or _current_lang)


def t(key: str, lang: str | None = None, /, **params: Any) -> str:
    """Translate ``key``; falls back to English, then to the key itself; never raises."""
    lang = lang or _current_lang
    template = load_catalog(lang).get(key)
    if template is None and lang != FALLBACK_LANG:
        template = load_catalog(FALLBACK_LANG).get(key)
    if template is None:
        return key
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return _safe_format(template, params)


def _safe_format(template: str, params: dict[str, Any]) -> str:
    class _Default(dict[str, Any]):
        def __missing__(self, k: str) -> str:
            return "{" + k + "}"

    try:
        return template.format_map(_Default(params))
    except (ValueError, IndexError):
        return template


def render_finding(finding: dict[str, Any], lang: str | None = None) -> tuple[str, str, str, str]:
    """(severity label, rule id, cause, fix) for a Finding dict from the JSON envelope."""
    sev = str(finding.get("severity", "INFO"))
    params = finding.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    cause = t(str(finding.get("message_key", "")), lang, **params)
    fix_key = finding.get("fix_key")
    fix = t(str(fix_key), lang, **params) if fix_key else ""
    return t(f"common.severity.{sev}", lang), str(finding.get("rule_id", "")), cause, fix
