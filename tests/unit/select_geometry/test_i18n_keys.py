"""Rule 11.6: every select_geometry key exists in both languages and every key used in code exists."""

from __future__ import annotations

import re
from pathlib import Path

from wintersar import i18n
from wintersar.select import dem, geometry_masks

NS = "select_geometry."
KEY_RE = re.compile(r"[\"'](select_geometry\.[A-Za-z0-9_.\-]+)[\"']")


def _module_keys() -> set[str]:
    keys: set[str] = set()
    for mod in (geometry_masks, dem):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        keys.update(KEY_RE.findall(src))
    return keys


def test_namespace_keys_match_between_ko_and_en() -> None:
    ko = {k for k in i18n.load_catalog("ko") if k.startswith(NS)}
    en = {k for k in i18n.load_catalog("en") if k.startswith(NS)}
    assert ko, "namespace must not be empty"
    assert ko == en, f"ko-only: {sorted(ko - en)} en-only: {sorted(en - ko)}"


def test_every_key_used_in_code_exists() -> None:
    used = _module_keys()
    assert used, "regex found no keys; check KEY_RE"
    for lang in ("ko", "en"):
        missing = i18n.missing_keys(sorted(used), lang)
        assert not missing, f"{lang}: {missing}"
    # keys assembled via DemSource
    for src in dem.DEM_SOURCES.values():
        for lang in ("ko", "en"):
            assert i18n.has_key(src.label_key, lang)
            assert i18n.has_key(src.attribution_key, lang)
    for lang in ("ko", "en"):
        assert i18n.has_key("env.ENV-006.cause", lang) and i18n.has_key("env.ENV-006.fix", lang)


def test_cause_then_fix_pairs_present() -> None:
    for lang in ("ko", "en"):
        cat = i18n.load_catalog(lang)
        assert "select_geometry.SEL-12.cause" in cat
        assert "select_geometry.SEL-12.fix" in cat
        assert "select_geometry.SEL-12.ok" in cat
        for band in geometry_masks.BAND_NAMES:
            assert f"select_geometry.band.{band}" in cat
        for stat in ("layover_fraction", "shadow_fraction", "foreshortening_mean", "aoi_pixels"):
            assert f"select_geometry.stats.{stat}" in cat


def test_templates_format_without_leftover_placeholders() -> None:
    params = {
        "layover_pct": 12.3,
        "shadow_pct": 4.5,
        "threshold_pct": 10.0,
        "flight_direction": "ASCENDING",
        "heading_deg": -12.0,
        "incidence_deg": 39.0,
        "mask_path": "~/x.tif",
        "recommended": "DESCENDING",
        "asc_pct": 16.8,
        "desc_pct": 3.2,
    }
    for lang in ("ko", "en"):
        for key in (
            "select_geometry.SEL-12.cause",
            "select_geometry.SEL-12.fix",
            "select_geometry.SEL-12.ok",
            "select_geometry.recommend",
        ):
            out = i18n.t(key, lang, **params)
            assert "{" not in out and "}" not in out, (lang, key, out)
