"""Sentinel-1 candidate search through ``asf_search`` (plan §5.1.1, R-01, PERF-01).

Flow
----
``search_from_config(cfg)`` -> :func:`aoi_to_wkt` -> :func:`search_bursts` which queries CMR
for ``processingLevel=BURST`` first and falls back to full ``SLC`` scenes when no burst
product exists for the AOI/period (SEL-SEARCH-02). Products are normalised with
:func:`wintersar.select.metadata.burst_record_from_asf` and written to ``candidates.json``
by :func:`save_records` (read back with :func:`load_records`).

No credentials are needed for searching (CMR is public); the search only *warns* when none
are configured (KB-AUTH-001) because every later download will need them.

Findings raised here use rule ids ``SEL-SEARCH-01..08`` (i18n namespace ``select_search``).
All asf_search keyword names below are verified against
``.venv/lib/python3.11/site-packages/asf_search/search/search.py`` and
``ASFSearchOptions/validator_map.py`` (asf_search 14.0.0).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal, cast

import shapely
from shapely.geometry import GeometryCollection, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from wintersar.i18n import t
from wintersar.io.schemas import BurstRecord, Finding
from wintersar.pipeline.config import Config
from wintersar.select.auth import credentials_finding
from wintersar.select.metadata import records_from_asf
from wintersar.util.masking import mask_mapping, mask_text

ProductType = Literal["BURST", "SLC"]
_NS = "select_search"
CANDIDATES_SCHEMA_VERSION = 1

# source: asf_search/constants/PLATFORM.py
_PLATFORM_KEYWORDS: dict[str, str] = {
    "S1A": "Sentinel-1A",
    "S1B": "Sentinel-1B",
    "S1C": "Sentinel-1C",
    "S1D": "Sentinel-1D",
}
# source: asf_search/constants/POLARIZATION.py (VV, VV+VH, HH, HH+HV) and the SLC stack
# option logic in asf_search/Products/S1Product.py::get_stack_opts
_SLC_POLARIZATION_FILTER: dict[str, list[str]] = {
    "VV": ["VV", "VV+VH"],
    "VH": ["VH", "VV+VH"],
    "HH": ["HH", "HH+HV"],
    "HV": ["HV", "HH+HV"],
}
# source: asf_search/constants/FLIGHT_DIRECTION.py
_FLIGHT_DIRECTION: dict[str, str] = {"asc": "ASCENDING", "desc": "DESCENDING"}


@dataclass
class SearchResult:
    """Outcome of one search (contract shared with ``select.network`` / ``select.cli``)."""

    records: list[BurstRecord]
    product_type: ProductType
    findings: list[Finding] = field(default_factory=list)
    query: dict[str, Any] = field(default_factory=dict)

    @property
    def dates(self) -> list[date]:
        return sorted({r.acquisition_date for r in self.records})

    @property
    def burst_ids(self) -> list[str]:
        return sorted({r.full_burst_id for r in self.records})

    @property
    def relative_orbits(self) -> list[int]:
        return sorted({r.relative_orbit for r in self.records})

    @property
    def ok(self) -> bool:
        return not any(f.is_fail for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CANDIDATES_SCHEMA_VERSION,
            "product_type": self.product_type,
            "query": self.query,
            "records": [r.model_dump(mode="json") for r in self.records],
            "findings": [f.model_dump(mode="json") for f in self.findings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchResult:
        product_type = cast(ProductType, str(data.get("product_type", "BURST")).upper())
        return cls(
            records=[BurstRecord.model_validate(r) for r in data.get("records", [])],
            product_type=product_type,
            findings=[Finding.model_validate(f) for f in data.get("findings", [])],
            query=dict(data.get("query", {})),
        )

    def summary(self) -> dict[str, Any]:
        per_date = {d.isoformat(): 0 for d in self.dates}
        for r in self.records:
            per_date[r.acquisition_date.isoformat()] += 1
        return {
            "product_type": self.product_type,
            "n_records": len(self.records),
            "n_dates": len(self.dates),
            "dates": [d.isoformat() for d in self.dates],
            "relative_orbits": self.relative_orbits,
            "flight_directions": sorted({r.flight_direction for r in self.records}),
            "polarizations": sorted({r.polarization for r in self.records}),
            "burst_ids": self.burst_ids,
            "records_per_date": per_date,
        }


# ---------------------------------------------------------------------------- AOI


def _geojson_to_geometry(obj: Any) -> BaseGeometry:
    if not isinstance(obj, dict) or "type" not in obj:
        raise ValueError("not a GeoJSON object")
    kind = obj["type"]
    if kind == "FeatureCollection":
        geoms = [_geojson_to_geometry(f) for f in obj.get("features", [])]
        return unary_union([g for g in geoms if not g.is_empty]) if geoms else GeometryCollection()
    if kind == "Feature":
        geometry = obj.get("geometry")
        if geometry is None:
            return GeometryCollection()
        return _geojson_to_geometry(geometry)
    return shape(obj)


def aoi_to_wkt(aoi_path: Path) -> str:
    """GeoJSON (Feature / FeatureCollection / Geometry) or WKT text file -> WKT ``POLYGON``.

    The result is what CMR needs (and what asf_search's ``validate_wkt`` would produce):
    one valid polygon, holes dropped, counter-clockwise exterior, 6-decimal lon/lat. Multi-
    part inputs are unioned; if they stay multi-part the convex hull is used (asf_search does
    the same, see ``asf_search/WKT/validate_wkt.py::_simplify_geometry``).
    """
    path = Path(aoi_path)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(t(f"{_NS}.aoi.invalid", error=str(exc))) from exc
    if not text:
        raise ValueError(t(f"{_NS}.aoi.empty", path=mask_text(str(path))))
    try:
        geom = (
            _geojson_to_geometry(json.loads(text))
            if text.startswith("{")
            else shapely.from_wkt(text)
        )
    except (ValueError, TypeError, AttributeError, shapely.errors.GEOSException) as exc:
        raise ValueError(t(f"{_NS}.aoi.invalid", error=str(exc))) from exc
    if geom is None or geom.is_empty:
        raise ValueError(t(f"{_NS}.aoi.empty", path=mask_text(str(path))))
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    if geom.geom_type != "Polygon":
        merged = unary_union(geom)
        geom = merged if merged.geom_type == "Polygon" else merged.convex_hull
    if geom.geom_type != "Polygon":
        raise ValueError(t(f"{_NS}.aoi.unsupported", geom_type=geom.geom_type))
    polygon = orient(Polygon(cast(Polygon, geom).exterior), sign=1.0)
    return str(shapely.to_wkt(polygon, rounding_precision=6, trim=True))


# ---------------------------------------------------------------------------- query


def build_query(cfg: Config, aoi_wkt: str, product_type: ProductType) -> dict[str, Any]:
    """Keyword arguments for ``asf_search.search`` (JSON-serialisable, recorded in results)."""
    start = datetime.combine(cfg.time_range.start, time.min)
    end = datetime.combine(cfg.time_range.end, time.max.replace(microsecond=0))
    platforms = [_PLATFORM_KEYWORDS[p] for p in cfg.data.platform if p in _PLATFORM_KEYWORDS]
    pol = cfg.data.polarization.upper()
    query: dict[str, Any] = {
        # source: asf_search/search/search.py keyword list; validator_map.py 'intersectsWith'
        "intersectsWith": aoi_wkt,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # source: asf_search/constants/PRODUCT_TYPE.py (BURST = 'BURST', SLC = 'SLC')
        "processingLevel": product_type,
        # source: asf_search/constants/BEAMMODE.py (IW = 'IW')
        "beamMode": "IW",
        "polarization": [pol]
        if product_type == "BURST"
        else _SLC_POLARIZATION_FILTER.get(pol, [pol]),
    }
    if platforms:
        query["platform"] = platforms
    direction = _FLIGHT_DIRECTION.get(cfg.data.orbit_direction)
    if direction:
        query["flightDirection"] = direction
    if cfg.data.relative_orbit != "auto":
        query["relativeOrbit"] = int(cfg.data.relative_orbit)
    return query


def _finding(
    rule: str,
    severity: Literal["FAIL", "WARN", "INFO"],
    params: dict[str, Any],
    evidence: dict[str, Any] | None = None,
    scope: str | None = None,
) -> Finding:
    return Finding(
        rule_id=rule,
        severity=severity,
        message_key=f"{_NS}.{rule}.cause",
        params=params,
        evidence=cast(dict[str, Any], mask_mapping(evidence or {})),
        fix_key=f"{_NS}.{rule}.fix",
        scope=scope,
    )


def _run_asf_search(query: dict[str, Any], session: Any) -> Any:
    """Call ``asf_search.search`` (looked up at call time so tests can monkeypatch it)."""
    import asf_search  # lazy, heavy import

    opts = asf_search.ASFSearchOptions(session=session) if session is not None else None
    # source: asf_search/search/search.py::search(**kwargs, opts=ASFSearchOptions | None)
    return asf_search.search(**query, opts=opts)


def _filter_to_aoi(records: list[BurstRecord], aoi_wkt: str) -> tuple[list[BurstRecord], int]:
    aoi = shapely.from_wkt(aoi_wkt)
    kept: list[BurstRecord] = []
    for rec in records:
        try:
            footprint = shapely.from_wkt(rec.footprint_wkt)
        except shapely.errors.GEOSException:
            kept.append(rec)
            continue
        if footprint.is_empty or footprint.intersects(aoi):
            kept.append(rec)
    return kept, len(records) - len(kept)


def search_bursts(cfg: Config, aoi_wkt: str, session: Any = None) -> SearchResult:
    """Query BURST products (then SLC as fallback) for ``cfg`` and ``aoi_wkt``.

    ``session`` is an optional authenticated ``ASFSession`` that is attached to the search
    options so that ``product.download`` later works; CMR itself needs no auth.
    Search errors never raise: they become a ``SEL-SEARCH-05`` FAIL finding.
    """
    findings: list[Finding] = []
    order: list[ProductType] = ["BURST", "SLC"] if cfg.data.product == "burst" else ["SLC"]
    query: dict[str, Any] = {}
    records: list[BurstRecord] = []
    product_type: ProductType = order[0]
    n_dropped = 0
    complete = True
    for product_type in order:
        query = build_query(cfg, aoi_wkt, product_type)
        try:
            results = _run_asf_search(query, session)
        except Exception as exc:
            findings.append(
                _finding(
                    "SEL-SEARCH-05",
                    "FAIL",
                    {"error_type": type(exc).__name__, "error": mask_text(str(exc))},
                    {"query": query},
                    scope="search",
                )
            )
            return SearchResult(
                records=[], product_type=product_type, findings=findings, query=query
            )
        complete = bool(getattr(results, "searchComplete", True))
        records = records_from_asf(list(results), preferred_polarization=cfg.data.polarization)
        records, n_dropped = _filter_to_aoi(records, aoi_wkt)
        if records:
            break
        if product_type == "BURST" and len(order) > 1:
            continue
    if cfg.data.product == "burst" and product_type == "SLC" and records:
        findings.append(
            _finding(
                "SEL-SEARCH-02",
                "INFO",
                {"n_slc": len(records)},
                {"burst_query": build_query(cfg, aoi_wkt, "BURST")},
                scope="search",
            )
        )
    if not complete:
        findings.append(
            _finding(
                "SEL-SEARCH-04", "WARN", {"n_records": len(records)}, {"query": query}, "search"
            )
        )
    if not records:
        findings.append(
            _finding(
                "SEL-SEARCH-03",
                "WARN",
                {
                    "product_type": "BURST/SLC" if len(order) > 1 else product_type,
                    "start": cfg.time_range.start.isoformat(),
                    "end": cfg.time_range.end.isoformat(),
                    "polarization": cfg.data.polarization,
                    "orbit_direction": cfg.data.orbit_direction,
                    "relative_orbit": str(cfg.data.relative_orbit),
                },
                {"query": query},
                scope="search",
            )
        )
        return SearchResult(records=[], product_type=product_type, findings=findings, query=query)

    result = SearchResult(
        records=records, product_type=product_type, findings=findings, query=query
    )
    tracks = ", ".join(
        f"{orbit}{'A' if d == 'ASCENDING' else 'D'}"
        for orbit, d in sorted({(r.relative_orbit, r.flight_direction) for r in records})
    )
    findings.append(
        _finding(
            "SEL-SEARCH-01",
            "INFO",
            {
                "product_type": product_type,
                "n_records": len(records),
                "n_dates": len(result.dates),
                "tracks": tracks,
                "n_bursts": len(result.burst_ids),
                "n_dropped": n_dropped,
            },
            {"summary": result.summary(), "n_dropped_outside_aoi": n_dropped},
            scope="search",
        )
    )
    return result


def search_from_config(cfg: Config, session: Any = None) -> SearchResult:
    """AOI file -> WKT -> :func:`search_bursts`, plus the offline credential check."""
    findings: list[Finding] = []
    cred = credentials_finding(cfg.data.credentials)
    if cred is not None:
        findings.append(cred)
    try:
        aoi_wkt = aoi_to_wkt(cfg.aoi)
    except ValueError as exc:
        findings.append(
            _finding(
                "SEL-SEARCH-07",
                "FAIL",
                {"path": mask_text(str(cfg.aoi)), "error": mask_text(str(exc))},
                scope="search",
            )
        )
        return SearchResult(records=[], product_type="BURST", findings=findings, query={})
    result = search_bursts(cfg, aoi_wkt, session=session)
    result.findings = findings + result.findings
    return result


# ---------------------------------------------------------------------------- persistence


def save_records(result: SearchResult, path: Path) -> Path:
    """Write ``candidates.json`` (masked, UTF-8, stable key order) and return its path."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = mask_mapping(result.to_dict())
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8"
    )
    return out


def load_records(path: Path) -> SearchResult:
    """Read ``candidates.json`` written by :func:`save_records`."""
    src = Path(path)
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("top level must be an object")
        return SearchResult.from_dict(data)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(
            t(f"{_NS}.candidates.bad_file", path=mask_text(str(src)), error=mask_text(str(exc)))
        ) from exc


__all__ = [
    "CANDIDATES_SCHEMA_VERSION",
    "ProductType",
    "SearchResult",
    "aoi_to_wkt",
    "build_query",
    "load_records",
    "save_records",
    "search_bursts",
    "search_from_config",
]
