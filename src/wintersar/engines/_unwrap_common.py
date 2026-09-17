"""Shared pieces of the 2-D / 3-D phase-unwrapping adapters (plan §5.2, §5.4; R-06, PERF-04).

The three unwrap engines (``snaphu``, ``tophu``, ``spurt``) expose the same structural
contract so that the unwrap scheduler (:mod:`wintersar.unwrap`) can treat them
interchangeably::

    engine.unwrap(igram, coh, mask, params) -> UnwrapResult(unw, conncomp, stats)
    engine.run("unwrap", inputs, params, log_dir) -> Artifacts   # whole stack, sequential

Conventions (same as :mod:`wintersar.io.igrams`):

* ``igram``: wrapped phase in radians, float32 ``(ny, nx)`` (NaN allowed = invalid)
* ``coh``: coherence in ``[0, 1]``
* ``mask``: bool, ``True`` = masked **out** (water / layover / invalid)
* ``UnwrapResult.unw``: float32, NaN where masked (SARscape-like behaviour, plan §5.4)
* ``UnwrapResult.conncomp``: uint32 connected-component labels, 0 = no component
* ``UnwrapResult.stats``: JSON-serialisable dict with at least ``wall_time_s``; when SNAPHU
  tiles were used it also carries ``tile_dir`` (for assemble-only re-runs, PERF-03).

Both callers put private (``_``-prefixed) keys into ``params``; the adapters must accept
either spelling, so the resolution of all three lives here:

* scratch / tile directory — ``_scratch_dir`` (per pair, from :meth:`UnwrapEngineBase.run`)
  or ``_tile_dir`` + ``_pair`` (one stage directory, from the scheduler): :func:`scratch_dir`
* adapter log — ``_log_path`` (run) or ``_log_dir`` (scheduler): :func:`engine_log`
* tile-level workers — ``nproc`` (the scheduler's planned value) or ``nproc_per_igram``
  (the raw ``UnwrapCfg`` field): :func:`resolve_nproc`

Nothing in this module re-implements SNAPHU/MCF (rule 11.3); it only prepares inputs,
maps parameters and post-processes outputs.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from numpy.typing import NDArray

from wintersar.engines.base import Engine
from wintersar.i18n import t
from wintersar.io.igrams import load_igram_stack, save_igram_stack
from wintersar.io.schemas import Artifact, Artifacts, Finding, Plan, Resources, Severity
from wintersar.util.masking import mask_mapping, mask_text

FloatArray = NDArray[np.floating[Any]]
BoolArray = NDArray[np.bool_]

I18N_NS = "engines_unwrap"

# SNAPHU single-tile memory model constant (MB per 1e6 pixels), plan §5.4 item 1.
# The value is a *placeholder* from the plan text until `wintersar bench` refits it
# (docs/open-questions.md #8); the config default lives in UnwrapCfg.memory_mb_per_mpixel.
DEFAULT_MEMORY_MB_PER_MPIXEL = 100.0


# ---------------------------------------------------------------------------- result


@dataclass
class UnwrapResult:
    """Output of one 2-D unwrapping call (field names are a structural contract)."""

    unw: NDArray[np.float32]
    conncomp: NDArray[np.uint32]
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.unw.shape[0]), int(self.unw.shape[1]))


# ---------------------------------------------------------------------------- errors


class UnwrapError(RuntimeError):
    """Adapter-level failure carrying a :class:`Finding` (cause → fix keys, rule 11.6)."""

    def __init__(self, finding: Finding, message: str | None = None) -> None:
        self.finding = finding
        super().__init__(message or t(finding.message_key, **finding.params))

    @classmethod
    def from_rule(
        cls,
        rule_id: str,
        *,
        severity: Severity = "FAIL",
        scope: str | None = None,
        evidence: dict[str, Any] | None = None,
        **params: Any,
    ) -> UnwrapError:
        return cls(
            make_finding(rule_id, severity=severity, scope=scope, evidence=evidence, **params)
        )


class StackOnlyEngineError(UnwrapError, NotImplementedError):
    """Raised by stack (3-D) unwrappers when asked to unwrap a single interferogram."""


class EngineRunError(UnwrapError):
    """External executable returned a non-zero exit status."""


def make_finding(
    rule_id: str,
    *,
    severity: Severity = "FAIL",
    scope: str | None = None,
    evidence: dict[str, Any] | None = None,
    **params: Any,
) -> Finding:
    """Build a ``Finding`` whose text lives under ``engines_unwrap.<rule_id>.{cause,fix}``."""
    safe_params = mask_mapping(params)
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message_key=f"{I18N_NS}.{rule_id}.cause",
        fix_key=f"{I18N_NS}.{rule_id}.fix",
        params=safe_params,
        evidence=mask_mapping(evidence or {}),
        scope=scope,
    )


# ---------------------------------------------------------------------------- parameters


def _unwrap_defaults() -> dict[str, Any]:
    """Defaults of ``UnwrapCfg`` (plan §4.4) without duplicating them here."""
    from wintersar.pipeline.config import UnwrapCfg

    return UnwrapCfg().model_dump(mode="json")


def unwrap_cfg(params: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the stage parameter mapping into one dict with ``UnwrapCfg`` defaults filled.

    ``Config.stage_params("unwrap")`` yields ``{"unwrap": {...}}``; the scheduler and tests
    may pass the flat form. Private executor keys (``_out_dir`` …) are preserved. Keys
    unknown to ``UnwrapCfg`` (``nlooks``, ``ntiles``, ``backend`` …) pass through untouched.
    """
    merged: dict[str, Any] = dict(_unwrap_defaults())
    top = {k: v for k, v in params.items() if k != "unwrap"}
    nested = params.get("unwrap")
    if isinstance(nested, Mapping):
        merged.update(nested)
    merged.update(top)
    return merged


def resolve_nlooks(
    cfg: Mapping[str, Any], attrs: Mapping[str, Any] | None = None
) -> tuple[float, str]:
    """Equivalent number of looks for the coherence statistics (SNAPHU ``NCORRLOOKS``).

    Precedence: ``cfg['nlooks']`` → ``cfg['looks']`` (int or ``[rg, az]``) →
    ``attrs['nlooks']`` → 1.0 (source string tells which one was used; the caller logs
    ``UNW-007`` for the fallback).
    """
    for src in ("nlooks", "looks"):
        v = cfg.get(src)
        if v is None or v == "auto":
            continue
        if isinstance(v, list | tuple):
            if len(v) == 2:
                return float(int(v[0]) * int(v[1])), src
            continue
        try:
            return float(v), src
        except (TypeError, ValueError):
            continue
    if attrs and attrs.get("nlooks") is not None:
        try:
            return float(attrs["nlooks"]), "attrs"
        except (TypeError, ValueError):
            pass
    return 1.0, "default"


def resolve_nproc(cfg: Mapping[str, Any]) -> int:
    """Tile-level worker processes for one interferogram (SNAPHU ``NPROC``), at least 1.

    Precedence: ``nproc`` — what :func:`wintersar.unwrap.api._backend_params` sends, i.e. the
    *planned* value, which the scheduler may raise above the config field (ADR-0047) — then
    ``nproc_per_igram`` (the raw ``UnwrapCfg`` field, used when an adapter is driven
    directly through :meth:`UnwrapEngineBase.run`), then 1.
    """
    for key in ("nproc", "nproc_per_igram"):
        value = cfg.get(key)
        if value is None:
            continue
        try:
            return max(int(value), 1)
        except (TypeError, ValueError):
            continue
    return 1


@dataclass(frozen=True)
class TileSpec:
    """SNAPHU tile grid: ``(rows, cols)`` tiles with ``(row, col)`` overlap in pixels."""

    rows: int = 1
    cols: int = 1
    overlap_px: tuple[int, int] = (0, 0)
    capped: bool = False
    requested_overlap_px: tuple[int, int] = (0, 0)

    @property
    def ntiles(self) -> tuple[int, int]:
        return (self.rows, self.cols)

    @property
    def tiled(self) -> bool:
        return self.rows > 1 or self.cols > 1


def _overlap_for_axis(n: int, ntiles: int, fraction: float, min_px: int) -> tuple[int, int, bool]:
    """(overlap_px, requested_px, capped) for one axis; 0 when the axis is not tiled."""
    if ntiles <= 1:
        return 0, 0, False
    tile = math.ceil(n / ntiles)
    requested = max(int(min_px), round(fraction * tile))
    limit = max(tile - 1, 0)
    if requested > limit:
        return limit, requested, True
    return requested, requested, False


def resolve_tiles(cfg: Mapping[str, Any], shape: tuple[int, int]) -> TileSpec:
    """Translate ``unwrap.tiles`` (plan §4.4) into a concrete grid for ``shape``.

    Precedence: explicit ``ntiles`` (+ ``tile_overlap`` px, as written by the scheduler)
    → ``tiles`` mapping ``{rows, cols, overlap (fraction of tile), min_overlap_px}`` →
    ``"auto"`` → single tile (the scheduler, not the adapter, decides auto-tiling).
    Overlaps larger than a tile are capped to ``tile - 1`` (``capped=True`` → UNW-006).
    """
    ny, nx = shape
    explicit = cfg.get("ntiles")
    if isinstance(explicit, list | tuple) and len(explicit) == 2:
        rows, cols = int(explicit[0]), int(explicit[1])
        ov = cfg.get("tile_overlap", 0)
        ov_r, ov_c = (
            (int(ov[0]), int(ov[1])) if isinstance(ov, list | tuple) else (int(ov), int(ov))
        )
        lim_r = max(math.ceil(ny / rows) - 1, 0) if rows > 1 else 0
        lim_c = max(math.ceil(nx / cols) - 1, 0) if cols > 1 else 0
        capped = ov_r > lim_r or ov_c > lim_c
        return TileSpec(
            rows=max(rows, 1),
            cols=max(cols, 1),
            overlap_px=(min(ov_r, lim_r), min(ov_c, lim_c)),
            capped=capped,
            requested_overlap_px=(ov_r, ov_c),
        )
    tiles = cfg.get("tiles", "auto")
    if not isinstance(tiles, Mapping):
        return TileSpec()
    rows = max(int(tiles.get("rows", 1)), 1)
    cols = max(int(tiles.get("cols", 1)), 1)
    fraction = float(tiles.get("overlap", 0.25))
    min_px = int(tiles.get("min_overlap_px", 200))
    ov_r, req_r, cap_r = _overlap_for_axis(ny, rows, fraction, min_px)
    ov_c, req_c, cap_c = _overlap_for_axis(nx, cols, fraction, min_px)
    return TileSpec(
        rows=rows,
        cols=cols,
        overlap_px=(ov_r, ov_c),
        capped=cap_r or cap_c,
        requested_overlap_px=(req_r, req_c),
    )


# ---------------------------------------------------------------------------- arrays


def build_masked(
    igram: FloatArray,
    coh: FloatArray,
    mask: BoolArray | None,
    coherence_threshold: float,
    use_coherence: bool = True,
) -> BoolArray:
    """``True`` where the pixel is excluded from unwrapping (plan §5.4 "마스크")."""
    masked = ~np.isfinite(igram) | ~np.isfinite(coh)
    if mask is not None:
        masked |= np.asarray(mask, dtype=bool)
    if use_coherence:
        masked |= np.nan_to_num(coh, nan=0.0) < float(coherence_threshold)
    return np.asarray(masked, dtype=bool)


def to_complex64(igram: FloatArray, coh: FloatArray, masked: BoolArray) -> NDArray[np.complex64]:
    """``coh * exp(i*phase)`` as complex64 with masked/NaN pixels set to 0 (same convention as
    :meth:`wintersar.io.igrams.IgramStack.complex`)."""
    phase = np.nan_to_num(np.asarray(igram, dtype=np.float64), nan=0.0)
    mag = np.clip(np.nan_to_num(np.asarray(coh, dtype=np.float64), nan=0.0), 0.0, 1.0)
    c = mag * np.exp(1j * phase)
    c[masked] = 0.0
    return np.asarray(c, dtype=np.complex64)


def clean_coherence(coh: FloatArray, masked: BoolArray) -> NDArray[np.float32]:
    out = np.clip(np.nan_to_num(np.asarray(coh, dtype=np.float32), nan=0.0), 0.0, 1.0)
    out[masked] = 0.0
    return np.asarray(out, dtype=np.float32)


def nan_masked(unw: FloatArray, masked: BoolArray) -> NDArray[np.float32]:
    out = np.array(unw, dtype=np.float32, copy=True)
    out[masked] = np.nan
    return out


def conncomp_masked(conncomp: NDArray[np.integer[Any]], masked: BoolArray) -> NDArray[np.uint32]:
    out = np.array(conncomp, dtype=np.uint32, copy=True)
    out[masked] = 0
    return out


def conncomp_stats(conncomp: NDArray[np.integer[Any]]) -> dict[str, Any]:
    labels, counts = np.unique(conncomp[conncomp != 0], return_counts=True)
    n = int(labels.size)
    total = int(conncomp.size) or 1
    return {
        "n_conncomp": n,
        "conncomp_max_label": int(labels.max()) if n else 0,
        "largest_conncomp_fraction": float(counts.max() / total) if n else 0.0,
        "labelled_fraction": float(counts.sum() / total) if n else 0.0,
    }


def single_tile_memory_mb(n_pixels: int, memory_mb_per_mpixel: float) -> float:
    """``m ~= c * P / 1e6`` (plan §5.4 item 1; ``c`` is refitted by bench, OQ #8)."""
    return float(memory_mb_per_mpixel) * float(n_pixels) / 1e6


# ---------------------------------------------------------------------------- versions

_VERSION_TOKEN = re.compile(r"\b(\d+\.\d+(?:\.\d+)?(?:[a-zA-Z0-9.+-]*)?)\b")


def command_version(
    cmd: Sequence[str],
    args: Sequence[str] = ("--version",),
    parser: Callable[[str], str | None] | None = None,
    timeout: float = 20.0,
) -> str | None:
    """Version of an already-resolved command (no ``PATH`` lookup, unlike
    :func:`wintersar.engines.base.executable_version`). ``"unknown"`` when it runs but prints
    nothing parseable; ``None`` when it cannot be executed at all."""
    try:
        proc = subprocess.run(
            [*cmd, *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    if parser is not None:
        return parser(out) or "unknown"
    m = _VERSION_TOKEN.search(out)
    return m.group(1) if m else "unknown"


# ---------------------------------------------------------------------------- logging


class EngineLog:
    """Append-only text log under ``log_dir`` (parsed later by :mod:`wintersar.diagnose`).

    Every line is masked with :func:`wintersar.util.masking.mask_text` (rule 11.11).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text: str, level: str = "INFO") -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.path.open("a", encoding="utf-8") as fh:
            for line in text.splitlines() or [""]:
                fh.write(f"{stamp} {level} {mask_text(line)}\n")

    def write_raw(self, text: str) -> None:
        """Verbatim engine stdout/stderr (masked) without per-line prefixes."""
        if not text:
            return
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(mask_text(text))
            if not text.endswith("\n"):
                fh.write("\n")

    def finding(self, f: Finding) -> None:
        self.write(f"{f.rule_id} {t(f.message_key, **f.params)}", level=f.severity)


def engine_log(cfg: Mapping[str, Any], engine_name: str) -> EngineLog | None:
    """Adapter log for one ``unwrap()`` call, or ``None`` when the caller asked for none.

    Precedence: ``_log_path`` (a complete path, what :meth:`UnwrapEngineBase.run` passes)
    then ``_log_dir``/``<engine>.log`` (the scheduler passes the stage log directory, so
    that adapter findings and engine stdout land where ``wintersar diagnose`` looks).
    """
    raw = cfg.get("_log_path")
    if raw:
        return EngineLog(Path(str(raw)))
    log_dir = cfg.get("_log_dir")
    if log_dir:
        return EngineLog(Path(str(log_dir)) / f"{engine_name}.log")
    return None


def scratch_dir(cfg: Mapping[str, Any], prefix: str) -> tuple[Path, bool]:
    """Directory for one interferogram's scratch/tile files, as ``(path, is_temp)``.

    Precedence: ``_scratch_dir`` / ``scratch_dir`` (already per pair, what
    :meth:`UnwrapEngineBase.run` passes) then ``_tile_dir``/``_pair`` (the scheduler passes
    one directory for the whole stage, so the pair key keeps concurrent pairs apart) then a
    fresh :func:`tempfile.mkdtemp`. Only the last is temporary (``is_temp=True``); the
    others outlive the call and may be reported as ``stats["tile_dir"]`` (PERF-03).
    """
    raw = cfg.get("_scratch_dir") or cfg.get("scratch_dir")
    if not raw:
        tile_dir = cfg.get("_tile_dir")
        if tile_dir:
            pair = cfg.get("_pair")
            raw = str(Path(str(tile_dir)) / str(pair)) if pair else str(tile_dir)
    if not raw:
        return Path(tempfile.mkdtemp(prefix=prefix)), True
    path = Path(str(raw))
    path.mkdir(parents=True, exist_ok=True)
    return path, False


def out_dir_from(params: Mapping[str, Any], log_dir: Path) -> Path:
    out = Path(params.get("_out_dir") or log_dir.parent)
    out.mkdir(parents=True, exist_ok=True)
    return out


def public_params(params: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if not str(k).startswith("_")}


# ---------------------------------------------------------------------------- engine base


class UnwrapEngineBase(Engine):
    """Common ``run``/``parse_log``/``estimate`` for the unwrap adapters.

    Subclasses implement :meth:`detect_version` and :meth:`unwrap`. ``run("unwrap")``
    loads the ``igrams`` artifact (``.npz`` interchange), unwraps every pair
    **sequentially** (the scheduler owns parallelism), writes ``unw.npz`` with
    :func:`save_igram_stack` semantics (``unw``, ``conncomp`` added to the input stack)
    plus ``stats.json`` and returns the artifacts ``unw`` and ``unw_stats``.
    """

    stages: ClassVar[tuple[str, ...]] = ("unwrap",)
    stack_only: ClassVar[bool] = False
    #: KB engine name handed to :func:`wintersar.diagnose.api.diagnose_logs` (``KB_ENGINES`` is
    #: ``snaphu, isce2, mintpy, hyp3, asf`` — tophu drives SNAPHU, spurt has no KB yet).
    diagnose_engine: ClassVar[str | None] = None

    def unwrap(
        self,
        igram: FloatArray,
        coh: FloatArray,
        mask: BoolArray | None,
        params: Mapping[str, Any],
    ) -> UnwrapResult:
        raise NotImplementedError

    # ------------------------------------------------------------------ run
    def run(
        self,
        stage: str,
        inputs: Artifacts,
        params: dict[str, Any],
        log_dir: Path,
    ) -> Artifacts:
        self._check_stage(stage)
        self.require_available()
        if "igrams" not in inputs:
            raise UnwrapError.from_rule(
                "UNW-002", engine=self.name, inputs=sorted(inputs.items), scope=stage
            )
        log = EngineLog(log_dir / f"{self.name}.log")
        out = out_dir_from(params, log_dir)
        stack = load_igram_stack(inputs["igrams"].path)
        cfg = unwrap_cfg(params)
        log.write(
            f"{self.name} unwrap start n_pairs={stack.n_pairs} shape={list(stack.shape)} "
            f"version={self.detect_version()} "
            f"params={json.dumps(mask_mapping(public_params(cfg)), default=str, sort_keys=True)}"
        )
        unws: list[NDArray[np.float32]] = []
        ccs: list[NDArray[np.uint32]] = []
        per_pair: dict[str, Any] = {}
        t0 = time.perf_counter()
        for i, key in enumerate(stack.pairs):
            pair_params = {
                **params,
                "_pair": key,
                "_scratch_dir": str(out / "scratch" / key),
                "_log_path": str(log.path),
                "_igram_attrs": dict(stack.attrs),
            }
            result = self.unwrap(
                stack.wrapped[i], stack.coherence[i], stack.mask_for(i), pair_params
            )
            unws.append(np.asarray(result.unw, dtype=np.float32))
            ccs.append(np.asarray(result.conncomp, dtype=np.uint32))
            per_pair[key] = mask_mapping(result.stats)
            log.write(
                f"{self.name} pair {key} done ({i + 1}/{stack.n_pairs}) "
                f"wall_time_s={result.stats.get('wall_time_s')} "
                f"n_conncomp={result.stats.get('n_conncomp')}"
            )
        stack.unw = np.stack(unws)
        stack.conncomp = np.stack(ccs)
        max_label = int(stack.conncomp.max()) if stack.conncomp.size else 0
        if max_label > np.iinfo(np.uint8).max:
            log.finding(make_finding("UNW-008", severity="WARN", max_label=max_label))
        unw_path = save_igram_stack(stack, out / "unw.npz")
        stats = {
            "engine": self.name,
            "engine_version": self.detect_version(),
            "n_pairs": stack.n_pairs,
            "shape": list(stack.shape),
            "wall_time_s": round(time.perf_counter() - t0, 3),
            "masked_fraction": float(np.isnan(stack.unw).mean()) if stack.unw.size else 0.0,
            "conncomp_max_label": max_label,
            "pairs": per_pair,
            "params": mask_mapping(public_params(cfg)),
        }
        stats_path = out / "stats.json"
        stats_path.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        log.write(f"{self.name} unwrap done wall_time_s={stats['wall_time_s']} out={unw_path}")
        meta = {k: v for k, v in stats.items() if k not in {"pairs", "params"}}
        return (
            Artifacts()
            .add(Artifact(name="unw", path=unw_path, kind="npz", meta=meta))
            .add(Artifact(name="unw_stats", path=stats_path, kind="json", meta={}))
        )

    def _check_stage(self, stage: str) -> None:
        if stage not in self.stages:
            raise UnwrapError.from_rule(
                "UNW-001", engine=self.name, stage=stage, stages=", ".join(self.stages)
            )

    # ------------------------------------------------------------------ estimate
    def estimate(self, plan: Plan) -> Resources:
        """Memory from the single-tile model when a stage record carries ``n_pixels``."""
        total = Resources()
        for rec in plan.stages:
            if rec.stage != "unwrap" or rec.engine not in (None, self.name):
                continue
            cfg = unwrap_cfg(rec.params)
            n_pixels = rec.extra.get("n_pixels") or cfg.get("_n_pixels")
            if not n_pixels:
                continue
            c = float(cfg.get("memory_mb_per_mpixel", DEFAULT_MEMORY_MB_PER_MPIXEL))
            mb = single_tile_memory_mb(int(n_pixels), c)
            total = total + Resources(
                peak_rss_gb=mb / 1024.0,
                n_jobs=int(rec.extra.get("n_pairs") or 1),
                notes={"memory_model": "single_tile_mb_per_mpixel", "c_mb_per_mpixel": c},
            )
        return total

    # ------------------------------------------------------------------ parse_log
    def parse_log(self, log_path: Path) -> list[Finding]:
        """Delegate to :mod:`wintersar.diagnose` lazily (absent → ``[]``).

        ``wintersar.diagnose.api.diagnose_logs(path: Path, engine: str | None = None, ...)``
        accepts one log file or a directory (source: src/wintersar/diagnose/api.py).
        """
        try:
            from wintersar.diagnose.api import diagnose_logs
        except ImportError:
            return []
        try:
            return list(diagnose_logs(Path(log_path), engine=self.diagnose_engine))
        except ValueError:  # engine name not in the KB yet -> engine-agnostic matching
            return list(diagnose_logs(Path(log_path), engine=None))
