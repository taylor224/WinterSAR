"""research/metrics.py — unwrap error fraction, seam jumps, closure RMS, offsets, timer."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from wintersar.io.timeseries import TimeSeries
from wintersar.research import metrics as mt
from wintersar.research import synth
from wintersar.research.repr_phase import truth_lowres_phase
from wintersar.research.stitching import stitch
from wintersar.validate.closure import closure_from_arrays


def test_phase_rmse_and_mae_wrap_and_mask():
    truth = np.zeros((4, 4))
    est = np.exp(1j * (truth + 0.1))
    assert abs(mt.phase_rmse(est, truth) - 0.1) < 1e-9
    assert abs(mt.phase_mae(est, truth) - 0.1) < 1e-9
    # a 2π difference is no error, a π+ε difference wraps to -π+ε
    assert mt.phase_rmse(np.exp(1j * (truth + 2 * np.pi)), truth) < 1e-9
    est2 = np.exp(1j * (truth + 0.5))
    assert abs(mt.phase_rmse(est2, truth, remove_bias=True)) < 1e-9
    mask = np.zeros((4, 4), bool)
    mask[0] = True
    est3 = est.copy()
    est3[0] = np.exp(1j * 3.0)
    assert abs(mt.phase_rmse(est3, truth, mask) - 0.1) < 1e-9
    assert np.isnan(mt.phase_rmse(np.zeros((2, 2), complex), np.zeros((2, 2))))
    with pytest.raises(ValueError, match="shape"):
        mt.phase_rmse(est, np.zeros((2, 2)))
    assert mt.rmse(np.array([1.0, 2.0]), np.array([1.0, 4.0])) == pytest.approx(np.sqrt(2.0))


def test_unwrap_error_fraction_and_offsets():
    truth = np.linspace(0, 20, 100).reshape(10, 10)
    bad = truth.copy()
    bad[:2] += 2 * np.pi
    assert mt.unwrap_error_fraction(truth + 5.0, truth) == 0.0  # constant offset ignored
    assert mt.unwrap_error_fraction(bad, truth) == pytest.approx(0.2)
    rec = mt.offsets_recovered([1, 2, 0], [0, 1, -1])
    assert rec["offsets_exact"] == 1.0 and rec["n_wrong_offsets"] == 0
    rec = mt.offsets_recovered([1, 2, 0], [0, 1, -1], relative=False)
    assert rec["offsets_exact"] == 0.0 and rec["n_wrong_offsets"] == 3
    with pytest.raises(ValueError, match="offsets"):
        mt.offsets_recovered([0], [0, 1])


def test_seam_jumps_counts_unstitched_tiles(tiled_truth):
    tt = tiled_truth
    # naive core merge without offset removal -> seams between tiles with different k
    merged, _, _ = stitch(tt.tiles, tt.shape, "overlap_consensus", coh=tt.coherence)
    clean = mt.seam_jumps(merged, tt.slices, tt.shape)
    assert clean["n_boundaries"] == 12 and clean["n_boundaries_with_jump"] == 0
    from wintersar.research.stitching import tiles_from_slices

    raw = np.full(tt.shape, np.nan)
    for (arr, _, _), box in zip(tt.tiles, tiles_from_slices(tt.slices, tt.shape), strict=True):
        raw[box.core] = arr[box.core_in_tile]  # core merge without offset removal
    assert len(set(tt.offsets_cycles)) > 1
    jumps = mt.seam_jumps(raw, tt.slices, tt.shape)
    assert jumps["n_boundaries_with_jump"] > 0 and jumps["n_jump_pixels"] > 0


def test_closure_rms_on_synthetic_stack(rng):
    st = synth.make_stack(n_dates=5, shape=(12, 12), rng=rng, atmosphere_std_rad=0.4)
    keys = [p.key for p in st.pairs]
    unw = np.stack([st.igrams[k].unw_true for k in keys])
    res = mt.closure_rms(unw, keys)
    assert res["n_triplets"] > 0 and res["closure_rms_rad"] < 1e-6
    assert res["nonzero_integer_fraction"] == 0.0
    bad = unw.copy()
    bad[0] += 2 * np.pi
    res2 = mt.closure_rms(bad, keys)
    assert res2["closure_rms_rad"] > 1.0 and res2["nonzero_integer_fraction"] > 0
    wrapped = np.stack([st.igrams[k].wrapped for k in keys])
    res3 = mt.closure_rms(wrapped, keys, wrapped=True)
    assert 0.0 <= res3["closure_rms_rad"] < np.pi
    # one implementation only: closure_rms is the shared wintersar.validate.closure path
    tri, closure, valid = closure_from_arrays(bad, keys, None, wrapped=False)
    assert len(tri) == res2["n_triplets"]
    vals = np.asarray(closure[valid], dtype=np.float64)
    assert abs(np.sqrt(np.mean(vals**2)) - res2["closure_rms_rad"]) < 1e-9


def test_ground_truth_hook_returns_rmse():
    from wintersar.io.schemas import GroundTruthRecord

    dates = [date(2024, 1, 1), date(2024, 1, 13), date(2024, 1, 25)]
    disp = np.zeros((3, 4, 4))
    disp[1] = -0.01
    disp[2] = -0.02
    ts = TimeSeries(
        dates=dates,
        displacement_m=disp,
        lat=np.linspace(37.5, 37.53, 4),
        lon=np.linspace(127.0, 127.03, 4),
        incidence_deg=39.0,
        heading_deg=192.0,
    )
    gt = [
        GroundTruthRecord(site_id="A", lat=37.51, lon=127.01, date=d, up_m=v, method="leveling")
        for d, v in zip(dates, (0.0, -0.013, -0.026), strict=True)
    ]
    res = mt.ground_truth_rmse(ts, gt)
    assert res["available"] is True
    assert np.isfinite(res["rmse_m"]) and res["n_points"] >= 2


def test_resource_timer_measures_wall_and_rss():
    with mt.ResourceTimer() as rt:
        _ = np.zeros((256, 256)).sum()
        rt.sample()
    m = rt.result
    assert m.wall_s >= 0.0 and m.cpu_s >= 0.0
    assert m.peak_rss_mb > 0 and m.samples == 3
    d = m.to_dict()
    assert set(d) >= {"wall_s", "cpu_s", "peak_rss_mb", "rss_start_mb", "rss_end_mb"}
    assert rt.wall_s == m.wall_s and rt.peak_rss_mb == m.peak_rss_mb
    assert set(mt.METRIC_NAMES) >= {"phase_rmse_rad", "offsets_exact", "wall_s", "peak_rss_mb"}


def test_truth_lowres_phase_is_block_mean():
    a = np.arange(36, dtype=float).reshape(6, 6)
    lo = truth_lowres_phase(a, 3)
    assert lo.shape == (2, 2) and lo[0, 0] == a[:3, :3].mean()
