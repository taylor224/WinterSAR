"""DEM fetch abstraction with a content-addressed cache (plan §5.1.5, PERF-02, ADR-0018).

The actual download is delegated to ``sardem`` (optional extra ``wintersar[dem]``; not a
core dependency, see ADR-0001). It is imported lazily through :mod:`importlib`; when it is
missing, :func:`get_dem` raises :class:`DemNotInstalledError` carrying an ``ENV-006``
:class:`~wintersar.io.schemas.Finding`. Tests inject a fake ``fetcher`` instead.

Cache layout (PERF-02: re-runs must not touch the network)::

    <cache_dir>/dem/<source>_<hash>.tif       DEM (WGS84 ellipsoid heights, float32 GeoTIFF)
    <cache_dir>/dem/<source>_<hash>.json      provenance sidecar (bbox, source, fetcher, time)
    <cache_dir>/dem/tiles/                    sardem tile cache (passed as ``cache_dir``)

``<hash>`` = :func:`wintersar.util.hashing.hash_params` of the rounded bbox, the source name
and the layout version, so the same AOI + source always maps to the same file.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wintersar.i18n import t
from wintersar.io.schemas import Finding, Severity
from wintersar.util.hashing import hash_params
from wintersar.util.masking import mask_mapping

BBox = tuple[float, float, float, float]
"""``(left, bottom, right, top)`` in degrees (lon/lat, WGS84) — the sardem bbox order."""

Fetcher = Callable[[BBox, str, Path, Path], Path]
"""``fetcher(bbox, source, out_path, tile_cache_dir) -> out_path``."""

DEM_CACHE_LAYOUT_VERSION = 1
DEM_SUBDIR = "dem"
TILE_SUBDIR = "tiles"
BBOX_DECIMALS = 4  # ~11 m at the equator: stable keys without spurious re-downloads
DEFAULT_BUFFER_DEG = 0.05  # ≈ 5 km margin so slope gradients at the AOI edge are valid

SARDEM_PACKAGE = "sardem"
# The pip extra is declared in pyproject.toml (``dem = ["sardem>=0.11"]``); PyPI name
# verified in ADR-0001 (sardem 0.13.0).
SARDEM_INSTALL_HINT = "pip install 'wintersar[dem]'  (or: pip install sardem)"


@dataclass(frozen=True)
class DemSource:
    """One supported DEM product and how ``sardem`` names it."""

    name: str
    sardem_data_source: str
    resolution_m: float
    label_key: str
    attribution_key: str


# source: https://raw.githubusercontent.com/scottstanie/sardem/master/README.md
#   (--data-source {NASA,NASA_WATER,COP,3DEP,NISAR}; COP = Copernicus DSM, NASA = SRTM 1 arcsec)
# source: https://raw.githubusercontent.com/scottstanie/sardem/master/sardem/cop_dem.py
#   (bucket https://copernicus-dem-30m.s3.amazonaws.com → GLO-30, EGM2008 → WGS84 conversion)
DEM_SOURCES: dict[str, DemSource] = {
    "copernicus_glo30": DemSource(
        name="copernicus_glo30",
        sardem_data_source="COP",
        resolution_m=30.0,
        label_key="select_geometry.dem.source.copernicus_glo30",
        attribution_key="select_geometry.dem.attribution.copernicus_glo30",
    ),
    "srtm1": DemSource(
        name="srtm1",
        sardem_data_source="NASA",
        resolution_m=30.0,
        label_key="select_geometry.dem.source.srtm1",
        attribution_key="select_geometry.dem.attribution.srtm1",
    ),
}
DEFAULT_SOURCE = "copernicus_glo30"


class DemError(RuntimeError):
    """Base class for DEM fetch problems."""


class DemNotInstalledError(DemError):
    """``sardem`` is not importable; carries the ENV-006 finding for reports."""

    def __init__(self, finding: Finding) -> None:
        super().__init__(
            t(finding.message_key, **finding.params)
            + " → "
            + (t(finding.fix_key, **finding.params) if finding.fix_key else "")
        )
        self.finding = finding


class DemFetchError(DemError):
    """The fetcher returned without producing the expected file."""


def env006_finding(severity: Severity = "WARN") -> Finding:
    """ENV-006 finding for the missing optional ``sardem`` package."""
    return Finding(
        rule_id="ENV-006",
        severity=severity,
        message_key="env.ENV-006.cause",
        fix_key="env.ENV-006.fix",
        params={"package": SARDEM_PACKAGE, "install_hint": SARDEM_INSTALL_HINT},
        evidence={"package": SARDEM_PACKAGE, "extra": "dem"},
        refs=["ADR-0001", "ADR-0018"],
        scope="select.dem",
    )


def sardem_available() -> bool:
    return importlib.util.find_spec(SARDEM_PACKAGE) is not None


def check_install() -> list[Finding]:
    """``wintersar check-install`` hook: ENV-006 (WARN) when ``sardem`` is absent."""
    return [] if sardem_available() else [env006_finding("WARN")]


# ---------------------------------------------------------------------------- bbox / cache


def bbox_from_wkt(aoi_wkt: str, buffer_deg: float = 0.0) -> BBox:
    """``(left, bottom, right, top)`` of a WKT geometry, optionally buffered (degrees)."""
    from shapely import wkt as shapely_wkt  # source: shapely 2.1.2 shapely/wkt.py loads()

    geom = shapely_wkt.loads(aoi_wkt)
    if geom.is_empty:
        raise ValueError(t("select_geometry.error.empty_aoi"))
    left, bottom, right, top = geom.bounds
    return (
        float(left - buffer_deg),
        float(max(bottom - buffer_deg, -90.0)),
        float(right + buffer_deg),
        float(min(top + buffer_deg, 90.0)),
    )


def normalize_bbox(bbox: BBox) -> BBox:
    """Round to :data:`BBOX_DECIMALS` so nearly identical requests share one cache entry."""
    left, bottom, right, top = (round(float(v), BBOX_DECIMALS) for v in bbox)
    if not (left < right and bottom < top):
        raise ValueError(t("select_geometry.error.invalid_bbox", bbox=str(bbox)))
    return (left, bottom, right, top)


def dem_cache_key(bbox: BBox, source: str) -> str:
    """Content address of a DEM request (bbox rounded, source name, layout version)."""
    return hash_params(
        {
            "bbox": list(normalize_bbox(bbox)),
            "source": source,
            "layout": DEM_CACHE_LAYOUT_VERSION,
        }
    )


def dem_cache_path(cache_dir: Path | str, bbox: BBox, source: str) -> Path:
    return Path(cache_dir) / DEM_SUBDIR / f"{source}_{dem_cache_key(bbox, source)}.tif"


def _sidecar(path: Path) -> Path:
    return path.with_suffix(".json")


def dem_provenance(dem_path: Path | str) -> dict[str, Any] | None:
    """Read the provenance sidecar written by :func:`get_dem` (``None`` if absent)."""
    side = _sidecar(Path(dem_path))
    if not side.exists():
        return None
    data: dict[str, Any] = json.loads(side.read_text(encoding="utf-8"))
    return data


# ---------------------------------------------------------------------------- fetchers


def _import_sardem_dem() -> Any:
    if not sardem_available():
        raise DemNotInstalledError(env006_finding("FAIL"))
    return importlib.import_module(f"{SARDEM_PACKAGE}.dem")


def sardem_fetcher(bbox: BBox, source: str, out_path: Path, tile_cache_dir: Path) -> Path:
    """Download ``bbox`` with ``sardem`` into ``out_path`` (float32 GeoTIFF, WGS84 heights).

    Keyword names verified against the upstream signature::

        def main(output_name=None, bbox=None, geojson=None, wkt_file=None, data_source=None,
                 xrate=1, yrate=1, make_isce_xml=False, keep_egm=False, shift_rsc=False,
                 cache_dir=None, output_type="float32", output_format="GTiff", vrt_filename=None)

    ``bbox`` is ``(left, bot, right, top)``; ``keep_egm=False`` (default) converts geoid
    heights to WGS84 ellipsoid heights, which is what InSAR processors expect.
    # source: https://raw.githubusercontent.com/scottstanie/sardem/master/sardem/dem.py
    """
    src = DEM_SOURCES[source]
    mod = _import_sardem_dem()
    tile_cache_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mod.main(
        output_name=str(out_path),
        bbox=tuple(bbox),
        data_source=src.sardem_data_source,
        cache_dir=str(tile_cache_dir),
        output_format="GTiff",
        output_type="float32",
    )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise DemFetchError(
            t("select_geometry.error.fetch_failed", source=source, path=str(out_path))
        )
    return out_path


# ---------------------------------------------------------------------------- public API


def get_dem(
    aoi_wkt: str,
    cache_dir: Path | str,
    source: str = DEFAULT_SOURCE,
    buffer_deg: float = DEFAULT_BUFFER_DEG,
    fetcher: Fetcher | None = None,
) -> Path:
    """Return a cached DEM GeoTIFF covering ``aoi_wkt`` (+ ``buffer_deg``), fetching once.

    Cache hits never call ``fetcher`` (PERF-02). A miss fetches into a temporary file and
    renames it atomically, then writes a provenance sidecar. ``fetcher`` defaults to
    :func:`sardem_fetcher`; pass a callable to test without network / without sardem.
    """
    if source not in DEM_SOURCES:
        raise ValueError(
            t(
                "select_geometry.error.unknown_source",
                source=source,
                allowed=", ".join(sorted(DEM_SOURCES)),
            )
        )
    bbox = normalize_bbox(bbox_from_wkt(aoi_wkt, buffer_deg))
    out = dem_cache_path(cache_dir, bbox, source)
    if out.exists() and out.stat().st_size > 0:
        return out

    fetch = fetcher if fetcher is not None else sardem_fetcher
    tile_dir = Path(cache_dir) / DEM_SUBDIR / TILE_SUBDIR
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + f".part{os.getpid()}.tif")
    try:
        produced = fetch(bbox, source, tmp, tile_dir)
        if not Path(produced).exists() or Path(produced).stat().st_size == 0:
            raise DemFetchError(
                t("select_geometry.error.fetch_failed", source=source, path=str(produced))
            )
        Path(produced).replace(out)
    finally:
        if tmp.exists():
            tmp.unlink()
    _sidecar(out).write_text(
        json.dumps(
            mask_mapping(
                {
                    "bbox": list(bbox),
                    "source": source,
                    "sardem_data_source": DEM_SOURCES[source].sardem_data_source,
                    "resolution_m": DEM_SOURCES[source].resolution_m,
                    "fetcher": getattr(fetch, "__name__", type(fetch).__name__),
                    "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "layout": DEM_CACHE_LAYOUT_VERSION,
                    "attribution": t(DEM_SOURCES[source].attribution_key, "en"),
                }
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out


__all__ = [
    "BBOX_DECIMALS",
    "DEFAULT_BUFFER_DEG",
    "DEFAULT_SOURCE",
    "DEM_CACHE_LAYOUT_VERSION",
    "DEM_SOURCES",
    "SARDEM_INSTALL_HINT",
    "SARDEM_PACKAGE",
    "BBox",
    "DemError",
    "DemFetchError",
    "DemNotInstalledError",
    "DemSource",
    "Fetcher",
    "bbox_from_wkt",
    "check_install",
    "dem_cache_key",
    "dem_cache_path",
    "dem_provenance",
    "env006_finding",
    "get_dem",
    "normalize_bbox",
    "sardem_available",
    "sardem_fetcher",
]
