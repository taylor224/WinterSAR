"""Stage-parameter seam: the executor passes the ``unwrap`` section nested (R-06).

``canonical_params(cfg, "unwrap")`` keeps config sections nested
(``{"unwrap": {...}}``, :mod:`wintersar.pipeline.dag`) while ``wintersar unwrap run`` and
the tests pass the flat form. Both must plan and run with the *user's* values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.unit.unwrap.conftest import make_machine
from wintersar.engines._unwrap_common import unwrap_cfg
from wintersar.pipeline.config import Config, TilesCfg
from wintersar.pipeline.dag import canonical_params
from wintersar.unwrap import api

NESTED: dict[str, Any] = {
    "unwrap": {
        "method": "snaphu",
        "cost": "smooth",
        "init": "mst",
        "coherence_threshold": 0.7,
        "tiles": {"rows": 2, "cols": 2, "overlap": 0.25, "min_overlap_px": 4},
        "nproc_per_igram": 2,
        "save_cost_file": True,
    },
    "_out_dir": "/tmp/out",
    "_cores": 4,
}


def _config(**unwrap: Any) -> Config:
    return Config.model_validate(
        {
            "project": {"name": "seam"},
            "aoi": "aoi.geojson",
            "time_range": {"start": "2024-01-01", "end": "2024-03-01"},
            "unwrap": unwrap,
        }
    )


def test_executor_really_nests_the_unwrap_section() -> None:
    """Guard on the other side of the seam (pipeline.dag owns this shape)."""
    params = canonical_params(_config(cost="smooth"), "unwrap")
    assert list(params) == ["unwrap"]
    assert params["unwrap"]["cost"] == "smooth"
    assert api.cfg_from_params(params).cost == "smooth"


def test_cfg_from_params_reads_the_nested_section() -> None:
    cfg = api.cfg_from_params(NESTED)
    assert cfg.method == "snaphu"
    assert cfg.cost == "smooth" and cfg.init == "mst"
    assert cfg.coherence_threshold == 0.7
    assert isinstance(cfg.tiles, TilesCfg) and (cfg.tiles.rows, cfg.tiles.cols) == (2, 2)
    assert cfg.nproc_per_igram == 2
    assert cfg.save_cost_file is True  # was missing from the hand-written PARAM_KEYS
    # flat stays supported and identical
    assert api.cfg_from_params(dict(NESTED["unwrap"])) == cfg


def test_flatten_params_top_level_wins_and_keeps_private_keys() -> None:
    flat = api.flatten_params({**NESTED, "cost": "topo"})
    assert flat["cost"] == "topo"  # same precedence as engines._unwrap_common.unwrap_cfg
    assert flat["init"] == "mst"
    assert flat["_out_dir"] == "/tmp/out" and flat["_cores"] == 4
    assert "unwrap" not in flat
    assert api.flatten_params({"cost": "topo"}) == {"cost": "topo"}


def test_backend_params_reach_the_adapter_with_the_configured_values() -> None:
    cfg = api.cfg_from_params(NESTED)
    plan = api.resolve_plan((64, 64), 3, make_machine(4, 8.0), cfg, requested_method="snaphu")
    params = api._backend_params(
        api.flatten_params(NESTED), cfg, plan, Path("/tmp/tiles"), Path("/tmp/logs")
    )
    assert "unwrap" not in params  # the adapters would give the top level precedence
    assert params["cost"] == "smooth" and params["init"] == "mst"
    assert params["coherence_threshold"] == 0.7
    assert params["save_cost_file"] is True
    assert params["ntiles"] == (2, 2) and params["nproc"] == plan.nproc_per_igram
    # the adapter-side merge (engines/_unwrap_common.unwrap_cfg) must see the same values
    merged = unwrap_cfg(params)
    assert merged["cost"] == "smooth" and merged["init"] == "mst"
    assert merged["coherence_threshold"] == 0.7


def test_plan_follows_the_nested_configuration() -> None:
    cfg = api.cfg_from_params(NESTED)
    plan = api.resolve_plan((64, 64), 4, make_machine(8, 16.0), cfg, requested_method="snaphu")
    assert plan.method == "snaphu"
    assert (plan.rows, plan.cols) == (2, 2)
    assert plan.nproc_per_igram == 2
