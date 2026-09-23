"""Synthetic interferogram / stack generator (plan §5.7, Phase 0 minimal version).

Provides ground truth for unit tests, the fake engine and the research module:

* :func:`deformation_field` — Gaussian bowl (subsidence) or linear ramp, metres per year.
* :func:`turbulent_atmosphere` — isotropic power-law spectrum phase screen.
* :func:`coherence_map` — smooth coherence with low-coherence blobs (vegetation/water).
* :func:`make_interferogram` — wrapped phase + coherence + true unwrapped phase + masks.
* :func:`make_stack` — N dates, SBAS-style pair list, per-date atmosphere, per-pair
  interferograms consistent with a common velocity field (loop-closure consistent).

Everything is deterministic given ``rng``. Units: phase in radians, displacement in metres,
Sentinel-1 C-band wavelength 0.05546576 m (ESA S1 product spec: 5.405 GHz).

**Golden-sensitive (ADR-0100).** :func:`deformation_field`, :func:`turbulent_atmosphere`,
:func:`coherence_map`, :func:`make_interferogram` and :func:`make_stack` feed the fake engine,
so any change to their output (not only their API) changes
``tests/regression/golden/S_synthetic/stats.json``: run
``.venv/bin/python scripts/make_golden.py --check`` before pushing and, when the change is
intended, regenerate with ``scripts/make_golden.py`` and say why in the PR body (rule 11.4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from wintersar.io.schemas import Pair
from wintersar.unwrap.tiling import tile_grid

WAVELENGTH_S1_M = 0.05546576  # C-band, 5.405 GHz
# radians per metre of LOS *displacement*, positive = towards the satellite (range
# decrease): phase = -(4π/λ)·d_LOS, i.e. +4π/λ per metre of range increase (ADR-0040,
# io.timeseries, HyP3: "negative values indicate movement towards the sensor")
PHASE_PER_M_LOS = -4.0 * np.pi / WAVELENGTH_S1_M

FloatArray = NDArray[np.float64]
ComplexArray = NDArray[np.complex128]
#: Phase-noise model of :func:`_phase_noise` — ``crlb`` is the Cramér-Rao lower bound
#: (default, optimistic at ``looks=1``), ``exact`` the true multi-look phase distribution.
NoiseModel = Literal["crlb", "exact"]


def deformation_field(
    shape: tuple[int, int],
    kind: str = "gaussian",
    amplitude_m: float = -0.05,
    sigma_px: float | None = None,
    center: tuple[float, float] | None = None,
    ramp: tuple[float, float] = (0.0, 0.0),
) -> FloatArray:
    """LOS displacement (metres, positive = towards satellite) for one epoch.

    ``gaussian``: bowl of peak ``amplitude_m`` (negative = subsidence / away from satellite).
    ``linear``: plane ``ramp[0]*y/ny + ramp[1]*x/nx`` metres.
    """
    ny, nx = shape
    y, x = np.mgrid[0:ny, 0:nx].astype(np.float64)
    if kind == "gaussian":
        cy, cx = center if center is not None else (ny / 2.0, nx / 2.0)
        s = sigma_px if sigma_px is not None else min(ny, nx) / 6.0
        return np.asarray(
            amplitude_m * np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / (2.0 * s * s)), dtype=np.float64
        )
    if kind == "linear":
        return np.asarray(
            ramp[0] * y / max(ny - 1, 1) + ramp[1] * x / max(nx - 1, 1), dtype=np.float64
        )
    if kind == "none":
        return np.zeros(shape, dtype=np.float64)
    msg = f"unknown deformation kind {kind!r}"
    raise ValueError(msg)


def turbulent_atmosphere(
    shape: tuple[int, int],
    rng: np.random.Generator,
    std_rad: float = 1.0,
    beta: float = 8.0 / 3.0,
) -> FloatArray:
    """Isotropic power-law phase screen with PSD ∝ k^-beta (Kolmogorov-like ~8/3)."""
    ny, nx = shape
    ky = np.fft.fftfreq(ny)[:, None]
    kx = np.fft.fftfreq(nx)[None, :]
    k = np.sqrt(ky * ky + kx * kx)
    k[0, 0] = np.inf
    amp = k ** (-beta / 2.0)
    amp[0, 0] = 0.0
    noise = rng.standard_normal((ny, nx)) + 1j * rng.standard_normal((ny, nx))
    screen = np.real(np.fft.ifft2(amp * noise))
    s = screen.std()
    if s > 0:
        screen = screen / s * std_rad
    return np.asarray(screen - screen.mean(), dtype=np.float64)


def coherence_map(
    shape: tuple[int, int],
    rng: np.random.Generator,
    base: float = 0.8,
    n_blobs: int = 3,
    blob_coh: float = 0.15,
) -> FloatArray:
    """Smooth coherence field in (0, 1] with a few low-coherence blobs."""
    ny, nx = shape
    y, x = np.mgrid[0:ny, 0:nx].astype(np.float64)
    coh = np.full(shape, base, dtype=np.float64)
    for _ in range(n_blobs):
        cy, cx = rng.uniform(0, ny), rng.uniform(0, nx)
        s = rng.uniform(min(ny, nx) / 12.0, min(ny, nx) / 5.0)
        w = np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / (2.0 * s * s))
        coh = coh * (1.0 - w) + blob_coh * w
    coh += 0.03 * rng.standard_normal(shape)
    return np.clip(coh, 0.01, 0.999)


def water_mask(shape: tuple[int, int], fraction: float = 0.1) -> NDArray[np.bool_]:
    """Rectangular 'water' region along the left edge covering ``fraction`` of columns."""
    _, nx = shape
    m = np.zeros(shape, dtype=bool)
    m[:, : round(nx * fraction)] = True
    return m


def wrap(phase: FloatArray) -> FloatArray:
    return np.asarray(np.angle(np.exp(1j * phase)), dtype=np.float64)


def _phase_noise(
    coh: FloatArray,
    rng: np.random.Generator,
    looks: int = 1,
    model: str = "crlb",
) -> FloatArray:
    """Interferometric phase noise (radians) for a coherence map (:data:`NoiseModel`).

    ``model="crlb"`` (default): zero-mean Gaussian with the Cramér-Rao std
    ``sqrt((1-γ²)/(2·L·γ²))``, clipped to ``[0, π/√3]`` (the uniform-phase limit). This is a
    *lower bound*, not the real distribution: it is tight for many looks (within ~5 % of the
    true std at ``L=16``) but optimistic at ``L=1``, where the exact single-look phase pdf
    (Lee, Hoppel, Mango & Miller 1994, IEEE TGRS 32(5):1017-1028, doi:10.1109/36.312890)
    gives up to ~2.2x the std (gamma 0.8: 0.92 vs 0.53 rad, gamma 0.95: 0.52 vs 0.23 rad).
    Synthetic stacks built with ``looks=1`` are therefore optimistically clean.

    ``model="exact"``: draws the real distribution instead — ``L`` independent correlated
    circular-Gaussian pairs per pixel, ``angle(mean_L(s1 · conj(s2)))`` with
    ``E[s1 s2*] = gamma`` — so ``looks=1`` is as noisy as real single-look data and larger
    ``looks`` converge on the CRLB from above.

    The default stays ``"crlb"``: the synthetic-noise model is a domain checkpoint and is not
    changed before the researcher confirms (rule 11.10, ADR-0060).
    """
    g = np.clip(np.asarray(coh, dtype=np.float64), 0.0, 1.0)
    n_looks = max(int(looks), 1)
    if model == "crlb":
        g2 = g * g
        with np.errstate(divide="ignore", invalid="ignore"):
            var = np.where(g2 > 0, (1.0 - g2) / (2.0 * n_looks * g2), np.inf)
        return np.asarray(
            rng.standard_normal(g.shape) * np.sqrt(np.clip(var, 0.0, (np.pi**2) / 3.0)),
            dtype=np.float64,
        )
    if model != "exact":
        msg = f"unknown noise model {model!r} (crlb | exact)"
        raise ValueError(msg)
    shape = (n_looks, *g.shape)
    s1 = (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) / np.sqrt(2.0)
    ind = (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) / np.sqrt(2.0)
    s2 = g * s1 + np.sqrt(np.maximum(1.0 - g * g, 0.0)) * ind
    return np.asarray(np.angle(np.mean(s1 * np.conj(s2), axis=0)), dtype=np.float64)


@dataclass
class SynthIgram:
    wrapped: FloatArray
    unw_true: FloatArray
    coherence: FloatArray
    mask: NDArray[np.bool_]
    displacement_m: FloatArray
    atmosphere: FloatArray
    meta: dict[str, float | str] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.wrapped.shape[0]), int(self.wrapped.shape[1]))

    @property
    def complex(self) -> ComplexArray:
        return np.asarray(self.coherence * np.exp(1j * self.wrapped), dtype=np.complex128)


def make_interferogram(
    shape: tuple[int, int] = (256, 256),
    rng: np.random.Generator | None = None,
    displacement_m: FloatArray | None = None,
    deformation_kind: str = "gaussian",
    deformation_amplitude_m: float = -0.05,
    deformation_ramp: tuple[float, float] = (0.0, 0.0),
    deformation_sigma_px: float | None = None,
    atmosphere_std_rad: float = 1.0,
    coherence_base: float = 0.8,
    looks: int = 1,
    water_fraction: float = 0.0,
    dem_error_rad: float = 0.0,
    noise_model: NoiseModel = "crlb",
) -> SynthIgram:
    """One synthetic interferogram with ground truth.

    ``deformation_ramp`` / ``deformation_sigma_px`` are the ``linear`` / ``gaussian``
    parameters of :func:`deformation_field`; ``noise_model`` selects the phase-noise
    distribution (see :func:`_phase_noise` — ``crlb`` understates ``looks=1`` noise).
    """
    rng = rng or np.random.default_rng(0)
    if displacement_m is None:
        displacement_m = deformation_field(
            shape,
            deformation_kind,
            deformation_amplitude_m,
            sigma_px=deformation_sigma_px,
            ramp=(float(deformation_ramp[0]), float(deformation_ramp[1])),
        )
    atmo = (
        turbulent_atmosphere(shape, rng, atmosphere_std_rad)
        if atmosphere_std_rad > 0
        else np.zeros(shape)
    )
    topo = (
        turbulent_atmosphere(shape, rng, dem_error_rad, beta=2.0)
        if dem_error_rad > 0
        else np.zeros(shape)
    )
    coh = coherence_map(shape, rng, base=coherence_base)
    mask = water_mask(shape, water_fraction) if water_fraction > 0 else np.zeros(shape, dtype=bool)
    coh = np.where(mask, 0.02, coh)
    unw_true = PHASE_PER_M_LOS * displacement_m + atmo + topo
    noisy = unw_true + _phase_noise(coh, rng, looks, noise_model)
    return SynthIgram(
        wrapped=wrap(noisy),
        unw_true=unw_true,
        coherence=coh,
        mask=mask,
        displacement_m=displacement_m,
        atmosphere=atmo,
        meta={
            "deformation_kind": deformation_kind,
            "looks": looks,
            "noise_model": noise_model,
        },
    )


@dataclass
class SynthStack:
    dates: list[date]
    pairs: list[Pair]
    velocity_m_per_yr: FloatArray
    displacement_m: FloatArray  # (n_dates, ny, nx), cumulative LOS displacement, 0 at dates[0]
    atmosphere: FloatArray  # (n_dates, ny, nx)
    igrams: dict[str, SynthIgram]  # pair.key -> interferogram
    coherence_base: float

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.velocity_m_per_yr.shape[0]), int(self.velocity_m_per_yr.shape[1]))


def sbas_pairs(
    dates: list[date],
    max_temporal_days: int = 48,
    perp_baselines_m: dict[date, float] | None = None,
    max_perp_m: float | None = None,
) -> list[Pair]:
    """All (i<j) pairs within the temporal (and optional perpendicular) threshold."""
    out: list[Pair] = []
    for i, d1 in enumerate(dates):
        for d2 in dates[i + 1 :]:
            dt = (d2 - d1).days
            if dt <= 0 or dt > max_temporal_days:
                continue
            bperp: float | None = None
            if perp_baselines_m is not None:
                bperp = perp_baselines_m[d2] - perp_baselines_m[d1]
                if max_perp_m is not None and abs(bperp) > max_perp_m:
                    continue
            out.append(
                Pair(reference=d1, secondary=d2, temporal_baseline_days=dt, perp_baseline_m=bperp)
            )
    return out


def make_stack(
    n_dates: int = 8,
    shape: tuple[int, int] = (128, 128),
    rng: np.random.Generator | None = None,
    start: date = date(2024, 1, 1),
    repeat_days: int = 12,
    velocity_peak_m_per_yr: float = -0.03,
    atmosphere_std_rad: float = 0.6,
    coherence_base: float = 0.8,
    max_temporal_days: int = 48,
    looks: int = 1,
    water_fraction: float = 0.0,
    noise_model: NoiseModel = "crlb",
) -> SynthStack:
    """SBAS-consistent synthetic stack: linear velocity bowl + per-date atmosphere.

    ``noise_model`` is passed to :func:`_phase_noise`; the ``looks=1`` default is the
    Cramér-Rao lower bound, so it is cleaner than real single-look data.
    """
    rng = rng or np.random.default_rng(0)
    dates = [start + timedelta(days=repeat_days * i) for i in range(n_dates)]
    vel = deformation_field(shape, "gaussian", velocity_peak_m_per_yr)
    years = np.array([(d - dates[0]).days / 365.25 for d in dates])
    disp = vel[None, :, :] * years[:, None, None]
    atmo = np.stack([turbulent_atmosphere(shape, rng, atmosphere_std_rad) for _ in dates])
    atmo[0] = 0.0
    bperp = {d: float(rng.uniform(-100, 100)) for d in dates}
    pairs = sbas_pairs(dates, max_temporal_days, bperp)
    idx = {d: i for i, d in enumerate(dates)}
    igrams: dict[str, SynthIgram] = {}
    for p in pairs:
        i, j = idx[p.reference], idx[p.secondary]
        ddisp = disp[j] - disp[i]
        datmo = atmo[j] - atmo[i]
        coh = coherence_map(shape, rng, base=coherence_base)
        mask = (
            water_mask(shape, water_fraction) if water_fraction > 0 else np.zeros(shape, dtype=bool)
        )
        coh = np.where(mask, 0.02, coh)
        unw_true = PHASE_PER_M_LOS * ddisp + datmo
        noisy = unw_true + _phase_noise(coh, rng, looks, noise_model)
        igrams[p.key] = SynthIgram(
            wrapped=wrap(noisy),
            unw_true=unw_true,
            coherence=coh,
            mask=mask,
            displacement_m=ddisp,
            atmosphere=datmo,
            meta={"looks": looks, "noise_model": noise_model},
        )
    return SynthStack(
        dates=dates,
        pairs=pairs,
        velocity_m_per_yr=vel,
        displacement_m=disp,
        atmosphere=atmo,
        igrams=igrams,
        coherence_base=coherence_base,
    )


def unwrap_error_fraction(
    unw: FloatArray, unw_true: FloatArray, mask: NDArray[np.bool_] | None = None
) -> float:
    """Fraction of (unmasked) pixels whose error is ≥ π after removing the constant offset."""
    valid = ~mask if mask is not None else np.ones(unw.shape, dtype=bool)
    valid &= np.isfinite(unw)
    if not valid.any():
        return float("nan")
    diff = unw - unw_true
    diff = diff - np.nanmedian(diff[valid])
    return float(np.mean(np.abs(diff[valid]) >= np.pi))


# ============================================================================ Phase 6 extensions
# Everything below is additive (plan §5.7, R-07): the Phase 0 functions above keep their
# signatures; new keyword arguments of :func:`make_interferogram` are optional.

IntArray = NDArray[np.int64]
BoolArray = NDArray[np.bool_]

# source: https://sentiwiki.copernicus.eu/web/s1-mission (693 km orbit) — same constant as
# src/wintersar/select/geometry_masks.py
S1_ORBIT_ALTITUDE_M = 693_000.0
# source: src/wintersar/select/looks.py IW_NOMINAL_INCIDENCE_DEG (SentiWiki IW incidence 30-46°)
IW_NOMINAL_INCIDENCE_DEG = 39.0


# ---------------------------------------------------------------- deformation sources
def okada_displacement(shape: tuple[int, int], **params: Any) -> FloatArray:
    """Okada (1985) rectangular dislocation hook — **not implemented**.

    Kept as a named hook so experiment YAMLs can reference ``kind: okada`` today; the
    implementation (or the adoption of an external, verified implementation) is tracked in
    ``docs/open-questions.md`` (research / Phase 6). Raises ``NotImplementedError``.
    """
    msg = (
        "okada deformation source is not implemented yet (docs/open-questions.md, research); "
        f"requested params={sorted(params)} for shape={shape}"
    )
    raise NotImplementedError(msg)


def deformation_sources(shape: tuple[int, int], sources: Sequence[Mapping[str, Any]]) -> FloatArray:
    """Sum of several deformation sources (metres LOS, positive = towards satellite).

    Each mapping holds keyword arguments of :func:`deformation_field` (``kind``,
    ``amplitude_m``, ``sigma_px``, ``center``, ``ramp``). ``kind: okada`` is dispatched to
    :func:`okada_displacement` (which raises ``NotImplementedError``). An empty list gives
    zeros. Pure and deterministic.
    """
    total = np.zeros(shape, dtype=np.float64)
    for src in sources:
        kw = dict(src)
        kind = str(kw.pop("kind", "gaussian"))
        if kind == "okada":
            total += okada_displacement(shape, **kw)
            continue
        total += deformation_field(shape, kind=kind, **kw)
    return total


# ---------------------------------------------------------------- DEM error ∝ Bperp
def slant_range_flat_earth(
    incidence_deg: float = IW_NOMINAL_INCIDENCE_DEG, altitude_m: float = S1_ORBIT_ALTITUDE_M
) -> float:
    """Flat-earth slant range ``R = H / cos(theta)`` (synthetic-geometry approximation)."""
    return float(altitude_m / np.cos(np.radians(incidence_deg)))


def height_to_phase(
    bperp_m: float,
    incidence_deg: float = IW_NOMINAL_INCIDENCE_DEG,
    slant_range_m: float | None = None,
    wavelength_m: float = WAVELENGTH_S1_M,
) -> float:
    """Topographic phase sensitivity ``dφ/dh = -4π/λ · Bperp / (R sin θ)`` (rad per metre).

    Standard flat-earth "height-to-phase" factor (Hanssen 2001, Radar Interferometry,
    eq. 2.4.x form ``φ_topo = -4π/λ · B⊥/(R sin θ) · h``); the sign follows
    :data:`PHASE_PER_M_LOS` so that a DEM error behaves like a fake range change scaled by
    ``B⊥/(R sin θ)``.
    """
    r = slant_range_m if slant_range_m is not None else slant_range_flat_earth(incidence_deg)
    return float((-4.0 * np.pi / wavelength_m) * bperp_m / (r * np.sin(np.radians(incidence_deg))))


def dem_error_height(
    shape: tuple[int, int], rng: np.random.Generator, std_m: float = 5.0, beta: float = 2.0
) -> FloatArray:
    """Spatially correlated DEM height error (metres, zero mean, std ``std_m``)."""
    if std_m <= 0:
        return np.zeros(shape, dtype=np.float64)
    return turbulent_atmosphere(shape, rng, std_rad=std_m, beta=beta)


def dem_error_phase(
    dh_m: FloatArray,
    bperp_m: float,
    incidence_deg: float = IW_NOMINAL_INCIDENCE_DEG,
    slant_range_m: float | None = None,
) -> FloatArray:
    """Phase (rad) of a DEM height error ``dh_m`` for a pair with perpendicular baseline
    ``bperp_m`` — proportional to Bperp, which is what separates it from deformation in a
    stack."""
    return np.asarray(
        height_to_phase(bperp_m, incidence_deg, slant_range_m) * dh_m, dtype=np.float64
    )


# ---------------------------------------------------------------- SHP-structured amplitude
def shp_regions(
    shape: tuple[int, int], kind: str = "halves", rng: np.random.Generator | None = None
) -> IntArray:
    """Integer label map of regions with distinct amplitude statistics.

    ``halves``: left/right (labels 0/1). ``quadrants``: 0..3. ``stripes``: 4 vertical
    stripes 0..3. ``blobs``: background 0 plus three round blobs labelled 1..3 (needs ``rng``).
    """
    ny, nx = shape
    labels = np.zeros(shape, dtype=np.int64)
    if kind == "halves":
        labels[:, nx // 2 :] = 1
    elif kind == "quadrants":
        labels[: ny // 2, nx // 2 :] = 1
        labels[ny // 2 :, : nx // 2] = 2
        labels[ny // 2 :, nx // 2 :] = 3
    elif kind == "stripes":
        for i in range(4):
            labels[:, (i * nx) // 4 : ((i + 1) * nx) // 4] = i
    elif kind == "blobs":
        rng = rng or np.random.default_rng(0)
        y, x = np.mgrid[0:ny, 0:nx].astype(np.float64)
        for i in range(1, 4):
            cy, cx = rng.uniform(0.2 * ny, 0.8 * ny), rng.uniform(0.2 * nx, 0.8 * nx)
            r = rng.uniform(min(ny, nx) / 10.0, min(ny, nx) / 6.0)
            labels[((y - cy) ** 2 + (x - cx) ** 2) <= r * r] = i
    else:
        msg = f"unknown region kind {kind!r}"
        raise ValueError(msg)
    return labels


def shp_amplitude_stack(
    shape: tuple[int, int],
    n_dates: int,
    rng: np.random.Generator,
    region_scales: Sequence[float] = (1.0, 3.0),
    regions: IntArray | None = None,
    region_kind: str = "halves",
) -> tuple[FloatArray, IntArray]:
    """Single-look amplitude stack ``(n_dates, ny, nx)`` whose pixels are i.i.d. Rayleigh
    with a per-region scale (circular-Gaussian speckle → Rayleigh amplitude).

    Pixels inside one region share an amplitude distribution (statistically homogeneous);
    pixels of different regions do not — the ground truth for SHP tests (Ferretti et al.
    2011, doi:10.1109/TGRS.2011.2124465; Parizzi & Brcic 2011, doi:10.1109/LGRS.2010.2083631).
    """
    labels = regions if regions is not None else shp_regions(shape, region_kind, rng)
    scales = np.asarray(region_scales, dtype=np.float64)
    if labels.max() >= scales.size:
        msg = f"{int(labels.max()) + 1} regions but only {scales.size} region_scales"
        raise ValueError(msg)
    scale_map = scales[labels]
    amp = rng.rayleigh(scale=np.broadcast_to(scale_map, (n_dates, *shape)))
    return np.asarray(amp, dtype=np.float64), labels


# ---------------------------------------------------------------- layover from a ridge
def ridge_dem(
    shape: tuple[int, int],
    height_m: float = 400.0,
    sigma_px: float = 8.0,
    col: float | None = None,
) -> FloatArray:
    """Gaussian ridge (metres) running along the azimuth (row) direction, centred at ``col``."""
    ny, nx = shape
    c = nx / 2.0 if col is None else col
    x = np.arange(nx, dtype=np.float64)
    profile = height_m * np.exp(-((x - c) ** 2) / (2.0 * sigma_px * sigma_px))
    return np.asarray(np.broadcast_to(profile[None, :], (ny, nx)).copy(), dtype=np.float64)


def layover_mask_from_dem(
    dem_m: FloatArray,
    pixel_spacing_m: float = 20.0,
    incidence_deg: float = IW_NOMINAL_INCIDENCE_DEG,
    sensor_side: str = "left",
) -> BoolArray:
    """Passive layover mask in radar (range = column) geometry.

    Layover occurs where the terrain slope *facing the sensor* exceeds the incidence angle
    (Kropatsch & Strobl 1990, doi:10.1109/36.45752; ESA-PhiLab slope-correction validity
    ``alpha_r < theta_i`` — both cited in ADR-0017). With the sensor on the ``left`` (range
    increases with column) the sensor-facing slope is the one whose *downslope* direction points
    toward the sensor (ADR-0017 ``range_slope`` sign: positive = facing), i.e. ``dz/dx > 0``.
    """
    dzdx = np.gradient(np.asarray(dem_m, dtype=np.float64), pixel_spacing_m, axis=1)
    facing = dzdx if sensor_side == "left" else -dzdx
    return np.asarray(np.degrees(np.arctan(facing)) > incidence_deg, dtype=bool)


def layover_mask_from_ridge(
    shape: tuple[int, int],
    height_m: float = 400.0,
    sigma_px: float = 8.0,
    pixel_spacing_m: float = 20.0,
    incidence_deg: float = IW_NOMINAL_INCIDENCE_DEG,
    col: float | None = None,
) -> tuple[BoolArray, FloatArray]:
    """``(layover_mask, dem_m)`` for a synthetic ridge; the sensor-facing flank lays over when
    ``height / (sigma · spacing) · e^-1/2 > tan(incidence)``."""
    dem = ridge_dem(shape, height_m, sigma_px, col)
    return layover_mask_from_dem(dem, pixel_spacing_m, incidence_deg), dem


# ---------------------------------------------------------------- tiled truth
TileArray = tuple[FloatArray, slice, slice]


def tile_slices(
    shape: tuple[int, int], rows: int, cols: int, overlap: int
) -> list[tuple[slice, slice]]:
    """Row-major tile extents (with overlap) — the shared partition
    ``wintersar.unwrap.tiling.tile_grid`` (ADR-0046/0048 semantics)."""
    return [(tl.slice_y, tl.slice_x) for tl in tile_grid(shape, rows, cols, overlap)]


@dataclass
class TiledTruth:
    """Independently 'unwrapped' tiles of one synthetic interferogram.

    ``tiles[i] = (unw_tile, slice_y, slice_x)`` where ``unw_tile = unw_true[extent] + 2π·k_i
    (+ noise)``; ``offsets_cycles[i] = k_i`` is the injected integer ambiguity. ``coherence``
    and ``unw_true`` are full-resolution ground truth.
    """

    unw_true: FloatArray
    coherence: FloatArray
    mask: BoolArray
    tiles: list[TileArray]
    offsets_cycles: list[int]
    rows: int
    cols: int
    overlap: int

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.unw_true.shape[0]), int(self.unw_true.shape[1]))

    @property
    def slices(self) -> list[tuple[slice, slice]]:
        return [(sy, sx) for _, sy, sx in self.tiles]


def make_tiled_truth(
    shape: tuple[int, int],
    rows: int,
    cols: int,
    overlap: int,
    rng: np.random.Generator | None = None,
    offsets_cycles: Sequence[int] | None = None,
    max_offset_cycles: int = 3,
    noise_std_rad: float = 0.0,
    igram: SynthIgram | None = None,
    **igram_kwargs: Any,
) -> TiledTruth:
    """Synthetic interferogram split into ``rows x cols`` overlapping tiles with injected
    integer-2π offsets (the stitching ground truth).

    ``offsets_cycles`` fixes the per-tile ambiguities (row-major); otherwise they are drawn
    uniformly from ``[-max_offset_cycles, max_offset_cycles]`` with tile 0 forced to 0 (the
    gauge). ``noise_std_rad`` adds Gaussian noise to the tiles only (truth stays clean).
    """
    rng = rng or np.random.default_rng(0)
    ig = igram if igram is not None else make_interferogram(shape, rng, **igram_kwargs)
    slices = tile_slices(ig.shape, rows, cols, overlap)
    n = len(slices)
    if offsets_cycles is None:
        ks = [int(v) for v in rng.integers(-max_offset_cycles, max_offset_cycles + 1, size=n)]
        ks[0] = 0
    else:
        ks = [int(v) for v in offsets_cycles]
        if len(ks) != n:
            msg = f"{len(ks)} offsets for {n} tiles"
            raise ValueError(msg)
    tiles: list[TileArray] = []
    for (sy, sx), k in zip(slices, ks, strict=True):
        arr = ig.unw_true[sy, sx] + 2.0 * np.pi * k
        if noise_std_rad > 0:
            arr = arr + rng.standard_normal(arr.shape) * noise_std_rad
        tiles.append((np.asarray(arr, dtype=np.float64), sy, sx))
    return TiledTruth(
        unw_true=ig.unw_true,
        coherence=ig.coherence,
        mask=ig.mask,
        tiles=tiles,
        offsets_cycles=ks,
        rows=rows,
        cols=cols,
        overlap=overlap,
    )


# ---------------------------------------------------------------- SLC stack (phase linking)
def coherence_matrix_model(
    n_dates: int, tau_dates: float = 4.0, floor: float = 0.0, model: str = "exponential"
) -> FloatArray:
    """Real ``(n, n)`` coherence magnitude matrix ``gamma_ij = floor + (1-floor)*exp(-|i-j|/tau)``
    (``exponential``) or ``gamma_ij = 1`` for ``i == j`` else ``floor`` (``constant``)."""
    i = np.arange(n_dates)[:, None]
    j = np.arange(n_dates)[None, :]
    if model == "exponential":
        g = floor + (1.0 - floor) * np.exp(-np.abs(i - j) / float(tau_dates))
    elif model == "constant":
        g = np.where(i == j, 1.0, floor)
    else:
        msg = f"unknown coherence model {model!r}"
        raise ValueError(msg)
    return np.asarray(g, dtype=np.float64)


@dataclass
class SynthSlcStack:
    slc: ComplexArray  # (n_dates, ny, nx)
    phase_true: FloatArray  # (n_dates, ny, nx), phase_true[0] == 0
    gamma: FloatArray  # (n_dates, n_dates) coherence magnitude model
    amplitude: FloatArray  # (ny, nx) mean amplitude

    @property
    def n_dates(self) -> int:
        return int(self.slc.shape[0])

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.slc.shape[1]), int(self.slc.shape[2]))

    def pair_phase_true(self, i: int, j: int) -> FloatArray:
        """True interferometric phase of pair ``(i, j)``: ``φ_j - φ_i`` (secondary minus
        reference), consistent with :func:`make_stack` (``disp[j] - disp[i]``)."""
        return np.asarray(self.phase_true[j] - self.phase_true[i], dtype=np.float64)


def make_slc_stack(
    n_dates: int,
    shape: tuple[int, int],
    rng: np.random.Generator | None = None,
    phase_true: FloatArray | None = None,
    gamma: FloatArray | None = None,
    tau_dates: float = 4.0,
    coherence_floor: float = 0.0,
    amplitude: FloatArray | float = 1.0,
) -> SynthSlcStack:
    """Complex SLC stack with a **known** covariance: per pixel ``E[s s^H] = A² · Γ ∘ e^{j(φ_i-φ_j)}``.

    Samples are drawn as ``s = D · L · w`` with ``w ~ CN(0, I)``, ``L = chol(Γ)`` and
    ``D = diag(e^{jφ})`` — the standard distributed-scatterer model used to validate phase
    linking (Ansari et al. 2018, doi:10.1109/TGRS.2018.2826045, §II). ``phase_true[0]`` is
    forced to zero (reference date).
    """
    rng = rng or np.random.default_rng(0)
    ny, nx = shape
    g = gamma if gamma is not None else coherence_matrix_model(n_dates, tau_dates, coherence_floor)
    if g.shape != (n_dates, n_dates):
        msg = f"gamma must be ({n_dates}, {n_dates}), got {g.shape}"
        raise ValueError(msg)
    if phase_true is None:
        years = np.arange(n_dates) * 12.0 / 365.25
        vel = deformation_field(shape, "gaussian", -0.03)
        phase_true = PHASE_PER_M_LOS * vel[None, :, :] * years[:, None, None]
    phase_true = np.asarray(phase_true, dtype=np.float64) - phase_true[0][None, :, :]
    chol = np.linalg.cholesky(g + 1e-9 * np.eye(n_dates))
    w = rng.standard_normal((n_dates, ny * nx)) + 1j * rng.standard_normal((n_dates, ny * nx))
    w /= np.sqrt(2.0)
    z = (chol @ w).reshape(n_dates, ny, nx)
    amp = np.broadcast_to(np.asarray(amplitude, dtype=np.float64), shape)
    slc = amp[None, :, :] * z * np.exp(1j * phase_true)
    return SynthSlcStack(
        slc=np.asarray(slc, dtype=np.complex128),
        phase_true=phase_true,
        gamma=np.asarray(g, dtype=np.float64),
        amplitude=np.asarray(amp, dtype=np.float64).copy(),
    )
