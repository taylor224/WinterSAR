"""Fake engine: runs the whole pipeline on synthetic data without network or external
engines (Phase 0 DoD: synthetic end-to-end integration test; PERF-06 incremental reference).

Stage outputs are ``.npz`` files inside ``params["_out_dir"]`` (set by the executor):

* ``interferogram`` → ``igrams.npz`` with ``wrapped``, ``coherence``, ``mask``, ``unw_true``
  stacks of shape ``(n_pairs, ny, nx)``, plus ``pairs`` (keys) and ``dates`` (ISO) and the
  ``velocity_true`` / ``displacement_true`` arrays.
* ``unwrap`` → ``unw.npz`` (``unw``, ``conncomp``).
* ``timeseries`` → ``timeseries.npz`` (``dates``, ``displacement_m`` (n_dates, ny, nx),
  ``velocity_m_per_yr``) — for the fake engine this is the synthetic truth plus noise, not an
  inversion (the SBAS core is never re-implemented here, rule 11.3).

``params`` understood — **top level only** (ADR-0034): ``n_dates``, ``shape``, ``seed``,
``atmosphere_std_rad``, ``coherence_base``, ``water_fraction``, ``looks``,
``max_temporal_days``, ``coherence_threshold`` (unwrap mask), ``fail_stage`` (raise at that
stage — used to test diagnose attachment).

Per ADR-0034 config values reach an engine nested under their section
(``params["unwrap"]["coherence_threshold"]``) while experiment overrides land at the top
level; the fake engine reads only the latter. A ``config.yaml`` value therefore changes the
node hash on the fake path but not the synthetic output — drive it with
``--set unwrap.coherence_threshold=…`` (CLI) or ``param_overrides`` (API) instead.

**Per-pair purity (PERF-06, ADR-0080).** The acquisition dates are deterministic
(``2024-01-01`` + 12 days * i, :func:`fake_dates`) and every interferogram is a pure
function of the synthesis parameters and its pair key: the per-date atmosphere is drawn
from ``rng(seed, date)`` and the per-pair coherence/noise from ``rng(seed, pair)``
(:meth:`FakeEngine.synth_pair`). Adding a date therefore leaves every old pair bitwise
identical, so the stage can rebuild the stack from cached pairs plus the new ones. The
executor asks for that by passing ``_pairs_dir`` / ``_pairs_done``
(:class:`wintersar.pipeline.incremental.PairCache`); without them the engine computes
everything in memory exactly as before. ``interferogram``, ``multilook`` and ``unwrap`` all
follow the contract; ``timeseries`` is always recomputed (it is the re-inversion).
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from numpy.typing import NDArray

from wintersar import __version__
from wintersar.engines.base import Engine, register_engine
from wintersar.io.schemas import Artifact, Artifacts, Pair
from wintersar.pipeline.incremental import PairCache, pair_content_hash, pair_identity
from wintersar.research import synth
from wintersar.util.hashing import hash_params

#: ``synth.make_stack`` defaults, kept so the synthetic site looks the same as before
START_DATE = date(2024, 1, 1)
REPEAT_DAYS = 12
DEFAULT_N_DATES = 6
DEFAULT_SHAPE = (64, 64)
DEFAULT_MAX_TEMPORAL_DAYS = 48
VELOCITY_PEAK_M_PER_YR = -0.03  # source: src/wintersar/research/synth.py::make_stack default

FloatArray = NDArray[np.floating[Any]]


class FakeEngineFailureError(RuntimeError):
    """Deliberate failure injected via ``params['fail_stage']``."""


def _out_dir(params: dict[str, Any], log_dir: Path) -> Path:
    out = Path(params.get("_out_dir") or log_dir.parent)
    out.mkdir(parents=True, exist_ok=True)
    return out


def _log(log_dir: Path, stage: str, text: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / f"{stage}.log"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")
    return p


# ---------------------------------------------------------------------- deterministic stack


def fake_dates(params: Mapping[str, Any]) -> list[date]:
    """``n_dates`` acquisitions, 12 days apart from 2024-01-01 (``N -> N+1`` appends one)."""
    n = int(params.get("n_dates", DEFAULT_N_DATES))
    return [START_DATE + timedelta(days=REPEAT_DAYS * i) for i in range(n)]


def fake_pairs(params: Mapping[str, Any]) -> list[Pair]:
    """SBAS pairs of :func:`fake_dates` within ``max_temporal_days`` (default 48)."""
    return synth.sbas_pairs(
        fake_dates(params), int(params.get("max_temporal_days", DEFAULT_MAX_TEMPORAL_DAYS))
    )


def rng_for(seed: int, *tokens: str) -> np.random.Generator:
    """Generator keyed by ``seed`` and a stable token (a date, a pair key …).

    The seed is a *sequence* of two non-negative ints: ``default_rng`` passes an
    ``array_like[ints]`` seed to ``SeedSequence``, whose entropy mixing makes the stream a
    pure function of ``(seed, token)`` — the per-pair purity ADR-0080 §3 relies on.
    # source: .venv/lib/python3.11/site-packages/numpy/random/_generator.pyi
    #   (``default_rng(seed: _ArrayLikeInt_co | SeedSequence | …)``) and
    #   ``numpy.random.default_rng.__doc__`` (numpy 2.4): "seed : {None, int,
    #   array_like[ints], SeedSequence, …} … If an int or array_like[ints] is passed, then
    #   all values must be non-negative and will be passed to SeedSequence to derive the
    #   initial BitGenerator state"
    # source: https://numpy.org/doc/stable/reference/random/bit_generators/generated/numpy.random.SeedSequence.html
    """
    digest = hashlib.sha256("\0".join(tokens).encode("utf-8")).digest()
    return np.random.default_rng([int(seed), int.from_bytes(digest[:8], "big")])


def synth_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """The parameters a single synthetic pair depends on (the per-pair identity payload)."""
    shape = tuple(int(x) for x in params.get("shape", DEFAULT_SHAPE))
    return {
        "shape": [shape[0], shape[1]],
        "seed": int(params.get("seed", 0)),
        "atmosphere_std_rad": float(params.get("atmosphere_std_rad", 0.3)),
        "coherence_base": float(params.get("coherence_base", 0.8)),
        "looks": int(params.get("looks", 1)),
        "water_fraction": float(params.get("water_fraction", 0.0)),
    }


@dataclass
class StackSpec:
    """Deterministic description of the synthetic stack (dates, velocity, atmosphere)."""

    dates: list[date]
    shape: tuple[int, int]
    seed: int
    atmosphere_std_rad: float
    coherence_base: float
    looks: int
    water_fraction: float
    velocity: FloatArray = field(init=False)
    _atmo: dict[date, FloatArray] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.velocity = synth.deformation_field(self.shape, "gaussian", VELOCITY_PEAK_M_PER_YR)

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> StackSpec:
        sp = synth_params(params)
        return cls(
            dates=fake_dates(params),
            shape=(int(sp["shape"][0]), int(sp["shape"][1])),
            seed=int(sp["seed"]),
            atmosphere_std_rad=float(sp["atmosphere_std_rad"]),
            coherence_base=float(sp["coherence_base"]),
            looks=int(sp["looks"]),
            water_fraction=float(sp["water_fraction"]),
        )

    def years(self, d: date) -> float:
        return (d - self.dates[0]).days / 365.25

    def displacement(self, d: date) -> FloatArray:
        """Cumulative LOS displacement at ``d`` (metres, 0 at the first date)."""
        return np.asarray(self.velocity * self.years(d), dtype=np.float64)

    def atmosphere(self, d: date) -> FloatArray:
        """Per-date atmospheric screen; the first date is the zero reference."""
        if d == self.dates[0] or self.atmosphere_std_rad <= 0:
            return np.zeros(self.shape, dtype=np.float64)
        if d not in self._atmo:
            self._atmo[d] = synth.turbulent_atmosphere(
                self.shape, rng_for(self.seed, "atmo", d.isoformat()), self.atmosphere_std_rad
            )
        return self._atmo[d]

    def displacement_stack(self) -> NDArray[np.float32]:
        return np.stack([self.displacement(d) for d in self.dates]).astype(np.float32)


PAIR_ARRAYS = ("wrapped", "coherence", "mask", "unw_true")


def _load_pair(path: Path, keys: tuple[str, ...]) -> dict[str, NDArray[Any]] | None:
    """Arrays of a cached pair file, or ``None`` when it cannot be loaded.

    The manifest's fast hash (size, mtime, head/tail) does not see every corruption; an
    unloadable hit is a cache miss for that pair (recomputed, re-stored), never a stage
    failure (ADR-0080).
    """
    try:
        with np.load(path, allow_pickle=False) as z:
            return {k: np.asarray(z[k]) for k in keys}
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
        return None


def _cached_pair(
    cache: PairCache | None, key: str, ident: str, keys: tuple[str, ...]
) -> dict[str, NDArray[Any]] | None:
    """Reusable arrays for ``key`` (identity match, file intact *and* loadable) or ``None``."""
    if cache is None:
        return None
    hit = cache.lookup(key, ident)
    if hit is None:
        return None
    data = _load_pair(hit, keys)
    if data is None:
        cache.discard(key)
    return data


@register_engine
class FakeEngine(Engine):
    name: ClassVar[str] = "fake"
    version_constraint: ClassVar[str] = "*"
    stages: ClassVar[tuple[str, ...]] = (
        "fetch",
        "coregister",
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
    )
    install_hint: ClassVar[str] = "built-in"
    license_note: ClassVar[str] = "Apache-2.0 (part of wintersar)"

    def detect_version(self) -> str | None:
        return __version__

    # ------------------------------------------------------------------ PERF-06 hooks
    def expected_pairs(
        self, stage: str, params: Mapping[str, Any], available: Artifacts | None = None
    ) -> list[str] | None:
        """Pair keys the interferogram stage will produce (``plan`` counts, ADR-0080)."""
        if stage != "interferogram":
            return None
        return [p.key for p in fake_pairs(params)]

    def synth_pair(self, spec: StackSpec, pair: Pair) -> dict[str, NDArray[Any]]:
        """One interferogram as a pure function of ``(spec, pair)`` (loop-closure consistent).

        Deformation and atmosphere are per-date differences, coherence and phase noise are
        drawn from ``rng(seed, pair)``, so the result never depends on which other dates are
        in the stack.
        """
        ddisp = spec.displacement(pair.secondary) - spec.displacement(pair.reference)
        datmo = spec.atmosphere(pair.secondary) - spec.atmosphere(pair.reference)
        ig = synth.make_interferogram(
            spec.shape,
            rng_for(spec.seed, "pair", pair.key),
            displacement_m=ddisp,
            atmosphere_std_rad=0.0,
            coherence_base=spec.coherence_base,
            looks=spec.looks,
            water_fraction=spec.water_fraction,
        )
        return {
            "wrapped": synth.wrap(ig.wrapped + datmo).astype(np.float32),
            "coherence": ig.coherence.astype(np.float32),
            "mask": np.asarray(ig.mask, dtype=bool),
            "unw_true": (ig.unw_true + datmo).astype(np.float32),
        }

    def unwrap_pair(
        self,
        coherence: NDArray[Any],
        mask: NDArray[Any],
        unw_true: NDArray[Any],
        coherence_threshold: float,
    ) -> tuple[NDArray[np.float32], NDArray[np.uint8]]:
        """Fake unwrapping of one pair: the truth, masked below the coherence threshold."""
        masked = (np.asarray(coherence) < coherence_threshold) | np.asarray(mask, dtype=bool)
        unw = np.asarray(unw_true, dtype=np.float32).copy()
        unw[masked] = np.nan
        conncomp = np.where(masked, 0, 1).astype(np.uint8)
        return unw, conncomp

    # ------------------------------------------------------------------ run
    def run(
        self, stage: str, inputs: Artifacts, params: dict[str, Any], log_dir: Path
    ) -> Artifacts:
        if params.get("fail_stage") == stage:
            _log(log_dir, stage, f"ERROR: fake engine failure injected at stage {stage}")
            msg = f"fake engine failure injected at stage {stage}"
            raise FakeEngineFailureError(msg)
        handler = getattr(self, f"_stage_{stage}", None)
        if handler is None:
            msg = f"fake engine does not implement stage {stage!r}"
            raise NotImplementedError(msg)
        out = _out_dir(params, log_dir)
        _log(
            log_dir,
            stage,
            f"fake {stage} start params={json.dumps({k: v for k, v in params.items() if not k.startswith('_')}, default=str)}",
        )
        result: Artifacts = handler(inputs, params, out)
        for art in result.items.values():
            if "pairs_reused" in art.meta:
                _log(
                    log_dir,
                    stage,
                    f"fake {stage} pairs reused={len(art.meta['pairs_reused'])} "
                    f"computed={len(art.meta['pairs_computed'])}",
                )
        _log(log_dir, stage, f"fake {stage} done outputs={list(result.items)}")
        return result

    # ------------------------------------------------------------------ stages
    def _stage_fetch(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        meta = {"n_dates": int(params.get("n_dates", DEFAULT_N_DATES)), "source": "synthetic"}
        p = out / "fetch.json"
        p.write_text(json.dumps(meta), encoding="utf-8")
        return Artifacts().add(Artifact(name="slc_manifest", path=p, kind="json", meta=meta))

    def _stage_coregister(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        p = out / "coreg.json"
        p.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
        return Artifacts().add(Artifact(name="coreg_manifest", path=p, kind="json"))

    def _stage_interferogram(
        self, inputs: Artifacts, params: dict[str, Any], out: Path
    ) -> Artifacts:
        spec = StackSpec.from_params(params)
        pairs = fake_pairs(params)
        cache = PairCache.from_params(params, "interferogram")
        param_hash = hash_params({"stage": "interferogram", **synth_params(params)})
        stacks: dict[str, list[NDArray[Any]]] = {k: [] for k in PAIR_ARRAYS}
        for pair in pairs:
            ident = pair_identity(param_hash, pair.key)
            data = _cached_pair(cache, pair.key, ident, PAIR_ARRAYS)
            if data is None:
                data = self.synth_pair(spec, pair)
                if cache is not None:
                    p = cache.path_for(pair.key)
                    np.savez(p, **data)  # type: ignore[arg-type]
                    cache.store(pair.key, ident, p)
            for k in PAIR_ARRAYS:
                stacks[k].append(data[k])
        keys = [p.key for p in pairs]
        p = out / "igrams.npz"
        np.savez_compressed(
            p,
            wrapped=np.stack(stacks["wrapped"]).astype(np.float32),
            coherence=np.stack(stacks["coherence"]).astype(np.float32),
            mask=np.stack(stacks["mask"]),
            unw_true=np.stack(stacks["unw_true"]).astype(np.float32),
            pairs=np.array(keys),
            dates=np.array([d.isoformat() for d in spec.dates]),
            velocity_true=spec.velocity.astype(np.float32),
            displacement_true=spec.displacement_stack(),
        )
        meta: dict[str, Any] = {
            "n_pairs": len(keys),
            "n_dates": len(spec.dates),
            "shape": [spec.shape[0], spec.shape[1]],
            "pairs": keys,
            "dates": [d.isoformat() for d in spec.dates],
        }
        if cache is not None:
            cache.save()
            meta.update(cache.summary())
        return Artifacts().add(Artifact(name="igrams", path=p, kind="npz", meta=meta))

    def _stage_multilook(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        # synthetic data is already at target resolution: pass through. With per-pair caching
        # requested the stack is rebuilt pair by pair so the contract is exercised end-to-end.
        src = inputs["igrams"]
        p = out / "igrams.npz"
        meta = dict(src.meta)
        cache = PairCache.from_params(params, "multilook")
        if cache is None:
            if p.resolve() != src.path.resolve():
                p.write_bytes(src.path.read_bytes())
            return Artifacts().add(Artifact(name="igrams", path=p, kind="npz", meta=meta))
        param_hash = hash_params({"stage": "multilook", "looks": params.get("looks", 1)})
        with np.load(src.path, allow_pickle=False) as z:
            arrays = {k: np.asarray(z[k]) for k in z.files}
        keys = [str(k) for k in arrays["pairs"]]
        stacks: dict[str, list[NDArray[Any]]] = {k: [] for k in PAIR_ARRAYS}
        for i, key in enumerate(keys):
            pair = {k: arrays[k][i] for k in PAIR_ARRAYS}
            ident = pair_identity(param_hash, pair_content_hash(*(pair[k] for k in PAIR_ARRAYS)))
            cached = _cached_pair(cache, key, ident, PAIR_ARRAYS)
            if cached is not None:
                pair = cached
            else:
                path = cache.path_for(key)
                np.savez(path, **pair)
                cache.store(key, ident, path)
            for k in PAIR_ARRAYS:
                stacks[k].append(pair[k])
        assembled: dict[str, NDArray[Any]] = {k: np.stack(v) for k, v in stacks.items()}
        assembled.update({k: v for k, v in arrays.items() if k not in PAIR_ARRAYS})
        np.savez_compressed(p, **assembled)  # type: ignore[arg-type]
        cache.save()
        meta.update(cache.summary())
        return Artifacts().add(Artifact(name="igrams", path=p, kind="npz", meta=meta))

    def _stage_unwrap(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        thr = float(params.get("coherence_threshold", 0.3))
        with np.load(inputs["igrams"].path, allow_pickle=False) as z:
            data = {k: np.asarray(z[k]) for k in (*PAIR_ARRAYS, "pairs", "dates")}
        keys = [str(k) for k in data["pairs"]]
        cache = PairCache.from_params(params, "unwrap")
        param_hash = hash_params({"stage": "unwrap", "coherence_threshold": thr})
        unw_l: list[NDArray[np.float32]] = []
        cc_l: list[NDArray[np.uint8]] = []
        for i, key in enumerate(keys):
            pair = {k: data[k][i] for k in PAIR_ARRAYS}
            result: tuple[NDArray[np.float32], NDArray[np.uint8]] | None = None
            ident = None
            if cache is not None:
                ident = pair_identity(
                    param_hash, pair_content_hash(*(pair[k] for k in PAIR_ARRAYS))
                )
                got = _cached_pair(cache, key, ident, ("unw", "conncomp"))
                if got is not None:
                    result = (got["unw"].astype(np.float32), got["conncomp"].astype(np.uint8))
            if result is None:
                result = self.unwrap_pair(pair["coherence"], pair["mask"], pair["unw_true"], thr)
                if cache is not None and ident is not None:
                    path = cache.path_for(key)
                    np.savez(path, unw=result[0], conncomp=result[1])
                    cache.store(key, ident, path)
            unw_l.append(result[0])
            cc_l.append(result[1])
        unw = np.stack(unw_l).astype(np.float32)
        conncomp = np.stack(cc_l).astype(np.uint8)
        p = out / "unw.npz"
        np.savez_compressed(p, unw=unw, conncomp=conncomp, pairs=data["pairs"], dates=data["dates"])
        masked = np.isnan(unw)
        meta: dict[str, Any] = {
            "n_pairs": int(unw.shape[0]),
            "masked_fraction": float(masked.mean()) if masked.size else 0.0,
            "coherence_threshold": thr,
            "pairs": keys,
        }
        if cache is not None:
            cache.save()
            meta.update(cache.summary())
        return Artifacts().add(Artifact(name="unw", path=p, kind="npz", meta=meta))

    def _stage_timeseries(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        # always recomputed: the time series is the re-inversion of the whole stack (PERF-06)
        ig = np.load(inputs["igrams"].path)
        rng = np.random.default_rng(int(params.get("seed", 0)) + 1)
        disp = ig["displacement_true"].astype(np.float32)
        disp = disp + rng.normal(0, 0.001, disp.shape).astype(np.float32)
        p = out / "timeseries.npz"
        np.savez_compressed(
            p, dates=ig["dates"], displacement_m=disp, velocity_m_per_yr=ig["velocity_true"]
        )
        dates = [str(d) for d in ig["dates"]]
        return Artifacts().add(
            Artifact(
                name="timeseries",
                path=p,
                kind="npz",
                meta={"n_dates": int(disp.shape[0]), "dates": dates},
            )
        )

    def _stage_corrections(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        src = inputs["timeseries"]
        p = out / "timeseries.npz"
        if p.resolve() != src.path.resolve():
            p.write_bytes(src.path.read_bytes())
        return Artifacts().add(Artifact(name="timeseries", path=p, kind="npz", meta=dict(src.meta)))

    def _stage_geocode(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        ts = np.load(inputs["timeseries"].path)
        vel = ts["velocity_m_per_yr"]
        p = out / "velocity.npy"
        np.save(p, vel)
        dates = [date.fromisoformat(str(d)) for d in ts["dates"]]
        meta = {
            "start": dates[0].isoformat(),
            "end": dates[-1].isoformat(),
            "n_dates": len(dates),
            "shape": list(vel.shape),
        }
        return Artifacts().add(Artifact(name="velocity", path=p, kind="npy", meta=meta))
