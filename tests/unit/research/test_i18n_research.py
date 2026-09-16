"""Rule 11.6: every research i18n key exists in both languages and is cause -> fix shaped."""

from __future__ import annotations

import re
from pathlib import Path

from wintersar.i18n import load_catalog, t

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "research"


def _namespace(cat: dict[str, str]) -> set[str]:
    return {k for k in cat if k.startswith("research.")}


def test_research_keys_identical_in_ko_and_en():
    ko, en = _namespace(load_catalog("ko")), _namespace(load_catalog("en"))
    assert ko, "research namespace is empty"
    assert ko == en, f"ko-only: {sorted(ko - en)} en-only: {sorted(en - ko)}"


def test_every_key_used_in_source_exists():
    used: set[str] = set()
    for py in SRC.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        used |= {
            k
            for k in re.findall(r'"(research\.[A-Za-z0-9_.\-]+)"', text)
            if k.count(".") >= 2 and not k.endswith(".")
        }
        used |= {
            f"research.{rid}.{part}"
            for rid in re.findall(r'ResearchError\(\s*"(RES-\d+)"', text)
            for part in ("cause", "fix")
        }
        used |= {
            f"research.{rid}.{part}"
            for rid in re.findall(r'_finding\(\s*"(RES-\d+)"', text)
            for part in ("cause", "fix")
        }
        used |= {
            f"research.experiment.skipped_reason.{r}"
            for r in re.findall(r'result\.reason = "([a-z_]+)"', text)
        }
    assert "research.cli.repr_done" in used and "research.RES-001.cause" in used
    assert "research.experiment.skipped_reason.requires" in used
    ko, en = load_catalog("ko"), load_catalog("en")
    missing = sorted(k for k in used if k not in ko or k not in en)
    assert not missing, missing


def test_diagnostics_have_cause_and_fix_in_both_languages():
    ko = load_catalog("ko")
    ids = {k.split(".")[1] for k in ko if k.startswith("research.RES-")}
    assert ids >= {f"RES-00{i}" for i in range(1, 9)}
    for rid in ids:
        for lang in ("ko", "en"):
            cause = t(f"research.{rid}.cause", lang)
            fix = t(f"research.{rid}.fix", lang)
            assert cause != f"research.{rid}.cause" and fix != f"research.{rid}.fix"
            assert cause.strip() and fix.strip()


def test_translations_format_without_errors():
    for lang in ("ko", "en"):
        s = t("research.cli.repr_done", lang, method="ml", factor=3, ny=10, nx=12, path="x.npz")
        assert "ml" in s and "10" in s and "x.npz" in s
        s = t("research.cli.stitch_jumps", lang, n_jump=1, n_boundaries=4, n_jump_pixels=7)
        assert "1" in s and "4" in s and "7" in s
        s = t("research.RES-006.cause", lang, name="factor", value=0, allowed=">= 1")
        assert "factor" in s and ">= 1" in s
        s = t("research.experiment.generated_from", lang, json="a.json")
        assert "a.json" in s
