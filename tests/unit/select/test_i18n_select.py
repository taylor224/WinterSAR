"""Rule 11.6: every select.* key exists in both languages and renders without leftovers."""

from __future__ import annotations

import re

from wintersar.i18n import load_catalog, t
from wintersar.select.rules import all_message_keys


def select_keys(lang: str) -> set[str]:
    return {k for k in load_catalog(lang) if k.startswith("select.")}


def test_ko_and_en_have_identical_select_keys() -> None:
    ko, en = select_keys("ko"), select_keys("en")
    assert ko == en, {"only_ko": sorted(ko - en), "only_en": sorted(en - ko)}
    assert ko  # non-empty
    assert set(all_message_keys()) <= ko


def test_placeholders_match_between_languages() -> None:
    ko, en = load_catalog("ko"), load_catalog("en")
    pat = re.compile(r"{(\w+)")
    for key in sorted(select_keys("ko")):
        assert set(pat.findall(ko[key])) == set(pat.findall(en[key])), key


def test_domain_explanations_present() -> None:
    ko = load_catalog("ko")
    assert "트랙 번호" in ko["select.SEL-01.fail"] and "175" in ko["select.SEL-01.fail"]
    assert "비대칭" in ko["select.SEL-09.info"] and "항상" in ko["select.SEL-09.info"]
    assert "레이오버" in ko["select.SEL-12.warn"]
    assert "burst 단위" in ko["select.SEL-04.fail"]
    rendered = t("select.SEL-01.fail", "en", stack="T052D_VV", tracks="61, 134")
    assert "{" not in rendered and "61, 134" in rendered
