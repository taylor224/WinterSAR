"""Phase 0 foundation tests: schemas, i18n, hashing, masking, config, CLI skeleton."""

from __future__ import annotations

import io
import json
import re
import sys
import tomllib
from datetime import date
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tests.conftest import make_burst
from wintersar import i18n
from wintersar.io.schemas import Finding, Pair, Resources, StackCandidate, sort_findings
from wintersar.pipeline.config import EXAMPLE_CONFIG, Config, load_config
from wintersar.util import hashing, masking

# ------------------------------------------------------------------ schemas


def test_burst_record_roundtrip() -> None:
    b = make_burst("2024-01-01")
    d = b.model_dump(mode="json")
    b2 = type(b).model_validate(d)
    assert b2 == b
    assert b.acquisition_date == date(2024, 1, 1)


def test_pair_requires_positive_temporal_baseline() -> None:
    with pytest.raises(ValueError):
        Pair(reference=date(2024, 1, 13), secondary=date(2024, 1, 1), temporal_baseline_days=-12)
    p = Pair(reference=date(2024, 1, 1), secondary=date(2024, 1, 13), temporal_baseline_days=12)
    assert p.key == "20240101_20240113"


def test_stack_candidate_id() -> None:
    s = StackCandidate(
        relative_orbit=52,
        flight_direction="DESCENDING",
        polarization="VV",
        burst_ids=["052_109903_IW2"],
        dates=[date(2024, 1, 1)],
        coverage_of_aoi=1.0,
    )
    assert s.stack_id == "T052D_VV"


def test_sort_findings_fail_first() -> None:
    fs = [
        Finding(rule_id="SEL-07", severity="INFO", message_key="x"),
        Finding(rule_id="SEL-01", severity="FAIL", message_key="x"),
        Finding(rule_id="SEL-06", severity="WARN", message_key="x"),
    ]
    assert [f.severity for f in sort_findings(fs)] == ["FAIL", "WARN", "INFO"]


def test_resources_add() -> None:
    r = Resources(wall_time_s=10, peak_rss_gb=2, credits=1) + Resources(
        wall_time_s=5, peak_rss_gb=4
    )
    assert r.wall_time_s == 15 and r.peak_rss_gb == 4 and r.credits == 1


# ------------------------------------------------------------------ i18n


def test_i18n_common_keys_exist_in_both_languages() -> None:
    ko = set(i18n.all_keys("ko"))
    en = set(i18n.all_keys("en"))
    assert ko == en, f"ko-only: {sorted(ko - en)[:10]} en-only: {sorted(en - ko)[:10]}"


def test_i18n_translate_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "엔진" in i18n.t("env.ENV-001.cause", "ko", engine="snaphu", install_hint="x")
    assert "Engine" in i18n.t("env.ENV-001.cause", "en", engine="snaphu", install_hint="x")
    assert i18n.t("does.not.exist") == "does.not.exist"
    # missing params do not raise
    assert "{engine}" in i18n.t("env.ENV-001.cause", "en")
    monkeypatch.setenv("WINTERSAR_LANG", "en")
    assert i18n.current_lang() == "en"


def test_i18n_module_files_are_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ko.yaml").write_text("a: {b: 'x'}\n", encoding="utf-8")
    (tmp_path / "ko").mkdir()
    (tmp_path / "ko" / "mod.yaml").write_text("a: {c: 'y'}\nm: {k: '{v}!'}\n", encoding="utf-8")
    monkeypatch.setattr(i18n, "_I18N_DIR", tmp_path)
    i18n.load_catalog.cache_clear()
    try:
        cat = i18n.load_catalog("ko")
        assert cat == {"a.b": "x", "a.c": "y", "m.k": "{v}!"}
        assert i18n.t("m.k", "ko", v=1) == "1!"
    finally:
        i18n.load_catalog.cache_clear()


# ------------------------------------------------------------------ hashing / masking


def test_hash_params_is_order_independent() -> None:
    a = hashing.hash_params({"b": 1, "a": [1, 2], "d": date(2024, 1, 1)})
    b = hashing.hash_params({"a": [1, 2], "d": date(2024, 1, 1), "b": 1})
    assert a == b and len(a) == 16
    assert hashing.hash_params({"b": 2, "a": [1, 2]}) != a


def test_hash_file_and_tree(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc" * 1000)
    h1 = hashing.hash_file(f)
    assert h1 == hashing.hash_file(f)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "y.bin").write_bytes(b"z")
    t1 = hashing.hash_tree(tmp_path)
    (tmp_path / "sub" / "y.bin").write_bytes(b"zz")
    assert hashing.hash_tree(tmp_path) != t1


def test_masking_hides_tokens_and_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARTHDATA_TOKEN", "supersecrettoken123")
    home = str(Path.home())
    txt = f"token=supersecrettoken123 path={home}/work Bearer abcdefghijklmnopqrstuvwxyz0123"
    out = masking.mask_text(txt)
    assert "supersecrettoken123" not in out
    assert home not in out
    assert "abcdefghijklmnopqrstuvwxyz0123" not in out
    assert masking.mask_mapping({"a": [f"{home}/x", {"password": "p"}]})["a"][0] == "~/x"


# ------------------------------------------------------------------ config


def test_example_config_loads(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.project.name == "site-a-subsidence"
    assert cfg.aoi == (tmp_path / "aoi.geojson").resolve()
    assert cfg.workdir == (tmp_path / "work").resolve()
    assert cfg.engine.looks == "auto"
    assert cfg.unwrap.tiles == "auto"


def test_config_stage_params_isolate_sections(tmp_path: Path) -> None:
    raw = yaml.safe_load(EXAMPLE_CONFIG)
    cfg = Config.model_validate(raw)
    unw1 = hashing.hash_params(cfg.stage_params("unwrap"))
    ig1 = hashing.hash_params(cfg.stage_params("interferogram"))
    raw["unwrap"]["coherence_threshold"] = 0.4
    cfg2 = Config.model_validate(raw)
    assert hashing.hash_params(cfg2.stage_params("unwrap")) != unw1
    assert hashing.hash_params(cfg2.stage_params("interferogram")) == ig1


def test_config_rejects_unknown_keys() -> None:
    raw = yaml.safe_load(EXAMPLE_CONFIG)
    raw["typo_section"] = {}
    with pytest.raises(ValueError):
        Config.model_validate(raw)


def test_config_looks_and_refpoint_lists() -> None:
    raw = yaml.safe_load(EXAMPLE_CONFIG)
    raw["engine"]["looks"] = [5, 1]
    raw["timeseries"]["reference_point"] = [37.5, 127.0]
    cfg = Config.model_validate(raw)
    assert cfg.engine.looks == (5, 1)
    assert cfg.timeseries.reference_point == (37.5, 127.0)


# ------------------------------------------------------------------ CLI


def test_cli_help_and_version() -> None:
    from wintersar.cli import app

    r = CliRunner().invoke(app, ["--help"])
    assert r.exit_code == 0, r.output
    assert "check-install" in r.output
    r = CliRunner().invoke(app, ["--json", "version"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["data"]["version"]


def test_cli_check_install_reports_missing_engines_as_findings() -> None:
    from wintersar.cli import app

    r = CliRunner().invoke(app, ["--json", "check-install"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["command"] == "check-install"
    assert "engines" in payload["data"]
    ids = {f["rule_id"] for f in payload["findings"]}
    # In this environment no external engine is installed -> ENV-001 must appear
    # once the engine adapters are registered (fake engine is always available).
    assert all(f["message_key"] for f in payload["findings"])
    assert ids <= {"ENV-001", "ENV-002", "ENV-003", "ENV-004", "ENV-005", "ENV-006"}


def test_cli_init_writes_config(tmp_path: Path) -> None:
    from wintersar.cli import app

    p = tmp_path / "config.yaml"
    r = CliRunner().invoke(app, ["init", str(p)])
    assert r.exit_code == 0, r.output
    assert load_config(p).project.name


def test_masking_hides_env_var_shaped_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 11.11: a log excerpt must not leak ``EARTHDATA_TOKEN=…`` shaped secrets.

    The value is *not* set in this process: engine wrappers (`set -x`, `env`, SDK debug
    dumps) print these lines and `wintersar diagnose` masks them later, from another shell.
    """
    for var in ("EARTHDATA_TOKEN", "HYP3_TOKEN", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    secret = "s3cr3tOpaqueTokenValue1234567890"
    lines = [
        f"+ export EARTHDATA_TOKEN={secret}",
        f"env: HYP3_TOKEN={secret}",
        f"AWS_SECRET_ACCESS_KEY={secret}",
        f"HYP3_TOKEN: {secret}",
        f"hyp3 --token {secret} --submit",
        f"os.environ['EARTHDATA_TOKEN'] = '{secret}'",
        f'{{"EARTHDATA_TOKEN": "{secret}"}}',
        f"Authorization: Bearer {secret}",
        f"machine urs.earthdata.nasa.gov login u password {secret}",
        f"https://u:{secret}@example.invalid/x",
    ]
    for line in lines:
        out = masking.mask_text(line)
        assert secret not in out, line
        assert "***" in out


def test_masking_leaves_ordinary_log_lines_alone() -> None:
    for line in (
        "stage=unwrap status=ok n_pairs=12 coherence_threshold=0.3",
        "ERROR: Exceeded maximum number of secondary nodes",
        "runconfig: worker_settings: n_workers: 4",
        "간섭도 12개 처리 완료 (masked_fraction=0.16)",
        "key: value",
    ):
        assert masking.mask_text(line) == line


def test_mask_mapping_masks_values_stored_under_a_secret_key() -> None:
    masked = masking.mask_mapping(
        {"token": "abcdefgh", "api_key": "k", "n_secrets": 3, "log_path": "/x/y"}
    )
    assert masked["token"] == "***"
    assert masked["api_key"] == "***"
    assert masked["n_secrets"] == 3
    assert masked["log_path"] == "/x/y"


def test_findings_keep_numpy_values_serialisable() -> None:
    """A rule that computes with numpy must not crash the serializers (io/schemas)."""
    import numpy as np

    from wintersar.io.schemas import Artifact, StageRecord
    from wintersar.util.output import emit_json

    f = Finding(
        rule_id="SEL-99",
        severity="WARN",
        message_key="x",
        params={"coverage": np.float32(0.5)},
        evidence={"n": np.int64(3), "hist": np.arange(3), "nested": {"ok": np.bool_(True)}},
    )
    assert f.evidence == {"n": 3, "hist": [0, 1, 2], "nested": {"ok": True}}
    assert f.model_dump(mode="json")["params"] == {"coverage": pytest.approx(0.5)}
    rec = StageRecord(stage="unwrap", node_hash="h", extra={"masked": np.float64(0.25)})
    assert rec.model_dump(mode="json")["extra"] == {"masked": 0.25}
    art = Artifact(name="unw", path=Path("unw.npz"), meta={"n_pairs": np.int32(2)})
    assert art.model_dump(mode="json")["meta"] == {"n_pairs": 2}
    buf = io.StringIO()
    emit_json("t", {"a": np.int64(1)}, [f], stream=buf)
    assert json.loads(buf.getvalue())["findings"][0]["evidence"]["n"] == 3


def test_cli_init_refuses_existing_file_with_catalog_text_and_envelope(tmp_path: Path) -> None:
    """Rule 11.6: no hard-coded English; ``--json`` always gets an envelope."""
    from wintersar.cli import app

    p = tmp_path / "config.yaml"
    p.write_text("project: {name: x}\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["--json", "init", str(p)])
    assert r.exit_code == 1
    payload = json.loads(r.stdout)
    assert payload["ok"] is False
    assert [f["rule_id"] for f in payload["findings"]] == ["CLI-002"]
    assert payload["findings"][0]["message_key"] == "cli.CLI-002.cause"
    for lang in ("ko", "en"):
        assert i18n.has_key("cli.CLI-002.cause", lang) and i18n.has_key("cli.CLI-002.fix", lang)
    r = CliRunner().invoke(app, ["--lang", "en", "init", str(p)])
    assert r.exit_code == 1
    assert "exists" in r.output and "--force" in r.output


def test_cli_check_install_strict_gates_on_fail_findings() -> None:
    """Default stays 0 (report command); ``--strict`` makes the exit code match ``ok``."""
    from wintersar.cli import app

    r = CliRunner().invoke(app, ["--json", "check-install"])
    assert r.exit_code == 0, r.output
    ok = json.loads(r.stdout)["ok"]
    assert ok is False  # no external engine is installed in this environment
    r = CliRunner().invoke(app, ["--json", "check-install", "--strict"])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["ok"] is False
    r = CliRunner().invoke(app, ["--json", "check-install", "--strict", "--engine", "fake"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["ok"] is True


def test_cli_main_converts_escaped_exceptions_into_the_output_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exception that escapes a sub-command must not end as a raw traceback.

    Typer re-raises it (``Typer.__call__``), so ``main`` has to emit the envelope itself;
    the message is masked (rule 11.11) and ``-v`` still gives the traceback.
    """
    from wintersar import cli

    def _boom() -> None:
        raise ValueError(f"--from 'unwrap' is after --until 'search' ({Path.home()}/c.yaml)")

    monkeypatch.setattr(cli, "app", _boom)
    monkeypatch.setattr(cli.state, "json", True)
    monkeypatch.setattr(cli.state, "verbose", 0)
    monkeypatch.setattr(sys, "argv", ["wintersar", "--json", "plan", "--from", "unwrap"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["command"] == "plan"
    assert [f["rule_id"] for f in payload["findings"]] == ["CLI-001"]
    assert str(Path.home()) not in payload["data"]["error"]
    for lang in ("ko", "en"):
        assert i18n.has_key("cli.CLI-001.cause", lang) and i18n.has_key("cli.CLI-001.fix", lang)
    monkeypatch.setattr(cli.state, "verbose", 1)
    with pytest.raises(ValueError):
        cli.main()


# ------------------------------------------------------------------ packaging / CI (Phase 0 DoD)

_ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_copies_only_paths_that_exist_in_the_build_context() -> None:
    """``docker build`` on a clean checkout fails on an empty/absent COPY source.

    git cannot track an empty directory, so ``COPY <empty dir>`` aborts the CI docker job.
    """
    for line in _ROOT.joinpath("Dockerfile").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY ") or "--from=" in stripped:
            continue
        for src in stripped.split()[1:-1]:
            path = _ROOT / src
            assert path.exists(), f"Dockerfile COPY {src}: not in the build context"
            if path.is_dir():
                assert any(p.is_file() for p in path.rglob("*")), (
                    f"Dockerfile COPY {src}: directory is empty, git cannot track it"
                )


def test_ci_runs_the_mocked_engine_tests_and_does_not_claim_a_bench_gate() -> None:
    """Plan §3.3: the adapter contract tests must run in CI, not be deselected."""
    ci = yaml.safe_load(_ROOT.joinpath(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = ci["jobs"]["lint-test"]["steps"]
    markers = pytest_step = None
    for step in steps:
        run = step.get("run", "")
        if "pytest" in run:
            pytest_step = step
            m = re.search(r'-m\s+"([^"]+)"', run)
            assert m, run
            markers = [part.strip() for part in m.group(1).split(" and ")]
    assert pytest_step is not None and markers is not None
    assert "not engine" not in markers, "CI must run the stub-driven adapter contract tests"
    assert "not engine_real" in markers
    declared = tomllib.loads(_ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8"))
    names = {m.split(":")[0] for m in declared["tool"]["pytest"]["ini_options"]["markers"]}
    assert {"engine", "engine_real"} <= names  # --strict-markers
    bench = next(s for s in steps if "wintersar --json bench" in s.get("run", ""))
    if "--compare" not in bench["run"]:
        # rule 11.8 / open question #58: no baseline is committed yet, so the step may not
        # advertise a regression guard it does not implement.
        assert "guard" not in bench.get("name", "").lower()
