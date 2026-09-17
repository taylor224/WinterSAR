"""The unwrap stage input seam (R-06): npz interchange, engine directories, UNW-005.

``isce2_topsstack`` and ``hyp3`` hand the unwrap stage a **directory** artifact
(``kind="dir"``, ``meta["format"]``), not an ``igrams.npz``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.unwrap.conftest import make_synth_stack, write_isce_igram_dir
from wintersar.io.igrams import IgramStack, load_igram_stack, save_igram_stack
from wintersar.io.schemas import Artifact
from wintersar.unwrap import inputs
from wintersar.unwrap.inputs import UnwrapInputError, load_igrams, write_worker_stack


def _isce_artifact(path: Path, **meta: Any) -> Artifact:
    return Artifact(
        name="igrams",
        path=path,
        kind="dir",
        meta={"format": inputs.ISCE_FORMAT, "reader": "wintersar.io.formats.read_isce_raster"}
        | meta,
    )


def test_npz_artifact_still_loads(tmp_path: Path) -> None:
    stack = make_synth_stack(n_dates=3, shape=(8, 8), seed=1)
    npz = save_igram_stack(stack, tmp_path / "igrams.npz")
    loaded, fmt = load_igrams(Artifact(name="igrams", path=npz, kind="npz"))
    assert fmt == inputs.NPZ_FORMAT
    assert loaded.pairs == stack.pairs and loaded.shape == stack.shape


def test_isce_directory_artifact_is_read_as_a_stack(tmp_path: Path) -> None:
    stack = make_synth_stack(n_dates=4, shape=(12, 10), seed=2, with_truth=False)
    igram_dir = write_isce_igram_dir(tmp_path / "merged" / "interferograms", stack)
    loaded, fmt = load_igrams(_isce_artifact(igram_dir))
    assert fmt == inputs.ISCE_FORMAT
    assert loaded.pairs == stack.pairs
    assert loaded.shape == stack.shape and loaded.n_pairs == stack.n_pairs
    assert loaded.dates == stack.dates
    np.testing.assert_allclose(loaded.coherence, stack.coherence, atol=1e-6)
    # wrapped phase comes back as angle(filt_fine.int) where the coherence is not zero
    strong = stack.coherence > 0.05
    np.testing.assert_allclose(loaded.wrapped[strong], stack.wrapped[strong], atol=1e-4)


def test_isce_manifest_file_list_wins_over_globbing(tmp_path: Path) -> None:
    stack = make_synth_stack(n_dates=3, shape=(8, 8), seed=3, with_truth=False)
    igram_dir = write_isce_igram_dir(tmp_path / "igrams", stack)
    # the adapter's manifest lists the files explicitly (engines/isce2_topsstack.py)
    manifest = {
        "format": inputs.ISCE_FORMAT,
        "pairs": stack.pairs,
        "interferograms": [
            {
                "pair": pair,
                "dir": str(igram_dir / pair),
                "files": {
                    "filt_int": {"path": str(igram_dir / pair / "filt_fine.int"), "exists": True},
                    "cor": {"path": str(igram_dir / pair / "filt_fine.cor"), "exists": True},
                    "unw": {"path": str(igram_dir / pair / "filt_fine.unw"), "exists": False},
                },
            }
            for pair in stack.pairs
        ],
    }
    manifest_path = tmp_path / "igrams_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    loaded, _ = load_igrams(_isce_artifact(igram_dir, manifest=str(manifest_path)))
    assert loaded.pairs == stack.pairs
    np.testing.assert_allclose(loaded.coherence, stack.coherence, atol=1e-6)


def test_isce_directory_without_rasters_reports_unw005(tmp_path: Path) -> None:
    empty = tmp_path / "interferograms"
    (empty / "20240101_20240113").mkdir(parents=True)
    with pytest.raises(UnwrapInputError) as exc:
        load_igrams(_isce_artifact(empty))
    assert exc.value.params["format"] == inputs.ISCE_FORMAT
    assert "filt_" in exc.value.params["detail"]


def test_hyp3_directory_is_dispatched_to_the_product_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GeoTIFF reading itself lives in io.formats; here only the dispatch is checked."""
    stack = make_synth_stack(n_dates=3, shape=(8, 8), seed=4, with_truth=False)
    product_dir = tmp_path / "hyp3"
    product_dir.mkdir()
    seen: list[Path] = []

    def fake_reader(path: Path | str, **kwargs: Any) -> IgramStack:
        seen.append(Path(path))
        return stack

    monkeypatch.setattr("wintersar.io.formats.read_hyp3_product_dir", fake_reader)
    loaded, fmt = load_igrams(
        Artifact(name="igrams", path=product_dir, kind="dir", meta={"format": inputs.HYP3_FORMAT})
    )
    assert fmt == inputs.HYP3_FORMAT and seen == [product_dir]
    assert loaded.pairs == stack.pairs


def test_missing_and_unknown_inputs_raise_unwrap_input_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.npz"
    with pytest.raises(UnwrapInputError) as exc:
        load_igrams(Artifact(name="igrams", path=missing, kind="npz"))
    assert exc.value.params["path"] == str(missing)
    assert "not found" in exc.value.params["detail"]

    other = tmp_path / "zarr_store"
    other.mkdir()
    with pytest.raises(UnwrapInputError):
        load_igrams(Artifact(name="igrams", path=other, kind="dir", meta={"format": "zarr"}))

    broken = tmp_path / "broken.npz"
    broken.write_bytes(b"not an npz")
    with pytest.raises(UnwrapInputError):
        load_igrams(Artifact(name="igrams", path=broken, kind="npz"))


def test_worker_stack_round_trips_through_the_npz_interchange(tmp_path: Path) -> None:
    stack = make_synth_stack(n_dates=3, shape=(8, 8), seed=5)
    path = write_worker_stack(stack, tmp_path / "scratch" / "igrams.npz")
    back = load_igram_stack(path)
    assert back.pairs == stack.pairs and back.dates == stack.dates
    np.testing.assert_array_equal(back.wrapped, stack.wrapped)
    np.testing.assert_array_equal(back.coherence, stack.coherence)
    assert back.mask is not None
    np.testing.assert_array_equal(back.mask, stack.mask)
    np.testing.assert_array_equal(back.truth["unw_true"], stack.truth["unw_true"])
