"""Rule 11.6: every key used by the isce2_topsstack / burst2safe / dolphin adapters exists in
ko AND en (engines.isce2.* and engines.dolphin.* namespaces)."""

from __future__ import annotations

import re
from pathlib import Path

from wintersar import i18n

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "engines"
FILES = ("isce2_topsstack.py", "burst2safe.py", "dolphin.py", "runfiles.py")
NAMESPACES = ("engines.isce2.", "engines.dolphin.")


def _keys(lang: str) -> set[str]:
    return {k for k in i18n.all_keys(lang) if k.startswith(NAMESPACES)}


def test_namespace_parity_ko_en() -> None:
    ko, en = _keys("ko"), _keys("en")
    assert ko == en, f"ko-only: {sorted(ko - en)} en-only: {sorted(en - ko)}"
    assert len(ko) >= 40


def test_every_key_referenced_in_code_exists() -> None:
    text = "\n".join((SRC / f).read_text(encoding="utf-8") for f in FILES)
    literal = {
        k
        for k in re.findall(r'"(engines\.(?:isce2|dolphin)\.[A-Za-z0-9_.\-]+)"', text)
        if "{" not in k
    }
    for rule in set(re.findall(r'_finding\(\s*"(ISCE2-\d{3})"', text)):
        literal |= {f"engines.isce2.{rule}.cause", f"engines.isce2.{rule}.fix"}
    for rule in set(re.findall(r'_finding\(\s*"(DOL-\d{3})"', text)):
        literal |= {f"engines.dolphin.{rule}.cause", f"engines.dolphin.{rule}.fix"}
    assert {
        "engines.isce2.ISCE2-001.cause",
        "engines.isce2.ISCE2-004.fix",
        "engines.dolphin.DOL-001.cause",
    } <= literal
    for lang in ("ko", "en"):
        missing = i18n.missing_keys(sorted(literal), lang)
        assert not missing, f"{lang} missing: {missing}"


def test_diagnostic_keys_have_cause_and_fix() -> None:
    for lang in ("ko", "en"):
        keys = _keys(lang)
        ids = {k.rsplit(".", 1)[0] for k in keys if k.endswith(".cause")}
        for base in ids:
            assert f"{base}.fix" in keys, f"{lang}: {base} has cause but no fix"
        assert {f"engines.isce2.ISCE2-{i:03d}" for i in range(1, 17)} <= ids
        assert {f"engines.dolphin.DOL-{i:03d}" for i in range(1, 7)} <= ids


def test_messages_render_in_both_languages() -> None:
    params = dict(
        step="unwrap",
        run_file="run_16_unwrap",
        job_index=3,
        returncode=1,
        attempts=2,
        command="SentinelWrapper.py -c x",
        log="~/log",
    )
    ko = i18n.t("engines.isce2.ISCE2-001.cause", "ko", **params)
    en = i18n.t("engines.isce2.ISCE2-001.cause", "en", **params)
    assert "{" not in ko and "{" not in en and "run_16_unwrap" in ko and "run_16_unwrap" in en
    assert "PERF-11" in i18n.t("engines.isce2.ISCE2-008.cause", "en", workdir="w")
    assert "diagnose" in i18n.t("engines.dolphin.DOL-001.fix", "ko", returncode=1, log="l")
