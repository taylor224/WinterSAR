"""Golden regression of the synthetic S site (plan §8 "regression" row, ADR-0100..0102).

``tests/regression/golden/S_synthetic/stats.json`` holds the statistics of one deterministic
fake-engine pipeline run (``benchmarks/sites/S_synthetic.yaml``, seed 0). Any change of the
synthetic generator, the fake engine, the pipeline or the bench metric definitions shows up
here. Regenerate only after a reviewed, intended change (rule 11.4)::

    .venv/bin/python scripts/make_golden.py --check   # see the differences
    .venv/bin/python scripts/make_golden.py           # rewrite the golden

Tolerances (ADR-0102): exact for counts, ids, names and shapes; ``rtol=1e-6``/``atol=1e-9``
for floats; ``atol=1e-4`` for pixel fractions.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from wintersar.bench import golden as g
from wintersar.bench.sites import Site, load_site
from wintersar.i18n import t

REPO = Path(__file__).resolve().parents[2]
SITE = REPO / "benchmarks" / "sites" / "S_synthetic.yaml"
GOLDEN = g.golden_path(REPO, "S_synthetic")
BUDGET_S = 90.0

pytestmark = [pytest.mark.slow, pytest.mark.timeout(int(BUDGET_S))]


def _regenerate_hint(mismatches: list[g.Mismatch]) -> str:
    return (
        f"golden mismatch ({len(mismatches)}):\n"
        + g.format_mismatches(mismatches)
        + "\n"
        + t("golden.check.regenerate", "en")
    )


@pytest.fixture(scope="module")
def site() -> Site:
    return load_site(SITE)


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    assert GOLDEN.exists(), (
        f"missing golden file {GOLDEN}; run `.venv/bin/python scripts/make_golden.py`"
    )
    assert GOLDEN.stat().st_size < g.MAX_GOLDEN_BYTES, "golden must stay < 50 kB (statistics)"
    return g.load_golden(GOLDEN)


@pytest.fixture(scope="module")
def fresh(site: Site, tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], float]:
    # WINTERSAR_CACHE is irrelevant here: run_golden_pipeline keeps the cache in the workdir.
    wd = tmp_path_factory.mktemp("golden")
    t0 = time.perf_counter()
    stats, result = g.compute_golden(site, wd)
    elapsed = time.perf_counter() - t0
    assert result.ok, [f.model_dump() for f in result.findings]
    return stats, elapsed


def test_golden_site_block_matches_current_yaml(golden: dict[str, Any], site: Site) -> None:
    """A changed site definition invalidates the golden (GOLDEN-003)."""
    assert golden["schema_version"] == g.GOLDEN_SCHEMA_VERSION
    assert golden["generator"] == g.GENERATOR
    assert golden["seed"] == g.golden_seed(site)
    site_mismatch = [
        m
        for m in g.compare_golden(golden, {"site": g.site_block(site)})
        if m.path.startswith("site")
    ]
    assert site_mismatch == [], _regenerate_hint(site_mismatch)


def test_golden_file_is_machine_independent(golden: dict[str, Any]) -> None:
    assert g.forbidden_keys(golden) == []
    text = GOLDEN.read_text(encoding="utf-8")
    assert str(Path.home()) not in text
    assert text == g.dump_golden(golden), "golden must be written by scripts/make_golden.py"


def test_s_synthetic_statistics_match_golden(
    golden: dict[str, Any], fresh: tuple[dict[str, Any], float]
) -> None:
    stats, elapsed = fresh
    assert elapsed < BUDGET_S, f"golden run took {elapsed:.1f} s (budget {BUDGET_S:.0f} s)"
    mismatches = g.compare_golden(golden, stats)
    assert mismatches == [], _regenerate_hint(mismatches)
    # the layer covers what plan §8 asks for: velocity-map statistics + Finding lists
    assert set(stats["velocity"]) >= {"min", "max", "mean", "std", "p05", "p50", "p95"}
    assert stats["n_pairs"] == golden["n_pairs"] and stats["n_dates"] == golden["n_dates"]
    assert all("findings" in s for s in stats["stages"].values())


def test_golden_run_is_reproducible(
    site: Site, fresh: tuple[dict[str, Any], float], tmp_path: Path
) -> None:
    """Fixed seeds → bit-identical statistics on the same machine (ADR-0100: no rounding)."""
    again, _ = g.compute_golden(site, tmp_path / "again")
    assert g.dump_golden(again) == g.dump_golden(fresh[0])
