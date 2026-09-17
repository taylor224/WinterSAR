"""Work-directory cache: ``work/<stage>/<node_hash>/{manifest.json, out/, logs/}``
(plan §5.3, PERF-03; layout decided in ADR-0032).

* ``manifest.json`` is a :class:`wintersar.io.schemas.StageRecord`. Output artifacts are
  stored in ``record.extra["artifacts"]`` (name -> ``Artifact`` dump, including the content
  hash) so that downstream node hashes can be resolved without touching the data.
* Artifact identity uses :func:`wintersar.util.hashing.hash_path` with ``fast=True``
  (size + mtime + head/tail) — multi-GB rasters must not be re-read on every plan
  (ADR-0031). The method is recorded in ``artifact.meta["hash_method"]``.
* :func:`gc` keeps the ``keep_latest`` most recent entries per stage and, with
  ``max_bytes``, additionally evicts the oldest of those until the work directory fits the
  byte budget (plan §6.2 PERF-03: "캐시 크기 제한과 ``wintersar cache gc``").
* :func:`node_lock` serialises two processes that resolve the *same* node hash on the same
  work directory: without it the second ``prepare_node_dir(clean=True)`` deletes the first
  run's in-flight ``out/`` (ADR-0032 has no ownership rule; ``engines/aux_cache.py`` locks
  the aux-data cache the same way).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from wintersar.io.schemas import Artifact, Artifacts, StageRecord
from wintersar.pipeline.stages import STAGE_ORDER
from wintersar.util.hashing import hash_path

MANIFEST_NAME = "manifest.json"
OUT_DIRNAME = "out"
LOGS_DIRNAME = "logs"
LOCK_NAME = ".lock"
ARTIFACTS_KEY = "artifacts"
HASH_METHOD_KEY = "hash_method"
ORPHAN_STATUS = "orphan"

try:  # POSIX advisory locks: released by the kernel when the process dies (no stale locks)
    import fcntl

    #: annotated so that ``warn_unreachable`` does not fold the Windows branches away
    HAS_FLOCK: bool = True
except ImportError:  # pragma: no cover - non-POSIX platform
    fcntl = None  # type: ignore[assignment]
    HAS_FLOCK = False


class NodeBusyError(RuntimeError):
    """Another process is executing this node directory."""


# ---------------------------------------------------------------------- layout


def stage_root(workdir: Path, stage: str) -> Path:
    return Path(workdir) / stage


def stage_dir(workdir: Path, stage: str, node_hash: str) -> Path:
    return stage_root(workdir, stage) / node_hash


def out_dir(node_dir: Path) -> Path:
    return node_dir / OUT_DIRNAME


def log_dir(node_dir: Path) -> Path:
    return node_dir / LOGS_DIRNAME


def manifest_path(node_dir: Path) -> Path:
    return node_dir / MANIFEST_NAME


def prepare_node_dir(node_dir: Path, clean: bool = True) -> tuple[Path, Path]:
    """Create ``out/`` and ``logs/`` (emptying them first when ``clean``)."""
    for sub in (out_dir(node_dir), log_dir(node_dir)):
        if clean and sub.exists():
            shutil.rmtree(sub)
        sub.mkdir(parents=True, exist_ok=True)
    return out_dir(node_dir), log_dir(node_dir)


# ---------------------------------------------------------------------- locking


def lock_path(node_dir: Path) -> Path:
    return node_dir / LOCK_NAME


@contextmanager
def node_lock(node_dir: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``node_dir`` for the duration of the block.

    Raises :class:`NodeBusyError` when another process holds it. On platforms without
    ``fcntl`` (Windows) this is a no-op, i.e. exactly today's behaviour.
    """
    node_dir.mkdir(parents=True, exist_ok=True)
    if not HAS_FLOCK:  # pragma: no cover - non-POSIX platform
        yield
        return
    fd = os.open(lock_path(node_dir), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            msg = f"node directory is locked by another run: {node_dir}"
            raise NodeBusyError(msg) from exc
        try:
            os.truncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def is_locked(node_dir: Path) -> bool:
    """True when another process currently holds the node lock (never blocks)."""
    if not HAS_FLOCK:  # pragma: no cover - non-POSIX platform
        return False
    p = lock_path(node_dir)
    if not p.is_file():
        return False
    try:
        fd = os.open(p, os.O_RDWR)
    except OSError:  # pragma: no cover - unreadable lock file
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# ---------------------------------------------------------------------- manifests


def write_record(record: StageRecord, node_dir: Path) -> Path:
    """Atomically write ``manifest.json`` (tmp file + ``os.replace``)."""
    node_dir.mkdir(parents=True, exist_ok=True)
    target = manifest_path(node_dir)
    payload = record.model_dump(mode="json")
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", suffix=".json", dir=node_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        Path(tmp).replace(target)
    finally:
        if Path(tmp).exists():
            Path(tmp).unlink()
    return target


def load_record(path: Path) -> StageRecord | None:
    """Read a manifest; ``None`` when missing or unparsable (treated as a cache miss)."""
    p = Path(path)
    if p.is_dir():
        p = manifest_path(p)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return StageRecord.model_validate(data)
    except (OSError, ValueError):
        return None


def record_artifacts(record: StageRecord) -> Artifacts:
    """Rebuild the ``Artifacts`` a stage produced from its manifest."""
    raw = record.extra.get(ARTIFACTS_KEY)
    arts = Artifacts()
    if isinstance(raw, dict):
        for name, dump in raw.items():
            if isinstance(dump, dict):
                arts.add(Artifact.model_validate({"name": name, **dump}))
        return arts
    # fallback: paths only (older manifests)
    for name, path in record.outputs.items():
        arts.add(Artifact(name=name, path=Path(path)))
    return arts


def artifacts_to_extra(artifacts: Artifacts) -> dict[str, Any]:
    return {name: a.model_dump(mode="json") for name, a in artifacts.items.items()}


# ---------------------------------------------------------------------- hashing


def hash_artifact(artifact: Artifact, fast: bool = True) -> Artifact:
    """Return a copy of ``artifact`` with ``sha256`` filled from its path."""
    path = Path(artifact.path)
    if not path.exists():
        msg = f"artifact {artifact.name!r} path does not exist: {path}"
        raise FileNotFoundError(msg)
    digest = hash_path(path, fast=fast)
    meta = {**artifact.meta, HASH_METHOD_KEY: "fast" if fast else "sha256"}
    return artifact.model_copy(update={"sha256": digest, "meta": meta, "path": path.resolve()})


def hash_artifacts(artifacts: Artifacts, fast: bool = True) -> Artifacts:
    out = Artifacts()
    for a in artifacts.items.values():
        out.add(hash_artifact(a, fast=fast))
    return out


def artifacts_unchanged(artifacts: Artifacts) -> bool:
    """True when every artifact exists and its current fast hash equals the recorded one."""
    for a in artifacts.items.values():
        path = Path(a.path)
        if not path.exists():
            return False
        if a.sha256 is None:
            continue
        fast = a.meta.get(HASH_METHOD_KEY, "fast") == "fast"
        if hash_path(path, fast=fast) != a.sha256:
            return False
    return True


# ---------------------------------------------------------------------- lookup


def find_cached(
    workdir: Path, stage: str, node_hash: str, verify: bool = True
) -> StageRecord | None:
    """The ``ok`` manifest for ``(stage, node_hash)`` whose outputs are still intact."""
    record = load_record(stage_dir(workdir, stage, node_hash))
    if record is None or record.status != "ok" or record.node_hash != node_hash:
        return None
    if verify and not artifacts_unchanged(record_artifacts(record)):
        return None
    return record


@dataclass(frozen=True)
class CacheEntry:
    """One ``work/<stage>/<hash>/`` directory.

    ``record`` is ``None`` for an *orphan*: a node directory whose manifest is missing or
    unparsable (a run killed between :func:`prepare_node_dir` and the first
    :func:`write_record`). Orphans are listed and collected so their ``out/`` cannot leak.
    """

    stage: str
    node_hash: str
    path: Path
    record: StageRecord | None
    size_bytes: int

    @property
    def orphan(self) -> bool:
        return self.record is None

    @property
    def status(self) -> str:
        return ORPHAN_STATUS if self.record is None else self.record.status

    @property
    def finished_at(self) -> datetime | None:
        if self.record is None:
            return None
        return self.record.finished_at or self.record.started_at

    def sort_key(self) -> float:
        ts = self.finished_at
        if ts is not None:
            return ts.timestamp()
        for p in (manifest_path(self.path), self.path):
            try:
                return p.stat().st_mtime
            except OSError:
                continue
        return 0.0  # pragma: no cover - the directory was just listed


def dir_size(path: Path) -> int:
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def list_records(workdir: Path, stage: str | None = None) -> list[CacheEntry]:
    """All node directories under ``workdir`` (newest first), optionally for one stage.

    Directories without a readable manifest are returned as orphans (``record is None``).
    """
    stages = [stage] if stage else STAGE_ORDER
    entries: list[CacheEntry] = []
    for s in stages:
        root = stage_root(workdir, s)
        if not root.is_dir():
            continue
        for node_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            entries.append(
                CacheEntry(
                    stage=s,
                    node_hash=node_dir.name,
                    path=node_dir,
                    record=load_record(node_dir),
                    size_bytes=dir_size(node_dir),
                )
            )
    entries.sort(key=lambda e: (-e.sort_key(), e.stage, e.node_hash))
    return entries


def latest_record(workdir: Path, stage: str, status: str = "ok") -> StageRecord | None:
    """Most recent manifest of ``stage`` with ``status`` whose outputs are intact."""
    for e in list_records(workdir, stage):
        if e.record is None:
            continue
        if e.record.status == status and artifacts_unchanged(record_artifacts(e.record)):
            return e.record
    return None


def cache_size(workdir: Path) -> dict[str, int]:
    """Bytes per stage (size accounting for ``cache ls``)."""
    sizes: dict[str, int] = {}
    for e in list_records(workdir):
        sizes[e.stage] = sizes.get(e.stage, 0) + e.size_bytes
    return sizes


# ---------------------------------------------------------------------- gc


@dataclass
class GcReport:
    removed: list[CacheEntry]
    kept: list[CacheEntry]
    dry_run: bool

    @property
    def freed_bytes(self) -> int:
        return sum(e.size_bytes for e in self.removed)


def gc(
    workdir: Path,
    keep_latest: int = 3,
    dry_run: bool = False,
    stages: list[str] | None = None,
    max_bytes: int | None = None,
) -> GcReport:
    """Keep the ``keep_latest`` newest entries of every stage and delete the rest.

    Entries are ordered by ``finished_at`` (fallback: manifest/directory mtime). Failed
    entries count like any other so that a failed run's logs survive until they age out;
    orphans (no readable manifest) never occupy a ``keep_latest`` slot. ``max_bytes`` caps
    the total size afterwards by evicting the oldest survivors first (plan §6.2 PERF-03).
    Entries locked by a running pipeline are always kept.
    """
    if keep_latest < 0:
        msg = "keep_latest must be >= 0"
        raise ValueError(msg)
    if max_bytes is not None and max_bytes < 0:
        msg = "max_bytes must be >= 0"
        raise ValueError(msg)
    removed: list[CacheEntry] = []
    kept: list[CacheEntry] = []
    for s in stages or STAGE_ORDER:
        entries = [e for e in list_records(workdir, s) if not is_locked(e.path)]  # newest first
        survivors = [e for e in entries if not e.orphan][:keep_latest]
        keys = {id(e) for e in survivors}
        kept.extend(survivors)
        removed.extend(e for e in entries if id(e) not in keys)
    if max_bytes is not None:
        kept.sort(key=lambda e: -e.sort_key())  # newest first, across stages
        total = sum(e.size_bytes for e in kept)
        while kept and total > max_bytes:
            victim = kept.pop()
            total -= victim.size_bytes
            removed.append(victim)
    if not dry_run:
        for e in removed:
            shutil.rmtree(e.path, ignore_errors=True)
    return GcReport(removed=removed, kept=kept, dry_run=dry_run)


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"  # pragma: no cover
