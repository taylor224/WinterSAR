"""Ground-truth (levelling / GNSS) CSV import and LOS projection (plan §5.6, R-10).

CSV schema (plan §5.6, header required, UTF-8, comma separated)::

    site_id, lat, lon, elev_m, date, up_m, east_m, north_m, method, sigma_mm

* ``method`` is ``leveling`` (alias ``levelling``) or ``gnss`` (case-insensitive).
* Levelling rows need ``up_m`` only; GNSS rows need ``east_m``, ``north_m`` **and** ``up_m``.
* ``elev_m`` and ``sigma_mm`` are optional (empty cell → ``None``).
* Displacements are **metres** (a magnitude above :data:`MAX_ABS_DISPLACEMENT_M` is rejected as
  a probable millimetre column, VAL-007); ``sigma_mm`` is millimetres.
* ``date`` is ISO ``YYYY-MM-DD`` (``YYYYMMDD`` accepted).

Every problem is reported as a :class:`wintersar.io.schemas.Finding` (``VAL-00x``) carried by
:class:`GroundTruthError`; the text lives in ``i18n/<lang>/validate.yaml`` (cause → fix).

NGII (국토지리정보원) levelling / GNSS CORS adapters are *not* implemented: ADR-0043 records what
the public portals offer (interactive download after login, no documented bulk API) and
``docs/open-questions.md`` carries the follow-up. CSV import is the required path.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from wintersar.i18n import t
from wintersar.io.schemas import Finding, GroundTruthRecord
from wintersar.io.timeseries import TimeSeries
from wintersar.util.masking import mask_text
from wintersar.validate.los import enu_to_los, vertical_to_los

COLUMNS: tuple[str, ...] = (
    "site_id",
    "lat",
    "lon",
    "elev_m",
    "date",
    "up_m",
    "east_m",
    "north_m",
    "method",
    "sigma_mm",
)
REQUIRED_COLUMNS: tuple[str, ...] = ("site_id", "lat", "lon", "date", "method")
METHOD_ALIASES: dict[str, str] = {
    "leveling": "leveling",
    "levelling": "leveling",
    "level": "leveling",
    "gnss": "gnss",
    "gps": "gnss",
}
# Unit sanity limit: cumulative ground displacement above 10 m is not plausible for the
# InSAR use case; such values are almost certainly millimetres pasted into a metre column.
MAX_ABS_DISPLACEMENT_M: float = 10.0


class GroundTruthError(ValueError):
    """CSV problem; ``finding`` carries the VAL-00x id and i18n keys (cause → fix)."""

    def __init__(self, finding: Finding) -> None:
        self.finding = finding
        super().__init__(t(finding.message_key, **finding.params))


def make_finding(rule_id: str, severity: str = "FAIL", **params: Any) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message_key=f"validate.{rule_id}.cause",
        fix_key=f"validate.{rule_id}.fix",
        params=params,
        evidence=dict(params),
    )


def _parse_date(value: str, line: int) -> date:
    v = value.strip()
    try:
        if len(v) == 8 and v.isdigit():
            return date(int(v[:4]), int(v[4:6]), int(v[6:]))
        return date.fromisoformat(v)
    except ValueError as exc:
        raise GroundTruthError(make_finding("VAL-002", column="date", line=line, value=v)) from exc


def _parse_float(value: str | None, column: str, line: int) -> float | None:
    if value is None:
        return None
    v = value.strip()
    if v == "" or v.lower() in ("nan", "na", "null", "none"):
        return None
    try:
        f = float(v)
    except ValueError as exc:
        raise GroundTruthError(make_finding("VAL-002", column=column, line=line, value=v)) from exc
    if not np.isfinite(f):
        raise GroundTruthError(make_finding("VAL-002", column=column, line=line, value=v))
    return f


def _check_units(value: float | None, column: str, line: int) -> None:
    if value is not None and abs(value) > MAX_ABS_DISPLACEMENT_M:
        raise GroundTruthError(
            make_finding(
                "VAL-007", column=column, line=line, value=value, limit=MAX_ABS_DISPLACEMENT_M
            )
        )


def parse_rows(
    rows: Iterable[dict[str, str | None]], source: str = "<csv>"
) -> list[GroundTruthRecord]:
    """Validate dict rows (as from ``csv.DictReader``) into :class:`GroundTruthRecord` list."""
    records: list[GroundTruthRecord] = []
    for line, raw in enumerate(rows, start=2):  # header is line 1
        row = {
            str(k).strip().lower(): (v.strip() if isinstance(v, str) else v)
            for k, v in raw.items()
            if k
        }
        if not any(v for v in row.values()):
            continue  # blank line
        site_id = row.get("site_id") or ""
        if not site_id:
            raise GroundTruthError(make_finding("VAL-002", column="site_id", line=line, value=""))
        method_raw = (row.get("method") or "").lower()
        method = METHOD_ALIASES.get(method_raw)
        if method is None:
            raise GroundTruthError(make_finding("VAL-005", method=method_raw, line=line))
        lat = _parse_float(row.get("lat"), "lat", line)
        lon = _parse_float(row.get("lon"), "lon", line)
        if lat is None or not -90.0 <= lat <= 90.0:
            raise GroundTruthError(
                make_finding("VAL-002", column="lat", line=line, value=row.get("lat"))
            )
        if lon is None or not -180.0 <= lon <= 180.0:
            raise GroundTruthError(
                make_finding("VAL-002", column="lon", line=line, value=row.get("lon"))
            )
        up = _parse_float(row.get("up_m"), "up_m", line)
        east = _parse_float(row.get("east_m"), "east_m", line)
        north = _parse_float(row.get("north_m"), "north_m", line)
        for col, val in (("up_m", up), ("east_m", east), ("north_m", north)):
            _check_units(val, col, line)
        if method == "leveling" and up is None:
            raise GroundTruthError(make_finding("VAL-003", line=line, site_id=site_id))
        if method == "gnss" and (up is None or east is None or north is None):
            raise GroundTruthError(make_finding("VAL-004", line=line, site_id=site_id))
        sigma = _parse_float(row.get("sigma_mm"), "sigma_mm", line)
        if sigma is not None and sigma < 0:
            raise GroundTruthError(
                make_finding("VAL-002", column="sigma_mm", line=line, value=sigma)
            )
        records.append(
            GroundTruthRecord(
                site_id=site_id,
                lat=lat,
                lon=lon,
                elev_m=_parse_float(row.get("elev_m"), "elev_m", line),
                date=_parse_date(row.get("date") or "", line),
                up_m=up,
                east_m=east,
                north_m=north,
                method=method,
                sigma_mm=sigma,
            )
        )
    if not records:
        raise GroundTruthError(make_finding("VAL-006", path=mask_text(source)))
    return records


def load_csv(path: Path | str) -> list[GroundTruthRecord]:
    """Read and validate the ground-truth CSV (raises :class:`GroundTruthError`)."""
    p = Path(path)
    if not p.exists():
        raise GroundTruthError(make_finding("VAL-006", path=mask_text(str(p))))
    with p.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip().lower() for h in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise GroundTruthError(
                make_finding("VAL-001", columns=", ".join(missing), path=mask_text(str(p)))
            )
        return parse_rows(reader, source=str(p))


def write_csv(records: Sequence[GroundTruthRecord], path: Path | str) -> Path:
    """Write records in the canonical column order (used for fixtures and conversions)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in records:
            w.writerow(
                [
                    r.site_id,
                    f"{r.lat:.7f}",
                    f"{r.lon:.7f}",
                    "" if r.elev_m is None else f"{r.elev_m:.3f}",
                    r.date.isoformat(),
                    "" if r.up_m is None else f"{r.up_m:.6f}",
                    "" if r.east_m is None else f"{r.east_m:.6f}",
                    "" if r.north_m is None else f"{r.north_m:.6f}",
                    r.method,
                    "" if r.sigma_mm is None else f"{r.sigma_mm:.3f}",
                ]
            )
    return p


def group_by_site(records: Iterable[GroundTruthRecord]) -> dict[str, list[GroundTruthRecord]]:
    """``{site_id: [records sorted by date]}`` (insertion order of first appearance)."""
    out: dict[str, list[GroundTruthRecord]] = {}
    for r in records:
        out.setdefault(r.site_id, []).append(r)
    for rs in out.values():
        rs.sort(key=lambda r: r.date)
    return out


@dataclass(frozen=True)
class LosSample:
    """One ground-truth observation projected onto the radar LOS at the nearest pixel."""

    site_id: str
    date: date
    lat: float
    lon: float
    row: int
    col: int
    los_m: float
    incidence_deg: float
    heading_deg: float | None
    method: str
    sigma_mm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "date": self.date.isoformat(),
            "lat": self.lat,
            "lon": self.lon,
            "row": self.row,
            "col": self.col,
            "los_m": self.los_m,
            "incidence_deg": self.incidence_deg,
            "heading_deg": self.heading_deg,
            "method": self.method,
            "sigma_mm": self.sigma_mm,
        }


def to_los(
    records: Iterable[GroundTruthRecord],
    ts: TimeSeries,
    heading_deg: float | None = None,
    incidence_deg: float | None = None,
) -> list[LosSample]:
    """Project every record onto the LOS using incidence (and heading for GNSS) at the nearest
    time-series pixel.

    Levelling rows use ``up_m * cos(inc)`` (no heading needed); GNSS rows use the full MintPy
    ``enu2los`` (:func:`wintersar.validate.los.enu_to_los`). ``heading_deg`` /
    ``incidence_deg`` override the values stored in ``ts``.
    """
    inc2d = ts.incidence2d()
    heading = heading_deg if heading_deg is not None else ts.heading_deg
    out: list[LosSample] = []
    for r in records:
        row, col = ts.nearest_pixel(r.lat, r.lon)
        if incidence_deg is not None:
            inc = float(incidence_deg)
        elif inc2d is not None and np.isfinite(inc2d[row, col]):
            inc = float(inc2d[row, col])
        else:
            raise GroundTruthError(make_finding("VAL-008", site_id=r.site_id, what="incidence_deg"))
        if r.method == "leveling":
            assert r.up_m is not None  # validated in parse_rows
            los = float(vertical_to_los(r.up_m, inc))
        else:
            if heading is None:
                raise GroundTruthError(
                    make_finding("VAL-008", site_id=r.site_id, what="heading_deg")
                )
            assert r.east_m is not None and r.north_m is not None and r.up_m is not None
            los = float(enu_to_los(r.east_m, r.north_m, r.up_m, inc, heading))
        out.append(
            LosSample(
                site_id=r.site_id,
                date=r.date,
                lat=r.lat,
                lon=r.lon,
                row=int(row),
                col=int(col),
                los_m=los,
                incidence_deg=inc,
                heading_deg=None if r.method == "leveling" else float(heading),  # type: ignore[arg-type]
                method=r.method,
                sigma_mm=r.sigma_mm,
            )
        )
    return out


__all__ = [
    "COLUMNS",
    "MAX_ABS_DISPLACEMENT_M",
    "REQUIRED_COLUMNS",
    "GroundTruthError",
    "LosSample",
    "group_by_site",
    "load_csv",
    "make_finding",
    "parse_rows",
    "to_los",
    "write_csv",
]
