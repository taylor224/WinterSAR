"""Reference-point recommendation (R-09, ADR-0041) and re-referencing."""

from __future__ import annotations

import json

import numpy as np
import pytest

from wintersar.io.timeseries import TimeSeries
from wintersar.validate.refpoint import (
    COMPONENTS,
    DEFAULT_WEIGHTS,
    aoi_majority_conncomp,
    aoi_mask_from_geojson,
    apply_reference,
    compare_with_mintpy_auto,
    normalise_weights,
    recommend,
    score_components,
    score_map,
)


def _controlled_ts(
    synth_ts: TimeSeries,
) -> tuple[TimeSeries, tuple[int, int], tuple[int, int], tuple[int, int]]:
    """Uniform background with one ideal pixel and two decoys."""
    rng = np.random.default_rng(7)
    ny, nx = synth_ts.shape
    coh = np.full((ny, nx), 0.6)
    dem = 100.0 + rng.normal(0.0, 30.0, (ny, nx))
    conncomp = np.ones((ny, nx), dtype=np.int16)
    conncomp[:, : nx // 5] = 2
    vel = synth_ts.velocity_m_per_yr.copy()
    ideal = (10, 16)  # near the AOI, same component, stable, similar elevation
    coh[ideal] = 0.95
    dem[ideal] = 100.0
    vel[ideal] = 0.0
    decoy_cc = (10, 2)  # high coherence but inside component 2
    coh[decoy_cc] = 0.99
    dem[decoy_cc] = 100.0
    vel[decoy_cc] = 0.0
    decoy_vel = (22, 16)  # high coherence but moving fast
    coh[decoy_vel] = 0.99
    dem[decoy_vel] = 100.0
    vel[decoy_vel] = -0.2
    ts = TimeSeries(
        dates=synth_ts.dates,
        displacement_m=synth_ts.displacement_m,
        lat=synth_ts.lat,
        lon=synth_ts.lon,
        incidence_deg=39.0,
        heading_deg=192.0,
        coherence=coh,
        velocity_m_per_yr=vel,
        dem_m=dem,
        conncomp=conncomp,
    )
    return ts, ideal, decoy_cc, decoy_vel


def _aoi(shape: tuple[int, int]) -> np.ndarray:
    m = np.zeros(shape, dtype=bool)
    m[12:21, 12:21] = True
    return m


def test_recommend_ranks_ideal_pixel_first(synth_ts):
    ts, ideal, decoy_cc, decoy_vel = _controlled_ts(synth_ts)
    cands = recommend(ts, _aoi(ts.shape), top_k=5)
    assert len(cands) == 5
    top = cands[0]
    assert (top.row, top.col) == ideal
    assert set(top.components) == set(COMPONENTS)
    assert top.components["coherence"] == pytest.approx(0.95)
    assert top.components["conncomp"] == 1.0 and top.components["velocity"] == pytest.approx(1.0)
    assert 0 < top.score <= 1
    assert top.coherence == pytest.approx(0.95)
    ranks = {(c.row, c.col): i for i, c in enumerate(cands)}
    assert decoy_cc not in ranks or ranks[decoy_cc] > 0
    assert decoy_vel not in ranks or ranks[decoy_vel] > 0
    # candidates are spatially distinct (min_separation_px = 5)
    for i, a in enumerate(cands):
        for b in cands[i + 1 :]:
            assert max(abs(a.row - b.row), abs(a.col - b.col)) >= 5
    assert cands == sorted(cands, key=lambda c: -c.score)


def test_score_components_and_majority(synth_ts):
    ts, ideal, decoy_cc, _ = _controlled_ts(synth_ts)
    comps, valid, aoi = score_components(ts, _aoi(ts.shape))
    assert set(comps) == set(COMPONENTS)
    for m in comps.values():
        assert np.nanmin(m) >= 0.0 and np.nanmax(m) <= 1.0
    assert comps["conncomp"][decoy_cc] == 0.0 and comps["conncomp"][ideal] == 1.0
    assert aoi_majority_conncomp(ts.conncomp, aoi) == 1
    assert comps["distance"][16, 16] == pytest.approx(1.0)  # AOI centroid
    assert valid[ideal]
    # score map is NaN outside valid pixels and weights are renormalised
    score, _, w = score_map(ts, _aoi(ts.shape), {"coherence": 1.0, "velocity": 1.0})
    assert w == {"coherence": 0.5, "velocity": 0.5}
    assert np.isfinite(score[ideal])


def test_weights_validation_and_missing_components(synth_ts):
    with pytest.raises(ValueError, match="unknown"):
        normalise_weights({"nope": 1.0}, list(COMPONENTS))
    with pytest.raises(ValueError, match="no usable"):
        normalise_weights({"coherence": 0.0}, ["coherence"])
    assert sum(normalise_weights(None, list(COMPONENTS)).values()) == pytest.approx(1.0)
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)
    # no coherence / dem / conncomp / velocity: only the distance term remains, still works
    bare = TimeSeries(
        dates=synth_ts.dates,
        displacement_m=synth_ts.displacement_m,
        lat=synth_ts.lat,
        lon=synth_ts.lon,
    )
    cands = recommend(bare, top_k=2)
    assert len(cands) == 2 and set(cands[0].components) == {"distance"}
    assert cands[0].coherence is None


def test_compare_with_mintpy_auto(synth_ts):
    ts, _ideal, decoy_cc, decoy_vel = _controlled_ts(synth_ts)
    cands = recommend(ts, _aoi(ts.shape), top_k=3)
    cmp = compare_with_mintpy_auto(ts, threshold=0.85, candidates=cands)
    assert cmp["n_candidates_mintpy"] == 3  # 0.95 + two 0.99 pixels
    assert cmp["threshold"] == 0.85
    assert cmp["top1_in_mintpy_set"] is True and cmp["ours"][0]["rank"] == 1
    assert (cmp["argmax"]["row"], cmp["argmax"]["col"]) in (decoy_cc, decoy_vel)
    assert cmp["argmax_is_our_top1"] is False
    assert "random" in cmp["rule"]
    json.dumps(cmp)
    # mask=0 pixels are excluded from MintPy's set (maskConnComp semantics)
    mask = (ts.conncomp == 1).astype(np.uint8)
    cmp2 = compare_with_mintpy_auto(ts, threshold=0.85, mask=mask, candidates=cands)
    assert cmp2["n_candidates_mintpy"] == 2
    with pytest.raises(ValueError, match="coherence"):
        compare_with_mintpy_auto(
            TimeSeries(dates=ts.dates, displacement_m=ts.displacement_m, lat=ts.lat, lon=ts.lon)
        )


def test_apply_reference_zeroes_reference_pixel(synth_ts):
    row, col = 5, 7
    ref = apply_reference(synth_ts, row, col)
    assert np.allclose(ref.displacement_m[:, row, col], 0.0)
    expected = synth_ts.displacement_m - synth_ts.displacement_m[:, row, col][:, None, None]
    np.testing.assert_allclose(ref.displacement_m, expected)
    assert ref.velocity_m_per_yr is not None and ref.velocity_m_per_yr[row, col] == 0.0
    assert ref.attrs["REF_Y"] == "5" and ref.attrs["REF_X"] == "7"
    assert ref.reference_latlon == (
        float(synth_ts.lat2d()[row, col]),
        float(synth_ts.lon2d()[row, col]),
    )
    assert ref.dates == synth_ts.dates and ref.shape == synth_ts.shape
    with pytest.raises(IndexError):
        apply_reference(synth_ts, 999, 0)
    nan_ts = TimeSeries(
        dates=synth_ts.dates,
        displacement_m=synth_ts.displacement_m.copy(),
        lat=synth_ts.lat,
        lon=synth_ts.lon,
    )
    nan_ts.displacement_m[0, 1, 1] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        apply_reference(nan_ts, 1, 1)


def test_aoi_mask_from_geojson(tmp_path, synth_ts):
    lat2, lon2 = synth_ts.lat2d(), synth_ts.lon2d()
    # polygon covering rows 10..20 (lat decreases with row) and cols 5..15
    lat_n, lat_s = float(lat2[10, 0]), float(lat2[20, 0])
    lon_w, lon_e = float(lon2[0, 5]), float(lon2[0, 15])
    poly = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [lon_w, lat_s],
                            [lon_e, lat_s],
                            [lon_e, lat_n],
                            [lon_w, lat_n],
                            [lon_w, lat_s],
                        ]
                    ],
                },
            }
        ],
    }
    p = tmp_path / "aoi.geojson"
    p.write_text(json.dumps(poly), encoding="utf-8")
    m = aoi_mask_from_geojson(p, synth_ts)
    assert m.shape == synth_ts.shape and m.dtype == bool
    assert m[15, 10] and not m[2, 2] and not m[15, 30]
    rows, cols = np.nonzero(m)
    assert rows.min() >= 10 and rows.max() <= 20 and cols.min() >= 5 and cols.max() <= 15
    # bare geometry and empty collection
    p2 = tmp_path / "geom.json"
    p2.write_text(json.dumps(poly["features"][0]["geometry"]), encoding="utf-8")
    assert aoi_mask_from_geojson(p2, synth_ts).sum() == m.sum()
    p3 = tmp_path / "empty.json"
    p3.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    assert not aoi_mask_from_geojson(p3, synth_ts).any()
