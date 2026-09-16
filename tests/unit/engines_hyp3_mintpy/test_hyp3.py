"""HyP3 adapter tests (PERF-02/06 resume, credits SEL-13, product validation, ADR-0020)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tests.unit.engines_hyp3_mintpy.conftest import (
    FakeHyp3Client,
    make_stack,
    write_candidates,
)
from wintersar.engines import hyp3
from wintersar.engines.base import get_engine
from wintersar.io.schemas import Artifact, Artifacts, Plan, StackCandidate, StageRecord

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "hyp3"


def _engine(client: FakeHyp3Client, **kw: object) -> hyp3.Hyp3Engine:
    return hyp3.Hyp3Engine(client=client, sleep=lambda s: None, **kw)  # type: ignore[arg-type]


def _run(eng: hyp3.Hyp3Engine, candidates: Path, tmp_path: Path, **params: object) -> Artifacts:
    p = {
        "_out_dir": str(tmp_path / "work"),
        "engine": {"looks": "auto", "target_pixel_m": 80},
        **params,
    }
    inputs = Artifacts().add(Artifact(name="candidates", path=candidates, kind="json"))
    return eng.run("interferogram", inputs, p, tmp_path / "work" / "logs")


# ------------------------------------------------------------------ registry / install


def test_registered_stage_and_produces() -> None:
    eng = get_engine("hyp3")
    assert isinstance(eng, hyp3.Hyp3Engine)
    assert eng.stages == ("interferogram",)
    assert hyp3.Hyp3Engine.produces == ("igrams", "unw")  # pipeline skips local unwrap


def test_check_install_reports_env001_and_env003(no_credentials: None) -> None:
    eng = get_engine("hyp3")
    assert eng.detect_version() is None  # hyp3-sdk is not installed (ADR-0001 policy)
    ids = [f.rule_id for f in eng.check_install()]
    assert "ENV-001" in ids
    assert "ENV-003" in ids


def test_resolve_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EARTHDATA_TOKEN", "eyJabc.def.ghi")
    assert hyp3.resolve_credentials(netrc_path=tmp_path / "none").token == "eyJabc.def.ghi"
    monkeypatch.delenv("EARTHDATA_TOKEN")
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine urs.earthdata.nasa.gov login u password p\n")
    creds = hyp3.resolve_credentials(env={}, netrc_path=netrc)
    assert creds.netrc and creds.available and creds.token is None
    assert (
        hyp3.credentials_findings(hyp3.resolve_credentials(env={}, netrc_path=tmp_path / "x"))[
            0
        ].rule_id
        == "ENV-003"
    )


# ------------------------------------------------------------------ credits (hyp3_costs.yaml)


@pytest.mark.parametrize(
    ("looks", "n_bursts", "credits"),
    [
        ("20x4", 1, 1),
        ("20x4", 4, 1),
        ("20x4", 5, 5),
        ("20x4", 12, 5),
        ("20x4", 13, 10),
        ("20x4", 15, 10),
        ("10x2", 3, 1),
        ("10x2", 4, 5),
        ("10x2", 10, 10),
        ("5x1", 1, 1),
        ("5x1", 2, 5),
        ("5x1", 3, 10),
        ("5x1", 4, 15),
        ("5x1", 10, 45),
        ("5x1", 11, 90),
        ("5x1", 15, 110),
    ],
)
def test_job_credits_match_credit_table(looks: str, n_bursts: int, credits: float) -> None:
    # source: https://hyp3-docs.asf.alaska.edu/using/credits/ (2026-09-16)
    assert hyp3.job_credits(looks, n_bursts) == credits


def test_job_credits_unknown_cases() -> None:
    assert hyp3.job_credits("5x1", 16) is None
    assert hyp3.job_credits("20x4", 0) is None
    assert hyp3.job_credits("7x7", 1) is None
    table = hyp3.load_cost_table()
    assert table.monthly_free_credits == 8000
    assert table.max_bursts_per_job == 15
    assert table.source_url.startswith("https://hyp3-docs.asf.alaska.edu/")
    assert table.fetched  # date recorded


def test_estimate_credits_and_plan() -> None:
    r = hyp3.estimate_credits(12, 1, "20x4")
    assert r.credits == 12 and r.n_jobs == 12 and r.notes["credits_per_job"] == 1
    assert r.notes["pixel_m"] == 80
    plan = Plan(
        stages=[
            StageRecord(
                stage="interferogram",
                node_hash="a",
                engine="hyp3",
                params={
                    "n_pairs": 12,
                    "n_bursts": 5,
                    "engine": {"looks": "auto", "target_pixel_m": 80},
                },
            ),
            StageRecord(stage="unwrap", node_hash="b", engine="snaphu", params={}),
            StageRecord(
                stage="interferogram",
                node_hash="c",
                engine="isce2_topsstack",
                params={"n_pairs": 99},
            ),
        ],
        to_run=["a"],
    )
    res = hyp3.Hyp3Engine().estimate(plan)
    assert res.credits == 12 * 5 and res.n_jobs == 12


# ------------------------------------------------------------------ looks


@pytest.mark.parametrize(
    ("looks", "target", "expected", "warn"),
    [
        ("10x2", None, "10x2", False),
        ((20, 4), None, "20x4", False),
        ([5, 1], None, "5x1", False),
        ("auto", 40, "10x2", False),
        ("auto", 80, "20x4", False),
        (None, None, "20x4", False),
        ("auto", 30, "10x2", True),
        ((7, 2), None, "5x1", True),
        ("30x6", None, "20x4", True),
    ],
)
def test_resolve_looks(looks: object, target: float | None, expected: str, warn: bool) -> None:
    used, finding = hyp3.resolve_looks(looks, target)
    assert used == expected
    assert (finding is not None) == warn
    if finding:
        assert finding.rule_id == "HYP3-003" and finding.severity == "WARN"


# ------------------------------------------------------------------ job specs


def test_job_spec_prepared_payload_matches_sdk() -> None:
    single = hyp3.JobSpec(
        pair="20240101_20240113", reference=("G1",), secondary=("G2",), looks="10x2", name="n1"
    )
    assert single.job_type == hyp3.JOB_TYPE_BURST
    assert single.to_prepared() == {
        "job_parameters": {"granules": ["G1", "G2"], "apply_water_mask": False, "looks": "10x2"},
        "job_type": "INSAR_ISCE_BURST",
        "name": "n1",
    }
    multi = hyp3.JobSpec(
        pair="p", reference=("A", "B"), secondary=("C", "D"), apply_water_mask=True
    )
    assert multi.job_type == hyp3.JOB_TYPE_MULTI_BURST
    prepared = multi.to_prepared()
    assert "name" not in prepared
    assert prepared["job_parameters"] == {
        "reference": ["A", "B"],
        "secondary": ["C", "D"],
        "apply_water_mask": True,
        "looks": "20x4",
    }


def test_specs_from_stack_single_and_multi() -> None:
    single = make_stack(n_bursts=1)
    specs, findings = hyp3.specs_from_stack(single, single.notes["granules"], looks="20x4")
    assert not findings and len(specs) == len(single.pairs) == 3
    assert all(s.n_bursts == 1 for s in specs)
    assert specs[0].pair == "20240101_20240113"
    assert specs[0].reference[0].startswith("S1_109903_IW2_20240101")
    multi = make_stack(n_bursts=2)
    specs, findings = hyp3.specs_from_stack(
        multi, multi.notes["granules"], looks="10x2", apply_water_mask=True
    )
    assert not findings and all(s.job_type == hyp3.JOB_TYPE_MULTI_BURST for s in specs)
    assert [g.split("_")[1] for g in specs[0].reference] == [
        "109903",
        "109904",
    ]  # sorted by burst id
    assert specs[0].looks == "10x2" and specs[0].apply_water_mask


def test_specs_missing_granule_and_too_many_bursts() -> None:
    stack = make_stack(n_bursts=1)
    granules = {k: dict(v) for k, v in stack.notes["granules"].items()}
    del granules["2024-01-25"]["052_109903_IW2"]
    specs, findings = hyp3.specs_from_stack(stack, granules)
    assert len(specs) == 1  # only 20240101_20240113 has both dates
    assert {f.rule_id for f in findings} == {"HYP3-007"}
    assert all(f.severity == "WARN" for f in findings)
    big = make_stack(n_bursts=16)
    specs, findings = hyp3.specs_from_stack(big, big.notes["granules"])
    assert specs == [] and findings[0].rule_id == "HYP3-004" and findings[0].is_fail


def test_load_candidates_shapes(tmp_path: Path) -> None:
    stack = make_stack(n_bursts=1)
    p = tmp_path / "c.json"
    p.write_text(json.dumps([stack.model_dump(mode="json")]))
    s, g = hyp3.load_candidates(Artifacts(), {"candidates": str(p)})
    assert s.stack_id == "T052D_VV" and "2024-01-01" in g
    p.write_text(json.dumps(stack.model_dump(mode="json")))
    s, _ = hyp3.load_candidates(
        Artifacts().add(Artifact(name="candidates", path=p, kind="json")), {}
    )
    assert isinstance(s, StackCandidate)
    with pytest.raises(FileNotFoundError):
        hyp3.load_candidates(Artifacts(), {})
    with pytest.raises(ValueError, match="not in candidates"):
        hyp3.load_candidates(Artifacts(), {"candidates": str(p), "stack_id": "T999A_VV"})


def test_load_candidates_granule_list_form(tmp_path: Path) -> None:
    stack = make_stack(n_bursts=2)
    listed = {d: list(v.values()) for d, v in stack.notes["granules"].items()}
    stack = stack.model_copy(update={"notes": {"granules": listed}})
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"stacks": [stack.model_dump(mode="json")]}))
    _, g = hyp3.load_candidates(Artifacts(), {"candidates": str(p)})
    assert set(g["2024-01-01"]) == {"109903_IW2", "109904_IW2"}
    specs, findings = hyp3.specs_from_stack(stack, g)
    assert not findings and specs[0].n_bursts == 2


# ------------------------------------------------------------------ products


def test_parse_metadata_txt_fixture_has_prep_hyp3_keys() -> None:
    meta = hyp3.parse_metadata_txt(FIXTURES / "S1_109903_IW2_20240101_20240113_VV_INT80_A1B2.txt")
    for key in hyp3.REQUIRED_METADATA_KEYS:
        assert key in meta, key
    assert meta["Rangelooks"] == "20" and meta["Heading"] == "-167.9"


def test_jobinfo_from_fixture_json() -> None:
    raw = json.loads((FIXTURES / "jobs.json").read_text())
    jobs = [hyp3.JobInfo.from_dict(j) for j in raw["jobs"]]
    assert jobs[0].succeeded and not jobs[0].expired()
    assert jobs[0].files[0]["filename"].endswith(".zip")
    assert jobs[1].failed and jobs[1].job_type == hyp3.JOB_TYPE_MULTI_BURST and jobs[1].logs
    assert not jobs[2].complete and jobs[2].credit_cost is None
    assert hyp3.product_name_of(jobs[0].files[0]["filename"]) == (
        "S1_109903_IW2_20240101_20240113_VV_INT80_A1B2",
        hyp3.JOB_TYPE_BURST,
    )
    assert (
        hyp3.product_name_of(
            "S1_064_000000s1n00-136231s2n02-000000s3n00_IW_20200604_20200616_VV_INT80_77F1.zip"
        )[1]
        == hyp3.JOB_TYPE_MULTI_BURST
    )
    assert hyp3.product_name_of("foo.zip") is None


# ------------------------------------------------------------------ run (fake client)


def test_run_end_to_end_and_resume(tmp_path: Path, candidates_single: Path) -> None:
    client = FakeHyp3Client()
    eng = _engine(client)
    arts = _run(eng, candidates_single, tmp_path)
    data_dir = tmp_path / "work" / "hyp3"
    assert set(arts.items) == {"igrams", "unw"}
    assert arts["igrams"].path == data_dir == arts["unw"].path
    meta = arts["unw"].meta
    assert meta["unwrapped"] and meta["conncomp"] and meta["n_pairs"] == 3
    assert meta["patterns"]["unwFile"] == "*/*/*_unw_phase.tif"
    assert meta["credits_used"] == 3 and meta["looks"] == "20x4" and meta["pixel_m"] == 80
    # layout: <data_dir>/<pair>/<product>/<product>_<suffix>.tif + <product>.txt (prep_hyp3)
    for pair in meta["pairs"]:
        prod = hyp3.find_product_dir(data_dir / pair)
        assert prod is not None and prod.parent.name == pair
        for suffix in hyp3.REQUIRED_SUFFIXES:
            assert (prod / f"{prod.name}{suffix}").exists()
        assert (prod / f"{prod.name}.txt").exists()
        assert not list((data_dir / pair).glob("*.zip"))  # zip removed after extraction
    state = json.loads((data_dir / "jobs.json").read_text())
    assert all(v["status"] == "SUCCEEDED" and v["product_dir"] for v in state["pairs"].values())
    manifest = json.loads((data_dir / "manifest.json").read_text())
    assert manifest["credits_used"] == 3 and manifest["n_pairs"] == 3
    assert str(Path.home()) not in (data_dir / "manifest.json").read_text()  # masked (rule 11.11)
    log = (tmp_path / "work" / "logs" / "interferogram.log").read_text()
    assert "SUBMITTED" in log and "DOWNLOADED" in log
    assert (tmp_path / "work" / "logs" / "interferogram.findings.json").exists()
    assert len(client.submitted) == 1 and len(client.submitted[0]) == 3
    n_download = client.download_calls
    # resume (PERF-06): nothing resubmitted, nothing re-downloaded
    arts2 = _run(_engine(client), candidates_single, tmp_path)
    assert len(client.submitted) == 1 and client.download_calls == n_download
    assert arts2["igrams"].meta["n_pairs"] == 3
    # delete one product -> that pair is re-downloaded from the still-valid job (no resubmission,
    # no credits); only the deleted pair is fetched again
    import shutil

    shutil.rmtree(data_dir / "20240101_20240113")
    arts3 = _run(_engine(client), candidates_single, tmp_path)
    assert len(client.submitted) == 1
    assert client.download_calls == n_download + 1
    assert arts3["igrams"].meta["n_pairs"] == 3
    state = json.loads((data_dir / "jobs.json").read_text())
    assert state["pairs"]["20240101_20240113"]["job_id"] == "job-0001"


def test_run_multi_burst_uses_multi_job_type(tmp_path: Path, candidates_multi: Path) -> None:
    client = FakeHyp3Client()
    arts = _run(_engine(client), candidates_multi, tmp_path, engine={"looks": [10, 2]})
    assert all(
        s.job_type == hyp3.JOB_TYPE_MULTI_BURST and s.n_bursts == 2 for s in client.submitted[0]
    )
    assert arts["igrams"].meta["looks"] == "10x2" and arts["igrams"].meta["credits_used"] == 3
    prod = hyp3.find_product_dir(tmp_path / "work" / "hyp3" / "20240101_20240113")
    assert prod is not None and hyp3.product_name_of(prod.name)[1] == hyp3.JOB_TYPE_MULTI_BURST


def test_run_failed_job_finding_and_parse_log(tmp_path: Path, candidates_single: Path) -> None:
    client = FakeHyp3Client(fail_pairs=["20240113_20240125"])
    eng = _engine(client)
    with pytest.raises(hyp3.Hyp3RunError) as ei:
        _run(eng, candidates_single, tmp_path)
    fails = [f for f in ei.value.findings if f.is_fail]
    assert [f.rule_id for f in fails] == ["HYP3-001"] and fails[0].scope == "20240113_20240125"
    assert fails[0].params["log_url"].startswith("https://")
    parsed = eng.parse_log(tmp_path / "work" / "logs" / "interferogram.log")
    assert [f.rule_id for f in parsed] == ["HYP3-001"]
    # the two good pairs were downloaded and are kept for the re-run
    state = json.loads((tmp_path / "work" / "hyp3" / "jobs.json").read_text())
    assert state["pairs"]["20240101_20240113"]["product_dir"]
    assert state["pairs"]["20240113_20240125"]["status"] == "FAILED"
    # re-run resubmits only the failed pair
    client.fail_pairs.clear()
    _run(_engine(client), candidates_single, tmp_path)
    assert [s.pair for s in client.submitted[1]] == ["20240113_20240125"]


def test_run_missing_required_file_is_fail(tmp_path: Path, candidates_single: Path) -> None:
    client = FakeHyp3Client(skip_files=["_corr.tif"])
    with pytest.raises(hyp3.Hyp3RunError) as ei:
        _run(_engine(client), candidates_single, tmp_path)
    f = next(f for f in ei.value.findings if f.rule_id == "HYP3-002")
    assert f.is_fail and "_corr.tif" in f.params["missing"]
    assert "~" in f.params["product_dir"] or str(Path.home()) not in f.params["product_dir"]


def test_run_missing_metadata_key_is_fail(tmp_path: Path) -> None:
    from tests.unit.engines_hyp3_mintpy.conftest import write_product

    spec = hyp3.JobSpec(pair="20240101_20240113", reference=("G1",), secondary=("G2",))
    prod = tmp_path / "S1_109903_IW2_20240101_20240113_VV_INT80_A1B2"
    write_product(prod, prod.name, spec)
    txt = prod / f"{prod.name}.txt"
    txt.write_text(txt.read_text().replace("Heading: -167.9\n", ""))
    files, findings = hyp3.validate_product(prod, spec.pair)
    assert (
        files is None
        and findings[0].rule_id == "HYP3-002"
        and ".txt:Heading" in findings[0].params["missing"]
    )


def test_run_missing_recommended_file_is_warn(tmp_path: Path, candidates_single: Path) -> None:
    client = FakeHyp3Client(skip_files=["_conncomp.tif"])
    eng = _engine(client)
    arts = _run(eng, candidates_single, tmp_path)
    assert arts["unw"].meta["conncomp"] is False
    warns = [f for f in eng.findings if f.rule_id == "HYP3-008"]
    assert warns and all(f.severity == "WARN" for f in warns)


def test_run_insufficient_credits_blocks_submission(
    tmp_path: Path, candidates_single: Path
) -> None:
    client = FakeHyp3Client(credits=2.0)
    with pytest.raises(hyp3.Hyp3RunError) as ei:
        _run(_engine(client), candidates_single, tmp_path)
    f = ei.value.findings[-1]
    assert f.rule_id == "HYP3-005" and f.params == {"needed": 3.0, "remaining": 2.0, "n_jobs": 3}
    assert client.submitted == []
    # explicit override submits anyway (documented escape hatch)
    _run(_engine(client), candidates_single, tmp_path, ignore_credits=True)
    assert len(client.submitted) == 1


def test_run_poll_timeout_then_resume(tmp_path: Path, candidates_single: Path) -> None:
    client = FakeHyp3Client(never_finish=True)
    ticks = iter(range(0, 10_000, 100))
    eng = hyp3.Hyp3Engine(client=client, sleep=lambda s: None, clock=lambda: float(next(ticks)))
    with pytest.raises(hyp3.Hyp3RunError) as ei:
        _run(eng, candidates_single, tmp_path, poll_timeout_s=250, poll_interval_s=5)
    assert [f.rule_id for f in ei.value.findings if f.is_fail] == ["HYP3-006"]
    assert client.refresh_calls >= 2
    state = json.loads((tmp_path / "work" / "hyp3" / "jobs.json").read_text())
    assert all(v["job_id"] for v in state["pairs"].values())
    # jobs finish now: re-run reuses the submitted job ids (no new submission)
    client.never_finish = False
    arts = _run(_engine(client), candidates_single, tmp_path)
    assert len(client.submitted) == 1 and arts["igrams"].meta["n_pairs"] == 3


def test_run_expired_job_is_resubmitted_next_time(tmp_path: Path, candidates_single: Path) -> None:
    client = FakeHyp3Client(expired_pairs=["20240101_20240125"])
    eng = _engine(client)
    arts = _run(eng, candidates_single, tmp_path)  # not a FAIL: two products, one warning
    assert arts["igrams"].meta["n_pairs"] == 2
    assert [f.rule_id for f in eng.findings if f.severity == "WARN"] == ["HYP3-012"]
    client.expired_pairs.clear()
    _run(_engine(client), candidates_single, tmp_path)
    assert [s.pair for s in client.submitted[1]] == ["20240101_20240125"]


def test_run_no_pairs_is_fail(tmp_path: Path) -> None:
    c = write_candidates(tmp_path / "c.json", n_bursts=1, with_granules=False)
    eng = _engine(FakeHyp3Client())
    with pytest.raises(hyp3.Hyp3RunError) as ei:
        _run(eng, c, tmp_path)
    ids = [f.rule_id for f in ei.value.findings]
    assert "HYP3-011" in ids and ids.count("HYP3-007") == 3


def test_run_explicit_jobs_param(tmp_path: Path) -> None:
    client = FakeHyp3Client()
    jobs = [{"pair": "20240101_20240113", "reference": ["G1"], "secondary": ["G2"]}]
    eng = _engine(client)
    arts = eng.run(
        "interferogram",
        Artifacts(),
        {"_out_dir": str(tmp_path / "w"), "jobs": jobs, "looks": "5x1"},
        tmp_path / "w" / "logs",
    )
    assert (
        arts["igrams"].meta["pairs"] == ["20240101_20240113"]
        and client.submitted[0][0].looks == "5x1"
    )
    with pytest.raises(ValueError, match="interferogram"):
        eng.run("unwrap", Artifacts(), {}, tmp_path / "logs")


def test_clip_to_common_extent(tmp_path: Path, candidates_single: Path) -> None:
    import rasterio

    client = FakeHyp3Client(shift_pairs={"20240113_20240125": (126.902, 37.598)})
    eng = _engine(client)
    arts = _run(eng, candidates_single, tmp_path)
    meta = arts["igrams"].meta
    assert meta["clipped"] is True and meta["patterns"]["unwFile"].endswith("_unw_phase_clip.tif")
    assert [f.rule_id for f in eng.findings if f.rule_id == "HYP3-009"] == ["HYP3-009"]
    data_dir = tmp_path / "work" / "hyp3"
    shapes = set()
    for pair in meta["pairs"]:
        prod = hyp3.find_product_dir(data_dir / pair)
        assert prod is not None
        clips = sorted(prod.glob("*_clip.tif"))
        assert len(clips) == len(hyp3.REQUIRED_SUFFIXES) + len(hyp3.RECOMMENDED_SUFFIXES)
        with rasterio.open(prod / f"{prod.name}_unw_phase_clip.tif") as src:
            shapes.add((src.height, src.width))
            assert abs(src.bounds.left - 126.902) < 1e-6 and abs(src.bounds.top - 37.598) < 1e-6
    assert len(shapes) == 1 and shapes.pop() == (4, 4)
    # identical extents -> no clipping
    same = [d for d in (hyp3.find_product_dir(data_dir / p) for p in meta["pairs"][:1]) if d]
    assert hyp3.clip_to_common_extent(same * 2) == (False, [])


def test_clip_disjoint_extents_is_fail(tmp_path: Path) -> None:
    from tests.unit.engines_hyp3_mintpy.conftest import write_product

    spec = hyp3.JobSpec(pair="p", reference=("G1",), secondary=("G2",))
    a = tmp_path / "a" / "S1_109903_IW2_20240101_20240113_VV_INT80_A1B2"
    b = tmp_path / "b" / "S1_109903_IW2_20240113_20240125_VV_INT80_A1B3"
    write_product(a, a.name, spec, west=126.9, north=37.6)
    write_product(b, b.name, spec, west=130.0, north=30.0)
    clipped, findings = hyp3.clip_to_common_extent([a, b])
    assert clipped is False and findings[0].rule_id == "HYP3-013" and findings[0].is_fail


def test_extract_zip_rejects_traversal(tmp_path: Path) -> None:
    import zipfile

    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(ValueError, match="unsafe"):
        hyp3.extract_product_zip(z, tmp_path / "out")


def test_run_without_client_and_sdk_raises(tmp_path: Path, candidates_single: Path) -> None:
    from wintersar.engines.base import EngineNotAvailableError

    eng = hyp3.Hyp3Engine()
    inputs = Artifacts().add(Artifact(name="candidates", path=candidates_single, kind="json"))
    with pytest.raises(EngineNotAvailableError):
        eng.run("interferogram", inputs, {"_out_dir": str(tmp_path)}, tmp_path / "logs")


def test_pair_key_consistency_with_schema() -> None:
    from wintersar.io.schemas import Pair

    p = Pair(reference=date(2024, 1, 1), secondary=date(2024, 1, 13), temporal_baseline_days=12)
    assert p.key == "20240101_20240113"
