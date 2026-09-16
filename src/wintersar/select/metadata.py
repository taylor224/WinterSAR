"""Normalise asf_search products into :class:`BurstRecord` and read post-download metadata.

Field provenance (plan §5.1.1, open question #1, ADR-0010)
--------------------------------------------------------------
asf_search 14.0.0 builds ``product.properties`` from the CMR UMM-G granule record; see
``.venv/lib/python3.11/site-packages/asf_search/ASFProduct.py`` (``_base_properties``),
``Products/S1Product.py`` and ``Products/S1BurstProduct.py``. Everything in the table below
marked *search* is available from the search response **before** any download; the *xml*
rows exist only in the SAFE annotation / ASF burst metadata XML and are filled later by
:func:`enrich_record`.

=========================  ========  ==================================================
BurstRecord field          when      asf_search / CMR source
=========================  ========  ==================================================
granule_id                 search    ``properties['sceneName']`` (BURST: ``fileID`` = UMM
                                     ``GranuleUR``; SLC: ``ProducerGranuleId``)
platform                   search    ``properties['platform']`` (UMM ``ASF_PLATFORM``,
                                     e.g. ``SENTINEL-1A`` / ``Sentinel-1A``)
mode                       search    ``properties['beamModeType']`` (UMM ``BEAM_MODE``)
subswath                   search    ``properties['burst']['subswath']`` (``SUBSWATH_NAME``);
                                     SLC: all IW sub-swaths (``IW1+IW2+IW3``)
full_burst_id              search    ``properties['burst']['fullBurstID']`` (``BURST_ID_FULL``,
                                     format ``<track>_<relativeBurstID>_<subswath>`` e.g.
                                     ``127_270859_IW2``); SLC: scene name
relative_orbit             search    ``properties['pathNumber']`` (``PATH_NUMBER``)
absolute_orbit             search    ``properties['orbit']`` (``OrbitCalculatedSpatialDomains``)
flight_direction           search    ``properties['flightDirection']`` (``ASCENDING_DESCENDING``)
polarization               search    ``properties['polarization']`` (``POLARIZATION``; BURST is
                                     single-pol ``VV``, SLC is ``VV+VH`` -> first / preferred)
acquisition_time           search    UMM ``TemporalExtent.RangeDateTime.BeginningDateTime``
                                     (microseconds) else ``properties['startTime']`` (seconds)
ipf_version                search    ``properties['pgeVersion']`` (UMM ``PGEVersionClass``:
                                     PGEName ``Sentinel-1 IPF``, PGEVersion ``003.71``)
range_pixel_spacing_m      xml       ``imageAnnotation/imageInformation/rangePixelSpacing``
azimuth_pixel_spacing_m    xml       ``imageAnnotation/imageInformation/azimuthPixelSpacing``
incidence_near/far_deg     xml       ``geolocationGrid/.../incidenceAngle`` at first/last pixel
footprint_wkt              search    ``product.geometry`` (UMM ``SpatialExtent`` GPolygon)
url                        search    ``properties['url']`` (BURST: ``USE SERVICE API`` URL of
                                     the burst extractor; SLC: ``GET DATA`` zip URL)
product_type               search    ``properties['processingLevel']`` (``PROCESSING_TYPE``)
extra.state_vectors        search    ``product.baseline['stateVectors']`` (UMM ``SV_POSITION_PRE/
                                     POST``, ``SV_VELOCITY_PRE/POST``: two ECEF vectors 10 s apart)
extra.metadata_url         search    ``properties['additionalUrls'][0]`` (burst XML, BURST only)
extra.azimuth_time_interval_s search UMM ``AZIMUTH_TIME_INTERVAL`` (not mapped by asf_search)
extra.lines_per_burst      search    UMM ``LINES_PER_BURST`` (not mapped by asf_search)
=========================  ========  ==================================================

Verified against live CMR responses on 2026-09-16 (fixtures in ``tests/fixtures/asf``).
XML element paths were verified against ISCE2 ``Sentinel1.py``, isce-framework ``s1-reader``,
bopen ``xarray-sentinel`` and a real ASF burst XML from ASFHyP3 ``burst2safe`` test data.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # trusted local files; defusedxml is not a dependency
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any, Literal, cast

from shapely.geometry import shape

from wintersar.i18n import t
from wintersar.io.schemas import BurstRecord, Platform

# ---------------------------------------------------------------------------- constants

# source: asf_search/constants/PLATFORM.py (SENTINEL1A = 'Sentinel-1A' ...); CMR ASF_PLATFORM
# values are upper-case ('SENTINEL-1A') for BURST and mixed-case for SLC (observed 2026-09-16)
PLATFORM_ALIASES: dict[str, Platform] = {
    "SENTINEL-1A": Platform.S1A,
    "SENTINEL-1B": Platform.S1B,
    "SENTINEL-1C": Platform.S1C,
    "SENTINEL-1D": Platform.S1D,
    "S1A": Platform.S1A,
    "S1B": Platform.S1B,
    "S1C": Platform.S1C,
    "S1D": Platform.S1D,
}

# source: asf_search/constants/BEAMMODE.py (IW, EW, S1..S6, WV)
_SM_BEAMS = {"S1", "S2", "S3", "S4", "S5", "S6", "SM"}

# Sub-swaths contained in one IW / EW SLC scene (ESA product definition); used for the SLC
# fallback where a scene, not a burst, is the record unit.
_MODE_SUBSWATHS = {"IW": "IW1+IW2+IW3", "EW": "EW1+EW2+EW3+EW4+EW5"}

# source: manifest.safe  <safe:software name="Sentinel-1 IPF" version="003.71"/>
_SAFE_NS = "{http://www.esa.int/safe/sentinel-1.0}"
# source: ASF burst XML  <burst><manifest><xfdu:XFDU ...> (burst2safe utils.get_subxml_from_metadata)
_XFDU_NS = "{urn:ccsds:schema:xfdu:1}"

# Machine-readable copy of the provenance table (used by reports / ADR-0010 tests).
FIELD_PROVENANCE: dict[str, Literal["search", "xml"]] = {
    "granule_id": "search",
    "platform": "search",
    "mode": "search",
    "subswath": "search",
    "full_burst_id": "search",
    "relative_orbit": "search",
    "absolute_orbit": "search",
    "flight_direction": "search",
    "polarization": "search",
    "acquisition_time": "search",
    "ipf_version": "search",
    "range_pixel_spacing_m": "xml",
    "azimuth_pixel_spacing_m": "xml",
    "incidence_near_deg": "xml",
    "incidence_far_deg": "xml",
    "footprint_wkt": "search",
    "url": "search",
    "product_type": "search",
}


# ---------------------------------------------------------------------------- normalisers


def normalize_platform(value: object) -> Platform:
    """``'SENTINEL-1A'`` / ``'Sentinel-1A'`` / ``'S1A'`` -> :class:`Platform`."""
    text = str(value or "").strip().upper()
    if text in PLATFORM_ALIASES:
        return PLATFORM_ALIASES[text]
    m = re.match(r"^S(?:ENTINEL-?)?1([A-D])\b", text)
    if m:
        return Platform(f"S1{m.group(1)}")
    raise ValueError(t("select_search.metadata.unknown_platform", value=str(value)))


def normalize_mode(value: object) -> Literal["IW", "EW", "SM"]:
    text = str(value or "").strip().upper()
    if text == "IW":
        return "IW"
    if text == "EW":
        return "EW"
    if text in _SM_BEAMS:
        return "SM"
    raise ValueError(t("select_search.metadata.unknown_mode", value=str(value)))


def normalize_ipf_version(value: object) -> str | None:
    """``'003.71'`` -> ``'3.71'`` (keeps the minor part verbatim); ``None`` stays ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(".")
    if parts[0].isdigit():
        parts[0] = str(int(parts[0]))
    return ".".join(parts)


def split_polarizations(value: object) -> list[str]:
    """``'VV+VH'`` -> ``['VV', 'VH']``; ``'DUAL VV'`` -> ``['VV']``; ``'VV'`` -> ``['VV']``."""
    return re.findall(r"\b[HV]{2}\b", str(value or "").upper())


def parse_time(value: object) -> datetime:
    """ISO-8601 (``...Z`` / offset / naive) or ``datetime`` -> **naive UTC** ``datetime``.

    Naive UTC keeps records comparable with the shared ``make_burst`` test factory.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------- UMM helpers


def umm_attribute(umm: Mapping[str, Any] | None, name: str) -> str | None:
    """First value of ``AdditionalAttributes[Name == name]`` (mirrors ``ASFProduct.umm_get``)."""
    if not umm:
        return None
    for attr in umm.get("AdditionalAttributes", []) or []:
        if attr.get("Name") == name:
            values = attr.get("Values") or []
            if values and values[0] not in (None, "", "NA", "N/A", "NOT AVAILABLE"):
                return str(values[0])
            return None
    return None


def _umm_begin_time(umm: Mapping[str, Any] | None) -> str | None:
    if not umm:
        return None
    try:
        value = umm["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]
    except (KeyError, TypeError):
        return None
    return str(value) if value else None


def _float_or_none(value: object) -> float | None:
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None


def state_vectors_from_product(product: Any) -> dict[str, Any] | None:
    """Serialisable copy of ``product.baseline['stateVectors']`` (pre/post position+velocity).

    Returns ``None`` when any vector is missing (asf_search then also cannot compute the
    perpendicular baseline for this product).
    """
    baseline = getattr(product, "baseline", None)
    if not isinstance(baseline, Mapping):
        return None
    sv = baseline.get("stateVectors")
    if not isinstance(sv, Mapping):
        return None
    pos = sv.get("positions") or {}
    vel = sv.get("velocities") or {}
    out: dict[str, Any] = {}
    for key, pkey, tkey, vkey in (
        ("pre", "prePosition", "prePositionTime", "preVelocity"),
        ("post", "postPosition", "postPositionTime", "postVelocity"),
    ):
        position = pos.get(pkey)
        time = pos.get(tkey)
        velocity = vel.get(vkey)
        if position is None or time is None or velocity is None:
            return None
        out[key] = {
            "time": parse_time(time).isoformat() + "Z",
            "position": [float(x) for x in position],
            "velocity": [float(x) for x in velocity],
        }
    anx = baseline.get("ascendingNodeTime")
    if anx:
        out["ascending_node_time"] = parse_time(anx).isoformat() + "Z"
    return out


# ---------------------------------------------------------------------------- asf -> record


def burst_record_from_asf(product: Any, preferred_polarization: str | None = None) -> BurstRecord:
    """Convert an ``asf_search`` ``S1BurstProduct`` (or ``S1Product`` for the SLC fallback).

    Only *search-time* fields are filled; pixel spacing / incidence angles stay ``None`` until
    :func:`enrich_record` is applied to a downloaded annotation XML (see module docstring).
    ``preferred_polarization`` picks the channel of a dual-pol SLC (``VV+VH`` -> ``VV``).
    """
    props: Mapping[str, Any] = product.properties
    umm: Mapping[str, Any] | None = getattr(product, "umm", None)
    meta: Mapping[str, Any] | None = getattr(product, "meta", None)
    burst: Mapping[str, Any] | None = props.get("burst")  # S1BurstProduct only
    processing_level = str(props.get("processingLevel") or "").upper()
    product_type: Literal["BURST", "SLC"] = "BURST" if processing_level == "BURST" else "SLC"

    scene_name = str(props.get("sceneName") or props.get("fileID") or "")
    platform = normalize_platform(props.get("platform") or scene_name[:3])
    mode = normalize_mode(props.get("beamModeType") or umm_attribute(umm, "BEAM_MODE") or "IW")

    if burst is not None:
        subswath = str(burst.get("subswath") or umm_attribute(umm, "SUBSWATH_NAME") or "")
        full_burst_id = str(burst.get("fullBurstID") or umm_attribute(umm, "BURST_ID_FULL") or "")
    else:
        subswath = _MODE_SUBSWATHS.get(mode, str(props.get("beamModeType") or mode))
        full_burst_id = scene_name

    polarizations = split_polarizations(props.get("polarization"))
    if preferred_polarization and preferred_polarization.upper() in polarizations:
        polarization = preferred_polarization.upper()
    elif polarizations:
        polarization = polarizations[0]
    else:
        polarization = str(props.get("polarization") or "")

    begin = _umm_begin_time(umm) or props.get("startTime")
    acquisition_time = parse_time(begin)

    geometry = getattr(product, "geometry", None) or {}
    coords = geometry.get("coordinates") if isinstance(geometry, Mapping) else None
    footprint_wkt = shape(geometry).wkt if coords else "POLYGON EMPTY"

    flight_direction = str(props.get("flightDirection") or "").upper()
    if flight_direction not in ("ASCENDING", "DESCENDING"):
        raise ValueError(f"unknown flightDirection {props.get('flightDirection')!r}")

    extra: dict[str, Any] = {
        "asf_class": type(product).__name__,
        "file_id": props.get("fileID"),
        "file_name": props.get("fileName"),
        "processing_level": props.get("processingLevel"),
        "group_id": props.get("groupID"),
        "bytes": props.get("bytes"),
        "md5sum": props.get("md5sum"),
        "sensor": props.get("sensor"),
        "center_lat": _float_or_none(props.get("centerLat")),
        "center_lon": _float_or_none(props.get("centerLon")),
        "start_time": props.get("startTime"),
        "stop_time": props.get("stopTime"),
        "pge_version": props.get("pgeVersion"),
        "polarizations": polarizations,
        "additional_urls": list(props.get("additionalUrls") or []),
        "s3_urls": list(props.get("s3Urls") or []),
        "concept_id": meta.get("concept-id") if meta else None,
        "provenance": "asf_search.search (CMR UMM-G, pre-download)",
    }
    if burst is not None:
        extra["burst"] = {
            "absolute_burst_id": burst.get("absoluteBurstID"),
            "relative_burst_id": burst.get("relativeBurstID"),
            "burst_index": burst.get("burstIndex"),
            "samples_per_burst": burst.get("samplesPerBurst"),
            "azimuth_time": burst.get("azimuthTime"),
            "azimuth_anx_time": burst.get("azimuthAnxTime"),
        }
        urls = props.get("additionalUrls") or []
        extra["metadata_url"] = urls[0] if urls else None
        extra["input_granule"] = (umm or {}).get("InputGranules", [None])[0] if umm else None
    else:
        extra["frame_number"] = props.get("frameNumber")
        extra["granule_type"] = props.get("granuleType")
    # UMM attributes asf_search does not map but SEL-09/10 can use (pre-download)
    extra["azimuth_time_interval_s"] = _float_or_none(umm_attribute(umm, "AZIMUTH_TIME_INTERVAL"))
    extra["lines_per_burst"] = _int_or_none(umm_attribute(umm, "LINES_PER_BURST"))
    extra["byte_offset"] = _int_or_none(umm_attribute(umm, "BYTE_OFFSET"))
    state_vectors = state_vectors_from_product(product)
    if state_vectors is not None:
        extra["state_vectors"] = state_vectors

    return BurstRecord(
        granule_id=scene_name,
        platform=platform,
        mode=mode,
        subswath=subswath,
        full_burst_id=full_burst_id,
        relative_orbit=int(props["pathNumber"]),
        absolute_orbit=int(props.get("orbit") or 0),
        flight_direction=cast(Literal["ASCENDING", "DESCENDING"], flight_direction),
        polarization=polarization,
        acquisition_time=acquisition_time,
        ipf_version=normalize_ipf_version(props.get("pgeVersion")),
        range_pixel_spacing_m=None,
        azimuth_pixel_spacing_m=None,
        incidence_near_deg=None,
        incidence_far_deg=None,
        footprint_wkt=footprint_wkt,
        url=str(props.get("url") or ""),
        product_type=product_type,
        extra=extra,
    )


def records_from_asf(
    products: Iterable[Any], preferred_polarization: str | None = None
) -> list[BurstRecord]:
    """Convert, de-duplicate (by ``granule_id``) and sort products by time then burst id."""
    seen: dict[str, BurstRecord] = {}
    for product in products:
        rec = burst_record_from_asf(product, preferred_polarization)
        seen.setdefault(rec.granule_id, rec)
    return sorted(seen.values(), key=lambda r: (r.acquisition_time, r.full_burst_id))


# ---------------------------------------------------------------------------- XML (post-download)


def _text(elem: ET.Element | None, path: str) -> str | None:
    if elem is None:
        return None
    node = elem.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _float(elem: ET.Element | None, path: str) -> float | None:
    return _float_or_none(_text(elem, path))


def _int(elem: ET.Element | None, path: str) -> int | None:
    return _int_or_none(_text(elem, path))


def _parse_xml(xml_path: Path) -> ET.Element:
    try:
        return ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ValueError(
            t("select_search.metadata.bad_xml", path=str(xml_path), error=str(exc))
        ) from exc


def annotation_from_element(product: ET.Element) -> dict[str, Any]:
    """Read the fields we need from a SAFE ``product`` annotation element.

    Element paths (verified 2026-09-16):
    - ``adsHeader/*``                                        ISCE2 ``Sentinel1.py``, s1-reader
    - ``imageAnnotation/imageInformation/rangePixelSpacing``  xarray-sentinel ``sentinel1.py``
    - ``imageAnnotation/imageInformation/azimuthPixelSpacing`` ISCE2 ``Sentinel1.py``
    - ``imageAnnotation/imageInformation/incidenceAngleMidSwath`` ISCE2 ``Sentinel1.py``
    - ``generalAnnotation/productInformation/{rangeSamplingRate,radarFrequency}`` s1-reader
    - ``swathTiming/{linesPerBurst,samplesPerBurst,burstList}`` burst2safe ``utils.py``
    - ``geolocationGrid/geolocationGridPointList/geolocationGridPoint/{pixel,line,incidenceAngle}``
      xarray-sentinel ``sentinel1.py``
    Near/far incidence = mean ``incidenceAngle`` at the smallest / largest grid ``pixel``.
    """
    ads = product.find("adsHeader")
    image = product.find("imageAnnotation/imageInformation")
    general = product.find("generalAnnotation/productInformation")
    timing = product.find("swathTiming")

    grid = product.findall("geolocationGrid/geolocationGridPointList/geolocationGridPoint")
    by_pixel: dict[int, list[float]] = {}
    for point in grid:
        pixel = _int(point, "pixel")
        inc = _float(point, "incidenceAngle")
        if pixel is None or inc is None:
            continue
        by_pixel.setdefault(pixel, []).append(inc)
    incidence_near = fmean(by_pixel[min(by_pixel)]) if by_pixel else None
    incidence_far = fmean(by_pixel[max(by_pixel)]) if by_pixel else None

    burst_list = timing.find("burstList") if timing is not None else None
    n_bursts = _int_or_none(burst_list.get("count")) if burst_list is not None else None
    if n_bursts is None and burst_list is not None:
        n_bursts = len(burst_list.findall("burst"))

    return {
        "mission_id": _text(ads, "missionId"),
        "product_type": _text(ads, "productType"),
        "polarisation": _text(ads, "polarisation"),
        "mode": _text(ads, "mode"),
        "swath": _text(ads, "swath"),
        "start_time": _text(ads, "startTime"),
        "stop_time": _text(ads, "stopTime"),
        "absolute_orbit": _int(ads, "absoluteOrbitNumber"),
        "pass": _text(general, "pass"),
        "platform_heading_deg": _float(general, "platformHeading"),
        "range_sampling_rate_hz": _float(general, "rangeSamplingRate"),
        "radar_frequency_hz": _float(general, "radarFrequency"),
        "range_pixel_spacing_m": _float(image, "rangePixelSpacing"),
        "azimuth_pixel_spacing_m": _float(image, "azimuthPixelSpacing"),
        "incidence_mid_deg": _float(image, "incidenceAngleMidSwath"),
        "incidence_near_deg": incidence_near,
        "incidence_far_deg": incidence_far,
        "azimuth_time_interval_s": _float(image, "azimuthTimeInterval"),
        "slant_range_time_s": _float(image, "slantRangeTime"),
        "number_of_samples": _int(image, "numberOfSamples"),
        "number_of_lines": _int(image, "numberOfLines"),
        "lines_per_burst": _int(timing, "linesPerBurst"),
        "samples_per_burst": _int(timing, "samplesPerBurst"),
        "n_bursts": n_bursts,
        "n_geolocation_points": len(grid),
        "ipf_version": None,  # not in the annotation; see parse_manifest_ipf_version
    }


def parse_safe_annotation(xml_path: Path) -> dict[str, Any]:
    """Parse a SAFE ``annotation/s1a-iw2-slc-vv-*.xml`` file (root element ``product``)."""
    root = _parse_xml(Path(xml_path))
    product = root if root.tag == "product" else root.find(".//product")
    if product is None:
        raise ValueError(
            t("select_search.metadata.bad_xml", path=str(xml_path), error="no <product> element")
        )
    out = annotation_from_element(product)
    out["source"] = str(xml_path)
    return out


def ipf_version_from_element(root: ET.Element) -> str | None:
    """``safe:software[@name='Sentinel-1 IPF']/@version`` anywhere below ``root``.

    source: ISCE2 ``Sentinel1.py`` (``metadataObject[@ID="processing"]//safe:software``),
    s1-reader ``get_ipf_version`` (``processing/facility/software`` attrib ``version``).
    """
    fallback: str | None = None
    for elem in root.iter(f"{_SAFE_NS}software"):
        version = (elem.get("version") or "").strip()
        if not version:
            continue
        if "IPF" in (elem.get("name") or "").upper():
            return version
        fallback = fallback or version
    return fallback


def parse_manifest_ipf_version(manifest_path: Path) -> str | None:
    """IPF version (raw, e.g. ``'003.71'``) from ``manifest.safe``."""
    return ipf_version_from_element(_parse_xml(Path(manifest_path)))


def parse_burst_xml_metadata(
    xml_path: Path, subswath: str | None = None, polarization: str | None = None
) -> dict[str, Any]:
    """Parse the ASF burst metadata XML (``additionalUrls[0]`` of a burst product).

    Layout (verified on burst2safe test data, 2026-09-16)::

        <burst>
          <manifest><xfdu:XFDU>...<safe:software name="Sentinel-1 IPF" version="003.71"/>...
          <metadata>
            <product source_filename="s1a-iw2-slc-vv-....xml">
              <swath>IW2</swath><polarisation>VV</polarisation>
              <content> ...full SAFE annotation (adsHeader, imageAnnotation, ...)... </content>
            </product>
            <noise .../><calibration .../><rfi .../>   (same swath/polarisation/content layout)

    ``subswath``/``polarization`` select the entry; when omitted the first ``product`` is used.
    """
    path = Path(xml_path)
    root = _parse_xml(path)
    metadata = root.find("metadata") if root.tag != "metadata" else root
    entries = (
        [e for e in (metadata if metadata is not None else [])] if metadata is not None else []
    )
    products = [e for e in entries if e.tag == "product"]
    chosen: ET.Element | None = None
    for entry in products:
        sw = (_text(entry, "swath") or "").upper()
        pol = (_text(entry, "polarisation") or "").upper()
        if subswath and sw != subswath.upper():
            continue
        if polarization and pol != polarization.upper():
            continue
        chosen = entry
        break
    if chosen is None:
        raise ValueError(
            t(
                "select_search.metadata.no_product_entry",
                subswath=subswath or "*",
                polarization=polarization or "*",
                path=str(path),
            )
        )
    content = chosen.find("content")
    if content is None:
        raise ValueError(
            t("select_search.metadata.bad_xml", path=str(path), error="no <content> element")
        )
    out = annotation_from_element(content)
    out["swath"] = out["swath"] or _text(chosen, "swath")
    out["polarisation"] = out["polarisation"] or _text(chosen, "polarisation")
    out["source_filename"] = chosen.get("source_filename")
    out["available_swaths"] = sorted(
        {(_text(e, "swath") or "").upper() for e in products if _text(e, "swath")}
    )
    manifest = root.find("manifest")
    out["ipf_version"] = ipf_version_from_element(manifest if manifest is not None else root)
    out["source"] = str(path)
    return out


def enrich_record(record: BurstRecord, meta: Mapping[str, Any]) -> BurstRecord:
    """Fill the *xml* fields of ``record`` from a parsed annotation / burst XML dict."""
    update: dict[str, Any] = {}
    for field in (
        "range_pixel_spacing_m",
        "azimuth_pixel_spacing_m",
        "incidence_near_deg",
        "incidence_far_deg",
    ):
        value = meta.get(field)
        if value is not None:
            update[field] = float(value)
    ipf = normalize_ipf_version(meta.get("ipf_version"))
    if ipf is not None:
        update["ipf_version"] = ipf
    extra = dict(record.extra)
    extra["annotation"] = {
        k: meta.get(k)
        for k in (
            "swath",
            "polarisation",
            "incidence_mid_deg",
            "azimuth_time_interval_s",
            "range_sampling_rate_hz",
            "lines_per_burst",
            "samples_per_burst",
            "n_bursts",
            "number_of_lines",
            "number_of_samples",
            "source",
        )
    }
    extra["provenance_xml"] = "SAFE annotation / ASF burst XML (post-download)"
    update["extra"] = extra
    return record.model_copy(update=update)


__all__ = [
    "FIELD_PROVENANCE",
    "PLATFORM_ALIASES",
    "annotation_from_element",
    "burst_record_from_asf",
    "enrich_record",
    "ipf_version_from_element",
    "normalize_ipf_version",
    "normalize_mode",
    "normalize_platform",
    "parse_burst_xml_metadata",
    "parse_manifest_ipf_version",
    "parse_safe_annotation",
    "parse_time",
    "records_from_asf",
    "split_polarizations",
    "state_vectors_from_product",
    "umm_attribute",
]
