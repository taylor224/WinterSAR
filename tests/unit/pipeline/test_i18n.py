"""Rule 11.6: every pipeline key exists in both languages and every key used in code exists."""

from __future__ import annotations

import re
from pathlib import Path

from wintersar.i18n import load_catalog

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "pipeline"
KEY_RE = re.compile(r"pipeline\.(?:PIPELINE-\d{3}\.(?:cause|fix)|cli\.[a-z_]+)")
ID_RE = re.compile(r"rule_id=\"(PIPELINE-\d{3})\"")


def _keys(lang: str) -> set[str]:
    return {k for k in load_catalog(lang) if k.startswith("pipeline.")}


def test_ko_en_key_parity() -> None:
    ko, en = _keys("ko"), _keys("en")
    assert ko, "no pipeline keys loaded"
    assert ko == en, f"ko-only: {sorted(ko - en)} en-only: {sorted(en - ko)}"


def test_every_key_used_in_code_exists() -> None:
    ko = _keys("ko")
    used: set[str] = set()
    for p in SRC.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        used.update(KEY_RE.findall(text))
        for rid in ID_RE.findall(text):
            used.add(f"pipeline.{rid}.cause")
            used.add(f"pipeline.{rid}.fix")
    # status labels are built dynamically in cli._status_label
    used.update(f"pipeline.cli.status_{s}" for s in ("blocked", "failed"))
    missing = sorted(used - ko)
    assert not missing, missing


def test_diagnostic_keys_have_cause_and_fix() -> None:
    ko = _keys("ko")
    ids = {k.split(".")[1] for k in ko if k.startswith("pipeline.PIPELINE-")}
    for rid in ids:
        assert f"pipeline.{rid}.cause" in ko
        assert f"pipeline.{rid}.fix" in ko


def test_message_key_variants_exist_in_both_languages() -> None:
    """Keys the regex above cannot see: they are built at runtime, not written literally.

    ``PIPELINE-001.fix_auto`` is chosen by an f-string in the executor, and
    ``PIPELINE-002.cause_no_cache``/``fix_no_cache`` replace the generic "needs input"
    text for the ``--from`` case.
    """
    ko, en = _keys("ko"), _keys("en")
    for key in (
        "pipeline.PIPELINE-001.fix_auto",
        "pipeline.PIPELINE-002.cause_no_cache",
        "pipeline.PIPELINE-002.fix_no_cache",
    ):
        assert key in ko and key in en, key


def test_pipeline_001_fix_uses_the_kb_engine_placeholder() -> None:
    """The KB rejects registry names such as 'fake' or 'isce2_topsstack' with exit 2."""
    for lang in ("ko", "en"):
        catalog = load_catalog(lang)
        assert "{kb_engine}" in catalog["pipeline.PIPELINE-001.fix"]
        assert "{engine}" not in catalog["pipeline.PIPELINE-001.fix"]
        assert "--engine" not in catalog["pipeline.PIPELINE-001.fix_auto"]
