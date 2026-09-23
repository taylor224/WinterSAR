"""Incremental update mode: per-pair sub-caching inside a stage (plan §6.1 PERF-06, §5.3;
ADR-0080).

Adding one acquisition date to an already processed stack must re-run only what the new
date needs — the interferogram pairs that touch it and their unwrapping — and re-invert the
time series. Two mechanisms make that possible without changing the DAG cache contract
(ADR-0031/0032):

1. **Hash rule** (:func:`hash_view`, :func:`identity_inputs`). For the *incremental stages*
   (:data:`INCREMENTAL_STAGES`) the pair/date set is *data*, not a parameter: the top-level
   :data:`DATA_KEYS` (``n_dates`` on the fake path) and the :data:`DATA_INPUTS` artifacts
   (``stack`` from precheck on the real path) are excluded from the node hash, and an input
   produced by another incremental stage contributes the producer's *node hash* instead of
   its content hash. The node directory therefore stays the same when a date is added or an
   upstream incremental stage re-runs; whether the cached result is still *fresh* is decided
   separately by comparing the recorded input content hashes and data values
   (``Dag.fresh``). A parameter change still yields a new hash and the full PERF-03
   downstream invalidation.

2. **Per-pair manifest** (:class:`PairCache`). A node directory keeps
   ``pairs/manifest.json`` (``{pair_key: {hash, path, file_hash, meta}}``) next to
   ``out/``. The executor hands the entries to the stage as ``params["_pairs_done"]`` and
   the directory as ``params["_pairs_dir"]``; the engine computes only the pairs whose
   identity hash is missing or different, assembles the stack artifact from old + new, and
   reports ``meta["pairs_reused"]`` / ``meta["pairs_computed"]``. The identity hash of a pair
   is ``hash(stage parameters that influence the pair, per-pair input hash)`` — for the
   fake interferogram the input is the pair key (synthesis is a pure function of it), for
   unwrapping it is the content hash of the pair's arrays (:func:`pair_content_hash`).
   Real engines receive the same keys and may ignore them (ADR-0082).

   Only the :data:`PAIR_STAGES` (interferogram, multilook, unwrap) take part in this
   contract: ``fetch``/``coregister`` follow the hash rule but their work is per date inside
   the engine's own working directory (ADR-0082), so they are never asked for sub-caching
   and never reported as "not supporting" it. A ``pairs/manifest.json`` that exists but
   cannot be read is discarded (every pair is computed again) and the cause is carried on
   :attr:`PairCache.manifest_error` so the plan/run can say so (``PIPELINE-017``); a
   cached pair file that turns out unloadable is a cache miss for that pair, not a stage
   failure (:meth:`PairCache.discard`).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from wintersar.io.schemas import Artifacts, Finding, StageRecord
from wintersar.util.hashing import hash_params, hash_path

__all__ = [
    "DATA_INPUTS",
    "DATA_KEYS",
    "INCREMENTAL_STAGES",
    "META_COMPUTED",
    "META_PAIRS",
    "META_REUSED",
    "PAIRS_DIRNAME",
    "PAIRS_DIR_KEY",
    "PAIRS_DONE_KEY",
    "PAIRS_MANIFEST",
    "PAIR_STAGES",
    "PairCache",
    "PairEntry",
    "PartialCache",
    "data_view",
    "expected_pairs",
    "hash_view",
    "identity_inputs",
    "is_incremental_stage",
    "is_pair_stage",
    "manifest_finding",
    "pair_content_hash",
    "pair_counts",
    "pair_identity",
    "pairs_dir",
]

#: engine stages whose work is per date/pair and whose node identity therefore treats the
#: pair set as data (plan §6.1 PERF-06: "정합 SLC·간섭도 캐시 재사용, 새 쌍만 생성·언래핑")
INCREMENTAL_STAGES: tuple[str, ...] = (
    "fetch",
    "coregister",
    "interferogram",
    "multilook",
    "unwrap",
)
#: incremental stages whose results are per interferogram pair and can be kept in ``pairs/``
#: (the sub-cache contract, ADR-0080 §2). ``fetch``/``coregister`` are incremental for the
#: hash rule only: no engine (fake included) has per-pair results for them (ADR-0082).
PAIR_STAGES: tuple[str, ...] = ("interferogram", "multilook", "unwrap")
#: top-level stage parameters that describe *which* dates/pairs, not *how* (fake path)
DATA_KEYS: frozenset[str] = frozenset({"n_dates", "dates", "pairs"})
#: input artifacts that carry the date/pair set on the real path (precheck ``stack.json``)
DATA_INPUTS: frozenset[str] = frozenset({"stack"})

PAIRS_DIRNAME = "pairs"
PAIRS_MANIFEST = "manifest.json"
PAIRS_DIR_KEY = "_pairs_dir"
PAIRS_DONE_KEY = "_pairs_done"
MANIFEST_VERSION = 1

#: artifact ``meta`` keys an incremental engine reports (read back by the executor)
META_PAIRS = "pairs"
META_REUSED = "pairs_reused"
META_COMPUTED = "pairs_computed"

#: artifacts whose ``meta["pairs"]`` lists the pair set (upstream → downstream propagation)
_PAIR_ARTIFACTS = ("igrams", "unw")


# ---------------------------------------------------------------------- hash rule


def is_incremental_stage(stage: str) -> bool:
    return stage in INCREMENTAL_STAGES


def is_pair_stage(stage: str) -> bool:
    """The stage keeps per-pair results (``_pairs_dir`` / ``_pairs_done`` contract)."""
    return stage in PAIR_STAGES


def hash_view(stage: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The part of ``params`` that enters the node hash (data keys dropped for incremental
    stages; other stages hash everything, exactly as before)."""
    if not is_incremental_stage(stage):
        return dict(params)
    return {k: v for k, v in params.items() if k not in DATA_KEYS}


def data_view(stage: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The data keys of ``params`` (empty for non-incremental stages)."""
    if not is_incremental_stage(stage):
        return {}
    return {k: params[k] for k in sorted(DATA_KEYS) if k in params}


def identity_inputs(stage: str, inputs: Mapping[str, str]) -> dict[str, str]:
    """Input identities that enter the node hash (data inputs dropped for incremental
    stages)."""
    if not is_incremental_stage(stage):
        return dict(inputs)
    return {k: v for k, v in inputs.items() if k not in DATA_INPUTS}


# ---------------------------------------------------------------------- per-pair identity


def pair_content_hash(*arrays: Any, length: int = 32) -> str:
    """Content hash of one pair's input arrays (dtype, shape and bytes of each array)."""
    h = hashlib.sha256()
    for a in arrays:
        if a is None:
            h.update(b"none\0")
            continue
        arr = np.ascontiguousarray(a)
        h.update(f"{arr.dtype.str}:{arr.shape}\0".encode())
        h.update(arr.tobytes())
    return h.hexdigest()[:length]


def pair_identity(params_hash: str, input_hash: str) -> str:
    """Identity of one pair's result: the stage parameters that influence it + its input."""
    return hash_params({"params": params_hash, "input": input_hash})


def pairs_dir(node_dir: Path) -> Path:
    return Path(node_dir) / PAIRS_DIRNAME


# ---------------------------------------------------------------------- manifest


@dataclass
class PairEntry:
    """One row of ``pairs/manifest.json``."""

    key: str
    hash: str
    path: str
    file_hash: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    computed_at: str | None = None

    def to_dict(self, base: Path | None = None) -> dict[str, Any]:
        path = self.path
        if base is not None:
            try:
                path = Path(self.path).relative_to(base).as_posix()
            except ValueError:
                path = str(self.path)
        return {
            "hash": self.hash,
            "path": path,
            "file_hash": self.file_hash,
            "meta": self.meta,
            "computed_at": self.computed_at,
        }

    @classmethod
    def from_dict(cls, key: str, raw: Mapping[str, Any], base: Path | None = None) -> PairEntry:
        path = Path(str(raw.get("path") or f"{key}.npz"))
        if base is not None and not path.is_absolute():
            path = base / path
        meta = raw.get("meta")
        return cls(
            key=key,
            hash=str(raw.get("hash") or ""),
            path=str(path),
            file_hash=str(raw["file_hash"]) if raw.get("file_hash") else None,
            meta=dict(meta) if isinstance(meta, Mapping) else {},
            computed_at=str(raw["computed_at"]) if raw.get("computed_at") else None,
        )

    def intact(self) -> bool:
        """The result (file or directory) exists and its recorded fast hash still matches.

        An entry without ``file_hash`` cannot be verified and is therefore never reused:
        recomputing one pair is cheaper than assembling a stack from a file nobody checked.
        """
        p = Path(self.path)
        if not p.exists() or self.file_hash is None:
            return False
        try:
            return hash_path(p, fast=True) == self.file_hash
        except OSError:
            return False


class PairCache:
    """Per-pair result cache of one node directory (engine side and executor side).

    * Engines build it with :meth:`from_params` (``None`` when the executor did not ask for
      sub-caching), ask :meth:`lookup` before computing a pair, :meth:`store` what they
      computed and :meth:`save` the manifest at the end; :meth:`summary` gives the artifact
      meta (``pairs``, ``pairs_reused``, ``pairs_computed``).
    * The executor/plan side uses :meth:`load` to read what a node directory already holds
      and :meth:`done_entries` to pass it on as ``_pairs_done``.
    """

    def __init__(
        self,
        directory: Path,
        stage: str | None = None,
        done: Mapping[str, Any] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.stage = stage
        self.done: dict[str, PairEntry] = {}
        for key, raw in (done or {}).items():
            if isinstance(raw, PairEntry):
                self.done[str(key)] = raw
            elif isinstance(raw, Mapping):
                self.done[str(key)] = PairEntry.from_dict(str(key), raw, self.directory)
        self.entries: dict[str, PairEntry] = {}
        self.reused: list[str] = []
        self.computed: list[str] = []
        #: set by :meth:`load` when ``pairs/manifest.json`` existed but was unreadable or
        #: malformed (the exception class name, or ``"InvalidManifest"``); ``done`` is then
        #: empty and the stage recomputes every pair
        self.manifest_error: str | None = None

    # ------------------------------------------------------------ constructors
    @classmethod
    def from_params(cls, params: Mapping[str, Any], stage: str | None = None) -> PairCache | None:
        """``None`` unless the executor passed ``_pairs_dir`` (sub-caching requested)."""
        directory = params.get(PAIRS_DIR_KEY)
        if not directory:
            return None
        done = params.get(PAIRS_DONE_KEY)
        return cls(Path(str(directory)), stage, done if isinstance(done, Mapping) else None)

    @classmethod
    def load(cls, node_dir: Path, stage: str | None = None) -> PairCache:
        """Read ``pairs/manifest.json`` of ``node_dir``.

        A missing manifest is the normal first-run case (empty, silent). One that exists but
        cannot be parsed, or whose ``pairs`` is not a mapping, is discarded the same way but
        flagged in :attr:`manifest_error` so the caller can report the full recompute.
        """
        directory = pairs_dir(node_dir)
        pc = cls(directory, stage)
        p = directory / PAIRS_MANIFEST
        if not p.is_file():
            return pc
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            pc.manifest_error = type(exc).__name__
            return pc
        pairs = raw.get("pairs") if isinstance(raw, dict) else None
        if not isinstance(pairs, dict):
            pc.manifest_error = "InvalidManifest"
            return pc
        if pc.stage is None and isinstance(raw.get("stage"), str):
            pc.stage = str(raw["stage"])
        for key, entry in pairs.items():
            if isinstance(entry, Mapping):
                e = PairEntry.from_dict(str(key), entry, directory)
                if Path(e.path).is_file():
                    pc.done[str(key)] = e
        return pc

    # ------------------------------------------------------------ engine side
    def path_for(self, key: str, suffix: str = ".npz") -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory / f"{key}{suffix}"

    def lookup(self, key: str, identity: str) -> Path | None:
        """Path of a reusable result for ``key`` (same identity, file intact) or ``None``."""
        entry = self.done.get(key)
        if entry is None or entry.hash != identity or not entry.intact():
            return None
        self.entries[key] = entry
        if key not in self.reused:
            self.reused.append(key)
        return Path(entry.path)

    def entry(self, key: str) -> PairEntry | None:
        return self.entries.get(key)

    def discard(self, key: str) -> None:
        """Forget a :meth:`lookup` hit whose file turned out to be unusable.

        The engine then computes the pair again and :meth:`store` re-registers it; the
        stale file is pruned by :meth:`save`. A corrupt cached pair is a cache miss for
        that pair, never a stage failure.
        """
        self.entries.pop(key, None)
        if key in self.reused:
            self.reused.remove(key)

    def store(
        self, key: str, identity: str, path: Path, meta: Mapping[str, Any] | None = None
    ) -> PairEntry:
        """Record a freshly computed result file for ``key``."""
        p = Path(path)
        try:
            file_hash: str | None = hash_path(p, fast=True)
        except OSError:
            file_hash = None
        entry = PairEntry(
            key=key,
            hash=identity,
            path=str(p),
            file_hash=file_hash,
            meta=dict(meta or {}),
            computed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self.entries[key] = entry
        if key not in self.computed:
            self.computed.append(key)
        return entry

    def save(self, prune: bool = True) -> Path:
        """Atomically write the manifest; ``prune`` deletes result files no entry references."""
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": MANIFEST_VERSION,
            "stage": self.stage,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "pairs": {k: e.to_dict(self.directory) for k, e in sorted(self.entries.items())},
        }
        target = self.directory / PAIRS_MANIFEST
        fd, tmp = tempfile.mkstemp(prefix=".manifest-", suffix=".json", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            Path(tmp).replace(target)
        finally:
            if Path(tmp).exists():
                Path(tmp).unlink()
        if prune:
            keep = {Path(e.path).resolve() for e in self.entries.values()} | {target.resolve()}
            for p in self.directory.iterdir():
                if p.is_file() and p.resolve() not in keep and not p.name.startswith("."):
                    p.unlink(missing_ok=True)
        return target

    def summary(self) -> dict[str, Any]:
        """Artifact ``meta`` keys the executor reads back (``pairs_reused`` /
        ``pairs_computed``; the stage sets ``meta["pairs"]`` itself, in stack order)."""
        return {META_REUSED: sorted(self.reused), META_COMPUTED: sorted(self.computed)}

    # ------------------------------------------------------------ executor side
    def done_entries(self) -> dict[str, dict[str, Any]]:
        """``_pairs_done`` payload: absolute paths, ready for the engine."""
        return {k: e.to_dict() for k, e in sorted(self.done.items())}


# ---------------------------------------------------------------------- plan view


def pair_counts(expected: list[str] | None, done: Mapping[str, Any]) -> dict[str, int | None]:
    """``{"expected", "done", "cached", "new"}`` for the plan (``None`` when unknown)."""
    if expected is None:
        return {"expected": None, "done": len(done), "cached": len(done), "new": None}
    exp = set(expected)
    cached = len(exp & set(done))
    return {"expected": len(exp), "done": len(done), "cached": cached, "new": len(exp) - cached}


@dataclass
class PartialCache:
    """What a node directory with the same hash already holds (plan status *incremental*).

    ``manifest_error`` (from :attr:`PairCache.manifest_error`) marks a directory whose
    per-pair manifest was present but unreadable: nothing is reusable (``usable`` is
    ``False``, the node is a plain full recompute) but the plan/run must name the cause.
    """

    node_dir: Path
    record: StageRecord | None
    done: dict[str, PairEntry]
    expected: list[str] | None = None
    manifest_error: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.done)

    @property
    def counts(self) -> dict[str, int | None]:
        return pair_counts(self.expected, self.done)

    def to_extra(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": "partial" if self.usable else "recompute", **self.counts}
        if self.manifest_error:
            out["manifest"] = "unreadable"
            out["manifest_error"] = self.manifest_error
        return out


def manifest_finding(stage: str, error: str, counts: Mapping[str, int | None]) -> Finding:
    """INFO ``PIPELINE-017``: the per-pair manifest of ``stage`` existed but was unreadable,
    so the whole pair set is computed again (this run rebuilds the manifest)."""
    expected = counts.get("expected")
    return Finding(
        rule_id="PIPELINE-017",
        severity="INFO",
        message_key="pipeline.PIPELINE-017.cause",
        fix_key="pipeline.PIPELINE-017.fix",
        params={
            "stage": stage,
            "error": error,
            "n_pairs": "?" if expected is None else int(expected),
        },
        evidence={"manifest_error": error, **dict(counts)},
        scope=stage,
    )


def _stack_pairs(path: Path) -> list[str] | None:
    """Pair keys of a precheck ``stack.json`` (``StackCandidate.pairs``).

    Parsed leniently: only the ``pairs`` list is read (``reference``/``secondary`` ISO dates,
    or ready-made ``YYYYMMDD_YYYYMMDD`` keys), so a stack written by an older schema still
    yields its pair set.
    # source: src/wintersar/io/schemas.py::Pair.key (``f"{reference:%Y%m%d}_{secondary:%Y%m%d}"``)
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pairs = raw.get("pairs") if isinstance(raw, dict) else None
    if not isinstance(pairs, list):
        return None
    keys: list[str] = []
    for item in pairs:
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, Mapping) and item.get("reference") and item.get("secondary"):
            ref, sec = (
                str(item["reference"]).replace("-", ""),
                str(item["secondary"]).replace("-", ""),
            )
            keys.append(f"{ref[:8]}_{sec[:8]}")
    return keys or None


def expected_pairs(
    engine: object | None, stage: str, params: Mapping[str, Any], available: Artifacts
) -> list[str] | None:
    """Pair set the stage will produce/consume, when it can be known before running.

    1. ``engine.expected_pairs(stage, params, available)`` (duck-typed; the fake engine
       derives it from ``n_dates``),
    2. ``meta["pairs"]`` of an available ``igrams``/``unw`` input artifact,
    3. ``StackCandidate.pairs`` of an available precheck ``stack`` artifact.
    """
    hook: Callable[..., Any] | None = getattr(engine, "expected_pairs", None)
    if callable(hook):
        try:
            got = hook(stage, params, available)
        except Exception:
            got = None
        if isinstance(got, list | tuple) and got:
            return [str(k) for k in got]
    for name in _PAIR_ARTIFACTS:
        art = available.items.get(name)
        pairs = art.meta.get(META_PAIRS) if art is not None else None
        if isinstance(pairs, list | tuple) and pairs:
            return [str(k) for k in pairs]
    stack = available.items.get("stack")
    if stack is not None:
        return _stack_pairs(Path(stack.path))
    return None
