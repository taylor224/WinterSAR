"""Stable hashing helpers for the DAG cache (PERF-03) and artifact identity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime | date):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, set | frozenset):
        return sorted(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    msg = f"unhashable object of type {type(o).__name__}"
    raise TypeError(msg)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON (sorted keys, no whitespace) used for hashing parameters."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), default=_json_default, ensure_ascii=False
    )


def hash_params(obj: Any, length: int = 16) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:length]


def hash_file(path: Path, length: int = 64) -> str:
    """SHA-256 of a file's content."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()[:length]


def hash_file_fast(path: Path, length: int = 64) -> str:
    """Cheap identity for large artifacts: size + mtime_ns + first/last MiB.

    Used for multi-GB rasters where a full content hash would dominate stage time. The
    manifest records which method was used.
    """
    st = path.stat()
    h = hashlib.sha256()
    h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    with path.open("rb") as fh:
        h.update(fh.read(_CHUNK))
        if st.st_size > 2 * _CHUNK:
            fh.seek(-_CHUNK, 2)
            h.update(fh.read(_CHUNK))
    return h.hexdigest()[:length]


def hash_tree(root: Path, length: int = 64, fast: bool = True) -> str:
    """Hash a directory: relative paths + per-file hashes, sorted."""
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel = p.relative_to(root).as_posix()
        fh = hash_file_fast(p) if fast else hash_file(p)
        h.update(f"{rel}\0{fh}\n".encode())
    return h.hexdigest()[:length]


def hash_path(path: Path, fast: bool = True, length: int = 64) -> str:
    if path.is_dir():
        return hash_tree(path, length=length, fast=fast)
    return (hash_file_fast if fast else hash_file)(path, length=length)


def combine_hashes(parts: Iterable[str], length: int = 16) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:length]
