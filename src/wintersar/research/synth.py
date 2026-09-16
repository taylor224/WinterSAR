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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
from numpy.typing import NDArray

from wintersar.io.schemas import Pair

WAVELENGTH_S1_M = 0.05546576  # C-band, 5.405 GHz
PHASE_PER_M_LOS = -4.0 * np.pi / WAVELENGTH_S1_M  # radians per metre of LOS *range change*

FloatArray = NDArray[np.float64]
ComplexArray = NDArray[np.complex128]


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


def _phase_noise(coh: FloatArray, rng: np.random.Generator, looks: int = 1) -> FloatArray:
    """Circular-Gaussian phase noise whose std follows the coherence (Cramér-Rao-like).

    std ≈ sqrt((1-γ²)/(2·L·γ²)); clipped to [0, π/√3] (uniform-phase limit).
    """
    g2 = coh * coh
    std = np.sqrt(np.clip((1.0 - g2) / (2.0 * max(looks, 1) * g2), 0.0, (np.pi**2) / 3.0))
    return rng.standard_normal(coh.shape) * std


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
    atmosphere_std_rad: float = 1.0,
    coherence_base: float = 0.8,
    looks: int = 1,
    water_fraction: float = 0.0,
    dem_error_rad: float = 0.0,
) -> SynthIgram:
    """One synthetic interferogram with ground truth."""
    rng = rng or np.random.default_rng(0)
    if displacement_m is None:
        displacement_m = deformation_field(shape, deformation_kind, deformation_amplitude_m)
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
    noisy = unw_true + _phase_noise(coh, rng, looks)
    return SynthIgram(
        wrapped=wrap(noisy),
        unw_true=unw_true,
        coherence=coh,
        mask=mask,
        displacement_m=displacement_m,
        atmosphere=atmo,
        meta={"deformation_kind": deformation_kind, "looks": looks},
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
) -> SynthStack:
    """SBAS-consistent synthetic stack: linear velocity bowl + per-date atmosphere."""
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
        noisy = unw_true + _phase_noise(coh, rng, looks)
        igrams[p.key] = SynthIgram(
            wrapped=wrap(noisy),
            unw_true=unw_true,
            coherence=coh,
            mask=mask,
            displacement_m=ddisp,
            atmosphere=datmo,
            meta={"looks": looks},
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
