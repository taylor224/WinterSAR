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
