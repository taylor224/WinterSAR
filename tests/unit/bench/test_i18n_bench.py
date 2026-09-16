"""Rule 11.6: every bench i18n key exists in both languages and diagnostics are cause -> fix."""

from __future__ import annotations

import re
from pathlib import Path

from wintersar.i18n import load_catalog, t

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "bench"


def _namespace(cat: dict[str, str]) -> set[str]:
    return {k for k in cat if k.startswith("bench.")}


def test_bench_keys_identical_in_ko_and_en() -> None:
    ko, en = _namespace(load_catalog("ko")), _namespace(load_catalog("en"))
    assert ko, "bench namespace is empty"
    assert ko == en, f"ko-only: {sorted(ko - en)} en-only: {sorted(en - ko)}"


def test_every_key_used_in_source_exists() -> None:
    used: set[str] = set()
    for py in SRC.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        used |= {k for k in re.findall(r'"(bench\.[A-Za-z0-9_.\-]+)"', text) if k.count(".") >= 2}
        used |= {
            f"bench.table.{k}" for k in re.findall(r'f"bench\.table\.\{key\}"', text) for k in ()
        }
        used |= {
            f"bench.{rid}.{part}"
            for rid in re.findall(r'_finding\(\s*"(BENCH-\d+)"', text)
            for part in ("cause", "fix")
        }
    # keys assembled from prefixes
    used |= {f"bench.table.{k}" for k in ("stage", "wall", "cpu", "rss", "disk", "net")}
    used |= {f"bench.metrics.{k}" for k in ("closure_rms", "unwrap_error_fraction", "gt_rmse")}
    assert "bench.run.start" in used and "bench.BENCH-001.cause" in used
    ko, en = load_catalog("ko"), load_catalog("en")
    missing = sorted(k for k in used if k not in ko or k not in en)
    assert not missing, missing


def test_diagnostics_have_cause_and_fix() -> None:
    ko = load_catalog("ko")
    ids = {k.split(".")[1] for k in ko if k.startswith("bench.BENCH-")}
    assert ids >= {f"BENCH-00{i}" for i in range(1, 8)}
    for rid in ids:
        for lang in ("ko", "en"):
            assert t(f"bench.{rid}.cause", lang) != f"bench.{rid}.cause"
            assert t(f"bench.{rid}.fix", lang) != f"bench.{rid}.fix"


def test_templates_format() -> None:
    for lang in ("ko", "en"):
        s = t(
            "bench.BENCH-001.cause",
            lang,
            stage="unwrap",
            pct=20.0,
            before=1.0,
            after=1.2,
            threshold=15.0,
        )
        assert "unwrap" in s and "20" in s and "15" in s
        s = t("bench.run.repeat_done", lang, i=1, n=3, wall_time_s=1.234, peak_rss_gb=0.5)
        assert "1" in s and "3" in s
