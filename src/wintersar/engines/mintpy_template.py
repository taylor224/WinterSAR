"""``smallbaselineApp.cfg`` generation for the MintPy adapter (plan §5.2; PERF-09; ADR-0021).

Only keys that exist in the official template are emitted (:data:`TEMPLATE_KEYS`);
unknown keys raise so that a typo never silently becomes a no-op inside MintPy.

# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/defaults/smallbaselineApp.cfg
#         (MintPy 1.6.4, key names + accepted values quoted in the comments below)
# source: https://github.com/insarlab/MintPy/blob/main/docs/dir_structure.md (HyP3 / topsStack
#         ``mintpy.load.*`` path patterns)
# source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/defaults/template.py STEP_LIST
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wintersar.i18n import t
from wintersar.util.sysinfo import MachineSpec

MINTPY_VERIFIED_VERSION = "1.6.4"  # source: https://pypi.org/pypi/mintpy/json (2026-07-25)

#: smallbaselineApp step order (mintpy/defaults/template.py STEP_LIST, 1.6.4)
STEP_LIST: tuple[str, ...] = (
    "load_data",
    "modify_network",
    "reference_point",
    "quick_overview",
    "correct_unwrap_error",
    "invert_network",
    "correct_LOD",
    "correct_SET",
    "correct_ionosphere",
    "correct_troposphere",
    "deramp",
    "correct_topography",
    "residual_RMS",
    "reference_date",
    "velocity",
    "geocode",
    "google_earth",
    "hdfeos5",
)

#: every template key this module may emit (verified 1:1 against smallbaselineApp.cfg)
TEMPLATE_KEYS: tuple[str, ...] = (
    "mintpy.compute.maxMemory",  # [float > 0.0], auto for 4, GB
    "mintpy.compute.cluster",  # [local / slurm / pbs / lsf / none], auto for none
    "mintpy.compute.numWorker",  # [int > 1 / all / num%], auto for 4 (local)
    "mintpy.compute.config",
    "mintpy.load.processor",  # [isce, aria, hyp3, gmtsar, snap, gamma, roipac, nisar]
    "mintpy.load.autoPath",
    "mintpy.load.updateMode",  # [yes / no], auto for yes
    "mintpy.load.compression",
    "mintpy.load.metaFile",
    "mintpy.load.baselineDir",
    "mintpy.load.unwFile",
    "mintpy.load.corFile",
    "mintpy.load.connCompFile",
    "mintpy.load.intFile",
    "mintpy.load.magFile",
    "mintpy.load.demFile",
    "mintpy.load.lookupYFile",
    "mintpy.load.lookupXFile",
    "mintpy.load.incAngleFile",
    "mintpy.load.azAngleFile",
    "mintpy.load.shadowMaskFile",
    "mintpy.load.waterMaskFile",
    "mintpy.load.bperpFile",
    "mintpy.subset.yx",
    "mintpy.subset.lalo",  # [S:N,W:E / no]
    "mintpy.multilook.method",
    "mintpy.multilook.ystep",
    "mintpy.multilook.xstep",
    "mintpy.network.tempBaseMax",  # [1-inf, no], days
    "mintpy.network.perpBaseMax",  # [1-inf, no], metres
    "mintpy.network.connNumMax",
    "mintpy.network.startDate",
    "mintpy.network.endDate",
    "mintpy.network.excludeDate",
    "mintpy.network.excludeDate12",
    "mintpy.network.excludeIfgIndex",
    "mintpy.network.referenceFile",
    "mintpy.network.coherenceBased",  # [yes / no], auto for no
    "mintpy.network.minCoherence",  # [0.0-1.0], auto for 0.7
    "mintpy.network.areaRatioBased",
    "mintpy.network.minAreaRatio",
    "mintpy.network.keepMinSpanTree",  # [yes / no], auto for yes
    "mintpy.network.maskFile",
    "mintpy.network.aoiYX",
    "mintpy.network.aoiLALO",
    "mintpy.reference.yx",
    "mintpy.reference.lalo",  # [31.8,130.8 / auto]
    "mintpy.reference.maskFile",
    "mintpy.reference.coherenceFile",
    "mintpy.reference.minCoherence",
    "mintpy.reference.date",
    "mintpy.unwrapError.method",  # [bridging / phase_closure / bridging+phase_closure / no]
    "mintpy.unwrapError.waterMaskFile",
    "mintpy.unwrapError.connCompMinArea",
    "mintpy.unwrapError.numSample",
    "mintpy.unwrapError.ramp",
    "mintpy.unwrapError.bridgePtsRadius",
    "mintpy.networkInversion.weightFunc",
    "mintpy.networkInversion.waterMaskFile",
    "mintpy.networkInversion.minNormVelocity",
    "mintpy.networkInversion.maskDataset",
    "mintpy.networkInversion.maskThreshold",
    "mintpy.networkInversion.minRedundancy",
    "mintpy.networkInversion.minTempCoh",  # [0.0-1.0], auto for 0.7
    "mintpy.networkInversion.minNumPixel",
    "mintpy.networkInversion.shadowMask",
    "mintpy.solidEarthTides",
    "mintpy.ionosphericDelay.method",
    "mintpy.ionosphericDelay.excludeDate",
    "mintpy.ionosphericDelay.excludeDate12",
    "mintpy.troposphericDelay.method",  # [pyaps / height_correlation / gacos / opera / no]
    "mintpy.troposphericDelay.weatherModel",  # [ERA5 / MERRA / NARR], auto for ERA5
    "mintpy.troposphericDelay.weatherDir",  # [path2directory], auto for WEATHER_DIR or "./"
    "mintpy.troposphericDelay.polyOrder",
    "mintpy.troposphericDelay.looks",
    "mintpy.troposphericDelay.minCorrelation",
    "mintpy.troposphericDelay.gacosDir",  # auto for "./GACOS"
    "mintpy.troposphericDelay.operaDir",
    "mintpy.deramp",  # [no / linear / quadratic], auto for no
    "mintpy.deramp.maskFile",
    "mintpy.topographicResidual",  # [yes / no], auto for yes
    "mintpy.topographicResidual.polyOrder",
    "mintpy.topographicResidual.phaseVelocity",
    "mintpy.topographicResidual.stepDate",
    "mintpy.topographicResidual.excludeDate",
    "mintpy.topographicResidual.pixelwiseGeometry",
    "mintpy.residualRMS.maskFile",
    "mintpy.residualRMS.deramp",
    "mintpy.residualRMS.cutoff",
    "mintpy.timeFunc.startDate",
    "mintpy.timeFunc.endDate",
    "mintpy.timeFunc.excludeDate",
    "mintpy.timeFunc.polynomial",
    "mintpy.timeFunc.periodic",
    "mintpy.timeFunc.stepDate",
    "mintpy.timeFunc.exp",
    "mintpy.timeFunc.log",
    "mintpy.timeFunc.uncertaintyQuantification",
    "mintpy.timeFunc.timeSeriesCovFile",
    "mintpy.timeFunc.bootstrapCount",
    "mintpy.geocode",  # [yes / no], auto for yes
    "mintpy.geocode.SNWE",
    "mintpy.geocode.laloStep",
    "mintpy.geocode.interpMethod",
    "mintpy.geocode.fillValue",
    "mintpy.save.kmz",  # [yes / no], auto for yes
    "mintpy.save.hdfEos5",  # [yes / no], auto for no
    "mintpy.save.hdfEos5.update",
    "mintpy.save.hdfEos5.subset",
    "mintpy.plot",
    "mintpy.plot.dpi",
    "mintpy.plot.maxMemory",
)
TEMPLATE_KEY_SET = frozenset(TEMPLATE_KEYS)

#: HyP3 burst products: ``<data_dir>/<pair>/<product>/<product>_<suffix>[_clip].tif``
#: source: MintPy docs dir_structure.md "HyP3" template example (``hyp3/*/*unw_phase_clip.tif``)
HYP3_LOAD_PATTERNS: dict[str, str] = {
    "mintpy.load.unwFile": "*/*/*_unw_phase{clip}.tif",
    "mintpy.load.corFile": "*/*/*_corr{clip}.tif",
    "mintpy.load.connCompFile": "*/*/*_conncomp{clip}.tif",
    "mintpy.load.demFile": "*/*/*_dem{clip}.tif",
    "mintpy.load.incAngleFile": "*/*/*_lv_theta{clip}.tif",
    "mintpy.load.azAngleFile": "*/*/*_lv_phi{clip}.tif",
    "mintpy.load.waterMaskFile": "*/*/*_water_mask{clip}.tif",
}
#: ISCE2 topsStack layout; source: MintPy docs dir_structure.md "ISCE / topsStack" example
ISCE_TOPSSTACK_LOAD_PATTERNS: dict[str, str] = {
    "mintpy.load.metaFile": "reference/IW*.xml",
    "mintpy.load.baselineDir": "baselines",
    "mintpy.load.unwFile": "merged/interferograms/*/filt_*.unw",
    "mintpy.load.corFile": "merged/interferograms/*/filt_*.cor",
    "mintpy.load.connCompFile": "merged/interferograms/*/filt_*.unw.conncomp",
    "mintpy.load.demFile": "merged/geom_reference/hgt.rdr",
    "mintpy.load.lookupYFile": "merged/geom_reference/lat.rdr",
    "mintpy.load.lookupXFile": "merged/geom_reference/lon.rdr",
    "mintpy.load.incAngleFile": "merged/geom_reference/los.rdr",
    "mintpy.load.azAngleFile": "merged/geom_reference/los.rdr",
    "mintpy.load.shadowMaskFile": "merged/geom_reference/shadowMask.rdr",
}
# alias for the artifact-meta patterns written by wintersar.engines.hyp3 (short key -> template key)
_SHORT_TO_TEMPLATE = {
    "unwFile": "mintpy.load.unwFile",
    "corFile": "mintpy.load.corFile",
    "connCompFile": "mintpy.load.connCompFile",
    "demFile": "mintpy.load.demFile",
    "incAngleFile": "mintpy.load.incAngleFile",
    "azAngleFile": "mintpy.load.azAngleFile",
    "waterMaskFile": "mintpy.load.waterMaskFile",
    "metaFile": "mintpy.load.metaFile",
    "baselineDir": "mintpy.load.baselineDir",
    "lookupYFile": "mintpy.load.lookupYFile",
    "lookupXFile": "mintpy.load.lookupXFile",
    "shadowMaskFile": "mintpy.load.shadowMaskFile",
}

#: config ``timeseries.troposphere`` -> (mintpy.troposphericDelay.method, weatherModel)
TROPO_METHODS: dict[str, tuple[str, str | None]] = {
    "era5": ("pyaps", "ERA5"),
    "gacos": ("gacos", None),
    "height_correlation": ("height_correlation", None),
    "none": ("no", None),
}
#: config ``timeseries.unwrap_error_correction`` -> mintpy.unwrapError.method
UNWRAP_ERROR_METHODS: dict[str, str] = {
    "phase_closure": "phase_closure",
    "bridging": "bridging",
    "bridging+phase_closure": "bridging+phase_closure",
    "no": "no",
    "none": "no",
}
DERAMP_METHODS: dict[str, str] = {
    "linear": "linear",
    "quadratic": "quadratic",
    "no": "no",
    "none": "no",
}
PROCESSORS = ("hyp3", "isce")
MIN_LOCAL_WORKERS = 2  # cfg: numWorker [int > 1 / all / num%]


class TemplateKeyError(KeyError):
    """A key that does not exist in MintPy's smallbaselineApp.cfg."""


# ---------------------------------------------------------------------- PERF-09


def compute_settings(
    machine: MachineSpec | None,
    *,
    cores: int | None = None,
    memory_gb: float | None = None,
    memory_fraction: float = 0.8,
) -> dict[str, str]:
    """``mintpy.compute.*`` from the machine spec (PERF-09).

    * ``cluster = local`` with ``numWorker = cores - 1`` (leave one core for the main process
      and the OS); ``cluster = none`` when fewer than :data:`MIN_LOCAL_WORKERS` workers remain
      because the template requires ``numWorker`` to be an int > 1.
    * ``maxMemory = memory_fraction x RAM`` in GB (one decimal), or the explicit budget.
    """
    n_cores = int(cores) if cores is not None else (machine.cores if machine else 1)
    mem = (
        float(memory_gb)
        if memory_gb is not None
        else (machine.memory_gb * memory_fraction if machine else 4.0)
    )
    workers = max(1, n_cores - 1)
    out: dict[str, str] = {"mintpy.compute.maxMemory": f"{max(1.0, mem):.1f}"}
    if workers >= MIN_LOCAL_WORKERS:
        out["mintpy.compute.cluster"] = "local"
        out["mintpy.compute.numWorker"] = str(workers)
    else:
        out["mintpy.compute.cluster"] = "none"
        out["mintpy.compute.numWorker"] = "auto"
    return out


# ---------------------------------------------------------------------- sections


def load_settings(
    processor: str,
    data_dir: Path,
    patterns: Mapping[str, str] | None = None,
    clip_suffix: str = "",
) -> dict[str, str]:
    """``mintpy.load.*`` entries: absolute path patterns under ``data_dir``."""
    if processor not in PROCESSORS:
        msg = f"unsupported processor {processor!r}; supported: {PROCESSORS}"
        raise ValueError(msg)
    base = Path(data_dir).resolve()
    if patterns:
        rel = {}
        for k, v in patterns.items():
            key = _SHORT_TO_TEMPLATE.get(k, k)
            if key not in TEMPLATE_KEY_SET or not key.startswith("mintpy.load."):
                raise TemplateKeyError(key)
            rel[key] = str(v)
    else:
        src = HYP3_LOAD_PATTERNS if processor == "hyp3" else ISCE_TOPSSTACK_LOAD_PATTERNS
        rel = {k: v.format(clip=clip_suffix) for k, v in src.items()}
    out = {"mintpy.load.processor": processor}
    for key, pattern in rel.items():
        p = Path(pattern)
        out[key] = str(p) if p.is_absolute() else str(base / pattern)
    return out


def reference_lalo_value(reference_point: Any) -> str:
    """``mintpy.reference.lalo`` value: ``'lat,lon'`` (cfg example ``31.8,130.8``) or ``auto``."""
    if isinstance(reference_point, list | tuple) and len(reference_point) == 2:
        return f"{float(reference_point[0]):.6f},{float(reference_point[1]):.6f}"
    return "auto"


def _yes_no(v: Any) -> str:
    return "yes" if bool(v) else "no"


def timeseries_settings(
    timeseries: Mapping[str, Any] | None,
    *,
    weather_dir: Path | None = None,
    gacos_dir: Path | None = None,
) -> dict[str, str]:
    """Map ``config.timeseries`` (pipeline/config.py TimeseriesCfg) to template keys."""
    ts = dict(timeseries or {})
    out: dict[str, str] = {}
    out["mintpy.reference.lalo"] = reference_lalo_value(ts.get("reference_point"))
    tropo = str(ts.get("troposphere", "era5")).lower()
    if tropo not in TROPO_METHODS:
        msg = f"unknown troposphere method {tropo!r}; known: {sorted(TROPO_METHODS)}"
        raise ValueError(msg)
    method, model = TROPO_METHODS[tropo]
    out["mintpy.troposphericDelay.method"] = method
    if model is not None:
        out["mintpy.troposphericDelay.weatherModel"] = model
        if weather_dir is not None:
            out["mintpy.troposphericDelay.weatherDir"] = str(Path(weather_dir).resolve())
    if method == "gacos" and gacos_dir is not None:
        out["mintpy.troposphericDelay.gacosDir"] = str(Path(gacos_dir).resolve())
    deramp = str(ts.get("deramp", "linear")).lower()
    if deramp not in DERAMP_METHODS:
        msg = f"unknown deramp {deramp!r}; known: {sorted(DERAMP_METHODS)}"
        raise ValueError(msg)
    out["mintpy.deramp"] = DERAMP_METHODS[deramp]
    unw = str(ts.get("unwrap_error_correction", "phase_closure")).lower()
    if unw not in UNWRAP_ERROR_METHODS:
        msg = f"unknown unwrap_error_correction {unw!r}; known: {sorted(UNWRAP_ERROR_METHODS)}"
        raise ValueError(msg)
    out["mintpy.unwrapError.method"] = UNWRAP_ERROR_METHODS[unw]
    if "coherence_threshold" in ts and ts["coherence_threshold"] is not None:
        thr = float(ts["coherence_threshold"])
        # data-driven network pruning (Yunjun et al. 2019 §4.2): drop ifgs with mean spatial
        # coherence < threshold while keeping the MST (mintpy.network.keepMinSpanTree auto=yes)
        out["mintpy.network.coherenceBased"] = "yes"
        out["mintpy.network.minCoherence"] = f"{thr:.2f}"
    return out


def network_settings(selection: Mapping[str, Any] | None) -> dict[str, str]:
    """``mintpy.network.tempBaseMax/perpBaseMax`` from ``config.selection`` (if given)."""
    sel = dict(selection or {})
    out: dict[str, str] = {}
    if sel.get("max_temporal_baseline_days") is not None:
        out["mintpy.network.tempBaseMax"] = str(int(sel["max_temporal_baseline_days"]))
    if sel.get("max_perp_baseline_m") is not None:
        out["mintpy.network.perpBaseMax"] = f"{float(sel['max_perp_baseline_m']):g}"
    return out


# ---------------------------------------------------------------------- build / render


def build_template(
    *,
    processor: str,
    data_dir: Path,
    timeseries: Mapping[str, Any] | None = None,
    selection: Mapping[str, Any] | None = None,
    machine: MachineSpec | None = None,
    cores: int | None = None,
    memory_gb: float | None = None,
    patterns: Mapping[str, str] | None = None,
    clip_suffix: str = "",
    weather_dir: Path | None = None,
    gacos_dir: Path | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Ordered ``{template key: value}`` for one project. ``extra`` may override any verified key."""
    entries: dict[str, str] = {}
    entries.update(compute_settings(machine, cores=cores, memory_gb=memory_gb))
    entries.update(load_settings(processor, data_dir, patterns=patterns, clip_suffix=clip_suffix))
    entries["mintpy.load.updateMode"] = "yes"  # resumable re-runs (PERF-03/06)
    entries.update(network_settings(selection))
    entries.update(timeseries_settings(timeseries, weather_dir=weather_dir, gacos_dir=gacos_dir))
    entries["mintpy.geocode"] = "yes"
    entries["mintpy.save.kmz"] = "yes"
    for k, v in (extra or {}).items():
        if k not in TEMPLATE_KEY_SET:
            raise TemplateKeyError(k)
        entries[k] = _yes_no(v) if isinstance(v, bool) else str(v)
    return entries


def render_template(entries: Mapping[str, str], lang: str | None = None) -> str:
    """Render ``key = value`` lines (MintPy reads ``key = value`` and ignores ``#`` comments)."""
    for k in entries:
        if k not in TEMPLATE_KEY_SET:
            raise TemplateKeyError(k)
    width = max((len(k) for k in entries), default=20)
    lines = [
        "# vim: set filetype=cfg:",
        f"# {t('engines.mintpy.template_header', lang)}",
        f"# generated: {datetime.now(UTC).isoformat(timespec='seconds')} (MintPy template keys verified against {MINTPY_VERIFIED_VERSION})",
        "",
    ]
    lines.extend(f"{k:<{width}} = {v}" for k, v in entries.items())
    return "\n".join(lines) + "\n"


def write_template(path: Path, entries: Mapping[str, str], lang: str | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_template(entries, lang), encoding="utf-8")
    return path


def parse_template(text: str) -> dict[str, str]:
    """Read ``key = value`` lines back (comments and blank lines ignored)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = (s.strip() for s in line.split("=", 1))
        out[k] = v
    return out


def expected_timeseries_filename(entries: Mapping[str, str]) -> str:
    """Name of the final corrected time-series file MintPy will produce.

    # source: mintpy/smallbaselineApp.py get_timeseries_filename: timeseries.h5 ->
    #   pyaps: _{weatherModel}.h5 | height_correlation: _tropHgt.h5 | gacos: _GACOS.h5 |
    #   opera: _OPERA.h5 ; deramp: _ramp.h5 ; topographicResidual (auto yes): _demErr.h5
    """
    name = "timeseries"
    method = entries.get("mintpy.troposphericDelay.method", "pyaps")
    if method in ("auto", "pyaps"):
        name += f"_{entries.get('mintpy.troposphericDelay.weatherModel', 'ERA5')}"
    elif method == "height_correlation":
        name += "_tropHgt"
    elif method == "gacos":
        name += "_GACOS"
    elif method == "opera":
        name += "_OPERA"
    if entries.get("mintpy.deramp", "no") not in ("no", "auto"):
        name += "_ramp"
    if entries.get("mintpy.topographicResidual", "yes") in ("yes", "auto"):
        name += "_demErr"
    return name + ".h5"
