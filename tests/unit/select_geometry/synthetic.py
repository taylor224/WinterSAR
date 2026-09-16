"""Synthetic DEMs shared by the unit tests and the golden-file generator (R-04, SEL-12).

Keep these deterministic: the golden regression file under
``tests/regression/golden/geometry/`` is produced from :func:`ridge_dem` with the
parameters in :data:`GOLDEN_PARAMS`.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

GOLDEN_PARAMS: dict[str, float] = {
    "nrows": 48,
    "ncols": 64,
    "dx_m": 30.0,
    "height_m": 400.0,
    "sigma_px": 5.0,
    "incidence_deg": 39.0,
    "heading_asc_deg": -12.0,
    "heading_desc_deg": 192.0,
}


def ridge_dem(
    nrows: int = 48,
    ncols: int = 64,
    dx_m: float = 30.0,
    height_m: float = 400.0,
    sigma_px: float = 5.0,
    sigma_px_east: float | None = None,
) -> npt.NDArray[np.float64]:
    """North-south ridge: Gaussian in x (columns), constant along rows.

    Centre at column ``(ncols - 1) / 2`` so that a symmetric ridge mirrors exactly under
    ``[:, ::-1]``. ``sigma_px_east`` makes the east flank gentler/steeper (asymmetric ridge).
    Max slope of a Gaussian flank: ``atan(height / (sigma · sqrt(e)))`` — with the defaults
    ≈ 58°, i.e. steeper than both the 39° layover limit and the 51° shadow limit.
    """
    x = (np.arange(ncols) - (ncols - 1) / 2.0) * dx_m
    sig_w = sigma_px * dx_m
    sig_e = (sigma_px_east if sigma_px_east is not None else sigma_px) * dx_m
    sigma = np.where(x < 0, sig_w, sig_e)
    z = height_m * np.exp(-(x**2) / (2.0 * sigma**2))
    return np.tile(z, (nrows, 1)).astype(np.float64)


def plane_dem(
    nrows: int, ncols: int, dx_m: float, slope_deg: float, rises_toward: str
) -> npt.NDArray[np.float64]:
    """Inclined plane with a known slope. ``rises_toward`` in {"east", "west", "north", "south"}.

    North-up array (row 0 = north).
    """
    g = np.tan(np.radians(slope_deg)) * dx_m
    cols = np.arange(ncols, dtype=np.float64)
    rows = np.arange(nrows, dtype=np.float64)
    if rises_toward == "east":
        z = np.tile(cols * g, (nrows, 1))
    elif rises_toward == "west":
        z = np.tile((ncols - 1 - cols) * g, (nrows, 1))
    elif rises_toward == "north":
        z = np.tile(((nrows - 1 - rows) * g)[:, None], (1, ncols))
    elif rises_toward == "south":
        z = np.tile((rows * g)[:, None], (1, ncols))
    else:  # pragma: no cover - test helper
        raise ValueError(rises_toward)
    return np.asarray(z, dtype=np.float64)
