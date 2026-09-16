"""Rule 11.6: every diagnose key exists in ko and en; KB texts equal the i18n texts."""

from __future__ import annotations

import re
from pathlib import Path

from wintersar.diagnose.kb_loader import load_kb
from wintersar.i18n import load_catalog

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "diagnose"
SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "render_kb_docs.py"


def _ns(cat: dict[str, str]) -> set[str]:
    return {k for k in cat if k.startswith("diagnose.")}


def test_ko_en_key_parity_under_diagnose_namespace() -> None:
    ko, en = _ns(load_catalog("ko")), _ns(load_catalog("en"))
    assert ko == en, {"only_ko": sorted(ko - en), "only_en": sorted(en - ko)}
    assert ko, "diagnose namespace is empty"


def test_kb_cause_fix_keys_exist_and_equal_kb_text() -> None:
    ko, en = load_catalog("ko"), load_catalog("en")
    for e in load_kb():
        assert ko[e.message_key] == e.cause.ko, e.id
        assert ko[e.fix_key] == e.fix.ko, e.id
        assert en[e.message_key] == e.cause.en, e.id
        assert en[e.fix_key] == e.fix.en, e.id
    for lang in ("ko", "en"):
        cat = load_catalog(lang)
        assert "diagnose.KB-UNKNOWN.cause" in cat and "diagnose.KB-UNKNOWN.fix" in cat


def test_keys_used_in_code_exist() -> None:
    used: set[str] = set()
    pat = re.compile(r"""t\(\s*['"](diagnose\.[A-Za-z0-9_.\-]+)['"]""")
    for py in SRC.rglob("*.py"):
        used |= set(pat.findall(py.read_text(encoding="utf-8")))
    assert used, "no t('diagnose.*') calls found"
    for lang in ("ko", "en"):
        cat = load_catalog(lang)
        missing = sorted(k for k in used if k not in cat)
        assert missing == [], (lang, missing)


def test_placeholders_match_between_languages() -> None:
    ko, en = load_catalog("ko"), load_catalog("en")
    ph = re.compile(r"\{(\w+)")
    for k in _ns(ko):
        assert set(ph.findall(ko[k])) == set(ph.findall(en[k])), k


def test_render_script_outputs_are_up_to_date() -> None:
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr + r.stdout
