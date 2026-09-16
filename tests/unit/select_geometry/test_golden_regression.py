"""Golden-file regression for the ridge masks (R-04, SEL-12; Phase 1 DoD "골든 파일").

The golden file is produced by ``tests/regression/golden/geometry/make_golden.py``. Any
change in slope/aspect, local incidence, layover/shadow thresholds or the foreshortening
index shows up here; regenerate only after a reviewed algorithm change (rule 11.4).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tests.unit.select_geometry.synthetic import GOLDEN_PARAMS, ridge_dem
from wintersar.select import geometry_masks as gm

GOLDEN = (
    Path(__file__).resolve().parents[2] / "regression" / "golden" / "geometry" / "ridge_masks.npz"
)
STAT_KEYS = ("layover_fraction", "shadow_fraction", "foreshortening_mean", "aoi_pixels")


@pytest.fixture(scope="module")
def golden() -> dict[str, np.ndarray]:
    assert GOLDEN.exists(), f"missing golden file {GOLDEN}; run make_golden.py"
    assert GOLDEN.stat().st_size < 100_000, "golden file must stay small (< 100 kB)"
    with np.load(GOLDEN, allow_pickle=True) as z:
        return {k: z[k] for k in z.files}


@pytest.fixture(scope="module")
def current() -> dict[str, gm.GeometryMaskResult]:
    p = GOLDEN_PARAMS
    dem = ridge_dem(
        nrows=int(p["nrows"]),
        ncols=int(p["ncols"]),
        dx_m=p["dx_m"],
        height_m=p["height_m"],
        sigma_px=p["sigma_px"],
    )
    return gm.masks_for_both_directions(
        dem,
        p["dx_m"],
        p["dx_m"],
        p["incidence_deg"],
        heading_asc=p["heading_asc_deg"],
        heading_desc=p["heading_desc_deg"],
    )


def test_golden_params_match_generator(golden: dict[str, np.ndarray]) -> None:
    stored = {str(k): float(v) for k, v in golden["params"]}
    assert stored == {k: float(v) for k, v in GOLDEN_PARAMS.items()}


@pytest.mark.parametrize("direction", [gm.ASCENDING, gm.DESCENDING])
def test_masks_match_golden(
    golden: dict[str, np.ndarray], current: dict[str, gm.GeometryMaskResult], direction: str
) -> None:
    tag = direction.lower()
    r = current[direction]
    assert np.array_equal(r.layover, golden[f"{tag}_layover"])
    assert np.array_equal(r.shadow, golden[f"{tag}_shadow"])
    np.testing.assert_allclose(
        r.foreshortening, golden[f"{tag}_foreshortening"], rtol=1e-5, atol=1e-5
    )
    np.testing.assert_allclose(
        r.local_incidence_deg, golden[f"{tag}_local_incidence_deg"], rtol=1e-5, atol=1e-3
    )
    np.testing.assert_allclose(
        [r.stats[k] for k in STAT_KEYS], golden[f"{tag}_stats"], rtol=1e-6, atol=1e-9
    )


def test_golden_encodes_direction_flip(golden: dict[str, np.ndarray]) -> None:
    """Sanity of the stored file itself: the two directions must be mirror images."""
    assert np.array_equal(golden["ascending_layover"], golden["descending_layover"][:, ::-1])
    assert golden["ascending_layover"].any() and golden["ascending_shadow"].any()
    assert not np.array_equal(golden["ascending_layover"], golden["descending_layover"])
