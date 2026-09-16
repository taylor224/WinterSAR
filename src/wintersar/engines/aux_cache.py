"""Content-addressed cache for auxiliary data (orbits, weather models, DEM) — PERF-02.

Every auxiliary download goes through :func:`cached_fetch`: the *key* (a JSON-serialisable
mapping such as ``{"kind": "orbit", "mission": "S1A", "date": "2024-01-01"}``) is hashed and
the fetched files live under ``<cache_dir>/<namespace>/<hash>/``. A ``.complete`` marker is
written only after the fetcher returned successfully, so a crashed download never counts as
a hit and a re-run of an already-fetched key makes **zero** network calls (tested with a
counting fetcher). Set ``WINTERSAR_OFFLINE=1`` (or ``offline=True``) to turn a cache miss
into :class:`CacheMissOfflineError` instead of a download.

Orbits are fetched through ``sentineleof`` when it is importable (it is *not* a dependency,
ADR-0001 / ADR-0022); otherwise an ``ENV-006`` finding is returned. Weather models (ERA5 via
PyAPS) are downloaded by MintPy itself into ``mintpy.troposphericDelay.weatherDir``; the cache
only provides the stable shared directory (:func:`weather_dir`) so that repeated runs reuse
the same files.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from wintersar.io.schemas import Finding
from wintersar.util.hashing import hash_params
from wintersar.util.masking import mask_mapping, mask_text

COMPLETE_MARKER = ".complete"
KEY_FILE = "key.json"
ORBIT_TYPES: tuple[str, ...] = ("precise", "restituted")
_LOCK_POLL_S = 0.1


class CacheFetchError(RuntimeError):
    """The fetcher failed or produced no files; nothing was cached."""


class CacheMissOfflineError(CacheFetchError):
    """Offline mode is active and the key is not in the cache."""


class CacheLockTimeoutError(CacheFetchError):
    """Another process holds the entry lock for longer than ``lock_timeout_s``."""


# ---------------------------------------------------------------------- keys / paths


def cache_key(key: Mapping[str, Any]) -> str:
    """Stable content-address of ``key`` (sorted canonical JSON, sha256, 32 hex chars)."""
    return hash_params(dict(key), length=32)


def cache_entry_dir(cache_dir: Path, key: Mapping[str, Any], namespace: str = "generic") -> Path:
    return Path(cache_dir) / namespace / cache_key(key)


def is_cached(cache_dir: Path, key: Mapping[str, Any], namespace: str = "generic") -> bool:
    return (cache_entry_dir(cache_dir, key, namespace) / COMPLETE_MARKER).exists()


def is_offline(offline: bool | None = None) -> bool:
    if offline is not None:
        return offline
    return os.environ.get("WINTERSAR_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------- locking


def _acquire_lock(lock: Path, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > 2 * timeout_s:  # stale lock from a crashed process
                lock.unlink(missing_ok=True)
                continue
            if time.monotonic() > deadline:
                msg = f"timed out waiting for cache lock {lock.name}"
                raise CacheLockTimeoutError(msg) from None
            time.sleep(_LOCK_POLL_S)
        else:
            os.write(fd, f"{os.getpid()} {_now_iso()}\n".encode())
            os.close(fd)
            return


def _release_lock(lock: Path) -> None:
    lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------- core


def cached_fetch(
    cache_dir: Path,
    key: Mapping[str, Any],
    fetcher: Callable[[Path], None],
    *,
    namespace: str = "generic",
    offline: bool | None = None,
    lock_timeout_s: float = 3600.0,
) -> Path:
    """Return the cache entry directory for ``key``, calling ``fetcher(tmp_dir)`` on a miss.

    ``fetcher`` must write its files into the directory it receives; the directory is renamed
    into place atomically after success and a ``.complete`` marker plus ``key.json`` are
    written. On any exception the temporary directory is removed and the error re-raised
    (wrapped in :class:`CacheFetchError` only when the fetcher produced no files).
    """
    cache_dir = Path(cache_dir)
    entry = cache_entry_dir(cache_dir, key, namespace)
    if (entry / COMPLETE_MARKER).exists():
        return entry
    if is_offline(offline):
        msg = f"offline mode: cache miss for {namespace}/{cache_key(key)}"
        raise CacheMissOfflineError(msg)

    entry.parent.mkdir(parents=True, exist_ok=True)
    lock = entry.with_name(entry.name + ".lock")
    _acquire_lock(lock, lock_timeout_s)
    try:
        if (entry / COMPLETE_MARKER).exists():  # another process filled it while we waited
            return entry
        tmp = entry.with_name(f"{entry.name}.tmp-{uuid.uuid4().hex[:8]}")
        tmp.mkdir()
        try:
            fetcher(tmp)
            files = sorted(p for p in tmp.rglob("*") if p.is_file())
            if not files:
                msg = f"fetcher produced no files for {namespace}/{cache_key(key)}"
                raise CacheFetchError(msg)
            manifest = {
                "key": mask_mapping(dict(key)),
                "namespace": namespace,
                "created": _now_iso(),
                "files": [p.relative_to(tmp).as_posix() for p in files],
                "bytes": int(sum(p.stat().st_size for p in files)),
            }
            (tmp / KEY_FILE).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (tmp / COMPLETE_MARKER).write_text(_now_iso() + "\n", encoding="utf-8")
            if entry.exists():  # stale partial entry without marker
                shutil.rmtree(entry, ignore_errors=True)
            tmp.rename(entry)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    finally:
        _release_lock(lock)
    return entry


def entry_files(entry: Path) -> list[Path]:
    """Data files of a cache entry (excluding the marker and key manifest)."""
    return sorted(
        p
        for p in Path(entry).rglob("*")
        if p.is_file() and p.name not in {COMPLETE_MARKER, KEY_FILE}
    )


# ---------------------------------------------------------------------- orbits (sentineleof)


def sentineleof_available() -> bool:
    return importlib.util.find_spec("eof") is not None


def sentineleof_version() -> str | None:
    try:
        return importlib.metadata.version("sentineleof")
    except importlib.metadata.PackageNotFoundError:
        return None


def orbit_key(mission: str, day: date, orbit_type: str = "precise") -> dict[str, str]:
    if orbit_type not in ORBIT_TYPES:
        msg = f"orbit_type must be one of {ORBIT_TYPES}, got {orbit_type!r}"
        raise ValueError(msg)
    return {
        "kind": "orbit",
        "mission": mission.upper(),
        "date": day.isoformat(),
        "orbit_type": orbit_type,
    }


def sentineleof_fetcher(
    mission: str, acquisition_time: datetime, orbit_type: str = "precise"
) -> Callable[[Path], None]:
    """Build a fetcher that downloads one EOF file with ``sentineleof`` into the given dir.

    # source: https://github.com/scottstanie/sentineleof/blob/master/eof/download.py
    #   download_eofs(orbit_dts=None, missions=None, sentinel_file=None, save_dir=".",
    #                 orbit_type="precise", force_asf=False, ...) -> list[Path]
    #   "missions ... Must be same length as orbit_dts."
    # CLI fallback (eof/cli.py): eof --date YYYY-MM-DD --mission S1A --save-dir DIR
    #                            --orbit-type precise|restituted
    """

    def _fetch(dest: Path) -> None:
        if importlib.util.find_spec("eof") is not None:
            download = importlib.import_module("eof.download")
            download.download_eofs(
                orbit_dts=[acquisition_time],
                missions=[mission.upper()],
                save_dir=str(dest),
                orbit_type=orbit_type,
            )
            return
        exe = shutil.which("eof")
        if exe is None:
            msg = "sentineleof is not installed (neither the 'eof' module nor the CLI)"
            raise CacheFetchError(msg)
        cmd = [
            exe,
            "--date",
            acquisition_time.date().isoformat(),
            "--mission",
            mission.upper(),
            "--save-dir",
            str(dest),
            "--orbit-type",
            orbit_type,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            msg = f"eof CLI failed ({proc.returncode}): {mask_text(proc.stderr[-500:])}"
            raise CacheFetchError(msg)

    return _fetch


@dataclass
class OrbitFetchResult:
    paths: dict[str, Path] = field(default_factory=dict)  # "<mission>_<date>" -> EOF file
    findings: list[Finding] = field(default_factory=list)
    n_hits: int = 0
    n_fetched: int = 0

    @property
    def ok(self) -> bool:
        return not any(f.is_fail for f in self.findings)


def fetch_orbits(
    cache_dir: Path,
    acquisitions: Iterable[tuple[str, datetime]],
    *,
    orbit_type: str = "precise",
    fetcher_factory: Callable[[str, datetime, str], Callable[[Path], None]] | None = None,
    offline: bool | None = None,
) -> OrbitFetchResult:
    """Fetch (or reuse) one orbit file per ``(mission, acquisition_time)``.

    Without ``sentineleof`` (and no injected ``fetcher_factory``) every acquisition yields an
    ``ENV-006`` finding (WARN) and no download is attempted: precise orbits are optional
    for the HyP3 path (HyP3 fetches its own) and only required by the local ISCE2 path.
    """
    result = OrbitFetchResult()
    use_factory = fetcher_factory
    if use_factory is None:
        if not sentineleof_available() and shutil.which("eof") is None:
            result.findings.append(
                Finding(
                    rule_id="ENV-006",
                    severity="WARN",
                    message_key="env.ENV-006.cause",
                    fix_key="env.ENV-006.fix",
                    params={
                        "package": "sentineleof",
                        "install_hint": "pip install 'wintersar[orbits]'  (sentineleof, MIT)",
                    },
                    evidence={"module": "eof", "cli": "eof"},
                    scope="orbits",
                )
            )
            return result
        use_factory = sentineleof_fetcher

    for mission, when in acquisitions:
        key = orbit_key(mission, when.date(), orbit_type)
        label = f"{key['mission']}_{key['date']}"
        hit = is_cached(cache_dir, key, namespace="orbits")
        try:
            entry = cached_fetch(
                cache_dir,
                key,
                use_factory(mission, when, orbit_type),
                namespace="orbits",
                offline=offline,
            )
        except CacheFetchError as e:
            result.findings.append(
                Finding(
                    rule_id="AUX-001",
                    severity="WARN",
                    message_key="engines.aux.AUX-001.cause",
                    fix_key="engines.aux.AUX-001.fix",
                    params={
                        "mission": key["mission"],
                        "date": key["date"],
                        "error": mask_text(str(e)),
                    },
                    evidence={"key": key},
                    scope="orbits",
                )
            )
            continue
        files = [p for p in entry_files(entry) if p.suffix.upper() == ".EOF"] or entry_files(entry)
        if files:
            result.paths[label] = files[0]
        if hit:
            result.n_hits += 1
        else:
            result.n_fetched += 1
    return result


# ---------------------------------------------------------------------- weather / generic dirs


def weather_dir(cache_dir: Path, model: str = "ERA5") -> Path:
    """Stable directory for MintPy/PyAPS weather files (``mintpy.troposphericDelay.weatherDir``).

    # source: smallbaselineApp.cfg lines 234-237: MintPy "will look for the GAM files in the
    #   directory before downloading a new one" -> a fixed shared dir is the PERF-02 cache.
    """
    d = Path(cache_dir) / "weather" / model.upper()
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_stats(cache_dir: Path) -> dict[str, Any]:
    cache_dir = Path(cache_dir)
    stats: dict[str, Any] = {"path": mask_text(str(cache_dir)), "namespaces": {}, "bytes": 0}
    if not cache_dir.exists():
        return stats
    for ns in sorted(p for p in cache_dir.iterdir() if p.is_dir()):
        entries = [e for e in ns.iterdir() if e.is_dir() and (e / COMPLETE_MARKER).exists()]
        nbytes = sum(p.stat().st_size for e in entries for p in entry_files(e))
        stats["namespaces"][ns.name] = {"entries": len(entries), "bytes": int(nbytes)}
        stats["bytes"] += int(nbytes)
    return stats


def prune(cache_dir: Path, max_bytes: int) -> list[Path]:
    """Remove least-recently-completed entries until the data files total <= ``max_bytes``."""
    cache_dir = Path(cache_dir)
    entries: list[tuple[float, int, Path]] = []
    for marker in cache_dir.glob(f"*/*/{COMPLETE_MARKER}"):
        entry = marker.parent
        size = sum(p.stat().st_size for p in entry_files(entry))
        entries.append((marker.stat().st_mtime, size, entry))
    total = sum(s for _, s, _ in entries)
    removed: list[Path] = []
    for _, size, entry in sorted(entries):
        if total <= max_bytes:
            break
        shutil.rmtree(entry, ignore_errors=True)
        total -= size
        removed.append(entry)
    return removed
