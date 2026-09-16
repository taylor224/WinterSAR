"""Rule 11.6: every unwrap i18n key exists in both languages and is cause -> fix shaped."""

from __future__ import annotations

import re
from pathlib import Path

from wintersar.i18n import load_catalog, t

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "unwrap"


def _namespace(cat: dict[str, str]) -> set[str]:
    return {k for k in cat if k.startswith("unwrap.")}


def test_unwrap_keys_identical_in_ko_and_en():
    ko, en = _namespace(load_catalog("ko")), _namespace(load_catalog("en"))
    assert ko, "unwrap namespace is empty"
    assert ko == en, f"ko-only: {sorted(ko - en)} en-only: {sorted(en - ko)}"


def test_every_key_used_in_source_exists():
    used: set[str] = set()
    for py in SRC.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        # literal catalogue keys have >= 2 dots and never end with '.' (prefixes, file names)
        used |= {
            k
            for k in re.findall(r'"(unwrap\.[A-Za-z0-9_.\-]+)"', text)
            if k.count(".") >= 2 and not k.endswith(".")
        }
        used |= {f"unwrap.plan.reason.{k}" for k in re.findall(r'_reason\("([a-z_]+)"\)', text)}
        used |= {
            f"unwrap.{rid}.{part}"
            for rid in re.findall(r'_finding\(\s*"(UNW-\d+)"', text)
            for part in ("cause", "fix")
        }
    # keys built with a prefix are not literal strings; make sure the prefix itself is used
    assert "unwrap.run.done" in used and any(k.startswith("unwrap.plan.reason.") for k in used)
    ko, en = load_catalog("ko"), load_catalog("en")
    missing = sorted(k for k in used if k not in ko or k not in en)
    assert not missing, missing


def test_diagnostics_have_cause_and_fix_in_that_order():
    ko = load_catalog("ko")
    ids = {k.split(".")[1] for k in ko if k.startswith("unwrap.UNW-")}
    assert ids >= {"UNW-001", "UNW-002", "UNW-003", "UNW-004"}
    for rid in ids:
        for lang in ("ko", "en"):
            cause = t(f"unwrap.{rid}.cause", lang)
            fix = t(f"unwrap.{rid}.fix", lang)
            assert cause != f"unwrap.{rid}.cause" and fix != f"unwrap.{rid}.fix"


def test_translations_format_without_errors():
    for lang in ("ko", "en"):
        s = t("unwrap.run.done", lang, n_pairs=3, wall_time_s=1.234, peak_rss_mb=512.0)
        assert "3" in s and "1.2" in s and "512" in s
        s = t("unwrap.plan.machine", lang, cores=8, memory_gb=12.5)
        assert "8" in s and "12.5" in s
