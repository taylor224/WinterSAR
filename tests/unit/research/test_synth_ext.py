"""Phase 6 extensions of research/synth.py (plan §5.7, R-07): sources, DEM error, SHP
amplitude regions, layover ridge, tiled truth, SLC stack with a known covariance."""

from __future__ import annotations

import numpy as np
import pytest

from wintersar.research import synth


def test_deformation_sources_sum_and_okada_hook():
    single = synth.deformation_field((32, 32), "gaussian", -0.02)
    ramp = synth.deformation_field((32, 32), "linear", ramp=(0.0, 0.01))
    both = synth.deformation_sources(
        (32, 32),
        [{"kind": "gaussian", "amplitude_m": -0.02}, {"kind": "linear", "ramp": (0.0, 0.01)}],
    )
    np.testing.assert_allclose(both, single + ramp)
    np.testing.assert_array_equal(synth.deformation_sources((4, 4), []), 0.0)
    with pytest.raises(NotImplementedError, match="okada"):
        synth.deformation_sources((8, 8), [{"kind": "okada", "strike_deg": 0.0}])


def test_dem_error_phase_is_proportional_to_bperp(rng):
    dh = synth.dem_error_height((16, 16), rng, std_m=5.0)
    assert abs(dh.std() - 5.0) < 1e-6
    p100 = synth.dem_error_phase(dh, 100.0)
    p200 = synth.dem_error_phase(dh, 200.0)
    np.testing.assert_allclose(p200, 2.0 * p100)
    # sign follows PHASE_PER_M_LOS (negative): positive Bperp and positive dh -> negative phase
    assert synth.height_to_phase(100.0) < 0
    # order of magnitude: |dphi/dh| = 4pi/lambda * B/(R sin theta) ~ 0.04 rad/m at B=100 m
    assert 0.02 < abs(synth.height_to_phase(100.0)) < 0.08
    assert synth.dem_error_height((8, 8), rng, std_m=0.0).any() is np.False_


def test_shp_amplitude_regions_have_distinct_statistics(rng):
    amp, labels = synth.shp_amplitude_stack((16, 16), 40, rng, region_scales=(1.0, 3.0))
    assert amp.shape == (40, 16, 16) and labels.shape == (16, 16)
    left = amp[:, labels == 0].mean()
    right = amp[:, labels == 1].mean()
    assert right / left > 2.5  # Rayleigh mean scales with the scale parameter
    for kind, n in (("quadrants", 4), ("stripes", 4), ("blobs", 4)):
        lab = synth.shp_regions((32, 32), kind, rng)
        assert lab.max() == n - 1
    with pytest.raises(ValueError, match="region_scales"):
        synth.shp_amplitude_stack((8, 8), 5, rng, region_scales=(1.0,), region_kind="quadrants")


def test_layover_mask_on_sensor_facing_flank_of_ridge():
    shape = (4, 64)
    mask, dem = synth.layover_mask_from_ridge(shape, height_m=400.0, sigma_px=8.0, col=32.0)
    assert dem.shape == shape and mask.shape == shape
    cols = np.nonzero(mask[0])[0]
    assert cols.size > 0
    # sensor on the left: the flank whose downslope points to the sensor (x < ridge) lays over
    assert cols.max() < 32
    # a gentle ridge does not lay over at 39 deg incidence
    gentle, _ = synth.layover_mask_from_ridge(shape, height_m=50.0, sigma_px=16.0)
    assert not gentle.any()
    right = synth.layover_mask_from_dem(dem, sensor_side="right")
    assert np.nonzero(right[0])[0].min() > 32


def test_make_tiled_truth_geometry_and_offsets(rng):
    tt = synth.make_tiled_truth((64, 64), 2, 2, 8, rng, offsets_cycles=[0, 2, -1, 3])
    assert len(tt.tiles) == 4 and tt.offsets_cycles == [0, 2, -1, 3]
    for (arr, sy, sx), k in zip(tt.tiles, tt.offsets_cycles, strict=True):
        assert arr.shape == (sy.stop - sy.start, sx.stop - sx.start)
        np.testing.assert_allclose(arr - 2 * np.pi * k, tt.unw_true[sy, sx], atol=1e-9)
    # extents of neighbours share exactly `overlap` pixels
    _, sy0, sx0 = tt.tiles[0]
    _, sy1, sx1 = tt.tiles[1]
    assert sy0 == sy1 and sx0.stop - sx1.start == 8
    auto = synth.make_tiled_truth((64, 64), 2, 2, 8, rng, max_offset_cycles=2)
    assert auto.offsets_cycles[0] == 0 and max(abs(k) for k in auto.offsets_cycles) <= 2
    with pytest.raises(ValueError, match="offsets"):
        synth.make_tiled_truth((64, 64), 2, 2, 8, rng, offsets_cycles=[0, 1])


def test_slc_stack_has_the_requested_covariance(rng):
    n = 6
    gamma = synth.coherence_matrix_model(n, tau_dates=3.0, floor=0.2)
    st = synth.make_slc_stack(n, (64, 64), rng, gamma=gamma, amplitude=2.0)
    assert st.slc.shape == (n, 64, 64) and np.all(st.phase_true[0] == 0)
    s = st.slc.reshape(n, -1)
    cov = (s @ s.conj().T) / s.shape[1]
    # the phase differs per pixel, so compare the magnitude of the *normalised* per-pixel model
    d = np.sqrt(np.real(np.diag(cov)))
    np.testing.assert_allclose(d, 2.0, rtol=0.05)
    # remove the deterministic phase before checking the coherence magnitudes
    s0 = s * np.exp(-1j * st.phase_true.reshape(n, -1))
    coh = (s0 @ s0.conj().T) / s.shape[1] / np.outer(d, d)
    np.testing.assert_allclose(np.abs(coh), gamma, atol=0.05)
    assert synth.coherence_matrix_model(4, model="constant", floor=0.3)[0, 1] == 0.3
    with pytest.raises(ValueError, match="gamma"):
        synth.make_slc_stack(3, (4, 4), rng, gamma=np.eye(2))
