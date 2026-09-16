"""Rule 11.6: every key used by the hyp3/mintpy/aux adapters exists in ko AND en."""

from __future__ import annotations

import re
from pathlib import Path

from wintersar import i18n

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "engines"
# only the modules owned by this adapter set (other engine adapters keep their own tests)
OWNED = ("hyp3.py", "mintpy.py", "mintpy_template.py", "aux_cache.py")
NAMESPACE = "engines."


def _keys(lang: str) -> set[str]:
    return {k for k in i18n.all_keys(lang) if k.startswith(NAMESPACE)}


def test_namespace_parity_ko_en() -> None:
    ko, en = _keys("ko"), _keys("en")
    assert ko == en, f"ko-only: {sorted(ko - en)} en-only: {sorted(en - ko)}"
    assert ko  # not empty


def test_every_key_referenced_in_code_exists() -> None:
    text = "\n".join((SRC / name).read_text(encoding="utf-8") for name in OWNED)
    literal = set(re.findall(r'"(engines\.[A-Za-z0-9_.\-]+)"', text))
    literal = {k for k in literal if "{" not in k}
    # rule ids passed to the mintpy _finding() helper build "engines.mintpy.<ID>.cause/fix"
    for rule in set(re.findall(r'_finding\(\s*"(MP-\d{3})"', text)):
        literal |= {f"engines.mintpy.{rule}.cause", f"engines.mintpy.{rule}.fix"}
    assert literal
    for lang in ("ko", "en"):
        missing = i18n.missing_keys(sorted(literal), lang)
        assert not missing, f"{lang} missing: {missing}"


def test_diagnostic_keys_have_cause_and_fix() -> None:
    for lang in ("ko", "en"):
        keys = _keys(lang)
        ids = {k.rsplit(".", 1)[0] for k in keys if k.endswith(".cause")}
        for base in ids:
            assert f"{base}.fix" in keys, f"{lang}: {base} has cause but no fix"
        assert {"engines.hyp3.HYP3-001", "engines.mintpy.MP-001", "engines.aux.AUX-001"} <= ids


def test_messages_render_in_both_languages() -> None:
    ko = i18n.t("engines.hyp3.HYP3-005.cause", "ko", needed=3, remaining=2, n_jobs=3)
    en = i18n.t("engines.hyp3.HYP3-005.cause", "en", needed=3, remaining=2, n_jobs=3)
    assert "크레딧" in ko and "credits" in en and "{" not in ko and "{" not in en
    assert "PERF-09" in i18n.t("engines.mintpy.MP-002.fix", "en", max_memory_gb=1, num_worker=1)
