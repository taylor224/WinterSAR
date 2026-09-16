"""SEL-06 / ADR-0011: perpendicular baselines from the asf_search stack API and from orbits."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import make_burst
from tests.unit.select_search.conftest import FIXTURE_DIR, FakeProduct
from wintersar.io.schemas import Finding, Pair
from wintersar.select import baseline as bl
from wintersar.select import metadata as md

T0 = datetime(2024, 1, 7, 9, 32, 30, tzinfo=UTC)


# ---------------------------------------------------------------- analytic geometry


def _enu_basis(lat_deg: float, lon_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    east = np.array([-math.sin(lon), math.cos(lon), 0.0])
    north = np.array(
        [-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)]
    )
    up = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    return east, north, up


def _straight_orbit(
    p0: np.ndarray, v: np.ndarray, n: int = 5, dt: float = 10.0
) -> list[bl.StateVector]:
    """Straight-line 'orbit' (exactly representable by cubic Hermite)."""
    return [
        bl.StateVector(
            time=T0 + timedelta(seconds=(i - n // 2) * dt),
            position=tuple(p0 + v * (i - n // 2) * dt),
            velocity=tuple(v),
        )
        for i in range(n)
    ]


@pytest.mark.parametrize(("lat", "lon"), [(0.0, 0.0), (37.55, 126.97), (-33.9, 151.2)])
def test_perpendicular_baseline_analytic_geometry(lat: float, lon: float) -> None:
    """Reference flies north at altitude H, offset D west of the target (looks east-down).

    In the local (east, north, up) frame: L = (-D, 0, H)/rho, v = north, n = v x L = (H, 0, D)/rho.
    A secondary displaced by B = (bx, 0, bz) has B_perp = (bx*H + bz*D)/rho and B_par = (-bx*D + bz*H)/rho.
    """
    east, north, up = _enu_basis(lat, lon)
    target = bl.llh_to_ecef(lat, lon, 0.0)
    height, offset, speed = 700e3, 525e3, 7500.0  # rho = 875 km
    rho = math.hypot(height, offset)
    p_ref = target + up * height - east * offset
    v = north * speed
    ref = _straight_orbit(p_ref, v)

    # (a) B along n -> B_perp = |B|
    normal = (east * height + up * offset) / rho
    sec = _straight_orbit(p_ref + 100.0 * normal, v)
    assert bl.perpendicular_baseline_from_state_vectors(ref, sec, (lat, lon, 0.0)) == pytest.approx(
        100.0, abs=1e-3
    )
    # (b) B along the look vector -> B_perp = 0
    look = (-east * offset + up * height) / rho
    sec = _straight_orbit(p_ref + 100.0 * look, v)
    assert bl.perpendicular_baseline_from_state_vectors(ref, sec, (lat, lon, 0.0)) == pytest.approx(
        0.0, abs=1e-3
    )
    # (c) 100 m east: B_perp = 100*H/rho = 80, B_par = -100*D/rho = -60 (3-4-5 triangle)
    sec = _straight_orbit(p_ref + 100.0 * east, v)
    comps = bl.baseline_components(ref, sec, (lat, lon, 0.0))
    assert comps.perpendicular_m == pytest.approx(80.0, abs=1e-3)
    assert comps.parallel_m == pytest.approx(-60.0, abs=1e-3)
    assert comps.along_track_m == pytest.approx(0.0, abs=1e-3)
    assert comps.total_m == pytest.approx(100.0, abs=1e-3)
    # (d) sign flips with the side of the reference
    sec = _straight_orbit(p_ref - 100.0 * east, v)
    assert bl.perpendicular_baseline_from_state_vectors(ref, sec, (lat, lon, 0.0)) == pytest.approx(
        -80.0, abs=1e-3
    )
    # (e) a pure along-track shift changes nothing (zero-Doppler alignment)
    sec = _straight_orbit(p_ref + 100.0 * east + 3000.0 * north, v)
    assert bl.perpendicular_baseline_from_state_vectors(ref, sec, (lat, lon, 0.0)) == pytest.approx(
        80.0, abs=1e-3
    )


def test_llh_to_ecef_known_points() -> None:
    assert bl.llh_to_ecef(0.0, 0.0) == pytest.approx([bl.WGS84_A, 0.0, 0.0])
    assert bl.llh_to_ecef(90.0, 0.0)[2] == pytest.approx(bl.WGS84_A * (1 - bl.WGS84_F), rel=1e-9)
    assert np.linalg.norm(bl.llh_to_ecef(0.0, 90.0, 100.0)) == pytest.approx(bl.WGS84_A + 100.0)


def test_interpolate_state_hermite_accuracy_on_circular_orbit() -> None:
    """10 s Hermite gap on a 7 km/s circular orbit: sub-cm error at the midpoint."""
    r, omega = 7.07e6, 7.5e3 / 7.07e6

    def sv(t: float) -> bl.StateVector:
        return bl.StateVector(
            time=T0 + timedelta(seconds=t),
            position=(r * math.cos(omega * t), r * math.sin(omega * t), 0.0),
            velocity=(-r * omega * math.sin(omega * t), r * omega * math.cos(omega * t), 0.0),
        )

    pos, vel = bl.interpolate_state([sv(0.0), sv(10.0)], T0 + timedelta(seconds=5.0))
    assert np.linalg.norm(pos - sv(5.0).pos) < 0.02
    assert np.linalg.norm(vel - sv(5.0).vel) < 0.01
    with pytest.raises(ValueError):
        bl.interpolate_state([sv(0.0)], T0)


def test_zero_doppler_time_bracket_expansion() -> None:
    east, north, up = _enu_basis(10.0, 20.0)
    target = bl.llh_to_ecef(10.0, 20.0)
    p_ref = target + up * 700e3 - east * 500e3 - north * 40e3  # broadside 40 km (5.3 s) after T0
    ref = _straight_orbit(p_ref, north * 7500.0, n=2, dt=1.0)  # 1 s arc, root well outside it
    t_zd = bl.zero_doppler_time(ref, target)
    assert (t_zd - T0).total_seconds() == pytest.approx(40e3 / 7500.0, abs=1e-3)
    with pytest.raises(ValueError):
        bl.zero_doppler_time(ref, target, max_window_s=2.0)


# ---------------------------------------------------------------- orbit files


def test_parse_eof_orbit_and_name() -> None:
    eof = (
        FIXTURE_DIR
        / "S1A_OPER_AUX_POEORB_OPOD_20240127T070649_V20240106T225942_20240108T005942.EOF"
    )
    svs = bl.parse_eof_orbit(eof)
    assert len(svs) == 4
    assert svs[1].time == datetime(2024, 1, 7, 9, 32, 32, tzinfo=UTC)
    assert svs[1].position == pytest.approx((-2946718.046583, 4874657.329086, 4189362.577055))
    assert svs[2].velocity == pytest.approx((3789.137459, -2913.782518, 5900.767262))
    info = bl.parse_eof_name(eof)
    assert info is not None and info["mission"] == "S1A" and info["orbit_type"] == "POEORB"
    assert info["start"] == datetime(2024, 1, 6, 22, 59, 42, tzinfo=UTC)
    assert bl.parse_eof_name(Path("random.EOF")) is None


def test_find_orbit_file_prefers_poeorb_and_validity(tmp_path: Path) -> None:
    names = [
        "S1A_OPER_AUX_RESORB_OPOD_20240107T120000_V20240107T090000_20240107T120000.EOF",
        "S1A_OPER_AUX_POEORB_OPOD_20240127T070649_V20240106T225942_20240108T005942.EOF",
        "S1A_OPER_AUX_POEORB_OPOD_20240128T070649_V20240106T225942_20240108T005942.EOF",  # newer creation
        "S1B_OPER_AUX_POEORB_OPOD_20240127T070649_V20240106T225942_20240108T005942.EOF",
        "S1A_OPER_AUX_POEORB_OPOD_20240107T070649_V20231231T225942_20240102T005942.EOF",  # outside
    ]
    sub = tmp_path / "orbits" / "2024"
    sub.mkdir(parents=True)
    for n in names:
        (sub / n).write_text("<x/>", encoding="utf-8")
    best = bl.find_orbit_file(tmp_path, "S1A", datetime(2024, 1, 7, 9, 32, 34))
    assert best is not None and best.name.startswith("S1A_OPER_AUX_POEORB_OPOD_20240128")
    assert bl.find_orbit_file(tmp_path, "S1C", datetime(2024, 1, 7, 9, 32, 34)) is None
    assert bl.find_orbit_file(tmp_path, "S1A", datetime(2024, 3, 1)) is None


# ---------------------------------------------------------------- CMR state vectors vs stack API


def _records(products: list[FakeProduct]):
    return md.records_from_asf(products)


def test_orbit_method_matches_stack_api_on_real_fixture(
    burst_products: list[FakeProduct], stack_expected, stack_reference_id: str
) -> None:
    """Self-computation (CMR state vectors, zero-Doppler, Hermite) vs asf_search stack values.

    asf_search rounds to integer metres; agreement within 1 m on all 10 dates (measured
    max |diff| = 0.4 m on 2026-09-16) is the validation recorded in ADR-0011.
    """
    recs = _records(burst_products)
    ref = next(r for r in recs if r.granule_id == stack_reference_id)
    stack_recs = [r for r in recs if r.granule_id in stack_expected]
    values = bl.perpendicular_baselines_orbit(stack_recs, ref)
    assert values[ref.granule_id] == 0.0
    diffs = []
    for r in stack_recs:
        expected = stack_expected[r.granule_id]["perpendicularBaseline"]
        assert values[r.granule_id] is not None
        diffs.append(abs(values[r.granule_id] - expected))
    assert max(diffs) < 1.0
    assert values["S1_270859_IW2_20231108T093236_VV_F8E9-BURST"] == pytest.approx(155, abs=1.0)
    assert values["S1_270859_IW2_20240119T093233_VV_938E-BURST"] == pytest.approx(-15, abs=1.0)


def test_orbit_method_with_eof_file(
    burst_products: list[FakeProduct], stack_reference_id: str, tmp_path: Path
) -> None:
    """When an EOF covers the reference date it is used (source 'POEORB'); other dates fall back to CMR."""
    recs = _records(burst_products)
    ref = next(r for r in recs if r.granule_id == stack_reference_id)
    orbit_dir = tmp_path / "orbits"
    orbit_dir.mkdir()
    src = (
        FIXTURE_DIR
        / "S1A_OPER_AUX_POEORB_OPOD_20240127T070649_V20240106T225942_20240108T005942.EOF"
    )
    (orbit_dir / src.name).write_bytes(src.read_bytes())
    used = bl.orbit_for_record(ref, orbit_dir)
    assert used is not None and used[1] == "POEORB" and len(used[0]) == 4
    other = next(r for r in recs if r.acquisition_date == date(2024, 1, 19))
    assert bl.orbit_for_record(other, orbit_dir)[1] == "cmr"
    v_eof = bl.perpendicular_baselines_orbit([other], ref, orbit_dir=orbit_dir)[other.granule_id]
    v_cmr = bl.perpendicular_baselines_orbit([other], ref)[other.granule_id]
    # the fixture EOF embeds the same two CMR vectors around the burst -> same answer
    assert v_eof == pytest.approx(v_cmr, abs=0.05)


def test_state_vectors_from_record_roundtrip(burst_products: list[FakeProduct]) -> None:
    rec = md.burst_record_from_asf(burst_products[0])
    svs = bl.state_vectors_from_record(rec)
    assert svs is not None and len(svs) == 2 and svs[0].time < svs[1].time
    assert bl.state_vectors_from_record(make_burst("2024-01-01")) is None
    assert bl.orbit_for_record(make_burst("2024-01-01")) is None


def test_record_target_llh(burst_products: list[FakeProduct]) -> None:
    rec = md.burst_record_from_asf(burst_products[0])
    lat, lon, h = bl.record_target_llh(rec)
    assert (lat, lon, h) == pytest.approx((rec.extra["center_lat"], rec.extra["center_lon"], 0.0))
    lat2, lon2, _ = bl.record_target_llh(make_burst("2024-01-01"))
    assert (lat2, lon2) == pytest.approx((37.55, 126.95))


# ---------------------------------------------------------------- stack API wrapper


def test_perpendicular_baselines_asf(
    burst_products: list[FakeProduct],
    fake_stack,
    stack_reference_id: str,
    stack_expected,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asf_search

    seen: dict[str, object] = {}

    def _stack_from_id(reference_id: str, opts=None, useSubclass=None):
        seen["reference_id"] = reference_id
        seen["opts"] = dict(opts) if opts is not None else {}
        return fake_stack()

    monkeypatch.setattr(asf_search, "stack_from_id", _stack_from_id)
    recs = _records(burst_products)
    ref = next(r for r in recs if r.granule_id == stack_reference_id)
    values = bl.perpendicular_baselines_asf(recs, ref)
    assert seen["reference_id"] == stack_reference_id
    assert seen["opts"]["start"].startswith("2023-11-07") and seen["opts"]["end"].startswith(
        "2024-02-25"
    )
    assert values[stack_reference_id] == 0.0
    assert values["S1_270859_IW2_20231226T093234_VV_86AF-BURST"] == 130.0
    # neighbouring burst 270858 of the same date is not in the 270859 stack -> matched by date/orbit
    assert values["S1_270858_IW2_20240107T093231_VV_0C4A-BURST"] == 0.0
    unmatched = make_burst("2024-01-31", full_burst_id="052_109903_IW2", relative_orbit=52)
    assert bl.perpendicular_baselines_asf([unmatched], ref)[unmatched.granule_id] is None
    assert bl.perpendicular_baselines_asf([], ref) == {}


def test_baselines_by_date_and_choose_reference(burst_products: list[FakeProduct]) -> None:
    recs = _records(burst_products)
    values = {
        r.granule_id: (10.0 if r.full_burst_id.endswith("270859_IW2") else 12.0) for r in recs
    }
    by_date = bl.baselines_by_date(recs, values)
    assert by_date[date(2024, 1, 7)] == pytest.approx(11.0)  # mean of the two bursts
    assert bl.baselines_by_date(recs, {})[date(2024, 1, 7)] is None
    ref = bl.choose_reference(recs)
    assert ref.acquisition_date == date(2024, 1, 7)  # median of 10 dates (index 5)
    assert ref.full_burst_id == "127_270859_IW2"  # burst present on the most dates
    with pytest.raises(ValueError):
        bl.choose_reference([])


def _pairs(dates: list[date]) -> list[Pair]:
    out = []
    for i in range(len(dates) - 1):
        out.append(
            Pair(
                reference=dates[i],
                secondary=dates[i + 1],
                temporal_baseline_days=(dates[i + 1] - dates[i]).days,
            )
        )
    return out


def test_compute_pair_baselines_asf(
    burst_products: list[FakeProduct], fake_stack, stack_expected, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asf_search

    monkeypatch.setattr(
        asf_search, "stack_from_id", lambda reference_id, opts=None, useSubclass=None: fake_stack()
    )
    recs = _records(burst_products)
    dates = sorted({r.acquisition_date for r in recs})
    pairs = _pairs(dates)
    findings: list[Finding] = []
    out = bl.compute_pair_baselines(recs, pairs, method="asf", findings=findings)
    assert len(out) == len(pairs) and all(p.perp_baseline_m is not None for p in out)
    by_key = {p.key: p.perp_baseline_m for p in out}
    # 2023-12-26 (130) -> 2024-01-07 (0): -130 ; 2024-01-07 (0) -> 2024-01-19 (-15): -15
    assert by_key["20231226_20240107"] == pytest.approx(-130.0)
    assert by_key["20240107_20240119"] == pytest.approx(-15.0)
    assert by_key["20231108_20231120"] == pytest.approx(-127 - 155)
    assert out[0].temporal_baseline_days == pairs[0].temporal_baseline_days
    assert findings == []


def test_compute_pair_baselines_orbit_and_missing(burst_products: list[FakeProduct]) -> None:
    recs = _records(burst_products)
    extra_date = make_burst(
        "2024-03-07",
        full_burst_id="127_270859_IW2",
        relative_orbit=127,
        flight_direction="ASCENDING",
    )
    recs_plus = [*recs, extra_date]
    dates = sorted({r.acquisition_date for r in recs_plus})
    pairs = _pairs(dates)
    findings: list[Finding] = []
    out = bl.compute_pair_baselines(recs_plus, pairs, method="orbit", findings=findings)
    by_key = {p.key: p.perp_baseline_m for p in out}
    assert by_key["20240107_20240119"] == pytest.approx(-15.0, abs=1.0)
    assert by_key["20240224_20240307"] is None  # no state vectors for the synthetic date
    assert [f.rule_id for f in findings] == ["SEL-SEARCH-06"]
    assert findings[0].params["n_missing"] == 1 and "2024-03-07" in findings[0].params["dates"]


def test_compute_pair_baselines_auto_falls_back_to_orbit(
    burst_products: list[FakeProduct], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asf_search

    def _stack_from_id(reference_id, opts=None, useSubclass=None):
        raise asf_search.ASFSearchError("CMR unavailable")

    monkeypatch.setattr(asf_search, "stack_from_id", _stack_from_id)
    recs = _records(burst_products)
    pairs = _pairs(sorted({r.acquisition_date for r in recs}))
    findings: list[Finding] = []
    out = bl.compute_pair_baselines(recs, pairs, method="auto", findings=findings)
    assert {p.key: p.perp_baseline_m for p in out}["20240107_20240119"] == pytest.approx(
        -15.0, abs=1.0
    )
    ids = [f.rule_id for f in findings]
    assert ids == ["SEL-SEARCH-05", "SEL-SEARCH-08"]
    assert findings[0].severity == "WARN" and findings[1].params["source"] == "cmr"
    # method='asf' with a failing API is a FAIL and leaves everything None
    findings2: list[Finding] = []
    out2 = bl.compute_pair_baselines(recs, pairs, method="asf", findings=findings2)
    assert all(p.perp_baseline_m is None for p in out2)
    assert [f.rule_id for f in findings2] == ["SEL-SEARCH-05", "SEL-SEARCH-06"] and findings2[
        0
    ].severity == "FAIL"


def test_compute_pair_baselines_validation() -> None:
    with pytest.raises(ValueError):
        bl.compute_pair_baselines([make_burst("2024-01-01")], [], method="magic")  # type: ignore[arg-type]
    assert bl.compute_pair_baselines([], [], method="asf") == []
