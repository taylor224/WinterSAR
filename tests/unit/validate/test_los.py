"""ADR-0040: ENU -> LOS with the MintPy sign/heading convention (R-10, plan §5.6).

Fixed cases (domain checkpoint, researcher confirmation pending — see ADR-0040):
* pure uplift  -> +cos(inc) on both ascending and descending (towards satellite = positive)
* pure eastward -> negative on ascending (heading -12°), positive on descending (192°)
"""

from __future__ import annotations

import numpy as np
import pytest

from wintersar.validate.los import (
    S1_HEADING_ASC_DEG,
    S1_HEADING_DESC_DEG,
    azimuth_to_heading,
    default_heading,
    enu_to_los,
    heading_to_azimuth,
    los_to_vertical,
    los_unit_vector,
    vertical_to_los,
    wrap_deg,
)

INC = 39.0
SIN, COS = np.sin(np.deg2rad(INC)), np.cos(np.deg2rad(INC))


def test_mintpy_heading_azimuth_examples():
    # utils0.azimuth2heading_angle docstring: asc heading -12 <-> az 102; desc -168 <-> -102
    assert heading_to_azimuth(-12.0) == pytest.approx(102.0)
    assert heading_to_azimuth(-168.0) == pytest.approx(-102.0)
    assert heading_to_azimuth(192.0) == pytest.approx(-102.0)  # 192 ≡ -168
    assert azimuth_to_heading(102.0) == pytest.approx(-12.0)
    assert azimuth_to_heading(-102.0) == pytest.approx(-168.0)
    for h in (-12.0, -168.0, 45.0, 200.0):
        assert azimuth_to_heading(heading_to_azimuth(h)) == pytest.approx(wrap_deg(h))
    # left-looking branch: (head + 90) * -1
    assert heading_to_azimuth(-12.0, "left") == pytest.approx(-78.0)


def test_wrap_deg_matches_mintpy_round_rule():
    assert wrap_deg(370.0) == pytest.approx(10.0)
    assert wrap_deg(-190.0) == pytest.approx(170.0)
    assert wrap_deg(192.0) == pytest.approx(-168.0)
    np.testing.assert_allclose(wrap_deg([0.0, 180.0, -180.0]), [0.0, 180.0, -180.0])


def test_unit_vector_formula_ascending_and_descending():
    # MintPy enu2los: [-sin(inc) sin(az), sin(inc) cos(az), cos(inc)]
    az_asc = np.deg2rad(102.0)
    e, n, u = los_unit_vector(INC, S1_HEADING_ASC_DEG)
    assert e == pytest.approx(-SIN * np.sin(az_asc))
    assert n == pytest.approx(SIN * np.cos(az_asc))
    assert u == pytest.approx(COS)
    az_desc = np.deg2rad(-102.0)
    e2, n2, u2 = los_unit_vector(INC, S1_HEADING_DESC_DEG)
    assert e2 == pytest.approx(-SIN * np.sin(az_desc))
    assert n2 == pytest.approx(SIN * np.cos(az_desc))
    assert u2 == pytest.approx(COS)
    # numeric values (inc 39°): east component ∓0.6156, north -0.1308, up 0.7771
    assert e == pytest.approx(-0.61557, abs=1e-4) and e2 == pytest.approx(0.61557, abs=1e-4)
    assert n == pytest.approx(-0.13084, abs=1e-4) and n2 == pytest.approx(-0.13084, abs=1e-4)
    assert u == pytest.approx(0.77715, abs=1e-4)
    # unit length
    assert np.hypot(np.hypot(e, n), u) == pytest.approx(1.0)


@pytest.mark.parametrize("heading", [S1_HEADING_ASC_DEG, S1_HEADING_DESC_DEG])
def test_pure_uplift_is_positive_towards_satellite(heading):
    los = enu_to_los(0.0, 0.0, 1.0, INC, heading)
    assert los == pytest.approx(COS)
    assert los > 0


def test_pure_east_opposite_signs_asc_vs_desc():
    asc = float(enu_to_los(1.0, 0.0, 0.0, INC, S1_HEADING_ASC_DEG))
    desc = float(enu_to_los(1.0, 0.0, 0.0, INC, S1_HEADING_DESC_DEG))
    # ascending: sin(az=102°) > 0 -> LOS = -sin(inc)·sin(az) < 0 (east motion = away from a
    # right-looking sensor flying north-ish and looking east). Descending: az = -102° -> LOS > 0.
    assert asc < 0 < desc
    assert asc == pytest.approx(-SIN * np.sin(np.deg2rad(102.0)))
    assert desc == pytest.approx(-asc)


def test_pure_north_small_and_negative_for_both_passes():
    asc = float(enu_to_los(0.0, 1.0, 0.0, INC, S1_HEADING_ASC_DEG))
    desc = float(enu_to_los(0.0, 1.0, 0.0, INC, S1_HEADING_DESC_DEG))
    assert asc == pytest.approx(SIN * np.cos(np.deg2rad(102.0)))
    assert asc == pytest.approx(desc)
    assert asc < 0 and abs(asc) < 0.2


def test_enu_to_los_is_linear_and_broadcasts():
    e = np.array([0.01, -0.02])
    n = np.array([0.0, 0.005])
    u = np.array([-0.03, 0.0])
    inc = np.array([35.0, 42.0])
    los = enu_to_los(e, n, u, inc, S1_HEADING_DESC_DEG)
    ue, un, uu = los_unit_vector(inc, S1_HEADING_DESC_DEG)
    np.testing.assert_allclose(los, e * ue + n * un + u * uu)
    assert los.shape == (2,)


def test_vertical_round_trip():
    up = np.array([-0.05, 0.0, 0.02])
    los = vertical_to_los(up, INC)
    np.testing.assert_allclose(los, up * COS)
    np.testing.assert_allclose(los_to_vertical(los, INC), up)
    # purely vertical assumption == enu2los with zero horizontal components
    np.testing.assert_allclose(los, enu_to_los(0.0, 0.0, up, INC, S1_HEADING_ASC_DEG))


def test_default_heading():
    assert default_heading("ASCENDING") == S1_HEADING_ASC_DEG
    assert default_heading("asc") == S1_HEADING_ASC_DEG
    assert default_heading("DESCENDING") == S1_HEADING_DESC_DEG
    assert default_heading(None) == S1_HEADING_DESC_DEG
