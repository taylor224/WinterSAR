"""Rule 11.6: every validate i18n key exists in both languages and is cause -> fix shaped."""

from __future__ import annotations

import re
from pathlib import Path

from wintersar.i18n import load_catalog, t
from wintersar.validate.sweep import METRIC_KEYS

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "validate"


def _namespace(cat: dict[str, str]) -> set[str]:
    return {k for k in cat if k.startswith("validate.")}


def test_validate_keys_identical_in_ko_and_en():
    ko, en = _namespace(load_catalog("ko")), _namespace(load_catalog("en"))
    assert ko, "validate namespace is empty"
    assert ko == en, f"ko-only: {sorted(ko - en)} en-only: {sorted(en - ko)}"


def test_every_key_used_in_source_exists():
    used: set[str] = set()
    for py in list(SRC.glob("*.py")) + list((SRC / "templates").glob("*.j2")):
        text = py.read_text(encoding="utf-8")
        used |= {
            k
            for k in re.findall(r"['\"](validate\.[A-Za-z0-9_.\-]+)['\"]", text)
            if k.count(".") >= 2 and not k.endswith(".")
        }
        used |= {
            f"validate.{rid}.{part}"
            for rid in re.findall(r'make_finding\(\s*"(VAL-\d+)"', text)
            for part in ("cause", "fix")
        }
        used |= {
            f"validate.{rid}.{part}"
            for rid in re.findall(r'_finding\("(VAL-\d+)"', text)
            for part in ("cause", "fix")
        }
        # template labels: L.<name> -> validate.report.<name>; three names alias common keys
        aliases = {"cause": "common.cause", "fix": "common.fix", "no_findings": "cli.no_findings"}
        used |= {
            aliases.get(k, f"validate.report.{k}")
            for k in re.findall(r"(?<![A-Za-z_])L\.([a-z_]+)", text)
        }
        used |= {f"validate.sweep.col_{k}" for k in METRIC_KEYS}
    assert "validate.VAL-001.cause" in used and "validate.cli.summary" in used
    ko, en = load_catalog("ko"), load_catalog("en")
    missing = sorted(k for k in used if k not in ko or k not in en)
    assert not missing, missing


def test_diagnostics_have_cause_and_fix_in_that_order():
    ko = load_catalog("ko")
    ids = {k.split(".")[1] for k in ko if k.startswith("validate.VAL-")}
    assert ids >= {f"VAL-{i:03d}" for i in range(1, 17)}
    for rid in ids:
        for lang in ("ko", "en"):
            cause = t(f"validate.{rid}.cause", lang)
            fix = t(f"validate.{rid}.fix", lang)
            assert cause != f"validate.{rid}.cause" and fix != f"validate.{rid}.fix"
            assert cause.strip() and fix.strip()


def test_translations_format_without_errors():
    for lang in ("ko", "en"):
        s = t(
            "validate.cli.summary",
            lang,
            rmse_mm="1.2",
            bias_mm="-0.3",
            n_sites=3,
            n_points=27,
            radius_m="100",
        )
        assert "1.2" in s and "-0.3" in s and "27" in s
        s = t("validate.VAL-009.cause", lang, site_id="A", distance_m=250.0, limit_m=200.0)
        assert "A" in s and "250" in s and "{" not in s
        s = t("validate.VAL-015.fix", lang)
        assert "grid" in s and "{{" not in s
