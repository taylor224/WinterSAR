"""The unwrap stage's ``igrams`` input: ``.npz`` interchange **and** engine directories.

``run_unwrap`` receives whatever the upstream ``interferogram``/``multilook`` stage produced:

* the fake engine and ``wintersar unwrap run`` hand over an ``igrams.npz``
  (:mod:`wintersar.io.igrams`, ``kind="npz"``),
* ``isce2_topsstack`` hands over ``merged/interferograms`` as a **directory**
  (``kind="dir"``, ``meta["format"] == "isce2_flat_binary"``),
* ``hyp3`` hands over its product directory (``meta["format"] == "hyp3_geotiff"``).

:func:`load_igrams` turns all three into one :class:`~wintersar.io.igrams.IgramStack` and
:func:`write_worker_stack` writes the ``.npz`` the worker processes memory-map per
interferogram (``_unwrap_one`` re-opens the file instead of pickling the whole stack).
Anything unreadable raises :class:`UnwrapInputError`, which ``run_unwrap`` turns into a
``UNW-005`` finding instead of letting a raw ``OSError`` escape the stage.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from wintersar.io.igrams import IgramStack, load_igram_stack
from wintersar.io.schemas import Artifact, Finding

__all__ = [
    "HYP3_FORMAT",
    "ISCE_FORMAT",
    "NPZ_FORMAT",
    "UnwrapInputError",
    "load_igrams",
    "read_isce_igram_dir",
    "write_worker_stack",
]

NPZ_FORMAT = "npz"
ISCE_FORMAT = "isce2_flat_binary"  # source: engines/isce2_topsstack.py Artifact meta
HYP3_FORMAT = "hyp3_geotiff"  # source: engines/hyp3.py Artifact meta

#: ``merged/interferograms/<YYYYMMDD_YYYYMMDD>/`` (source: engines/isce2_topsstack.merged_pairs)
_PAIR_RE = re.compile(r"\d{8}_\d{8}")
# source: engines/isce2_topsstack.MINTPY_LOAD_PATTERNS (MintPy docs/dir_structure.md, ISCE-2
# topsStack): corFile ``merged/interferograms/*/filt_*.cor``. The wrapped interferogram is the
# filtered one written next to it by topsStack FilterAndCoherence.runFilter (``filt_fine.int``).
# source: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/FilterAndCoherence.py
_ISCE_WRAPPED_GLOBS = ("filt_*.int", "*.int")
_ISCE_COHERENCE_GLOBS = ("filt_*.cor", "*.cor")
#: keys of ``igrams_manifest.json`` ``interferograms[i]["files"]`` (isce2_topsstack)
_MANIFEST_WRAPPED_KEYS = ("filt_int", "int")
_MANIFEST_COHERENCE_KEYS = ("cor",)


class UnwrapInputError(ValueError):
    """The ``igrams`` artifact cannot be read as an interferogram stack (``UNW-005``).

    ``params`` are the ``UNW-005`` finding parameters (``path``, ``format``, ``detail``);
    the class derives from :class:`ValueError` so that callers which already handle stage
    parameter errors keep working.
    """

    rule_id = "UNW-005"

    def __init__(self, detail: str, path: Path | str, fmt: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.params: dict[str, Any] = {"path": str(path), "format": fmt, "detail": detail}
        #: filled in by :func:`wintersar.unwrap.api.run_unwrap` (one ``UNW-005``)
        self.findings: list[Finding] = []


# ---------------------------------------------------------------------------- ISCE2


def _dates_of(pairs: list[str]) -> list[date]:
    out: set[date] = set()
    for key in pairs:
        for token in key.split("_")[:2]:
            out.add(date.fromisoformat(f"{token[:4]}-{token[4:6]}-{token[6:8]}"))
    return sorted(out)


def _first_glob(pair_dir: Path, globs: tuple[str, ...]) -> Path | None:
    for pattern in globs:
        hits = sorted(p for p in pair_dir.glob(pattern) if p.is_file())
        if hits:
            return hits[0]
    return None


def _manifest_files(manifest_path: Path) -> dict[str, dict[str, Path]]:
    """``{pair: {"wrapped": path, "coherence": path}}`` from ``igrams_manifest.json``."""
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, Path]] = {}
    for entry in raw.get("interferograms") or []:
        files = entry.get("files") or {}
        picked: dict[str, Path] = {}
        for name, keys in (
            ("wrapped", _MANIFEST_WRAPPED_KEYS),
            ("coherence", _MANIFEST_COHERENCE_KEYS),
        ):
            for key in keys:
                info = files.get(key) or {}
                if info.get("exists") and info.get("path"):
                    picked[name] = Path(str(info["path"]))
                    break
        if len(picked) == 2:
            out[str(entry.get("pair"))] = picked
    return out


def _read_2d(path: Path, *, coherence: bool = False) -> NDArray[Any]:
    """One ISCE raster as a 2-D array (last band for the rare multi-band ``.cor``)."""
    from wintersar.io.formats import read_isce_raster

    arr, _meta = read_isce_raster(path)
    if arr.ndim == 3:
        # topsStack writes a single-band FLOAT coherence (FilterAndCoherence.estCoherence:
        # ``phsigImage.bands = 1``); other workflows ship (amplitude, coherence) rasters, so
        # take the last band. See docs/open-questions.md (multi-band .cor).
        arr = arr[-1] if coherence else arr[0]
    return np.asarray(arr)


def read_isce_igram_dir(path: Path, manifest: Path | None = None) -> IgramStack:
    """Read an ISCE2 topsStack ``merged/interferograms`` directory into an ``IgramStack``.

    ``manifest`` is the ``igrams_manifest.json`` written by the adapter (its per-pair file
    listing wins); without it the pair directories are globbed for ``filt_*.int`` /
    ``filt_*.cor``. The wrapped phase is ``angle`` of the filtered interferogram.
    """
    by_pair = _manifest_files(manifest) if manifest and manifest.exists() else {}
    names = sorted(
        {d.name for d in path.iterdir() if d.is_dir() and _PAIR_RE.fullmatch(d.name)}
        | {k for k in by_pair if _PAIR_RE.fullmatch(k)}
    )
    if not names:
        raise UnwrapInputError("no <YYYYMMDD_YYYYMMDD> interferogram directory", path, ISCE_FORMAT)
    pairs: list[str] = []
    wrapped_l: list[NDArray[np.float32]] = []
    coh_l: list[NDArray[np.float32]] = []
    shape: tuple[int, int] | None = None
    for pair in names:
        files = by_pair.get(pair) or {}
        pair_dir = path / pair
        w_path = files.get("wrapped") or _first_glob(pair_dir, _ISCE_WRAPPED_GLOBS)
        c_path = files.get("coherence") or _first_glob(pair_dir, _ISCE_COHERENCE_GLOBS)
        if w_path is None or c_path is None:
            raise UnwrapInputError(
                f"{pair}: no {_ISCE_WRAPPED_GLOBS[0]} / {_ISCE_COHERENCE_GLOBS[0]}",
                path,
                ISCE_FORMAT,
            )
        try:
            igram = _read_2d(w_path)
            coh = _read_2d(c_path, coherence=True)
        except (OSError, ValueError) as e:
            raise UnwrapInputError(f"{pair}: {e}", path, ISCE_FORMAT) from e
        phase = np.angle(igram) if np.iscomplexobj(igram) else igram
        wrapped = np.asarray(phase, dtype=np.float32)
        if shape is None:
            shape = (int(wrapped.shape[0]), int(wrapped.shape[1]))
        if wrapped.shape != shape or coh.shape != shape:
            raise UnwrapInputError(
                f"{pair}: shape {wrapped.shape}/{coh.shape} != {shape}", path, ISCE_FORMAT
            )
        pairs.append(pair)
        wrapped_l.append(wrapped)
        coh_l.append(np.asarray(coh, dtype=np.float32))
    return IgramStack(
        wrapped=np.stack(wrapped_l),
        coherence=np.stack(coh_l),
        pairs=pairs,
        dates=_dates_of(pairs),
        attrs={"source": str(path), "format": ISCE_FORMAT},
    )


# ---------------------------------------------------------------------------- dispatch


def _artifact_format(igrams: Artifact) -> str:
    fmt = str(igrams.meta.get("format") or "").strip()
    if fmt:
        return fmt
    return NPZ_FORMAT if Path(igrams.path).is_file() else str(igrams.kind or "dir")


def load_igrams(igrams: Artifact) -> tuple[IgramStack, str]:
    """Load the stage input artifact; returns ``(stack, format)``.

    Raises :class:`UnwrapInputError` when the path is missing or its format has no reader
    (the caller reports ``UNW-005`` rather than leaking ``FileNotFoundError`` /
    ``IsADirectoryError``).
    """
    path = Path(igrams.path)
    fmt = _artifact_format(igrams)
    if not path.exists():
        raise UnwrapInputError("interferogram stack not found", path, fmt)
    if fmt == ISCE_FORMAT:
        manifest = igrams.meta.get("manifest")
        return read_isce_igram_dir(path, Path(str(manifest)) if manifest else None), fmt
    if fmt == HYP3_FORMAT:
        from wintersar.io.formats import Hyp3FormatError, read_hyp3_product_dir

        try:
            return read_hyp3_product_dir(path), fmt
        except (Hyp3FormatError, OSError, ValueError, IndexError) as e:
            raise UnwrapInputError(str(e), path, fmt) from e
    if path.is_dir():
        raise UnwrapInputError("no reader for this directory format", path, fmt)
    try:
        return load_igram_stack(path), NPZ_FORMAT
    except (OSError, ValueError, KeyError) as e:
        raise UnwrapInputError(f"not a wintersar igrams.npz ({e})", path, fmt) from e


def write_worker_stack(stack: IgramStack, path: Path) -> Path:
    """Write the ``.npz`` the unwrap workers read (uncompressed: it is scratch, PERF-03)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, NDArray[Any]] = {
        "wrapped": np.asarray(stack.wrapped, dtype=np.float32),
        "coherence": np.asarray(stack.coherence, dtype=np.float32),
        "pairs": np.array(stack.pairs),
        "dates": np.array([d.isoformat() for d in stack.dates]),
    }
    if stack.mask is not None:
        arrays["mask"] = np.asarray(stack.mask, dtype=bool)
    for key, value in stack.truth.items():
        arrays[key] = np.asarray(value)
    np.savez(path, **arrays)  # type: ignore[arg-type]
    return path
