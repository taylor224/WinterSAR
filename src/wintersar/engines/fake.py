"""Fake engine: runs the whole pipeline on synthetic data without network or external
engines (Phase 0 DoD: synthetic end-to-end integration test).

Stage outputs are ``.npz`` files inside ``params["_out_dir"]`` (set by the executor):

* ``interferogram`` → ``igrams.npz`` with ``wrapped``, ``coherence``, ``mask`` stacks of shape
  ``(n_pairs, ny, nx)``, plus ``pairs`` (keys) and ``dates`` (ISO) and ``truth`` arrays.
* ``unwrap`` → ``unw.npz`` (``unw``, ``conncomp``).
* ``timeseries`` → ``timeseries.npz`` (``dates``, ``displacement_m`` (n_dates, ny, nx),
  ``velocity_m_per_yr``) — for the fake engine this is the synthetic truth plus noise, not an
  inversion (the SBAS core is never re-implemented here, rule 11.3).

``params`` understood: ``n_dates``, ``shape``, ``seed``, ``atmosphere_std_rad``,
``coherence_base``, ``water_fraction``, ``looks``, ``coherence_threshold`` (unwrap mask),
``fail_stage`` (raise at that stage — used to test diagnose attachment).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from wintersar import __version__
from wintersar.engines.base import Engine, register_engine
from wintersar.io.schemas import Artifact, Artifacts
from wintersar.research import synth


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
        _log(log_dir, stage, f"fake {stage} done outputs={list(result.items)}")
        return result

    # ------------------------------------------------------------------ stages
    def _stage_fetch(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        meta = {"n_dates": int(params.get("n_dates", 6)), "source": "synthetic"}
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
        n_dates = int(params.get("n_dates", 6))
        shape = tuple(int(x) for x in params.get("shape", (64, 64)))
        rng = np.random.default_rng(int(params.get("seed", 0)))
        stack = synth.make_stack(
            n_dates=n_dates,
            shape=(shape[0], shape[1]),
            rng=rng,
            atmosphere_std_rad=float(params.get("atmosphere_std_rad", 0.3)),
            coherence_base=float(params.get("coherence_base", 0.8)),
            looks=int(params.get("looks", 1)),
            water_fraction=float(params.get("water_fraction", 0.0)),
            max_temporal_days=int(params.get("max_temporal_days", 48)),
        )
        keys = [p.key for p in stack.pairs]
        p = out / "igrams.npz"
        np.savez_compressed(
            p,
            wrapped=np.stack([stack.igrams[k].wrapped for k in keys]).astype(np.float32),
            coherence=np.stack([stack.igrams[k].coherence for k in keys]).astype(np.float32),
            mask=np.stack([stack.igrams[k].mask for k in keys]),
            unw_true=np.stack([stack.igrams[k].unw_true for k in keys]).astype(np.float32),
            pairs=np.array(keys),
            dates=np.array([d.isoformat() for d in stack.dates]),
            velocity_true=stack.velocity_m_per_yr.astype(np.float32),
            displacement_true=stack.displacement_m.astype(np.float32),
        )
        meta = {"n_pairs": len(keys), "n_dates": n_dates, "shape": list(shape)}
        return Artifacts().add(Artifact(name="igrams", path=p, kind="npz", meta=meta))

    def _stage_multilook(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        # synthetic data is already at target resolution; pass through with a manifest
        src = inputs["igrams"]
        p = out / "igrams.npz"
        if p.resolve() != src.path.resolve():
            p.write_bytes(src.path.read_bytes())
        return Artifacts().add(Artifact(name="igrams", path=p, kind="npz", meta=dict(src.meta)))

    def _stage_unwrap(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        data = np.load(inputs["igrams"].path)
        thr = float(params.get("coherence_threshold", 0.3))
        coh = data["coherence"]
        unw = data["unw_true"].astype(np.float32).copy()
        masked = (coh < thr) | data["mask"]
        unw[masked] = np.nan
        conncomp = np.where(masked, 0, 1).astype(np.uint8)
        p = out / "unw.npz"
        np.savez_compressed(p, unw=unw, conncomp=conncomp, pairs=data["pairs"], dates=data["dates"])
        meta = {
            "n_pairs": int(unw.shape[0]),
            "masked_fraction": float(masked.mean()),
            "coherence_threshold": thr,
        }
        return Artifacts().add(Artifact(name="unw", path=p, kind="npz", meta=meta))

    def _stage_timeseries(self, inputs: Artifacts, params: dict[str, Any], out: Path) -> Artifacts:
        ig = np.load(inputs["igrams"].path)
        rng = np.random.default_rng(int(params.get("seed", 0)) + 1)
        disp = ig["displacement_true"].astype(np.float32)
        disp = disp + rng.normal(0, 0.001, disp.shape).astype(np.float32)
        p = out / "timeseries.npz"
        np.savez_compressed(
            p, dates=ig["dates"], displacement_m=disp, velocity_m_per_yr=ig["velocity_true"]
        )
        return Artifacts().add(
            Artifact(name="timeseries", path=p, kind="npz", meta={"n_dates": int(disp.shape[0])})
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
            "shape": list(vel.shape),
        }
        return Artifacts().add(Artifact(name="velocity", path=p, kind="npy", meta=meta))
