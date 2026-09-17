"""Rule 11.6 for the ``select_search`` namespace.

Four checks, matching what the neighbouring module i18n tests enforce, so this namespace
cannot regress on a check only its neighbours have:

1. ko/en key parity;
2. every key referenced in the sources exists in both languages;
3. ``<ns>.<ID>.cause`` is always paired with a ``fix`` (in *both* languages);
4. templates render with no leftover ``{placeholder}`` — driven by the findings the module
   actually produces, so a template and its call site cannot drift apart.
"""

from __future__ import annotations

import itertools
import re
import string
from pathlib import Path
from typing import Any

import pytest

from tests.unit.select_search.conftest import SEOUL_WKT, FakeProduct, FakeResults
from wintersar.i18n import load_catalog, t
from wintersar.io.schemas import Finding, Pair
from wintersar.pipeline.config import Config
from wintersar.select import auth
from wintersar.select import baseline as bl
from wintersar.select import metadata as md
from wintersar.select import search as sr

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "select"
NS = "select_search."
LANGS = ("ko", "en")
SOURCES = ("search.py", "metadata.py", "baseline.py", "auth.py")
RULE_IDS = (
    "SEL-SEARCH-01",
    "SEL-SEARCH-02",
    "SEL-SEARCH-03",
    "SEL-SEARCH-04",
    "SEL-SEARCH-05",
    "SEL-SEARCH-06",
    "SEL-SEARCH-07",
    "SEL-SEARCH-08",
    "KB-AUTH-001",
)


def _ns_keys(lang: str) -> set[str]:
    return {k for k in load_catalog(lang) if k.startswith(NS)}


def _placeholders(template: str) -> set[str]:
    """Field names used by ``template`` (``{a}`` and ``{a.b}``/``{a[0]}`` roots)."""
    return {
        re.split(r"[.\[]", field, maxsplit=1)[0]
        for _, field, _, _ in string.Formatter().parse(template)
        if field
    }


# ---------------------------------------------------------------- 1. key parity


def test_ko_en_key_parity() -> None:
    ko, en = _ns_keys("ko"), _ns_keys("en")
    assert ko, "select_search catalogue is empty"
    assert ko == en, f"missing in en: {sorted(ko - en)}; missing in ko: {sorted(en - ko)}"


def test_ko_en_placeholder_parity() -> None:
    """A translation that drops or renames a placeholder is invisible at runtime.

    ``i18n.t`` falls back to ``_safe_format``, so the mismatched name is silently rendered
    as a literal ``{name}`` in that language only.
    """
    ko, en = load_catalog("ko"), load_catalog("en")
    bad = {
        key: (
            sorted(_placeholders(ko[key]) - _placeholders(en[key])),
            sorted(_placeholders(en[key]) - _placeholders(ko[key])),
        )
        for key in _ns_keys("ko")
        if _placeholders(ko[key]) != _placeholders(en[key])
    }
    assert not bad, f"placeholders differ between ko and en (ko-only, en-only): {bad}"


# ---------------------------------------------------------------- 2. keys used in code


def test_every_key_used_in_code_exists() -> None:
    ko = load_catalog("ko")
    en = load_catalog("en")
    used: set[str] = set()
    for path in SOURCES:
        text = (SRC / path).read_text(encoding="utf-8")
        used.update(re.findall(r"[\"']select_search\.[A-Za-z0-9_.\-]+[\"']", text))
        used.update(
            "select_search." + m
            for m in re.findall(r"f?[\"']\{_NS\}\.([A-Za-z0-9_.\-]+)[\"']", text)
        )
    literal = {k.strip("\"'") for k in used if "{" not in k}
    assert literal, "no i18n keys found in sources"
    missing = sorted(k for k in literal if k not in ko or k not in en)
    assert not missing, missing
    # rule-id based keys built dynamically (f"{_NS}.{rule}.cause" / .fix)
    for rule in RULE_IDS:
        for suffix in ("cause", "fix"):
            assert f"{NS}{rule}.{suffix}" in ko and f"{NS}{rule}.{suffix}" in en
    for reason in ("missing", "invalid_token", "netrc_error", "network"):
        assert f"{NS}KB-AUTH-001.cause_{reason}" in ko and f"{NS}KB-AUTH-001.cause_{reason}" in en
    for fix in ("fix", "fix_invalid_token", "fix_network"):
        assert f"{NS}KB-AUTH-001.{fix}" in ko and f"{NS}KB-AUTH-001.{fix}" in en


# ---------------------------------------------------------------- 3. cause -> fix pairing


@pytest.mark.parametrize("lang", LANGS)
def test_cause_before_fix_pairs(lang: str) -> None:
    cat = load_catalog(lang)
    for key in _ns_keys(lang):
        if key.endswith(".cause"):
            assert key[: -len("cause")] + "fix" in cat, f"{lang}: {key} has no fix key"


# ---------------------------------------------------------------- 4. rendering


def _produced_findings(
    config: Config,
    burst_products: list[FakeProduct],
    slc_products: list[FakeProduct],
    patch_search: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> list[Finding]:
    """Every finding this module can emit, built by the real code paths."""
    import asf_search

    out: list[Finding] = []

    # SEL-SEARCH-01 (summary) + SEL-SEARCH-04 (incomplete page set)
    patch_search(lambda **kw: FakeResults(burst_products, complete=False))
    out += sr.search_bursts(config, SEOUL_WKT).findings

    # SEL-SEARCH-02: no BURST products, SLC fallback
    patch_search(lambda **kw: FakeResults(slc_products if kw["processingLevel"] == "SLC" else []))
    out += sr.search_bursts(config, SEOUL_WKT).findings

    # SEL-SEARCH-03: nothing at all
    patch_search(lambda **kw: FakeResults([]))
    out += sr.search_bursts(config, SEOUL_WKT).findings

    # SEL-SEARCH-05: asf_search raises
    def _boom(**kw: Any) -> FakeResults:
        raise asf_search.ASFSearchError("CMR 503 at https://cmr.earthdata.nasa.gov/search")

    patch_search(_boom)
    out += sr.search_bursts(config, SEOUL_WKT).findings

    # SEL-SEARCH-07: unreadable AOI
    config.aoi.write_text("garbage", encoding="utf-8")
    out += sr.search_from_config(config).findings

    # SEL-SEARCH-06 / SEL-SEARCH-08: stack API down -> orbit fallback, one date with no orbit
    def _stack_from_id(reference_id: str, opts: Any = None, useSubclass: Any = None) -> Any:
        raise asf_search.ASFSearchError("CMR unavailable")

    monkeypatch.setattr(asf_search, "stack_from_id", _stack_from_id)
    recs = md.records_from_asf(burst_products)
    dates = sorted({r.acquisition_date for r in recs})
    pairs = [
        Pair(
            reference=a,
            secondary=b,
            temporal_baseline_days=(b - a).days,
        )
        for a, b in itertools.pairwise(dates)
    ]
    findings: list[Finding] = []
    bl.compute_pair_baselines(recs, pairs, method="auto", findings=findings)
    out += findings
    findings = []
    bl.compute_pair_baselines(recs, pairs, method="asf", findings=findings)
    out += findings

    # KB-AUTH-001: one finding per failure reason
    out += [
        auth.auth_finding(reason, error="boom")
        for reason in ("missing", "invalid_token", "netrc_error", "network")
    ]
    return out


def test_produced_findings_render_in_both_languages(
    config: Config,
    burst_products: list[FakeProduct],
    slc_products: list[FakeProduct],
    patch_search: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = _produced_findings(config, burst_products, slc_products, patch_search, monkeypatch)
    assert {f.rule_id for f in findings} == set(RULE_IDS), "a rule is not exercised here"
    for f in findings:
        for key in (f.message_key, f.fix_key):
            assert key is not None
            for lang in LANGS:
                rendered = t(key, lang, **f.params)
                assert rendered != key, f"{lang}: {key} has no translation"
                assert "{" not in rendered and "}" not in rendered, (lang, key, rendered)


def test_produced_findings_pass_every_placeholder(
    config: Config,
    burst_products: list[FakeProduct],
    slc_products: list[FakeProduct],
    patch_search: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``params`` must cover the template: a missing one renders as a literal ``{name}``."""
    ko = load_catalog("ko")
    findings = _produced_findings(config, burst_products, slc_products, patch_search, monkeypatch)
    for f in findings:
        for key in (f.message_key, f.fix_key):
            assert key is not None
            missing = sorted(_placeholders(ko[key]) - set(f.params))
            assert not missing, f"{f.rule_id} {key}: params missing {missing}"
