"""R-03 / SEL-09: automatic looks (plan section 5.1.4, ADR-0015).

Hand derivation for the IW representative values (2.33 m slant range, 14.1 m azimuth,
theta = 39 deg; nominal IW SLC spacing 2.3 x 14.1 m per SentiWiki S1 Products Table 6):

    ground range spacing g = 2.33 / sin(39 deg) = 2.33 / 0.62932 = 3.7024 m
    multilooked pixel     = (g * rg_looks) x (14.1 * az_looks)
    aspect                = max / min  (must be <= 1.2)
    size metric           = sqrt(rg_m * az_m)  (side of the equal-area square)

target 20 m
    az = 1 (14.1 m): aspect <= 1.2 needs 11.75 <= g*rg <= 16.92 -> rg = 4 -> 14.81 x 14.1,
        size 14.45 (|diff| 5.55)
    az = 2 (28.2 m): rg in {7, 8, 9} -> sizes 27.03 / 28.90 / 30.65 (|diff| >= 7.03)
    -> (4, 1).  (5, 1) would give 18.5 x 14.1 = HyP3's "20 m" option but its aspect is
       1.31 > 1.2, so it is excluded at the default bound; see test_relaxed_aspect.
target 40 m
    az = 3 (42.3 m): 35.25 <= g*rg <= 50.76 -> rg in {10..13}
        (10,3) 37.02 x 42.3 size 39.57 (|diff| 0.43) <- best
        (11,3) 40.73 x 42.3 size 41.51 (|diff| 1.51)
    az = 2: best (9,2) 33.32 x 28.2 size 30.65 (|diff| 9.35);  az = 4: (13,4) size 52.1
    -> (10, 3)
target 80 m
    az = 6 (84.6 m): 70.5 <= g*rg <= 101.5 -> rg in {20..27}
        (20,6) 74.05 x 84.6 size 79.15 (|diff| 0.85) <- best
        (21,6) 77.75 x 84.6 size 81.10 (|diff| 1.10)
    az = 5 (70.5 m): rg in {16..22}, best (22,5) 81.45 x 70.5 size 75.78 (|diff| 4.22)
    -> (20, 6)
"""

from __future__ import annotations

import math

import pytest

from tests.conftest import make_burst
from wintersar.select.looks import (
    IW_NOMINAL_AZIMUTH_SPACING_M,
    IW_NOMINAL_RANGE_SPACING_M,
    LooksResult,
    compute_looks,
    describe_looks,
    ground_range_spacing,
    looks_for_records,
    spacing_stats,
)

RG, AZ, THETA = 2.33, 14.1, 39.0
G = RG / math.sin(math.radians(THETA))  # 3.7024 m


def test_ground_range_spacing() -> None:
    assert ground_range_spacing(RG, THETA) == pytest.approx(3.7024, abs=1e-3)
    with pytest.raises(ValueError):
        ground_range_spacing(RG, 0.0)
    with pytest.raises(ValueError):
        ground_range_spacing(-1.0, THETA)


@pytest.mark.parametrize(
    ("target", "expected", "pixel_rg", "pixel_az"),
    [
        (20.0, (4, 1), 4 * G, 14.1),
        (40.0, (10, 3), 10 * G, 42.3),
        (80.0, (20, 6), 20 * G, 84.6),
    ],
)
def test_iw_fixed_values(
    target: float, expected: tuple[int, int], pixel_rg: float, pixel_az: float
) -> None:
    r = compute_looks(RG, AZ, THETA, target_pixel_m=target)
    assert isinstance(r, LooksResult)
    assert (r.rg_looks, r.az_looks) == expected
    assert r.pixel_rg_m == pytest.approx(pixel_rg, abs=1e-6)
    assert r.pixel_az_m == pytest.approx(pixel_az, abs=1e-6)
    assert r.aspect_ratio <= 1.2
    assert r.aspect_ratio >= 1.0
    assert r.target_m == target
    assert r.ground_range_spacing_m == pytest.approx(G)
    assert r.pixel_m == pytest.approx(math.sqrt(pixel_rg * pixel_az))


def test_relaxed_aspect_matches_hyp3_5x1() -> None:
    # HyP3 burst InSAR offers 5x1 (20 m), 10x2 (40 m), 20x4 (80 m); at theta=39 deg those have
    # aspect ~1.31 and are only reachable with a looser bound (ADR-0015).
    # source: https://hyp3-docs.asf.alaska.edu/guides/burst_insar_product_guide/
    r = compute_looks(RG, AZ, THETA, target_pixel_m=20.0, max_aspect=1.35)
    assert (r.rg_looks, r.az_looks) == (5, 1)
    assert r.aspect_ratio == pytest.approx((5 * G) / 14.1)


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        compute_looks(RG, 0.0, THETA)
    with pytest.raises(ValueError):
        compute_looks(RG, AZ, THETA, target_pixel_m=0)
    with pytest.raises(ValueError):
        compute_looks(RG, AZ, THETA, max_aspect=0.9)
    with pytest.raises(ValueError):
        describe_looks(0, 1, RG, AZ, THETA)


def test_describe_explicit_looks() -> None:
    r = describe_looks(10, 2, RG, AZ, THETA)
    assert (r.rg_looks, r.az_looks) == (10, 2)
    assert r.pixel_az_m == pytest.approx(28.2)
    assert r.aspect_ratio == pytest.approx(10 * G / 28.2)
    d = r.as_dict()
    assert d["rg_looks"] == 10 and "pixel_m" in d


def test_spacing_stats_and_nominal_fallback() -> None:
    recs = [make_burst("2024-01-01"), make_burst("2024-01-13", azimuth_pixel_spacing_m=14.4)]
    st = spacing_stats(recs)
    assert st.n_with_metadata == 2 and not st.nominal
    assert st.az_min == 14.1 and st.az_max == 14.4
    assert st.az_deviation == pytest.approx(0.3 / 14.25)
    assert st.incidence_deg == pytest.approx((36.5 + 41.9) / 2)

    bare = make_burst("2024-01-01").model_copy(
        update={"range_pixel_spacing_m": None, "azimuth_pixel_spacing_m": None}
    )
    st2 = spacing_stats([bare])
    assert st2.nominal
    assert st2.rg_median == IW_NOMINAL_RANGE_SPACING_M
    assert st2.az_median == IW_NOMINAL_AZIMUTH_SPACING_M


def test_looks_for_records_uses_median_and_explicit() -> None:
    recs = [make_burst("2024-01-01"), make_burst("2024-01-13")]
    auto = looks_for_records(recs, 40.0)
    assert (auto.rg_looks, auto.az_looks) == (10, 3)  # theta = 39.2 -> g = 3.686 m
    fixed = looks_for_records(recs, 40.0, looks=(10, 2))
    assert (fixed.rg_looks, fixed.az_looks) == (10, 2)
