"""Seams between the pipeline and the hyp3/mintpy adapters (ADR-0020/0021).

The executor hands an engine only the artifacts declared by its ``StageSpec`` and the
parameters of :meth:`wintersar.pipeline.config.Config.stage_params`, so these tests use the
*real* shapes:

* ``interferogram`` runs with ``inputs`` that contain the precheck ``stack`` artifact only —
  or nothing at all, because ``fetch``/``coregister`` are skipped on the HyP3 path — and with
  the nested ``{"engine": {"hyp3": {...}}}`` parameters ``stage_params`` produces.
* ``corrections``/``geocode`` run with the declared ``timeseries`` artifact only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import make_burst
from tests.unit.engines_hyp3_mintpy.conftest import FakeHyp3Client, make_stack
from wintersar.engines import hyp3
from wintersar.engines import mintpy as mp
from wintersar.io.schemas import Artifact, Artifacts, StageRecord
from wintersar.pipeline import cache
from wintersar.pipeline.config import EXAMPLE_CONFIG, Config

HYP3_OPTIONS: dict[str, Any] = {
    "apply_water_mask": True,
    "submit_batch_size": 1,
    "clip_to_common_extent": False,
    "poll_interval_s": 0,
    "poll_max_interval_s": 0,
}


def _config(tmp_path: Path, **hyp3_options: Any) -> Config:
    raw = yaml.safe_load(EXAMPLE_CONFIG)
    raw["project"]["workdir"] = str(tmp_path / "work")
    raw["aoi"] = str(tmp_path / "aoi.geojson")
    raw["engine"]["interferogram"] = "hyp3"
    raw["engine"]["hyp3"] = dict(hyp3_options)
    raw["validate"] = {}
    return Config.model_validate(raw)


def _write_precheck_stack(path: Path, n_bursts: int = 1) -> Path:
    """``<out>/stack.json`` exactly as ``select_precheck_stage`` writes it."""
    # source: src/wintersar/pipeline/stages.py::select_precheck_stage
    #         stack_path.write_text(stack.model_dump_json(indent=2))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(make_stack(n_bursts=n_bursts).model_dump_json(indent=2), encoding="utf-8")
    return path


def _cached_precheck_node(workdir: Path, stack_json: Path) -> StageRecord:
    """A ``work/precheck/<hash>/manifest.json`` as the executor leaves it behind."""
    arts = cache.hash_artifacts(
        Artifacts().add(Artifact(name="stack", path=stack_json, kind="json"))
    )
    record = StageRecord(
        stage="precheck",
        node_hash="deadbeef",
        status="ok",
        outputs={n: str(a.path) for n, a in arts.items.items()},
        extra={cache.ARTIFACTS_KEY: cache.artifacts_to_extra(arts)},
    )
    cache.write_record(record, cache.stage_dir(workdir, "precheck", record.node_hash))
    return record


# ------------------------------------------------------------------ hyp3: stack resolution


def test_load_candidates_reads_the_precheck_stack_artifact(tmp_path: Path) -> None:
    """``StageSpec('precheck')`` produces ``stack`` (a single StackCandidate), not ``candidates``."""
    stack_json = _write_precheck_stack(tmp_path / "out" / "stack.json", n_bursts=2)
    inputs = Artifacts().add(Artifact(name="stack", path=stack_json, kind="json"))
    stack, granules = hyp3.load_candidates(inputs, {})
    assert stack.stack_id == "T052D_VV"
    assert set(granules) == {"2024-01-01", "2024-01-13", "2024-01-25"}
    specs, findings = hyp3.specs_from_stack(stack, granules)
    assert not findings and len(specs) == 3 and specs[0].n_bursts == 2


def test_load_candidates_rejects_the_search_candidates_json(tmp_path: Path) -> None:
    """``search`` writes a SearchResult; the adapter needs the precheck stack instead."""
    # source: src/wintersar/select/search.py::SearchResult.to_dict
    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product_type": "BURST",
                "query": {},
                "records": [make_burst("20240101").model_dump(mode="json")],
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"wintersar search"):
        hyp3.load_candidates(Artifacts(), {"candidates": str(path)})


def test_load_candidates_falls_back_to_the_cached_precheck_node(tmp_path: Path) -> None:
    """No inputs at all (the interferogram StageSpec declares only ``coreg_manifest``)."""
    workdir = tmp_path / "work"
    stack_json = _write_precheck_stack(workdir / "precheck" / "deadbeef" / "out" / "stack.json")
    _cached_precheck_node(workdir, stack_json)
    assert hyp3.stack_path_from_workdir({"_workdir": str(workdir)}) == stack_json.resolve()
    stack, granules = hyp3.load_candidates(Artifacts(), {"_workdir": str(workdir)})
    assert stack.stack_id == "T052D_VV" and granules
    # no work directory and no artifact -> unchanged contract (FileNotFoundError)
    with pytest.raises(FileNotFoundError, match="precheck"):
        hyp3.load_candidates(Artifacts(), {"_workdir": str(tmp_path / "empty")})


# ------------------------------------------------------------------ hyp3: nested engine.hyp3


def test_run_with_stack_input_and_nested_engine_hyp3_options(tmp_path: Path) -> None:
    """``Config.stage_params('interferogram')`` nests the adapter options under engine.hyp3."""
    cfg = _config(tmp_path, **HYP3_OPTIONS)
    stack_json = _write_precheck_stack(tmp_path / "precheck" / "stack.json")
    params = {**cfg.stage_params("interferogram"), "_out_dir": str(tmp_path / "out")}
    client = FakeHyp3Client()
    eng = hyp3.Hyp3Engine(client=client, sleep=lambda _s: None)
    arts = eng.run(
        "interferogram",
        Artifacts().add(Artifact(name="stack", path=stack_json, kind="json")),
        params,
        tmp_path / "logs",
    )
    # submit_batch_size = 1 -> one job per submit call (default SUBMIT_BATCH_SIZE would be 1 call)
    assert [len(batch) for batch in client.submitted] == [1, 1, 1]
    assert all(spec.apply_water_mask for batch in client.submitted for spec in batch)
    # clip_to_common_extent = False although three products share an extent
    assert arts["igrams"].meta["clipped"] is False
    assert arts["igrams"].meta["patterns"]["unwFile"] == "*/*/*_unw_phase.tif"
    assert arts["igrams"].meta["n_pairs"] == 3
    # engine.target_pixel_m (40 m) still snaps the looks
    assert arts["igrams"].meta["looks"] == "10x2"


def test_run_without_inputs_uses_the_precheck_node_in_the_workdir(tmp_path: Path) -> None:
    """Reproduces the pipeline call: the executor passes ``inputs == {}`` (PIPELINE-007)."""
    cfg = _config(tmp_path, **HYP3_OPTIONS)
    workdir = Path(cfg.workdir)
    stack_json = _write_precheck_stack(workdir / "precheck" / "deadbeef" / "out" / "stack.json")
    _cached_precheck_node(workdir, stack_json)
    params = {
        **cfg.stage_params("interferogram"),
        "_out_dir": str(tmp_path / "out"),
        "_workdir": str(workdir),
    }
    eng = hyp3.Hyp3Engine(client=FakeHyp3Client(), sleep=lambda _s: None)
    arts = eng.run("interferogram", Artifacts(), params, tmp_path / "logs")
    assert set(arts.items) == {"igrams", "unw"} and arts["igrams"].meta["n_pairs"] == 3


def test_hyp3_options_flat_keys_win_over_the_nested_section() -> None:
    opts = hyp3.hyp3_options(
        {
            "engine": {"looks": "auto", "hyp3": {"name_prefix": "nested", "api_url": "https://x"}},
            "name_prefix": "flat",
        }
    )
    assert opts["name_prefix"] == "flat" and opts["api_url"] == "https://x"
    assert hyp3.hyp3_options({}) == {}


# ------------------------------------------------------------------ mintpy: work dir inheritance


def _mintpy_inputs(tmp_path: Path) -> Artifacts:
    data = tmp_path / "hyp3"
    data.mkdir(exist_ok=True)
    meta = {"processor": "hyp3", "patterns": {"unwFile": "*/*/*_unw_phase.tif"}, "clipped": False}
    return Artifacts().add(Artifact(name="unw", path=data, kind="dir", meta=meta))


def _mintpy_params(tmp_path: Path, stage: str) -> dict[str, Any]:
    return {
        "_out_dir": str(tmp_path / "work" / stage),
        "_cache_dir": str(tmp_path / "cache"),
        "timeseries": {"reference_point": (37.55, 126.95), "troposphere": "era5"},
    }


def test_corrections_and_geocode_inherit_workdir_from_the_timeseries_artifact(
    tmp_path: Path, fake_mintpy_exe: Path
) -> None:
    """``StageSpec('corrections')`` forwards ``timeseries`` only — no ``mintpy_workdir``."""
    eng = mp.MintPyEngine()
    produced = eng.run(
        "timeseries", _mintpy_inputs(tmp_path), _mintpy_params(tmp_path, "timeseries"), tmp_path
    )
    workdir = Path(produced["mintpy_workdir"].path)
    declared = Artifacts().add(produced["timeseries"])
    assert set(declared.items) == {"timeseries"}

    corrected = eng.run("corrections", declared, _mintpy_params(tmp_path, "corrections"), tmp_path)
    assert Path(corrected["mintpy_workdir"].path) == workdir
    assert corrected["velocity"].path == workdir / "velocity.h5"

    geocoded = eng.run(
        "geocode",
        Artifacts().add(corrected["timeseries"]),
        _mintpy_params(tmp_path, "geocode"),
        tmp_path,
    )
    assert Path(geocoded["mintpy_workdir"].path) == workdir


def test_workdir_inherited_from_an_artifact_one_level_below_the_workdir(
    tmp_path: Path, fake_mintpy_exe: Path
) -> None:
    """``inputs/geometry*.h5`` and ``geo/geo_*.h5`` sit one level under the work directory."""
    eng = mp.MintPyEngine()
    produced = eng.run(
        "timeseries", _mintpy_inputs(tmp_path), _mintpy_params(tmp_path, "timeseries"), tmp_path
    )
    workdir = Path(produced["mintpy_workdir"].path)
    only_geometry = Artifacts().add(produced["geometry"])
    assert Path(only_geometry["geometry"].path).parent == workdir / "inputs"
    assert mp.MintPyEngine._inherit_workdir(only_geometry, {}) == workdir
    # a path that is not a MintPy work directory is still rejected (MP-004)
    assert mp.MintPyEngine._inherit_workdir(Artifacts(), {"workdir": str(tmp_path)}) is None
