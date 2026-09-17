"""Scheduler rules of plan §5.4 (R-06, PERF-04): memory model, tiling, parallelism, method."""

from __future__ import annotations

import numpy as np
import pytest

from tests.unit.unwrap.conftest import make_machine
from wintersar.i18n import load_catalog
from wintersar.pipeline.config import TilesCfg, UnwrapCfg
from wintersar.research import synth
from wintersar.unwrap import scheduler
from wintersar.unwrap.scheduler import (
    DEFAULT_MB_PER_MPIXEL,
    DEFAULT_MIN_OVERLAP_PX,
    DEFAULT_OVERLAP_FRACTION,
    FRINGE_HIGH,
    REASON_PREFIX,
    UnwrapPlan,
    choose_strategy,
    estimate_memory_mb,
    fringe_density,
    near_square_grid,
)

R = REASON_PREFIX

# ------------------------------------------------------------------ memory model


def test_memory_model_is_linear_in_pixels():
    assert estimate_memory_mb((1000, 1000), 100.0) == pytest.approx(100.0)
    assert estimate_memory_mb((2000, 2000), 100.0) == pytest.approx(400.0)
    assert estimate_memory_mb((30000, 30000), 100.0) == pytest.approx(90_000.0)
    assert estimate_memory_mb((1000, 1000)) == estimate_memory_mb(
        (1000, 1000), DEFAULT_MB_PER_MPIXEL
    )
    assert estimate_memory_mb((0, 10)) == 0.0
    with pytest.raises(ValueError):
        estimate_memory_mb((10, 10), 0.0)


def test_constants_match_config_defaults():
    cfg = UnwrapCfg()
    assert cfg.memory_mb_per_mpixel == DEFAULT_MB_PER_MPIXEL
    fields = TilesCfg.model_fields
    assert fields["overlap"].default == DEFAULT_OVERLAP_FRACTION
    assert fields["min_overlap_px"].default == DEFAULT_MIN_OVERLAP_PX


# ------------------------------------------------------------------ strategy


def test_medium_igram_single_tile_igram_parallel(machine_64gb_10c):
    plan = choose_strategy((2000, 2000), 20, machine_64gb_10c, UnwrapCfg())
    assert (plan.rows, plan.cols) == (1, 1)
    assert plan.overlap_px == 0
    assert plan.tile_shape == (2000, 2000)
    assert plan.est_mb_per_igram == pytest.approx(400.0)
    assert plan.n_parallel == 10  # cores-bound: 20 igrams, 10 cores, memory allows 163
    assert plan.n_parallel > 1
    assert plan.nproc_per_igram == 1
    assert plan.method == "snaphu"
    assert R + "single_tile" in plan.reason_keys
    assert R + "parallel_cores_bound" in plan.reason_keys
    assert R + "method_snaphu_default" in plan.reason_keys


def test_huge_igram_is_tiled_and_fits_budget(machine_64gb_10c):
    plan = choose_strategy((30000, 30000), 20, machine_64gb_10c, UnwrapCfg())
    assert plan.n_tiles > 1
    assert plan.single_tile_mb > plan.budget_mb
    assert plan.tile_mb <= plan.budget_mb
    assert plan.nproc_per_igram * plan.tile_mb <= plan.budget_mb
    assert plan.nproc_per_igram > 1  # tile-level workers fill the cores (ADR-0047)
    assert plan.n_parallel == 1  # one interferogram at a time (memory-bound)
    assert plan.overlap_px >= DEFAULT_MIN_OVERLAP_PX
    assert plan.overlap_px == max(
        round(DEFAULT_OVERLAP_FRACTION * min(plan.tile_shape[0] - plan.overlap_px, 30000)),
        DEFAULT_MIN_OVERLAP_PX,
    )
    assert plan.method == "tophu"
    assert R + "tiled_memory" in plan.reason_keys
    assert R + "nproc_raised" in plan.reason_keys
    assert R + "method_tophu_large" in plan.reason_keys
    # near-square grid
    assert abs(plan.rows - plan.cols) <= 1


def test_huge_igram_without_tophu_falls_back_to_snaphu(machine_64gb_10c):
    plan = choose_strategy((30000, 30000), 3, machine_64gb_10c, UnwrapCfg(), available=["snaphu"])
    assert plan.method == "snaphu" and plan.n_tiles > 1
    assert R + "method_snaphu_default" in plan.reason_keys


def test_small_ram_reduces_parallelism():
    plan = choose_strategy((4000, 4000), 20, make_machine(10, 8.0), UnwrapCfg())
    assert (plan.rows, plan.cols) == (1, 1)
    assert plan.est_mb_per_igram == pytest.approx(1600.0)
    assert plan.n_parallel == 5  # floor(8192 / 1600)
    assert R + "parallel_memory_bound" in plan.reason_keys
    big = choose_strategy((4000, 4000), 20, make_machine(10, 64.0), UnwrapCfg())
    assert big.n_parallel > plan.n_parallel


def test_few_igrams_bound_parallelism(machine_64gb_10c):
    plan = choose_strategy((1000, 1000), 3, machine_64gb_10c, UnwrapCfg())
    assert plan.n_parallel == 3
    assert R + "parallel_igrams_bound" in plan.reason_keys


def test_explicit_tiles_override_auto(machine_64gb_10c):
    cfg = UnwrapCfg(
        tiles=TilesCfg(rows=2, cols=3, overlap=0.1, min_overlap_px=10), nproc_per_igram=2
    )
    plan = choose_strategy((1000, 1200), 5, machine_64gb_10c, cfg)
    assert (plan.rows, plan.cols) == (2, 3)
    assert plan.overlap_px == 40  # 0.1 x min(core 500, 400)
    assert plan.tile_shape == (540, 440)
    assert plan.nproc_per_igram == 2  # user value kept
    assert plan.n_parallel == 5  # min(10 // 2, memory, 5 igrams)
    assert R + "tiles_explicit" in plan.reason_keys
    assert R + "tiled_memory" not in plan.reason_keys
    # min_overlap_px wins over the fraction when larger
    cfg2 = UnwrapCfg(tiles=TilesCfg(rows=2, cols=2, overlap=0.01, min_overlap_px=200))
    assert choose_strategy((1000, 1000), 1, machine_64gb_10c, cfg2).overlap_px == 200


def test_explicit_tiles_larger_than_budget_flagged():
    cfg = UnwrapCfg(tiles=TilesCfg(rows=1, cols=2, overlap=0.0, min_overlap_px=0))
    plan = choose_strategy((30000, 30000), 2, make_machine(4, 8.0), cfg)
    assert R + "memory_insufficient" in plan.reason_keys
    assert plan.n_parallel == 1


def test_method_explicit_and_fringe_rules(machine_64gb_10c):
    explicit = choose_strategy((30000, 30000), 2, machine_64gb_10c, UnwrapCfg(method="snaphu"))
    assert explicit.method == "snaphu" and R + "method_explicit" in explicit.reason_keys
    high = choose_strategy((1000, 1000), 2, machine_64gb_10c, UnwrapCfg(), fringe=FRINGE_HIGH + 0.1)
    assert high.method == "tophu" and R + "method_tophu_fringe" in high.reason_keys
    low = choose_strategy((1000, 1000), 2, machine_64gb_10c, UnwrapCfg(), fringe=0.05)
    assert low.method == "snaphu"
    nan = choose_strategy((1000, 1000), 2, machine_64gb_10c, UnwrapCfg(), fringe=float("nan"))
    assert nan.method == "snaphu"
    none = choose_strategy((1000, 1000), 2, machine_64gb_10c, UnwrapCfg(), available=[])
    assert none.method == "snaphu" and R + "method_none_available" in none.reason_keys
    # choose_strategy takes ``available`` as given (it knows no engine registry); the
    # stack-only engines are filtered out one level up, in api.resolve_plan (ADR-0045).
    other = choose_strategy((1000, 1000), 2, machine_64gb_10c, UnwrapCfg(), available=["spurt"])
    assert other.method == "spurt" and R + "method_first_available" in other.reason_keys


def test_unknown_memory_budget_means_single_tile():
    plan = choose_strategy((30000, 30000), 4, make_machine(4, 0.0), UnwrapCfg())
    assert (plan.rows, plan.cols) == (1, 1)
    assert plan.n_parallel == 4


def test_invalid_shape_raises(machine_64gb_10c):
    with pytest.raises(ValueError):
        choose_strategy((0, 10), 1, machine_64gb_10c, UnwrapCfg())


# ------------------------------------------------------------------ grid helper


@pytest.mark.parametrize(
    ("ntiles", "shape", "expected"),
    [
        (1, (100, 100), (1, 1)),
        (2, (1000, 4000), (1, 2)),
        (14, (100, 100), (4, 4)),
        (6, (300, 100), (4, 2)),
    ],
)
def test_near_square_grid(ntiles, shape, expected):
    rows, cols = near_square_grid(ntiles, shape)
    assert (rows, cols) == expected
    assert rows * cols >= ntiles


def test_near_square_grid_never_exceeds_dimensions():
    rows, cols = near_square_grid(50, (3, 3))
    assert rows <= 3 and cols <= 3


# ------------------------------------------------------------------ fringe density


def test_fringe_density_steep_ramp_vs_flat():
    ny, nx = 64, 64
    x = np.arange(nx, dtype=np.float64)[None, :]
    steep = synth.wrap(np.broadcast_to(2.5 * x, (ny, nx)).copy())  # 2.5 rad/px along x
    flat = np.zeros((ny, nx))
    gentle = synth.wrap(np.broadcast_to(0.1 * x, (ny, nx)).copy())
    f_steep, f_flat, f_gentle = fringe_density(steep), fringe_density(flat), fringe_density(gentle)
    assert f_flat == 0.0
    assert f_steep == pytest.approx((2.5 / 2.0) / np.pi, rel=1e-6)  # mean of x- and y-gradients
    assert f_gentle < FRINGE_HIGH < f_steep
    # complex input gives the same answer; NaN / mask are ignored
    assert fringe_density(np.exp(1j * steep)) == pytest.approx(f_steep, rel=1e-6)
    masked = np.ones((ny, nx), dtype=bool)
    assert np.isnan(fringe_density(steep, masked))
    half = np.zeros((ny, nx), dtype=bool)
    half[:, : nx // 2] = True
    assert fringe_density(steep, half) == pytest.approx(f_steep, abs=0.02)


def test_fringe_density_noise_is_about_half(rng):
    noise = rng.uniform(-np.pi, np.pi, (256, 256))
    assert fringe_density(noise) == pytest.approx(0.5, abs=0.03)


def test_fringe_density_rejects_non_2d():
    with pytest.raises(ValueError):
        fringe_density(np.zeros(10))


# ------------------------------------------------------------------ plan object


def test_plan_to_dict_and_explain_both_languages(machine_64gb_10c):
    plan = choose_strategy((30000, 30000), 20, machine_64gb_10c, UnwrapCfg(), fringe=0.3)
    d = plan.to_dict()
    assert d["n_tiles"] == plan.rows * plan.cols
    assert isinstance(d["tile_shape"], list)
    for lang in ("ko", "en"):
        lines = plan.explain(lang)
        assert len(lines) == len(plan.reason_keys)
        for line in lines:
            assert "{" not in line and "}" not in line, line
            assert not line.startswith(REASON_PREFIX), line


def test_all_reason_keys_used_by_scheduler_exist_in_catalogs():
    import inspect

    src = inspect.getsource(scheduler)
    import re

    used = {R + k for k in re.findall(r'_reason\("([a-z_]+)"\)', src)}
    assert used, "no reason keys found in scheduler source"
    ko, en = load_catalog("ko"), load_catalog("en")
    missing = [k for k in used if k not in ko or k not in en]
    assert not missing, missing


def test_plan_dataclass_defaults():
    p = UnwrapPlan("snaphu", 1, 1, 0, 1, 1, 10.0)
    assert not p.tiled and p.n_tiles == 1 and p.reason_keys == []


def test_fringe_density_pools_both_axes_so_one_directional_fringes_score_half():
    """The documented scale (ADR-0045): the score is the mean over x *and* y neighbours.

    A fringe pattern that runs in one direction only therefore reaches FRINGE_HIGH at
    π/2 rad/px, not at π/4 rad/px — the docstring of ``fringe_density`` says so.
    """
    ny, nx = 64, 64
    x = np.arange(nx, dtype=np.float64)[None, :]
    y = np.arange(ny, dtype=np.float64)[:, None]
    one_d = synth.wrap(np.broadcast_to(np.pi / 4 * x, (ny, nx)).copy())
    both = synth.wrap(np.pi / 4 * (x + y))
    assert fringe_density(one_d) == pytest.approx(0.125, abs=1e-3)
    assert fringe_density(both) == pytest.approx(FRINGE_HIGH, abs=1e-3)
    one_d_steeper = synth.wrap(np.broadcast_to(np.pi / 2 * x, (ny, nx)).copy())
    assert fringe_density(one_d_steeper) == pytest.approx(FRINGE_HIGH, abs=1e-3)
    aliased = synth.wrap(np.pi * (x + y))
    assert fringe_density(aliased) == pytest.approx(1.0, abs=1e-3)
