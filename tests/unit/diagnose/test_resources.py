"""Resource model (ADR-0036): linear initial-guess coefficients + verified HyP3 credits."""

from __future__ import annotations

import pytest

from wintersar.diagnose import resources
from wintersar.util.sysinfo import MachineSpec

M = MachineSpec(cores=8, memory_gb=32.0, gpu=False, gpu_name=None, python="3.11", os="test")


def test_model_file_flags_initial_guess() -> None:
    model = resources.load_model()
    assert model["status"] == "initial_guess"
    assert model["defaults"]["memory_mb_per_mpixel"] == 100.0
    for eng, table in model["stages"].items():
        for stage, coef in table.items():
            assert coef["basis"] in {"guess", "config_default", "dtype"}, (eng, stage)
            assert coef["parallel"] in {"pairs", "single"}


def test_estimate_scales_with_pixels_and_pairs() -> None:
    r1 = resources.estimate("unwrap", 4, 1_000_000, "snaphu", M)
    r2 = resources.estimate("unwrap", 4, 4_000_000, "snaphu", M)
    r3 = resources.estimate("unwrap", 8, 1_000_000, "snaphu", M)
    assert r1.wall_time_s is not None and r2.wall_time_s is not None and r3.wall_time_s is not None
    assert r2.wall_time_s > r1.wall_time_s
    assert r3.disk_gb is not None and r1.disk_gb is not None and r3.disk_gb > r1.disk_gb
    assert r1.n_jobs == 4 and r3.n_jobs == 8
    assert r1.notes["coefficients"] == "initial_guess"
    assert r1.notes["model"] == "linear"
    assert r1.credits is None


def test_estimate_workers_limited_by_cores_memory_and_nproc() -> None:
    small = MachineSpec(cores=2, memory_gb=1.0, gpu=False, gpu_name=None, python="3", os="t")
    r = resources.estimate("unwrap", 10, 20_000_000, "snaphu", small)
    assert r.notes["workers"] == 1
    assert r.notes.get("exceeds_machine_memory") is True
    r = resources.estimate("unwrap", 10, 1_000_000, "snaphu", M, params={"nproc": 3})
    assert r.notes["workers"] == 3
    r = resources.estimate("unwrap", 2, 1_000_000, "snaphu", M)
    assert r.notes["workers"] == 2


def test_estimate_tiles_and_memory_override() -> None:
    base = resources.estimate("unwrap", 1, 10_000_000, "snaphu", M)
    tiled = resources.estimate("unwrap", 1, 10_000_000, "snaphu", M, params={"ntiles": [2, 2]})
    assert tiled.peak_rss_gb is not None and base.peak_rss_gb is not None
    assert tiled.peak_rss_gb < base.peak_rss_gb
    doubled = resources.estimate(
        "unwrap", 1, 10_000_000, "snaphu", M, params={"memory_mb_per_mpixel": 200.0}
    )
    assert doubled.peak_rss_gb is not None and doubled.peak_rss_gb > base.peak_rss_gb


def test_estimate_single_stage_and_unknown() -> None:
    r = resources.estimate("timeseries", 30, 1_000_000, "mintpy", M)
    assert r.n_jobs == 1 and r.wall_time_s is not None and r.wall_time_s > 0
    r = resources.estimate("nonexistent", 1, 1, "snaphu", M)
    assert r.notes["model"] == "none" and r.wall_time_s is None
    r = resources.estimate("unwrap", 1, 1_000_000, "some_new_engine", M)
    assert r.notes["model"] == "linear"  # default table fallback
    r = resources.estimate("unwrap", 0, 0, "snaphu", M)
    assert r.wall_time_s is not None and r.peak_rss_gb is not None


@pytest.mark.parametrize(
    ("looks", "n_bursts", "product", "expected"),
    [
        ("20x4", 1, "burst", 1.0),
        ("20x4", 4, "burst", 1.0),
        ("20x4", 5, "burst", 5.0),
        ("20x4", 12, "burst", 5.0),
        ("20x4", 13, "burst", 10.0),
        ("10x2", 3, "burst", 1.0),
        ("10x2", 9, "burst", 5.0),
        ("10x2", 15, "burst", 10.0),
        ("5x1", 1, "burst", 1.0),
        ("5x1", 10, "burst", 45.0),
        ("5x1", 11, "burst", 90.0),
        ("5x1", 15, "burst", 110.0),
        ("5x1", 16, "burst", None),
        ((20, 4), 2, "burst", 1.0),
        (80, 2, "burst", 1.0),
        ("40m", 1, "slc", 15.0),
        ("20x4", 1, "slc", 10.0),
        ("7x7", 1, "burst", None),
    ],
)
def test_hyp3_credits_table(
    looks: object, n_bursts: int, product: str, expected: float | None
) -> None:
    # source: https://hyp3-docs.asf.alaska.edu/using/credits/
    assert resources.hyp3_credits_per_job(looks, n_bursts, product) == expected


def test_hyp3_estimate_uses_verified_credits() -> None:
    r = resources.estimate(
        "interferogram", 24, 0, "hyp3", M, params={"looks": "20x4", "n_bursts": 3}
    )
    assert r.credits == 24.0
    assert r.n_jobs == 24
    assert r.wall_time_s is None  # remote, not modelled
    assert r.notes["credits_source"].startswith("https://hyp3-docs.asf.alaska.edu/")
    assert r.notes["monthly_free_allotment"] == 8000.0
    assert resources.hyp3_monthly_allotment() == 8000.0
    r = resources.estimate("interferogram", 3, 0, "hyp3", M, params={"looks": "9x9"})
    assert r.credits is None and r.notes["credits_unknown"] is True
    r = resources.estimate("unwrap", 3, 0, "hyp3", M)
    assert r.notes["model"] == "none"


def test_normalize_looks() -> None:
    assert resources.normalize_looks("20x4") == "20x4"
    assert resources.normalize_looks([10, 2]) == "10x2"
    assert resources.normalize_looks(20) == "5x1"
    assert resources.normalize_looks("80 m") == "20x4"
    assert resources.normalize_looks(None) is None
    assert resources.normalize_looks(True) is None


def test_engine_aliases_map_pipeline_names() -> None:
    a = resources.estimate("coregister", 3, 1_000_000, "isce2_topsstack", M)
    b = resources.estimate("coregister", 3, 1_000_000, "isce2", M)
    assert a.wall_time_s == b.wall_time_s and a.peak_rss_gb == b.peak_rss_gb
    assert resources.stage_coefficients(
        "isce2_topsstack", "coregister"
    ) == resources.stage_coefficients("isce2", "coregister")
