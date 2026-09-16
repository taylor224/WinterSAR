"""Shared in-memory time-series container used by validate/bench/QGIS export.

Loaders for concrete formats (MintPy ``timeseries.h5`` via h5py, dolphin outputs, the fake
engine ``.npz``) live in :mod:`wintersar.io.formats` and return this object.

Conventions (MintPy compatible, verified in ADR-0040):
* ``displacement_m`` is LOS displacement in metres, **positive = towards the satellite**
  (range decrease), cumulative and zero at ``dates[0]`` unless ``attrs['REF_DATE']`` says
  otherwise.
* ``heading_deg`` is the satellite heading (azimuth of flight direction, clockwise from
  north). ``incidence_deg`` is the local incidence angle at each pixel (or a scalar).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
from numpy.typing import NDArray


@dataclass
class TimeSeries:
    dates: list[date]
    displacement_m: NDArray[np.floating]  # (n_dates, ny, nx)
    lat: NDArray[np.floating]  # (ny, nx) or (ny,)
    lon: NDArray[np.floating]  # (ny, nx) or (nx,)
    incidence_deg: NDArray[np.floating] | float | None = None
    heading_deg: float | None = None
    coherence: NDArray[np.floating] | None = None  # (ny, nx) temporal/mean coherence
    velocity_m_per_yr: NDArray[np.floating] | None = None  # (ny, nx)
    reference_latlon: tuple[float, float] | None = None
    dem_m: NDArray[np.floating] | None = None
    conncomp: NDArray[np.integer] | None = None  # (ny, nx) connected component of the stack
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.displacement_m.shape[1]), int(self.displacement_m.shape[2]))

    @property
    def n_dates(self) -> int:
        return len(self.dates)

    def lat2d(self) -> NDArray[np.floating]:
        if self.lat.ndim == 2:
            return self.lat
        return np.broadcast_to(self.lat[:, None], self.shape)

    def lon2d(self) -> NDArray[np.floating]:
        if self.lon.ndim == 2:
            return self.lon
        return np.broadcast_to(self.lon[None, :], self.shape)

    def incidence2d(self) -> NDArray[np.floating] | None:
        if self.incidence_deg is None:
            return None
        if isinstance(self.incidence_deg, int | float):
            return np.full(self.shape, float(self.incidence_deg))
        return self.incidence_deg

    def years(self) -> NDArray[np.floating]:
        d0 = self.dates[0]
        return np.array([(d - d0).days / 365.25 for d in self.dates], dtype=np.float64)

    def nearest_pixel(self, lat: float, lon: float) -> tuple[int, int]:
        la, lo = self.lat2d(), self.lon2d()
        d2 = (la - lat) ** 2 + (np.cos(np.deg2rad(lat)) * (lo - lon)) ** 2
        idx = int(np.nanargmin(d2))
        return divmod(idx, self.shape[1])
