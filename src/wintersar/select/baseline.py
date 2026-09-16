"""Perpendicular baselines (plan §5.1.2, SEL-06, open question #2, ADR-0011).

Two sources, same sign convention:

``asf``
    ``asf_search.stack_from_id(reference)`` returns the reference's burst stack with
    ``properties['perpendicularBaseline']`` (integer metres, reference = 0) and
    ``properties['temporalBaseline']`` (days). asf_search computes it *client side* from the
    two CMR state vectors of each granule (``asf_search/baseline/calc.py``), so no
    credentials are needed.
``orbit``
    Self-computation from state vectors: POEORB/RESORB ``.EOF`` files when an ``orbit_dir``
    is given, otherwise the two CMR state vectors stored in ``BurstRecord.extra['state_vectors']``.
    For each satellite the zero-Doppler time of the target is found on a cubic-Hermite
    interpolated orbit; then, with ``L`` the unit vector target -> reference satellite and
    ``v`` the reference velocity, ``B_perp = (P_sec - P_ref) . normalize(v x L)`` — the same
    projection asf_search uses (``get_up_beam_vector`` / ``get_paired_granule_baseline``), i.e.
    ``|B| sin(angle between baseline and look vector)`` with asf/Vertex sign.

Pair baselines are differences of per-date values relative to one reference date
(``B_perp(i, j) = B_perp(j) - B_perp(i)``), the usual SBAS approximation.

Validation (2026-09-16, burst 127_270859_IW2, 10 dates): ``orbit`` from CMR state vectors
agrees with the stack API within 0.5 m on every date (see ``tests/unit/select_search``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from xml.etree import ElementTree as ET  # trusted local EOF files

import numpy as np
import numpy.typing as npt
import shapely
from scipy.optimize import brentq

from wintersar.i18n import t
from wintersar.io.schemas import BurstRecord, Finding, Pair
from wintersar.util.masking import mask_mapping

Vec = npt.NDArray[np.float64]
BaselineMethod = Literal["asf", "orbit", "auto"]
_NS = "select_search"

# WGS84 (same constants as asf_search/baseline/calc.py: a = 6378137, 1/f = 298.257224)
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)

# source: sentineleof eof/products.py SentinelOrbit.FILE_REGEX / TIME_FMT
_EOF_NAME = re.compile(
    r"(?P<mission>S1A|S1B|S1C|S1D|S1E)_OPER_AUX_"
    r"(?P<orbit_type>[\w_]{6})_OPOD_"
    r"(?P<created>[T\d]{15})_"
    r"V(?P<start>[T\d]{15})_"
    r"(?P<stop>[T\d]{15})",
    re.IGNORECASE,
)
_EOF_TIME_FMT = "%Y%m%dT%H%M%S"
_ORBIT_PREFERENCE = ("POEORB", "RESORB", "PREORB")


# ---------------------------------------------------------------------------- state vectors


@dataclass(frozen=True)
class StateVector:
    """ECEF position (m) / velocity (m/s) at ``time`` (UTC, tz-aware)."""

    time: datetime
    position: tuple[float, float, float]
    velocity: tuple[float, float, float]

    @property
    def pos(self) -> Vec:
        return np.asarray(self.position, dtype=float)

    @property
    def vel(self) -> Vec:
        return np.asarray(self.velocity, dtype=float)


def to_utc(dt: datetime) -> datetime:
    """Naive datetimes are taken as UTC; aware ones are converted."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return to_utc(datetime.fromisoformat(text))


def llh_to_ecef(lat_deg: float, lon_deg: float, h_m: float = 0.0) -> Vec:
    """Geodetic WGS84 -> ECEF (closed form, N = a / sqrt(1 - e² sin²φ))."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * math.sin(lat) ** 2)
    return np.array(
        [
            (n + h_m) * math.cos(lat) * math.cos(lon),
            (n + h_m) * math.cos(lat) * math.sin(lon),
            (n * (1.0 - WGS84_E2) + h_m) * math.sin(lat),
        ],
        dtype=float,
    )


def interpolate_state(svs: Sequence[StateVector], when: datetime) -> tuple[Vec, Vec]:
    """Cubic Hermite interpolation of position/velocity between the bracketing vectors.

    With position **and** velocity at both ends the interpolation error for a 10 s
    Sentinel-1 gap is ~1 cm (h⁴ term), unlike linear interpolation (~100 m radial sag).
    Outside the arc the nearest segment is extrapolated (fine for a few seconds).
    """
    if len(svs) < 2:
        raise ValueError(t(f"{_NS}.baseline.too_few_state_vectors", n=len(svs)))
    ordered = sorted(svs, key=lambda s: s.time)
    tq = to_utc(when)
    idx = 0
    for i in range(len(ordered) - 1):
        if ordered[i + 1].time >= tq:
            idx = i
            break
    else:
        idx = len(ordered) - 2
    a, b = ordered[idx], ordered[idx + 1]
    h = (b.time - a.time).total_seconds()
    if h <= 0:
        raise ValueError("state vectors must have distinct increasing times")
    s = (tq - a.time).total_seconds() / h
    h00 = 2 * s**3 - 3 * s**2 + 1
    h10 = s**3 - 2 * s**2 + s
    h01 = -2 * s**3 + 3 * s**2
    h11 = s**3 - s**2
    pos = h00 * a.pos + h10 * h * a.vel + h01 * b.pos + h11 * h * b.vel
    d00 = 6 * s**2 - 6 * s
    d10 = 3 * s**2 - 4 * s + 1
    d01 = -6 * s**2 + 6 * s
    d11 = 3 * s**2 - 2 * s
    vel = (d00 * a.pos + d10 * h * a.vel + d01 * b.pos + d11 * h * b.vel) / h
    return pos, vel


def zero_doppler_time(
    svs: Sequence[StateVector],
    target_ecef: Vec,
    guess: datetime | None = None,
    max_window_s: float = 600.0,
) -> datetime:
    """Time at which ``(P(t) - T) . V(t) = 0`` (satellite broadside to the target).

    ``f`` is monotonically increasing (``V.V`` dominates), so a bracketing root finder is
    safe. The bracket starts at the arc (± 30 s) and is widened up to ``max_window_s``.
    """
    ordered = sorted(svs, key=lambda s: s.time)
    t0 = to_utc(guess) if guess is not None else ordered[0].time

    def f(seconds: float) -> float:
        pos, vel = interpolate_state(ordered, t0 + timedelta(seconds=seconds))
        return float(np.dot(pos - target_ecef, vel))

    pad = min(30.0, max_window_s)
    lo = (ordered[0].time - t0).total_seconds() - pad
    hi = (ordered[-1].time - t0).total_seconds() + pad
    step = 30.0
    while f(lo) > 0 and -lo < max_window_s:
        lo -= step
    while f(hi) < 0 and hi < max_window_s:
        hi += step
    if f(lo) > 0 or f(hi) < 0:
        raise ValueError(t(f"{_NS}.baseline.no_zero_doppler", granule=t0.isoformat()))
    root = float(brentq(f, lo, hi, xtol=1e-6))
    return t0 + timedelta(seconds=root)


@dataclass(frozen=True)
class BaselineComponents:
    perpendicular_m: float
    parallel_m: float
    along_track_m: float
    total_m: float
    t_ref: datetime
    t_sec: datetime


def baseline_components(
    ref_sv: Sequence[StateVector],
    sec_sv: Sequence[StateVector],
    target_llh: tuple[float, float, float],
    t_ref_guess: datetime | None = None,
    t_sec_guess: datetime | None = None,
) -> BaselineComponents:
    """Perpendicular / parallel / along-track components of ``P_sec - P_ref`` at zero Doppler."""
    target = llh_to_ecef(*target_llh)
    t_ref = zero_doppler_time(ref_sv, target, t_ref_guess)
    t_sec = zero_doppler_time(sec_sv, target, t_sec_guess)
    p_ref, v_ref = interpolate_state(ref_sv, t_ref)
    p_sec, _ = interpolate_state(sec_sv, t_sec)
    look = p_ref - target
    look /= np.linalg.norm(look)
    v_hat = v_ref / np.linalg.norm(v_ref)
    normal = np.cross(v_hat, look)  # asf_search get_up_beam_vector(velocity, along_beam)
    normal /= np.linalg.norm(normal)
    b = p_sec - p_ref
    return BaselineComponents(
        perpendicular_m=float(np.dot(b, normal)),
        parallel_m=float(np.dot(b, look)),
        along_track_m=float(np.dot(b, v_hat)),
        total_m=float(np.linalg.norm(b)),
        t_ref=t_ref,
        t_sec=t_sec,
    )


def perpendicular_baseline_from_state_vectors(
    ref_sv: Sequence[StateVector],
    sec_sv: Sequence[StateVector],
    target_llh: tuple[float, float, float],
    t_ref_guess: datetime | None = None,
    t_sec_guess: datetime | None = None,
) -> float:
    """Signed perpendicular baseline (m) of the secondary w.r.t. the reference orbit.

    ``B_perp = (P_sec - P_ref) . n``, ``n = normalize(v_ref x L)``, ``L`` = target -> P_ref,
    both satellites evaluated at their zero-Doppler time for ``target_llh`` (lat, lon, h).
    """
    return baseline_components(ref_sv, sec_sv, target_llh, t_ref_guess, t_sec_guess).perpendicular_m


# ---------------------------------------------------------------------------- orbit files


def parse_eof_orbit(path: Path) -> list[StateVector]:
    """Parse a Sentinel-1 ``AUX_POEORB`` / ``AUX_RESORB`` EOF file.

    Elements: ``Data_Block/List_of_OSVs/OSV`` with ``UTC`` (``'UTC=2024-01-06T22:59:42.000000'``,
    prefix stripped), ``X Y Z`` (m) and ``VX VY VZ`` (m/s).
    source: ISCE2 components/isceobj/Sensor/TOPS/Sentinel1.py::extractPreciseOrbit;
    isce-framework/s1-reader src/s1reader/s1_reader.py::get_burst_orbit
    """
    root = ET.parse(Path(path)).getroot()
    osv_list = root.find("Data_Block/List_of_OSVs")
    if osv_list is None:
        raise ValueError(f"{path}: no Data_Block/List_of_OSVs element")
    out: list[StateVector] = []
    for osv in osv_list.findall("OSV"):
        utc = osv.findtext("UTC")
        if utc is None:
            continue
        stamp = utc.strip()
        if stamp.upper().startswith("UTC="):
            stamp = stamp[4:]
        out.append(
            StateVector(
                time=_parse_iso(stamp),
                position=(
                    float(osv.findtext("X") or "nan"),
                    float(osv.findtext("Y") or "nan"),
                    float(osv.findtext("Z") or "nan"),
                ),
                velocity=(
                    float(osv.findtext("VX") or "nan"),
                    float(osv.findtext("VY") or "nan"),
                    float(osv.findtext("VZ") or "nan"),
                ),
            )
        )
    out.sort(key=lambda s: s.time)
    return out


def parse_eof_name(path: Path) -> dict[str, Any] | None:
    """``{'mission', 'orbit_type', 'created', 'start', 'stop'}`` from an EOF filename."""
    m = _EOF_NAME.search(Path(path).name)
    if m is None:
        return None
    try:
        return {
            "mission": m.group("mission").upper(),
            "orbit_type": m.group("orbit_type").upper(),
            "created": datetime.strptime(m.group("created"), _EOF_TIME_FMT).replace(tzinfo=UTC),
            "start": datetime.strptime(m.group("start"), _EOF_TIME_FMT).replace(tzinfo=UTC),
            "stop": datetime.strptime(m.group("stop"), _EOF_TIME_FMT).replace(tzinfo=UTC),
        }
    except ValueError:
        return None


def find_orbit_file(orbit_dir: Path, platform: str, acquisition_time: datetime) -> Path | None:
    """Best EOF in ``orbit_dir`` (recursive) covering ``acquisition_time``: POEORB > RESORB,
    then most recently created."""
    when = to_utc(acquisition_time)
    mission = str(platform).upper()
    best: tuple[int, datetime, Path] | None = None
    for candidate in sorted(Path(orbit_dir).rglob("*")):
        if candidate.suffix.upper() != ".EOF" or not candidate.is_file():
            continue
        info = parse_eof_name(candidate)
        if info is None or info["mission"] != mission:
            continue
        if not (info["start"] <= when <= info["stop"]):
            continue
        rank = (
            _ORBIT_PREFERENCE.index(info["orbit_type"])
            if info["orbit_type"] in _ORBIT_PREFERENCE
            else len(_ORBIT_PREFERENCE)
        )
        key = (-rank, info["created"], candidate)
        if best is None or key > best:
            best = key
    return best[2] if best else None


def state_vectors_from_record(record: BurstRecord) -> list[StateVector] | None:
    """The two CMR state vectors stored by ``metadata.burst_record_from_asf`` (or ``None``)."""
    raw = record.extra.get("state_vectors")
    if not isinstance(raw, Mapping):
        return None
    out: list[StateVector] = []
    for key in ("pre", "post"):
        entry = raw.get(key)
        if not isinstance(entry, Mapping):
            return None
        try:
            out.append(
                StateVector(
                    time=_parse_iso(str(entry["time"])),
                    position=tuple(float(x) for x in entry["position"]),  # type: ignore[arg-type]
                    velocity=tuple(float(x) for x in entry["velocity"]),  # type: ignore[arg-type]
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    return sorted(out, key=lambda s: s.time)


def orbit_for_record(
    record: BurstRecord, orbit_dir: Path | None = None
) -> tuple[list[StateVector], str] | None:
    """``(state vectors, source)`` with source ``'POEORB'``/``'RESORB'``/``'cmr'``, else ``None``."""
    if orbit_dir is not None:
        eof = find_orbit_file(orbit_dir, record.platform.value, record.acquisition_time)
        if eof is not None:
            info = parse_eof_name(eof) or {}
            svs = parse_eof_orbit(eof)
            if len(svs) >= 2:
                return svs, str(info.get("orbit_type", "EOF"))
    svs_cmr = state_vectors_from_record(record)
    if svs_cmr is not None:
        return svs_cmr, "cmr"
    return None


def record_target_llh(record: BurstRecord) -> tuple[float, float, float]:
    """Scene centre used as the baseline target (CMR CENTER_LAT/LON like asf_search, else
    footprint centroid)."""
    lat = record.extra.get("center_lat")
    lon = record.extra.get("center_lon")
    if isinstance(lat, int | float) and isinstance(lon, int | float):
        return float(lat), float(lon), 0.0
    centroid = shapely.from_wkt(record.footprint_wkt).centroid
    return float(centroid.y), float(centroid.x), 0.0


# ---------------------------------------------------------------------------- per-record values


def perpendicular_baselines_asf(
    records: list[BurstRecord],
    reference: BurstRecord,
    session: Any = None,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, float | None]:
    """``{granule_id: B_perp}`` relative to ``reference`` from the asf_search stack API.

    Uses ``asf_search.stack_from_id(reference.granule_id, opts)``
    (source: asf_search/search/baseline_search.py). For bursts the stack is every date of the
    same ``fullBurstID``/polarization; ``start``/``end`` (default: the records' date span ± 1
    day) keep the query small. Records that are *other* bursts of the same acquisition are
    matched by date and relative orbit (same orbit -> same B_perp to within metres;
    evidence key ``matched_by='date'``). Unmatched records map to ``None``.
    """
    import asf_search  # lazy

    if not records:
        return {}
    dates = [r.acquisition_date for r in [*records, reference]]
    lo = (start or min(dates)) - timedelta(days=1)
    hi = (end or max(dates)) + timedelta(days=1)
    opts = asf_search.ASFSearchOptions(
        start=f"{lo.isoformat()}T00:00:00Z", end=f"{hi.isoformat()}T23:59:59Z"
    )
    if session is not None:
        opts.session = session
    stack = asf_search.stack_from_id(reference.granule_id, opts=opts)

    by_name: dict[str, float | None] = {}
    by_date: dict[tuple[date, int], float | None] = {}
    for product in stack:
        props = product.properties
        value = props.get("perpendicularBaseline")  # int metres or None (missing state vectors)
        bperp = float(value) if value is not None else None
        name = str(props.get("sceneName"))
        by_name[name] = bperp
        try:
            when = _parse_iso(str(props.get("startTime"))).date()
            key = (when, int(props.get("pathNumber") or reference.relative_orbit))
        except (ValueError, TypeError):
            continue
        if key not in by_date or by_date[key] is None:
            by_date[key] = bperp
    out: dict[str, float | None] = {}
    for rec in records:
        if rec.granule_id in by_name:
            out[rec.granule_id] = by_name[rec.granule_id]
        else:
            out[rec.granule_id] = by_date.get((rec.acquisition_date, rec.relative_orbit))
    return out


def perpendicular_baselines_orbit(
    records: list[BurstRecord],
    reference: BurstRecord,
    target_llh: tuple[float, float, float] | None = None,
    orbit_dir: Path | None = None,
) -> dict[str, float | None]:
    """``{granule_id: B_perp}`` relative to ``reference`` computed from state vectors."""
    ref_orbit = orbit_for_record(reference, orbit_dir)
    if ref_orbit is None:
        return {r.granule_id: None for r in records}
    ref_sv, _ = ref_orbit
    target = target_llh or record_target_llh(reference)
    out: dict[str, float | None] = {}
    for rec in records:
        if rec.granule_id == reference.granule_id:
            out[rec.granule_id] = 0.0
            continue
        sec_orbit = orbit_for_record(rec, orbit_dir)
        if sec_orbit is None:
            out[rec.granule_id] = None
            continue
        try:
            out[rec.granule_id] = perpendicular_baseline_from_state_vectors(
                ref_sv,
                sec_orbit[0],
                target,
                t_ref_guess=reference.acquisition_time,
                t_sec_guess=rec.acquisition_time,
            )
        except ValueError:
            out[rec.granule_id] = None
    return out


def baselines_by_date(
    records: Iterable[BurstRecord], values: Mapping[str, float | None]
) -> dict[date, float | None]:
    """Average the per-granule values of each acquisition date (``None`` if none available)."""
    buckets: dict[date, list[float]] = {}
    for rec in records:
        buckets.setdefault(rec.acquisition_date, [])
        value = values.get(rec.granule_id)
        if value is not None:
            buckets[rec.acquisition_date].append(float(value))
    return {d: (sum(v) / len(v) if v else None) for d, v in buckets.items()}


def choose_reference(records: Sequence[BurstRecord]) -> BurstRecord:
    """Median-date record whose burst id appears on the most dates (stable tie-break)."""
    if not records:
        raise ValueError("no records")
    dates = sorted({r.acquisition_date for r in records})
    median = dates[len(dates) // 2]
    dates_per_burst: dict[str, set[date]] = {}
    for r in records:
        dates_per_burst.setdefault(r.full_burst_id, set()).add(r.acquisition_date)
    candidates = sorted(
        (r for r in records if r.acquisition_date == median),
        key=lambda r: (-len(dates_per_burst[r.full_burst_id]), r.full_burst_id, r.granule_id),
    )
    return candidates[0]


def compute_pair_baselines(
    records: list[BurstRecord],
    pairs: list[Pair],
    method: BaselineMethod = "asf",
    *,
    reference: BurstRecord | None = None,
    session: Any = None,
    orbit_dir: Path | None = None,
    target_llh: tuple[float, float, float] | None = None,
    findings: list[Finding] | None = None,
) -> list[Pair]:
    """Return copies of ``pairs`` with ``perp_baseline_m`` filled (``None`` when unavailable).

    ``method='asf'`` uses the stack API, ``'orbit'`` the self-computation, ``'auto'`` the stack
    API with orbit fallback for missing dates (SEL-SEARCH-08 INFO). Dates without a value are
    reported as SEL-SEARCH-06 WARN in ``findings`` (when given).
    """
    if method not in ("asf", "orbit", "auto"):
        raise ValueError(t(f"{_NS}.baseline.method_unknown", method=method))
    if not pairs or not records:
        return list(pairs)
    ref = reference or choose_reference(records)
    values: dict[str, float | None] = {r.granule_id: None for r in records}
    sources: dict[str, str] = {}
    if method in ("asf", "auto"):
        try:
            values.update(perpendicular_baselines_asf(records, ref, session=session))
        except Exception as exc:
            if findings is not None:
                findings.append(
                    Finding(
                        rule_id="SEL-SEARCH-05",
                        severity="WARN" if method == "auto" else "FAIL",
                        message_key=f"{_NS}.SEL-SEARCH-05.cause",
                        params={"error_type": type(exc).__name__, "error": str(exc)},
                        evidence=cast(dict[str, Any], mask_mapping({"reference": ref.granule_id})),
                        fix_key=f"{_NS}.SEL-SEARCH-05.fix",
                        scope="baseline",
                    )
                )
        sources.update({gid: "asf" for gid, v in values.items() if v is not None})
    if method == "orbit" or (method == "auto" and any(v is None for v in values.values())):
        subset = (
            records
            if method == "orbit"
            else [r for r in records if values.get(r.granule_id) is None]
        )
        orbit_values = perpendicular_baselines_orbit(
            subset, ref, target_llh=target_llh, orbit_dir=orbit_dir
        )
        for gid, value in orbit_values.items():
            if value is not None and values.get(gid) is None:
                values[gid] = value
                info = orbit_for_record(next(r for r in records if r.granule_id == gid), orbit_dir)
                sources[gid] = info[1] if info else "orbit"
        if method == "auto" and findings is not None:
            n_orbit = sum(1 for gid in orbit_values if values.get(gid) is not None)
            if n_orbit:
                findings.append(
                    Finding(
                        rule_id="SEL-SEARCH-08",
                        severity="INFO",
                        message_key=f"{_NS}.SEL-SEARCH-08.cause",
                        params={
                            "n_orbit": n_orbit,
                            "source": ", ".join(
                                sorted({sources[g] for g in orbit_values if g in sources})
                            ),
                        },
                        evidence={
                            "granules": sorted(g for g in orbit_values if values.get(g) is not None)
                        },
                        fix_key=f"{_NS}.SEL-SEARCH-08.fix",
                        scope="baseline",
                    )
                )
    by_date = baselines_by_date(records, values)
    out: list[Pair] = []
    missing: set[date] = set()
    for pair in pairs:
        a = by_date.get(pair.reference)
        b = by_date.get(pair.secondary)
        if a is None:
            missing.add(pair.reference)
        if b is None:
            missing.add(pair.secondary)
        perp = (b - a) if (a is not None and b is not None) else None
        out.append(pair.model_copy(update={"perp_baseline_m": perp}))
    if missing and findings is not None:
        findings.append(
            Finding(
                rule_id="SEL-SEARCH-06",
                severity="WARN",
                message_key=f"{_NS}.SEL-SEARCH-06.cause",
                params={
                    "n_missing": len(missing),
                    "dates": ", ".join(d.isoformat() for d in sorted(missing)),
                },
                evidence={
                    "reference": ref.granule_id,
                    "method": method,
                    "sources": sorted(set(sources.values())),
                },
                fix_key=f"{_NS}.SEL-SEARCH-06.fix",
                scope="baseline",
            )
        )
    return out


__all__ = [
    "WGS84_A",
    "WGS84_F",
    "BaselineComponents",
    "BaselineMethod",
    "StateVector",
    "baseline_components",
    "baselines_by_date",
    "choose_reference",
    "compute_pair_baselines",
    "find_orbit_file",
    "interpolate_state",
    "llh_to_ecef",
    "orbit_for_record",
    "parse_eof_name",
    "parse_eof_orbit",
    "perpendicular_baseline_from_state_vectors",
    "perpendicular_baselines_asf",
    "perpendicular_baselines_orbit",
    "record_target_llh",
    "state_vectors_from_record",
    "to_utc",
    "zero_doppler_time",
]
