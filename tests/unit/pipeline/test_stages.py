"""Stage registry / engine resolution (plan §5.3)."""

from __future__ import annotations

import pytest

from tests.unit.pipeline._support import write_fake_config
from wintersar.pipeline import stages
from wintersar.pipeline.stages import STAGE_ORDER, STAGES, resolve_engine, stage_window


def test_stage_order_matches_plan() -> None:
    assert STAGE_ORDER == [
        "search",
        "precheck",
        "fetch",
        "coregister",
        "interferogram",
        "multilook",
        "unwrap",
        "timeseries",
        "corrections",
        "geocode",
        "validate",
    ]
    assert list(STAGES) == STAGE_ORDER


def test_every_input_has_an_upstream_producer() -> None:
    produced: set[str] = set()
    for name in STAGE_ORDER:
        spec = STAGES[name]
        for inp in [*spec.inputs, *spec.optional_inputs]:
            assert inp in produced, f"{name} needs {inp} before any stage produces it"
        produced.update(spec.outputs)


def test_python_and_engine_stages() -> None:
    assert [s for s in STAGE_ORDER if STAGES[s].is_python] == ["search", "precheck", "validate"]
    assert STAGES["unwrap"].engine_key == "unwrap.method"
    assert STAGES["timeseries"].engine_key == "timeseries.engine"
    assert STAGES["validate"].optional


def test_post_timeseries_stages_belong_to_the_timeseries_engine() -> None:
    """geocode is MintPy's stage, not the interferogram engine's.

    Keyed on ``engine.interferogram`` it was skipped on every real path (hyp3 and
    isce2_topsstack do not declare "geocode"), so ``velocity`` was never produced and the
    validate stage was blocked with PIPELINE-002 at plan time.
    """
    assert STAGES["geocode"].engine_key == "timeseries.engine"
    assert STAGES["corrections"].engine_key == "timeseries.engine"
    # MintPy's corrections step already writes velocity.h5, so validate has a producer even
    # when the chosen time-series engine does not geocode.
    assert "velocity" in STAGES["corrections"].outputs
    assert STAGES["validate"].inputs == []
    assert STAGES["validate"].optional_inputs == ["velocity", "timeseries"]


def test_resolve_engine_fake_path(tmp_path) -> None:
    cfg = write_fake_config(tmp_path)
    for s in STAGE_ORDER:
        expected = None if STAGES[s].is_python else "fake"
        assert resolve_engine(cfg, s) == expected
    assert stages.is_fake_path(cfg)


def test_resolve_engine_real_path(tmp_path) -> None:
    cfg = write_fake_config(
        tmp_path,
        engine={"interferogram": "hyp3"},
        timeseries={"engine": "mintpy"},
        unwrap={"method": "auto"},
    )
    assert resolve_engine(cfg, "interferogram") == "hyp3"
    assert resolve_engine(cfg, "geocode") == "mintpy"  # post-processing of the ts engine
    assert resolve_engine(cfg, "corrections") == "mintpy"
    assert resolve_engine(cfg, "unwrap") == "snaphu"  # auto -> snaphu
    assert resolve_engine(cfg, "timeseries") == "mintpy"
    assert resolve_engine(cfg, "search") is None
    cfg.unwrap.method = "tophu"
    assert resolve_engine(cfg, "unwrap") == "tophu"


def test_stage_window() -> None:
    assert stage_window() == STAGE_ORDER
    assert stage_window(until="unwrap")[-1] == "unwrap"
    assert stage_window(until="unwrap", from_stage="fetch")[0] == "search"
    with pytest.raises(ValueError):
        stage_window(until="unwrap", from_stage="timeseries")
    with pytest.raises(KeyError):
        stage_window(until="nope")


def test_stage_is_configured(tmp_path) -> None:
    cfg = write_fake_config(tmp_path)
    assert not stages.stage_is_configured(cfg, "validate")
    cfg.validation.leveling_csv = tmp_path / "lev.csv"
    assert stages.stage_is_configured(cfg, "validate")
    assert stages.stage_is_configured(cfg, "unwrap")


def test_load_entrypoint_missing_is_none() -> None:
    assert stages.load_entrypoint("wintersar.__no_such_module__", "fn") is None
    assert stages.load_entrypoint("wintersar.pipeline.stages", "__no_such_fn__") is None
    assert stages.load_entrypoint("wintersar.pipeline.stages", "resolve_engine") is resolve_engine
