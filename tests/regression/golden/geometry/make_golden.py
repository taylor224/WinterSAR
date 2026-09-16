"""Regenerate ``ridge_masks.npz`` (R-04 golden file for select/geometry_masks).

Run from the repository root::

    .venv/bin/python tests/regression/golden/geometry/make_golden.py

Only regenerate after a *reviewed* algorithm change (rule 11.4): the regression test
``tests/unit/select_geometry/test_golden_regression.py`` compares masks exactly and the
float layers to 1e-5. The DEM and parameters come from
``tests.unit.select_geometry.synthetic.GOLDEN_PARAMS`` so the file is reproducible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from tests.unit.select_geometry.synthetic import GOLDEN_PARAMS, ridge_dem  # noqa: E402
from wintersar.select import geometry_masks as gm  # noqa: E402

OUT = Path(__file__).with_name("ridge_masks.npz")


def build() -> dict[str, np.ndarray]:
    p = GOLDEN_PARAMS
    dem = ridge_dem(
        nrows=int(p["nrows"]),
        ncols=int(p["ncols"]),
        dx_m=p["dx_m"],
        height_m=p["height_m"],
        sigma_px=p["sigma_px"],
    )
    res = gm.masks_for_both_directions(
        dem,
        p["dx_m"],
        p["dx_m"],
        p["incidence_deg"],
        heading_asc=p["heading_asc_deg"],
        heading_desc=p["heading_desc_deg"],
    )
    arrays: dict[str, np.ndarray] = {"params": np.array(list(p.items()), dtype=object)}
    for direction, r in res.items():
        tag = direction.lower()
        arrays[f"{tag}_layover"] = r.layover
        arrays[f"{tag}_shadow"] = r.shadow
        arrays[f"{tag}_foreshortening"] = r.foreshortening.astype(np.float32)
        arrays[f"{tag}_local_incidence_deg"] = r.local_incidence_deg.astype(np.float32)
        arrays[f"{tag}_stats"] = np.array(
            [
                r.stats[k]
                for k in (
                    "layover_fraction",
                    "shadow_fraction",
                    "foreshortening_mean",
                    "aoi_pixels",
                )
            ]
        )
    return arrays


if __name__ == "__main__":
    data = build()
    np.savez_compressed(OUT, **data, allow_pickle=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
