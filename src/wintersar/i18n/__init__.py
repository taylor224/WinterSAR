"""Message catalogue (rule 11.6: no user-facing strings hard-coded in Python).

Layout
------
``i18n/ko.yaml`` and ``i18n/en.yaml`` hold common keys. Each module may add its own file
under ``i18n/ko/<module>.yaml`` / ``i18n/en/<module>.yaml``; all files of one language are
deep-merged at load time. Keys are dotted paths (``select.sel01.fail``) that map to nested
mappings in YAML. Values are ``str.format`` templates: ``"트랙 {a}와 {b}는 다릅니다"``.

Diagnostic keys follow the ``<ns>.<id>.cause`` / ``<ns>.<id>.fix`` convention so that
reports can always render "cause → fix" in that order.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_LANG = "ko"
FALLBACK_LANG = "en"
SUPPORTED = ("ko", "en")

_I18N_DIR = Path(__file__).resolve().parent


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
        key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        elif v is None:
            continue
        else:
            flat[key] = str(v)
    return flat


@lru_cache(maxsize=8)
def load_catalog(lang: str) -> dict[str, str]:
    """Return the flattened ``{dotted.key: template}`` catalogue for ``lang``."""
    if lang not in SUPPORTED:
        msg = f"unsupported language {lang!r}; supported: {SUPPORTED}"
        raise ValueError(msg)
    merged: dict[str, Any] = {}
    files: list[Path] = []
    base = _I18N_DIR / f"{lang}.yaml"
    if base.exists():
        files.append(base)
    sub = _I18N_DIR / lang
    if sub.is_dir():
        files.extend(sorted(sub.glob("*.yaml")))
    for f in files:
        with f.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            msg = f"{f}: top level must be a mapping"
            raise ValueError(msg)
        merged = _deep_merge(merged, data)
    return _flatten(merged)


def current_lang() -> str:
    lang = os.environ.get("WINTERSAR_LANG", DEFAULT_LANG).lower()
    return lang if lang in SUPPORTED else DEFAULT_LANG


def has_key(key: str, lang: str | None = None) -> bool:
    return key in load_catalog(lang or current_lang())


def t(key: str, lang: str | None = None, /, **params: Any) -> str:
    """Translate ``key`` for ``lang`` (default: env ``WINTERSAR_LANG`` or ko).

    Falls back to English, then to the key itself so a missing translation never crashes.
    Missing format parameters are left as ``{name}`` rather than raising.
    """
    lang = lang or current_lang()
    template = load_catalog(lang).get(key)
    if template is None and lang != FALLBACK_LANG:
        template = load_catalog(FALLBACK_LANG).get(key)
    if template is None:
        return key
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        # partial format: leave unknown placeholders intact
        return _safe_format(template, params)


def _safe_format(template: str, params: dict[str, Any]) -> str:
    class _Default(dict[str, Any]):
        def __missing__(self, k: str) -> str:
            return "{" + k + "}"

    try:
        return template.format_map(_Default(params))
    except (ValueError, IndexError):
        return template


def missing_keys(keys: list[str], lang: str | None = None) -> list[str]:
    """Utility for tests: which of ``keys`` have no translation in ``lang``."""
    cat = load_catalog(lang or current_lang())
    return [k for k in keys if k not in cat]


def all_keys(lang: str | None = None) -> list[str]:
    return sorted(load_catalog(lang or current_lang()).keys())
