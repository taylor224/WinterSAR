"""Regenerate tests/fixtures/ground_truth/*.csv from the synthetic truth in conftest.py.

Run from the repo root: ``.venv/bin/python tests/unit/validate/_make_fixtures.py``. Not a test
(no ``test_`` prefix); kept next to the conftest so the two stay in sync (rule 11.4).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests.unit.validate.conftest import (
    FIXTURES,
    SYNTH_INCIDENCE,
    make_synth_stack,
    timeseries_from_stack,
)
from wintersar.io.schemas import GroundTruthRecord
from wintersar.validate.ground_truth import write_csv

stack = make_synth_stack()
ts = timeseries_from_stack(stack)
lat2, lon2 = ts.lat2d(), ts.lon2d()
cos_inc = np.cos(np.deg2rad(SYNTH_INCIDENCE))
rng = np.random.default_rng(2024)
NOISE_M = 0.001  # 1 mm white noise on every ground-truth epoch


def rows(
    site, r, c, method, dates, offset_m, noise_m=NOISE_M, enu_noise_m=0.0005, sigma_mm=1.0, every=1
):
    out = []
    truth = stack.displacement_m[:, r, c]
    for i, d in enumerate(stack.dates):
        if i % every:
            continue
        up = truth[i] / cos_inc + offset_m + rng.normal(0, noise_m)
        rec = dict(
            site_id=site,
            lat=float(lat2[r, c]),
            lon=float(lon2[r, c]),
            elev_m=float(ts.dem_m[r, c]),
            date=d + timedelta(days=dates),
            method=method,
            sigma_mm=sigma_mm,
        )
        if method == "gnss":
            rec.update(
                up_m=up, east_m=rng.normal(0, enu_noise_m), north_m=rng.normal(0, enu_noise_m)
            )
        else:
            rec.update(up_m=up)
        out.append(GroundTruthRecord(**rec))
    return out


lev = []
lev += rows("L01-center", 16, 16, "leveling", 2, 0.0123)  # bowl centre, +2 day offset
lev += rows("L02-edge", 4, 26, "leveling", 0, -0.004, every=2)  # far from bowl, every other epoch
lev += rows("L03-slope", 10, 16, "leveling", -3, 0.0)  # mid slope, -3 day offset
lev.append(
    GroundTruthRecord(
        site_id="L99-outside",
        lat=38.5,
        lon=128.0,
        elev_m=10.0,
        date=date(2024, 1, 1),
        up_m=0.0,
        method="leveling",
    )
)
lev.append(
    GroundTruthRecord(
        site_id="L99-outside",
        lat=38.5,
        lon=128.0,
        elev_m=10.0,
        date=date(2024, 1, 13),
        up_m=0.001,
        method="leveling",
    )
)
write_csv(lev, FIXTURES / "leveling_synth.csv")

# GNSS: two sites, epochs every 6 days (half fall between InSAR epochs → nearest/interp both work)
gn = []
for site, r, c, off in (("G01", 20, 12, 0.05), ("G02", 24, 22, -0.02)):
    truth = stack.displacement_m[:, r, c]
    days = np.array([(d - stack.dates[0]).days for d in stack.dates], dtype=float)
    for k in range(0, int(days[-1]) + 1, 6):
        v = float(np.interp(k, days, truth))
        gn.append(
            GroundTruthRecord(
                site_id=site,
                lat=float(lat2[r, c]),
                lon=float(lon2[r, c]),
                elev_m=float(ts.dem_m[r, c]),
                date=stack.dates[0] + timedelta(days=k),
                up_m=v / cos_inc + off + rng.normal(0, NOISE_M),
                east_m=rng.normal(0, 0.0005),
                north_m=rng.normal(0, 0.0005),
                method="gnss",
                sigma_mm=2.0,
            )
        )
write_csv(gn, FIXTURES / "gnss_synth.csv")
print(len(lev), "leveling rows,", len(gn), "gnss rows")
print((FIXTURES / "leveling_synth.csv").read_text()[:400])
