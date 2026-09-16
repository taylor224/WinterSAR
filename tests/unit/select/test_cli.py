"""`wintersar search` / `wintersar precheck` on a fixture candidates.json (no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
import yaml
from typer.testing import CliRunner

from tests.unit.select.conftest import DATES_12D, full_stack
from wintersar.io.schemas import BurstRecord
from wintersar.select import cli as select_cli
from wintersar.util.clistate import state

runner = CliRunner()


@pytest.fixture
def app() -> typer.Typer:
    a = typer.Typer()
    select_cli.register(a)
    return a


@pytest.fixture
def project(tmp_path: Path, aoi_geojson: Path) -> dict[str, Path]:
    cfg = {
        "project": {"name": "t", "workdir": str(tmp_path / "work"), "language": "ko"},
        "aoi": str(aoi_geojson),
        "time_range": {"start": "2024-01-01", "end": "2024-12-31"},
        "data": {"polarization": "VV"},
        "selection": {"min_coverage": 0.95, "budget_credits": 100},
        "engine": {"interferogram": "fake"},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return {"config": cfg_path, "root": tmp_path}


def write_candidates(path: Path, records: list[BurstRecord]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "product_type": "BURST",
        "query": {},
        "records": [r.model_dump(mode="json") for r in records],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "json", False)


@pytest.fixture(autouse=True)
def _offline_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let unit tests reach the ASF stack API: baseline module returns pairs unchanged."""

    def compute_pair_baselines(records, pairs, method="asf", *, findings=None, **kw):
        return list(pairs)

    monkeypatch.setitem(
        sys.modules,
        "wintersar.select.baseline",
        SimpleNamespace(compute_pair_baselines=compute_pair_baselines),
    )


def test_precheck_ok(app: typer.Typer, project: dict[str, Path]) -> None:
    cand = write_candidates(project["root"] / "candidates.json", full_stack())
    out = project["root"] / "report"
    res = runner.invoke(
        app,
        [
            "precheck",
            str(cand),
            "--baseline",
            "none",
            "--config",
            str(project["config"]),
            "--out",
            str(out),
        ],
    )
    assert res.exit_code == 0, res.output
    for ext in ("json", "md", "html"):
        assert (out / f"precheck_report.{ext}").exists()
    payload = json.loads((out / "precheck_report.json").read_text(encoding="utf-8"))
    assert payload["recommended"] == "T052D_VV"
    assert payload["candidates"][0]["notes"]["recommended"] is True
    assert payload["summary"]["FAIL"] == 0
    rules = {f["rule_id"] for f in payload["findings"]}
    assert rules == {"SEL-09", "SEL-13", "SEL-06"}  # looks info, unknown credits, unknown baselines
    assert payload["resources"]["T052D_VV"]["n_jobs"] == 10 * 2  # 10 sbas pairs x 2 bursts
    assert "T052D_VV" in res.output


def test_precheck_default_out_is_workdir_select(app: typer.Typer, project: dict[str, Path]) -> None:
    cand = write_candidates(project["root"] / "candidates.json", full_stack(DATES_12D[:3]))
    res = runner.invoke(
        app, ["precheck", str(cand), "--baseline", "none", "--config", str(project["config"])]
    )
    assert res.exit_code == 0, res.output
    assert (project["root"] / "work" / "select" / "precheck_report.json").exists()


def test_precheck_fail_exit_code_and_no_fail(app: typer.Typer, project: dict[str, Path]) -> None:
    cand = write_candidates(project["root"] / "candidates.json", full_stack(polarization="VH"))
    args = [
        "precheck",
        str(cand),
        "--config",
        str(project["config"]),
        "--out",
        str(project["root"] / "r"),
    ]
    res = runner.invoke(app, args)
    assert res.exit_code == 1
    assert "SEL-05" in res.output
    res2 = runner.invoke(app, [*args, "--no-fail"])
    assert res2.exit_code == 0


def test_precheck_json_envelope(
    app: typer.Typer, project: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state, "json", True)
    cand = write_candidates(project["root"] / "candidates.json", full_stack(DATES_12D[:3]))
    res = runner.invoke(
        app,
        [
            "precheck",
            str(cand),
            "--baseline",
            "none",
            "--config",
            str(project["config"]),
            "--out",
            str(project["root"] / "r"),
        ],
    )
    assert res.exit_code == 0, res.output
    env = json.loads(res.stdout)
    assert env["ok"] is True and env["command"] == "precheck"
    assert env["data"]["recommended"] == "T052D_VV"
    assert set(env["data"]["paths"]) == {"json", "md", "html"}
    assert all(f["message_key"].startswith("select.") for f in env["findings"])


def test_precheck_geometry_option(app: typer.Typer, project: dict[str, Path]) -> None:
    cand = write_candidates(project["root"] / "candidates.json", full_stack(DATES_12D[:3]))
    geom = project["root"] / "geom.json"
    geom.write_text(
        json.dumps({"DESCENDING": {"layover_fraction": 0.2, "shadow_fraction": 0.0}}),
        encoding="utf-8",
    )
    res = runner.invoke(
        app,
        [
            "precheck",
            str(cand),
            "--baseline",
            "none",
            "--config",
            str(project["config"]),
            "--out",
            str(project["root"] / "r"),
            "--geometry",
            str(geom),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "SEL-12" in res.output


def test_precheck_errors(app: typer.Typer, project: dict[str, Path]) -> None:
    missing = runner.invoke(
        app,
        [
            "precheck",
            str(project["root"] / "nope.json"),
            "--baseline",
            "none",
            "--config",
            str(project["config"]),
        ],
    )
    assert missing.exit_code == 2
    bad_cfg = runner.invoke(
        app,
        [
            "precheck",
            str(project["root"] / "nope.json"),
            "--baseline",
            "none",
            "--config",
            str(project["root"] / "no.yaml"),
        ],
    )
    assert bad_cfg.exit_code == 2
    empty = write_candidates(project["root"] / "empty.json", [])
    assert (
        runner.invoke(
            app, ["precheck", str(empty), "--baseline", "none", "--config", str(project["config"])]
        ).exit_code
        == 2
    )
    broken = project["root"] / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert (
        runner.invoke(
            app, ["precheck", str(broken), "--baseline", "none", "--config", str(project["config"])]
        ).exit_code
        == 2
    )


def test_precheck_rejects_unknown_baseline_option(
    app: typer.Typer, project: dict[str, Path]
) -> None:
    cand = write_candidates(project["root"] / "candidates.json", full_stack(DATES_12D[:2]))
    res = runner.invoke(
        app, ["precheck", str(cand), "--baseline", "magic", "--config", str(project["config"])]
    )
    assert res.exit_code == 2


def test_run_precheck_collects_baseline_findings(
    project: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A baseline module that fills B-perp and emits its own findings is merged in."""
    from datetime import date as _date

    from wintersar.io.schemas import Finding
    from wintersar.pipeline.config import load_config

    def fake_compute(records, pairs, method, *, findings=None, **kw):
        assert method == "orbit"
        if findings is not None:
            findings.append(
                Finding(rule_id="SEL-SEARCH-08", severity="INFO", message_key="x", scope="baseline")
            )
        return [p.model_copy(update={"perp_baseline_m": 500.0}) for p in pairs]

    fake = SimpleNamespace(compute_pair_baselines=fake_compute)
    monkeypatch.setitem(sys.modules, "wintersar.select.baseline", fake)
    cfg = load_config(project["config"])
    recs = full_stack(DATES_12D[:3])
    result = select_cli.run_precheck(
        recs, cfg, select_cli.aoi_file_to_wkt(cfg.aoi), baseline_method="orbit"
    )
    c = result.candidates[0]
    assert c.pairs == []  # every pair is above max_perp (150 m) -> dropped from the sbas network
    assert any(f.rule_id == "SEL-SEARCH-08" and f.scope == c.stack_id for f in result.findings)
    assert c.reference_date == _date(2024, 1, 13)
    assert isinstance(result.candidates[0].notes["perp_by_date"], dict)


def test_load_candidates_accepts_bare_list(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps([r.model_dump(mode="json") for r in full_stack(DATES_12D[:1])]), encoding="utf-8"
    )
    assert len(select_cli.load_candidates_file(p)) == 2


def test_aoi_file_to_wkt(aoi_geojson: Path, tmp_path: Path) -> None:
    wkt = select_cli.aoi_file_to_wkt(aoi_geojson)
    assert wkt.startswith("POLYGON")
    raw = tmp_path / "aoi.wkt"
    raw.write_text("POLYGON((0 0,1 0,1 1,0 1,0 0))", encoding="utf-8")
    from shapely import wkt as shapely_wkt

    got = shapely_wkt.loads(select_cli.aoi_file_to_wkt(raw))
    assert got.equals(shapely_wkt.loads("POLYGON((0 0,1 0,1 1,0 1,0 0))"))


def test_search_delegates_to_search_module(
    app: typer.Typer, project: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    recs = full_stack(DATES_12D[:2])
    calls: dict[str, object] = {}

    def search_from_config(cfg: object) -> object:
        calls["cfg"] = cfg
        return SimpleNamespace(records=recs, product_type="BURST", findings=[], query={})

    def save_records(result: object, path: Path) -> None:
        write_candidates(Path(path), list(result.records))  # type: ignore[attr-defined]
        calls["path"] = path

    fake = SimpleNamespace(search_from_config=search_from_config, save_records=save_records)
    monkeypatch.setitem(sys.modules, "wintersar.select.search", fake)
    res = runner.invoke(app, ["search", "--config", str(project["config"])])
    assert res.exit_code == 0, res.output
    assert calls["path"] == project["root"] / "work" / "select" / "candidates.json"
    assert (project["root"] / "work" / "select" / "candidates.json").exists()
    assert "4" in res.output  # 2 dates x 2 bursts


def test_mounted_on_main_app() -> None:
    from wintersar.cli import app as main_app

    names = {c.name for c in main_app.registered_commands}
    assert {"search", "precheck"} <= names
