"""Rule 11.6: every select_search key exists in both languages and every key used in code exists."""

from __future__ import annotations

import re
from pathlib import Path

from wintersar.i18n import load_catalog

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "select"
NS = "select_search."


def _ns_keys(lang: str) -> set[str]:
    return {k for k in load_catalog(lang) if k.startswith(NS)}


def test_ko_en_key_parity() -> None:
    ko, en = _ns_keys("ko"), _ns_keys("en")
    assert ko, "select_search catalogue is empty"
    assert ko == en, f"missing in en: {sorted(ko - en)}; missing in ko: {sorted(en - ko)}"


def test_every_key_used_in_code_exists() -> None:
    ko = load_catalog("ko")
    en = load_catalog("en")
    used: set[str] = set()
    for path in ("search.py", "metadata.py", "baseline.py", "auth.py"):
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
    for rule in (
        "SEL-SEARCH-01",
        "SEL-SEARCH-02",
        "SEL-SEARCH-03",
        "SEL-SEARCH-04",
        "SEL-SEARCH-05",
        "SEL-SEARCH-06",
        "SEL-SEARCH-07",
        "SEL-SEARCH-08",
    ):
        for suffix in ("cause", "fix"):
            assert f"{NS}{rule}.{suffix}" in ko and f"{NS}{rule}.{suffix}" in en
    for reason in ("missing", "invalid_token", "netrc_error", "network"):
        assert f"{NS}KB-AUTH-001.cause_{reason}" in ko and f"{NS}KB-AUTH-001.cause_{reason}" in en
    for fix in ("fix", "fix_invalid_token", "fix_network"):
        assert f"{NS}KB-AUTH-001.{fix}" in ko and f"{NS}KB-AUTH-001.{fix}" in en


def test_cause_before_fix_pairs() -> None:
    ko = load_catalog("ko")
    for key in ko:
        if key.startswith(NS) and key.endswith(".cause"):
            assert key[: -len("cause")] + "fix" in ko, f"{key} has no fix key"
