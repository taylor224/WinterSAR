"""Fixtures for select/geometry_masks and select/dem tests (R-04, SEL-12, PERF-02)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.select_geometry.synthetic import ridge_dem

INCIDENCE = 39.0
DX = 30.0


@pytest.fixture
def incidence() -> float:
    return INCIDENCE


@pytest.fixture
def dx() -> float:
    return DX


@pytest.fixture
def ridge() -> np.ndarray:
    return ridge_dem()


@pytest.fixture
def asym_ridge() -> np.ndarray:
    """Steep west flank (sigma 4 px ≈ 63° max), gentle east flank (sigma 20 px ≈ 22° max)."""
    return ridge_dem(sigma_px=4.0, sigma_px_east=20.0)


@pytest.fixture
def flat() -> np.ndarray:
    return np.full((32, 40), 120.0)


@pytest.fixture
def write_geotiff(tmp_path: Path):
    """Write ``array`` as a single-band float32 GeoTIFF and return its path."""
    import rasterio

    def _write(
        name: str,
        array: np.ndarray,
        transform: Any,
        crs: Any,
        nodata: float | None = None,
    ) -> Path:
        p = tmp_path / name
        with rasterio.open(
            p,
            "w",
            driver="GTiff",
            height=array.shape[0],
            width=array.shape[1],
            count=1,
            dtype="float32",
            crs=crs,
            transform=transform,
            nodata=nodata,
        ) as ds:
            ds.write(array.astype(np.float32), 1)
        return p

    return _write
