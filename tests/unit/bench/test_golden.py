"""bench.golden: tolerance policy (ADR-0102), statistics reduction (ADR-0100), findings,
script mains (make/check) and i18n keys."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from wintersar.bench import golden as g
from wintersar.bench.sites import Site, load_site
from wintersar.i18n import has_key, t

TINY_SITE: dict[str, Any] = {
    "name": "tiny",
    "size": "S",
    "synthetic": True,
    "runner": "pipeline",
    "repeats": 1,
    "config": {
        "engine": {"interferogram": "fake"},
        "timeseries": {"engine": "fake"},
        "unwrap": {"method": "auto", "coherence_threshold": 0.3},
        "compute": {"cores": 1, "memory_gb": 2},
    },
    "param_overrides": {
        "interferogram": {"n_dates": 4, "shape": [16, 16], "water_fraction": 0.1},
    },
    "metrics": ["closure_rms", "unwrap_error_fraction"],
}


@pytest.fixture
def tiny_site_path(tmp_path: Path) -> Path:
    p = tmp_path / "tiny.yaml"
    p.write_text(yaml.safe_dump(TINY_SITE), encoding="utf-8")
    return p


@pytest.fixture
def tiny_site(tiny_site_path: Path) -> Site:
    return load_site(tiny_site_path)


@pytest.fixture
def repo(tmp_path: Path, tiny_site_path: Path) -> Path:
    """A fake repository root whose default site is the tiny site."""
    root = tmp_path / "repo"
    (root / g.DEFAULT_SITE).parent.mkdir(parents=True)
    (root / g.DEFAULT_SITE).write_text(tiny_site_path.read_text(encoding="utf-8"), "utf-8")
    return root


# ---------------------------------------------------------------- tolerance policy
def test_tolerances_exact_for_non_floats() -> None:
    e = {"n": 3, "name": "x", "flag": True, "none": None, "ids": ["A", "B"], "shape": [2, 3]}
    assert g.compare_golden(e, json.loads(json.dumps(e))) == []
    bad = {**e, "n": 4, "name": "y", "flag": False, "ids": ["A", "C"], "shape": [2, 4]}
    kinds = {m.path: m.kind for m in g.compare_golden(e, bad)}
    assert kinds == {
        "n": "value",
        "name": "value",
        "flag": "value",
        "ids[1]": "value",
        "shape[1]": "value",
    }
    assert [m.kind for m in g.compare_golden({"ids": ["A"]}, {"ids": ["A", "B"]})] == ["length"]
    assert [m.kind for m in g.compare_golden({"v": 1}, {"v": "1"})] == ["type"]
    assert [m.kind for m in g.compare_golden({"v": None}, {"v": 0.0})] == ["type"]


def test_tolerances_floats_rtol_and_fraction_atol() -> None:
    tol = g.Tolerances(rtol=1e-6, atol=1e-9, fraction_atol=1e-4)
    e = {"velocity": {"mean": -0.005}, "unwrap": {"masked_fraction": 0.16}}
    ok = {"velocity": {"mean": -0.005 * (1 + 5e-7)}, "unwrap": {"masked_fraction": 0.16 + 5e-5}}
    assert g.compare_golden(e, ok, tol) == []
    bad = {"velocity": {"mean": -0.005 * (1 + 5e-6)}, "unwrap": {"masked_fraction": 0.16 + 5e-4}}
    ms = g.compare_golden(e, bad, tol)
    assert [(m.path, m.kind, m.tolerance) for m in ms] == [
        ("velocity.mean", "float", "rtol=1e-06 atol=1e-09"),
        ("unwrap.masked_fraction", "float", "atol=0.0001"),
    ]
    # a fraction is exact-only in the absolute sense: rtol does not apply
    assert tol.for_path("unwrap.masked_fraction") == (0.0, 1e-4)
    assert tol.for_path("igrams.coherence.mean") == (1e-6, 1e-9)
    assert tol.for_path("x.nan_fraction") == (0.0, 1e-4)
    # zero golden value: only atol can save an ulp of noise
    assert g.compare_golden({"m": 0.0}, {"m": 5e-10}, tol) == []
    assert len(g.compare_golden({"m": 0.0}, {"m": 5e-9}, tol)) == 1
    # int vs float compare as floats (JSON never distinguishes 0 and 0.0 reliably)
    assert g.compare_golden({"m": 0}, {"m": 0.0}, tol) == []
    # NaN equals NaN, and inf must match exactly
    assert g.compare_golden({"m": math.nan}, {"m": math.nan}, tol) == []
    assert len(g.compare_golden({"m": math.nan}, {"m": 0.0}, tol)) == 1
    assert len(g.compare_golden({"m": math.inf}, {"m": 1.0}, tol)) == 1


def test_missing_and_extra_keys_are_mismatches() -> None:
    ms = g.compare_golden({"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"d": 2}})
    assert {(m.path, m.kind) for m in ms} == {("b.c", "missing"), ("b.d", "extra")}


def test_mismatches_to_findings_split_site_from_values() -> None:
    ms = [
        g.Mismatch("site.param_overrides.interferogram.n_dates", "value", 6, 8),
        g.Mismatch("velocity.mean", "float", -0.005, -0.006, "rtol=1e-06 atol=1e-09"),
        g.Mismatch("stages.unwrap.findings", "length", 0, 1),
    ]
    fs = g.mismatches_to_findings(ms)
    assert [f.rule_id for f in fs] == ["GOLDEN-003", "GOLDEN-001", "GOLDEN-001"]
    assert all(f.severity == "FAIL" and f.scope == m.path for f, m in zip(fs, ms, strict=True))
    assert fs[1].params["tolerance"] == "rtol=1e-06 atol=1e-09"
    assert fs[1].evidence["expected_value"] == -0.005
    assert fs[0].message_key == "golden.GOLDEN-003.cause" and fs[0].fix_key
    text = g.format_mismatches(ms, limit=2, lang="en")
    assert "velocity.mean" in text and "... 1 more" in text


def test_i18n_keys_exist_in_both_languages() -> None:
    keys = [
        "golden.make.written",
        "golden.make.unchanged",
        "golden.make.changed",
        "golden.check.start",
        "golden.check.ok",
        "golden.check.regenerate",
    ]
    keys += [f"golden.mismatch.{k}" for k in ("line", "line_float", "more")]
    keys += [f"golden.args.{k}" for k in g.ARG_KEYS]
    for rid in ("GOLDEN-001", "GOLDEN-002", "GOLDEN-003", "GOLDEN-004"):
        keys += [f"golden.{rid}.cause", f"golden.{rid}.fix"]
    for rid in ("GOLDEN-005", "GOLDEN-006", "GOLDEN-007"):
        keys += [f"golden.{rid}.cause", f"golden.{rid}.fix"]
    for lang in ("ko", "en"):
        missing = [k for k in keys if not has_key(k, lang)]
        assert missing == [], (lang, missing)


# ---------------------------------------------------------------- statistics
def test_array_stats_finite_only_float64() -> None:
    a = np.array([[1.0, 2.0], [np.nan, 4.0]], dtype=np.float32)
    s = g.array_stats(a)
    assert s["n"] == 4 and s["nan_fraction"] == pytest.approx(0.25)
    assert "n_finite" not in s  # ADR-0102: the finite count lives only in nan_fraction
    assert (s["min"], s["max"], s["mean"]) == (1.0, 4.0, pytest.approx(7.0 / 3.0))
    assert s["p50"] == 2.0 and all(isinstance(s[k], float) for k in ("std", "p05", "p95"))
    assert g.array_stats(np.full(3, np.nan)) == {"n": 3, "nan_fraction": 1.0}


def test_one_masked_pixel_flip_is_within_fraction_tolerance() -> None:
    """ADR-0102: a threshold-boundary pixel flipping between platforms (1 of 2**17) must
    not fail the layer, so ``array_stats`` stores no exact finite-pixel count next to the
    tolerance-compared ``nan_fraction`` (the two carry the same information)."""
    a = np.ones((256, 512), dtype=np.float32)  # constant values: only nan-ness differs
    b = a.copy()
    b[3, 7] = np.nan
    sa, sb = g.array_stats(a), g.array_stats(b)
    assert sa["n"] == sb["n"] == 131072 and sb["nan_fraction"] == pytest.approx(1 / 131072)
    assert g.compare_golden({"unw": sa}, {"unw": sb}) == []
    # ... whereas more than fraction_atol * n flipped pixels is still a mismatch
    c = a.copy()
    c.ravel()[:20] = np.nan  # 20 / 131072 = 1.5e-4 > 1e-4
    ms = g.compare_golden({"unw": sa}, {"unw": g.array_stats(c)})
    assert [(m.path, m.kind, m.tolerance) for m in ms] == [
        ("unw.nan_fraction", "float", "atol=0.0001")
    ]
    # structural counts stay exact: a different size is a different array
    assert [m.path for m in g.compare_golden({"unw": sa}, {"unw": g.array_stats(a[:-1])})] == [
        "unw.n"
    ]


def test_golden_stats_of_tiny_site(tiny_site: Site, tmp_path: Path) -> None:
    stats, result = g.compute_golden(tiny_site, tmp_path / "w")
    assert result.ok and stats["run"] == {"ok": True, "failed_stage": None, "findings": []}
    assert stats["schema_version"] == g.GOLDEN_SCHEMA_VERSION and stats["seed"] == 0
    assert stats["site"] == g.site_block(tiny_site)
    assert stats["n_dates"] == 4 and stats["n_pairs"] == len(stats["igrams"]["pairs"])
    assert list(stats["stages"]) == [
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
    ]
    assert stats["stages"]["search"]["status"] == "skipped"
    assert stats["stages"]["unwrap"] == {"status": "ok", "engine": "fake", "findings": []}
    assert stats["artifacts"]["velocity"]["arrays"]["velocity"] == {
        "shape": [16, 16],
        "dtype": "float32",
    }
    assert stats["artifacts"]["igrams"]["arrays"]["wrapped"]["shape"] == [stats["n_pairs"], 16, 16]
    assert stats["artifacts"]["slc_manifest"] == {"kind": "json", "keys": ["n_dates", "source"]}
    assert stats["velocity"]["shape"] == [16, 16] and stats["velocity"]["min"] < 0
    assert 0.0 < stats["igrams"]["mask_fraction"] < 1.0
    assert stats["unwrap"]["masked_fraction"] >= stats["igrams"]["mask_fraction"]
    assert stats["unwrap"]["conncomp_n_labels"] == 2
    assert stats["metrics"]["unwrap_error_fraction"] == 0.0
    assert 0.0 < stats["metrics"]["closure_rms"] < math.pi
    assert stats["timeseries"]["n_dates"] == 4
    # machine independence: no timings, hashes, paths, timestamps anywhere
    assert g.forbidden_keys(stats) == []
    assert str(Path.home()) not in g.dump_golden(stats)
    assert g.count_leaves(stats) > 50


def test_run_golden_pipeline_is_hermetic(
    tiny_site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same construction as bench.runner.pipeline_runner (seed = repeat 0, until the last site
    stage) and the aux cache inside the work directory, never ``~/.cache``."""
    from wintersar.pipeline import api

    seen: dict[str, Any] = {}

    def spy(cfg: Any, **kw: Any) -> Any:
        seen["cache_dir"] = cfg.cache_dir
        seen["workdir"] = cfg.workdir
        seen.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(api, "run", spy)
    with pytest.raises(RuntimeError, match="stop"):
        g.run_golden_pipeline(tiny_site, tmp_path / "w")
    assert seen["cache_dir"] == tmp_path / "w" / "cache"
    assert seen["workdir"] == tmp_path / "w" / "work"
    assert seen["until"] == "geocode"
    assert seen["param_overrides"]["interferogram"]["seed"] == 0
    assert seen["param_overrides"]["interferogram"]["n_dates"] == 4
    assert tiny_site.param_overrides["interferogram"].get("seed") is None  # site untouched


def test_golden_stats_are_bit_identical_across_runs(tiny_site: Site, tmp_path: Path) -> None:
    a, _ = g.compute_golden(tiny_site, tmp_path / "a")
    b, _ = g.compute_golden(tiny_site, tmp_path / "b")
    assert g.dump_golden(a) == g.dump_golden(b)
    c, _ = g.compute_golden(tiny_site, tmp_path / "c", seed=1)
    assert c["seed"] == 1 and g.compare_golden(a, c)  # a different seed changes the values


def test_site_fixed_seed_wins_over_default(tmp_path: Path) -> None:
    spec = json.loads(json.dumps(TINY_SITE))
    spec["param_overrides"]["interferogram"]["seed"] = 7
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    site = load_site(p)
    assert g.golden_seed(site) == 7
    stats, _ = g.compute_golden(site, tmp_path / "w")
    assert stats["seed"] == 7


def test_write_and_load_golden_roundtrip_and_size_guard(tmp_path: Path) -> None:
    stats = {"b": 1.5, "a": {"z": [1, 2], "y": "s"}}
    p = tmp_path / "g" / "stats.json"
    n = g.write_golden(stats, p)
    assert n == len(g.dump_golden(stats).encode()) and p.read_text().endswith("}\n")
    assert g.load_golden(p) == stats
    assert p.read_text(encoding="utf-8").index('"a"') < p.read_text(encoding="utf-8").index('"b"')
    with pytest.raises(ValueError, match="statistics, not arrays"):
        g.write_golden({"arr": list(range(20_000))}, tmp_path / "big.json")
    (tmp_path / "list.json").write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        g.load_golden(tmp_path / "list.json")


def test_forbidden_keys_walks_nested() -> None:
    bad = {"stages": {"unwrap": {"wall_time_s": 1.0}}, "runs": [{"git_sha": "x"}]}
    assert g.forbidden_keys(bad) == ["stages.unwrap.wall_time_s", "runs[0].git_sha"]


# ---------------------------------------------------------------- check / scripts
def test_check_golden_missing_file_is_golden_002(tiny_site: Site, tmp_path: Path) -> None:
    res = g.check_golden(tiny_site, tmp_path / "nope.json", tmp_path / "w")
    assert not res.ok and [f.rule_id for f in res.findings] == ["GOLDEN-002"]
    assert res.stats is None and res.to_dict()["n_leaves"] is None


def test_check_golden_failed_run_is_golden_004(tmp_path: Path) -> None:
    spec = json.loads(json.dumps(TINY_SITE))
    spec["param_overrides"]["unwrap"] = {"fail_stage": "unwrap"}
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    site = load_site(p)
    gp = tmp_path / "stats.json"
    g.write_golden({"x": 1}, gp)
    res = g.check_golden(site, gp, tmp_path / "w")
    f = res.findings[0]
    assert not res.ok and f.rule_id == "GOLDEN-004" and f.params["stage"] == "unwrap"
    assert res.mismatches == []


def test_make_then_check_scripts(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    golden_file = g.golden_path(repo, "tiny")
    assert g.main_make(["--lang", "en"], repo=repo) == 0
    assert golden_file.exists() and golden_file.stat().st_size < g.MAX_GOLDEN_BYTES
    out = capsys.readouterr().out
    assert "Golden statistics written" in out
    # check: identical → 0, with the JSON envelope
    assert g.main_check(["--json"], repo=repo) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] and env["command"] == "check-golden" and env["data"]["n_mismatches"] == 0
    assert env["data"]["n_leaves"] == g.count_leaves(g.load_golden(golden_file))
    # make --check is the same gate without writing
    before = golden_file.read_text(encoding="utf-8")
    assert g.main_make(["--check", "--lang", "en"], repo=repo) == 0
    assert golden_file.read_text(encoding="utf-8") == before
    assert "Golden statistics match" in capsys.readouterr().out
    # a tampered golden → 1 with GOLDEN-001, markdown written, regenerate hint printed
    data = g.load_golden(golden_file)
    data["velocity"]["mean"] *= 2.0
    data["n_pairs"] += 1
    g.write_golden(data, golden_file)
    md = tmp_path / "summary.md"
    assert g.main_check(["--lang", "en", "--markdown", str(md)], repo=repo) == 1
    captured = capsys.readouterr()
    assert "GOLDEN-001" in captured.out and "make_golden.py" in captured.err
    assert "velocity.mean" in md.read_text(encoding="utf-8")
    assert g.main_make(["--check", "--json"], repo=repo) == 1
    env = json.loads(capsys.readouterr().out)
    assert not env["ok"] and {m["path"] for m in env["data"]["mismatches"]} == {
        "velocity.mean",
        "n_pairs",
    }
    # regenerating reports what changed relative to the tampered file and restores it
    assert g.main_make(["--lang", "en"], repo=repo) == 0
    out = capsys.readouterr().out
    assert "2 values differ" in out and "velocity.mean" in out
    assert golden_file.read_text(encoding="utf-8") == before
    assert g.main_make(["--lang", "en"], repo=repo) == 0
    assert "Identical to the previous golden" in capsys.readouterr().out


def test_site_change_is_golden_003(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert g.main_make(["--json"], repo=repo) == 0
    capsys.readouterr()
    site_path = repo / g.DEFAULT_SITE
    spec = yaml.safe_load(site_path.read_text(encoding="utf-8"))
    spec["param_overrides"]["interferogram"]["n_dates"] = 5
    site_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    assert g.main_check(["--json"], repo=repo) == 1
    env = json.loads(capsys.readouterr().out)
    ids = {f["rule_id"] for f in env["findings"]}
    assert "GOLDEN-003" in ids and "GOLDEN-001" in ids
    site_f = next(f for f in env["findings"] if f["rule_id"] == "GOLDEN-003")
    assert site_f["params"]["path"] == "site.param_overrides.interferogram.n_dates"


def test_scripts_missing_site_is_cli_006_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bad input keeps the ADR-0091 contract: cause → fix on stderr, or under ``--json`` the
    envelope as the only stdout (nightly.yml redirects stdout into golden_check.json)."""
    missing = tmp_path / "missing.yaml"
    assert g.main_check(["--site", str(missing)], repo=tmp_path) == 2
    assert g.main_make(["--site", str(missing)], repo=tmp_path) == 2
    cap = capsys.readouterr()
    assert cap.out == "" and "missing.yaml" in cap.err and "CLI-006" in cap.err
    assert "Traceback" not in cap.err and "FileNotFoundError" not in cap.err
    md = tmp_path / "summary.md"
    argv = ["--json", "--site", str(missing), "--markdown", str(md)]
    assert g.main_check(argv, repo=tmp_path) == 2
    cap = capsys.readouterr()
    env = json.loads(cap.out)
    assert cap.err == "" and not env["ok"] and env["command"] == "check-golden"
    (finding,) = env["findings"]
    assert finding["rule_id"] == "CLI-006" and finding["params"]["option"] == "--site"
    assert "missing.yaml" in finding["params"]["path"] and "CLI-006" in md.read_text("utf-8")
    assert g.main_make(["--json", "--site", str(missing)], repo=tmp_path) == 2
    assert json.loads(capsys.readouterr().out)["command"] == "make-golden"


def test_scripts_unreadable_site_is_golden_006_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """yaml.YAMLError is not a ValueError: a syntactically broken site YAML used to escape
    as a traceback (exit 1); a non-mapping or schema-invalid site is the same finding."""
    broken = tmp_path / "bad.yaml"
    broken.write_text("name: [unclosed\n", encoding="utf-8")
    assert g.main_check(["--json", "--site", str(broken)], repo=tmp_path) == 2
    cap = capsys.readouterr()
    env = json.loads(cap.out)
    assert cap.err == "" and not env["ok"] and env["command"] == "check-golden"
    (finding,) = env["findings"]
    assert finding["rule_id"] == "GOLDEN-006" and "bad.yaml" in finding["params"]["path"]
    assert finding["params"]["option"] == "--site" and finding["params"]["error"]
    not_mapping = tmp_path / "list.yaml"
    not_mapping.write_text("- 1\n", encoding="utf-8")
    assert g.main_make(["--json", "--site", str(not_mapping)], repo=tmp_path) == 2
    env = json.loads(capsys.readouterr().out)
    assert env["command"] == "make-golden" and env["findings"][0]["rule_id"] == "GOLDEN-006"
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("name: x\nstages: not-a-list\n", encoding="utf-8")
    assert g.main_check(["--lang", "en", "--site", str(invalid)], repo=tmp_path) == 2
    cap = capsys.readouterr()
    assert cap.out == "" and "GOLDEN-006" in cap.err and "Traceback" not in cap.err
    assert "Site definition" in cap.err  # translated cause, not a raw key


def test_corrupt_golden_is_golden_005(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A golden that is not JSON / not an object: check → GOLDEN-005 FAIL (exit 1, no
    traceback); make → GOLDEN-005 WARN and the file is regenerated (exit 0)."""
    golden_file = g.golden_path(repo, "tiny")
    golden_file.parent.mkdir(parents=True)
    golden_file.write_text("{bad", encoding="utf-8")
    res = g.check_golden(load_site(repo / g.DEFAULT_SITE), golden_file, tmp_path / "w")
    assert not res.ok and [f.rule_id for f in res.findings] == ["GOLDEN-005"]
    assert res.golden is None and res.stats is None and res.mismatches == []
    assert g.main_check(["--json"], repo=repo) == 1
    env = json.loads(capsys.readouterr().out)
    assert not env["ok"] and [f["rule_id"] for f in env["findings"]] == ["GOLDEN-005"]
    assert (
        env["data"]["n_leaves"] is None
        and "JSONDecodeError" in env["findings"][0]["params"]["error"]
    )
    assert g.main_make(["--check", "--lang", "en"], repo=repo) == 1
    cap = capsys.readouterr()
    assert "GOLDEN-005" in cap.out and "Traceback" not in cap.err
    assert g.main_make(["--json"], repo=repo) == 0
    env = json.loads(capsys.readouterr().out)
    (warning,) = env["findings"]
    assert env["ok"] and warning["rule_id"] == "GOLDEN-005" and warning["severity"] == "WARN"
    assert env["data"]["n_changed"] == 0  # nothing to diff against
    assert g.load_golden(golden_file)["schema_version"] == g.GOLDEN_SCHEMA_VERSION
    assert g.main_check(["--json"], repo=repo) == 0
    capsys.readouterr()
    golden_file.write_text("[1, 2]", encoding="utf-8")  # JSON, but not an object
    assert g.main_make(["--lang", "en"], repo=repo) == 0
    cap = capsys.readouterr()
    assert "GOLDEN-005" in cap.err and "Golden statistics written" in cap.out


def test_unwritable_golden_is_golden_007_exit_1(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    out = blocker / "stats.json"  # parent is a regular file → OSError on mkdir
    assert g.main_make(["--json", "--out", str(out)], repo=repo) == 1
    env = json.loads(capsys.readouterr().out)
    assert not env["ok"] and [f["rule_id"] for f in env["findings"]] == ["GOLDEN-007"]
    assert "stats.json" in env["findings"][0]["params"]["path"]


def test_unexpected_exception_is_cli_001_not_a_traceback(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(g, "compute_golden", boom)
    assert g.main_make(["--json"], repo=repo) == 1
    env = json.loads(capsys.readouterr().out)
    assert not env["ok"] and env["findings"][0]["rule_id"] == "CLI-001"
    assert "kaboom" in env["data"]["error"]
    g.write_golden({"x": 1}, g.golden_path(repo, "tiny"))
    assert g.main_check(["--lang", "en"], repo=repo) == 1
    cap = capsys.readouterr()
    assert "CLI-001" in cap.err and "kaboom" in cap.err and "Traceback" not in cap.err


def test_help_and_mismatch_listing_come_from_the_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 11.6 for the dev scripts: option descriptions and the mismatch lines are
    catalogue keys; ``--lang`` is resolved from argv before argparse renders --help."""
    monkeypatch.setenv("COLUMNS", "500")  # no line wrapping in format_help()
    for lang in ("ko", "en"):
        check_help = g._parser("check_golden.py", check_only=True, lang=lang).format_help()
        make_help = g._parser("make_golden.py", check_only=False, lang=lang).format_help()
        assert t("golden.args.lang", lang) in check_help
        assert t("golden.args.golden", lang, default=str(g.GOLDEN_DIRNAME)) in check_help
        assert t("golden.args.out", lang, default=str(g.GOLDEN_DIRNAME)) in make_help
        assert t("golden.args.check", lang) in make_help and "--check" not in check_help
        assert t("golden.args.check_description", lang) in check_help
    m = [
        g.Mismatch("velocity.mean", "float", 1.0, 2.0, "rtol=1e-06 atol=1e-09"),
        g.Mismatch("n_pairs", "value", 1, 2),
    ]
    ko, en = g.format_mismatches(m, lang="ko"), g.format_mismatches(m, lang="en")
    assert "기대=1.0 실제=2.0 (rtol=1e-06 atol=1e-09)" in ko and "n_pairs: value 기대=1" in ko
    assert "expected=1.0 actual=2.0 (rtol=1e-06 atol=1e-09)" in en and "(exact)" not in en
    assert g.format_mismatches(m, limit=1, lang="en").endswith(t("golden.mismatch.more", "en", n=1))
    assert g._script_lang(["--json", "--lang", "en"]) == "en"
    assert g._script_lang(["--lang=ko"]) == "ko" and g._script_lang([]) is None
    with pytest.raises(SystemExit) as ei:  # argparse rejects an unsupported language
        g.main_check(["--lang", "fr"])
    assert ei.value.code == 2


def test_repo_scripts_are_thin_wrappers() -> None:
    root = Path(__file__).resolve().parents[3]
    for name, entry in (("make_golden.py", "main_make"), ("check_golden.py", "main_check")):
        text = (root / "scripts" / name).read_text(encoding="utf-8")
        assert f"from wintersar.bench.golden import {entry}" in text
        assert "raise SystemExit" in text
