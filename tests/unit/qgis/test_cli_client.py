"""``wintersar_qgis.cli_client``: envelope parsing, argv builders, env resolution, process
handling (R-12, plan §5.8, ADR-0070/0071). No QGIS needed."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from wintersar_qgis import cli_client as cc
from wintersar_qgis.cli_client import CliResponse, WintersarClient

from wintersar.i18n import load_catalog
from wintersar.pipeline.stages import STAGE_ORDER as CORE_STAGE_ORDER

ENVELOPE = {"ok": True, "command": "version", "data": {"version": "0.1.0.dev0"}, "findings": []}


def _env_text(**overrides: object) -> str:
    return json.dumps({**ENVELOPE, **overrides}, indent=2) + "\n"


# ------------------------------------------------------------------ envelope


def test_parse_envelope_plain() -> None:
    assert cc.parse_envelope(_env_text()) == ENVELOPE


def test_parse_envelope_skips_leading_noise() -> None:
    text = "warning: something\n{not json\n" + _env_text(command="search")
    env = cc.parse_envelope(text)
    assert env is not None and env["command"] == "search"


def test_parse_envelope_rejects_non_envelope_json_and_garbage() -> None:
    assert cc.parse_envelope(json.dumps({"foo": 1})) is None
    assert cc.parse_envelope("") is None
    assert cc.parse_envelope("garbage") is None


def test_stage_order_matches_core() -> None:
    assert list(cc.STAGE_ORDER) == list(CORE_STAGE_ORDER)


# ------------------------------------------------------------------ argv builders


def test_argv_builders_match_cli_signatures(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    assert cc.search_args(cfg) == ["search", "--config", str(cfg)]
    assert cc.precheck_args("c.json", cfg, out="rep", no_fail=True, baseline="orbit") == [
        "precheck",
        "c.json",
        "--config",
        str(cfg),
        "--out",
        "rep",
        "--no-fail",
        "--baseline",
        "orbit",
    ]
    assert cc.plan_args(
        cfg, until="unwrap", from_="fetch", force=["unwrap"], set_=["unwrap.cost=smooth"]
    ) == [
        "plan",
        "--config",
        str(cfg),
        "--until",
        "unwrap",
        "--from",
        "fetch",
        "--force",
        "unwrap",
        "--set",
        "unwrap.cost=smooth",
    ]
    assert cc.run_args(cfg, force=["unwrap", "timeseries"], dry_run=True) == [
        "run",
        "--config",
        str(cfg),
        "--force",
        "unwrap",
        "--force",
        "timeseries",
        "--dry-run",
    ]
    assert cc.diagnose_args("work/logs", engine="snaphu", out="r.md", assume_failed=True) == [
        "diagnose",
        "work/logs",
        "--engine",
        "snaphu",
        "--out",
        "r.md",
        "--assume-failed",
    ]
    assert cc.validate_args("ts.h5", "lev.csv") == [
        "validate",
        "--ts",
        "ts.h5",
        "--leveling",
        "lev.csv",
    ]
    assert cc.validate_args("ts.h5", None, "gnss.csv") == [
        "validate",
        "--ts",
        "ts.h5",
        "--gnss",
        "gnss.csv",
    ]
    assert cc.refpoint_args("ts.h5", "aoi.geojson", 3) == [
        "refpoint",
        "--ts",
        "ts.h5",
        "--aoi",
        "aoi.geojson",
        "--top",
        "3",
    ]
    assert cc.refpoint_args("ts.npz") == ["refpoint", "--ts", "ts.npz", "--top", "5"]
    assert cc.validate_args("ts.h5", "lev.csv", out="rep", radius_m=50) == [
        "validate",
        "--ts",
        "ts.h5",
        "--leveling",
        "lev.csv",
        "--out",
        "rep",
        "--radius",
        "50",
    ]
    assert cc.check_install_args(["fake", "snaphu"]) == [
        "check-install",
        "--engine",
        "fake",
        "--engine",
        "snaphu",
    ]
    assert cc.check_install_args() == ["check-install"]
    assert cc.init_args("c.yaml", force=True) == ["init", "c.yaml", "--force"]
    assert cc.cache_ls_args(workdir="work") == ["cache", "ls", "--workdir", "work"]


def test_argv_builders_validate_inputs() -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        cc.run_args("c.yaml", until="nope")
    with pytest.raises(ValueError, match="unknown stage"):
        cc.plan_args("c.yaml", force=["fetch", "typo"])
    with pytest.raises(ValueError, match="leveling"):
        cc.validate_args("ts.h5")
    with pytest.raises(ValueError, match="top"):
        cc.refpoint_args("ts", "aoi", 0)


# ------------------------------------------------------------------ environment resolution


def _prefix(tmp_path: Path, name: str, *, venv: bool = False, script: bool = True) -> Path:
    p = tmp_path / name
    (p / "bin").mkdir(parents=True)
    (p / "bin" / "python").write_text("", encoding="utf-8")
    if script:
        (p / "bin" / "wintersar").write_text("", encoding="utf-8")
    if venv:
        (p / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    return p


def test_parse_env_hint() -> None:
    assert cc.parse_env_hint(None) is None
    assert cc.parse_env_hint("  ") is None
    assert cc.parse_env_hint("conda:wintersar") == cc.EnvHint("conda", "wintersar")
    assert cc.parse_env_hint("uv:/repo") == cc.EnvHint("uv", "/repo")
    with pytest.raises(ValueError, match="environment hint"):
        cc.parse_env_hint("docker:x")
    with pytest.raises(ValueError, match="environment hint"):
        cc.parse_env_hint("conda")


def test_resolve_explicit_python_wins(tmp_path: Path) -> None:
    py = tmp_path / "python"
    assert cc.resolve_command(py, "conda:whatever") == [str(py), "-m", "wintersar.cli"]


def test_resolve_conda_name_prefers_registered_prefix(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path, "wintersar")
    argv = cc.resolve_command(None, "conda:wintersar", conda_envs=[prefix], which=lambda _n: None)
    assert argv == [str(prefix / "bin" / "python"), "-m", "wintersar.cli"]


def test_resolve_conda_name_falls_back_to_conda_run(tmp_path: Path) -> None:
    argv = cc.resolve_command(
        None,
        "conda:wintersar",
        conda_envs=[],
        which=lambda n: "/usr/bin/conda" if n == "conda" else None,
    )
    assert argv == [
        "/usr/bin/conda",
        "run",
        "-n",
        "wintersar",
        "--no-capture-output",
        "python",
        "-m",
        "wintersar.cli",
    ]


def test_resolve_conda_prefix_and_venv(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path, "envA", venv=True)
    assert cc.resolve_command(None, f"conda:{prefix}", which=lambda _n: None) == [
        str(prefix / "bin" / "python"),
        "-m",
        "wintersar.cli",
    ]
    assert cc.resolve_command(None, f"venv:{prefix}", which=lambda _n: None) == [
        str(prefix / "bin" / "python"),
        "-m",
        "wintersar.cli",
    ]
    assert cc.resolve_command(None, f"venv:{tmp_path / 'missing'}", which=lambda _n: None) is None


def test_resolve_uv_prefers_dot_venv_then_uv_run(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    assert cc.resolve_command(
        None, f"uv:{project}", which=lambda n: "/opt/uv" if n == "uv" else None
    ) == ["/opt/uv", "run", "--project", str(project), "--no-sync", "python", "-m", "wintersar.cli"]
    _prefix(project, ".venv", venv=True)
    assert cc.resolve_command(None, f"uv:{project}", which=lambda _n: None) == [
        str(project / ".venv" / "bin" / "python"),
        "-m",
        "wintersar.cli",
    ]


def test_resolve_pixi_and_python_hint(tmp_path: Path) -> None:
    assert cc.resolve_command(
        None, f"pixi:{tmp_path}", which=lambda n: "/opt/pixi" if n == "pixi" else None
    ) == [
        "/opt/pixi",
        "run",
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
        "python",
        "-m",
        "wintersar.cli",
    ]
    assert cc.resolve_command(None, f"pixi:{tmp_path}", which=lambda _n: None) is None
    assert cc.resolve_command(None, "python:/x/python", which=lambda _n: None) == [
        "/x/python",
        "-m",
        "wintersar.cli",
    ]


def test_resolve_falls_back_to_console_script_or_none() -> None:
    assert cc.resolve_command(
        None, None, which=lambda n: "/usr/local/bin/wintersar" if n == "wintersar" else None
    ) == ["/usr/local/bin/wintersar"]
    assert cc.resolve_command(None, None, which=lambda _n: None) is None


def test_conda_prefixes_reads_environments_txt(tmp_path: Path) -> None:
    f = tmp_path / "environments.txt"
    f.write_text("# comment\n/opt/conda\n\n/opt/conda/envs/wintersar\n", encoding="utf-8")
    assert cc.conda_prefixes(f) == [Path("/opt/conda"), Path("/opt/conda/envs/wintersar")]
    assert cc.conda_prefixes(tmp_path / "missing.txt") == []


def test_discover_environments_finds_wintersar_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".conda").mkdir(parents=True)
    with_script = _prefix(tmp_path, "envs" + "/good", venv=False)
    without = _prefix(tmp_path, "envs" + "/bare", script=False)
    venv = _prefix(tmp_path, "proj/.venv", venv=True)
    (home / ".conda" / "environments.txt").write_text(
        f"{with_script}\n{without}\n", encoding="utf-8"
    )
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    found = cc.discover_environments(home=home, extra_prefixes=[venv])
    labels = {c.label: c for c in found}
    assert set(labels) == {"good", ".venv"}
    assert labels["good"].hint == f"conda:{with_script}"
    assert labels[".venv"].hint == f"venv:{venv}"
    assert labels["good"].python_exe == str(with_script / "bin" / "python")


# ------------------------------------------------------------------ process handling


def test_run_real_cli_version(venv_python: Path) -> None:
    client = WintersarClient(python_exe=venv_python, lang="en")
    resp = client.version()
    assert resp.ok and resp.error is None and resp.exit_code == 0
    assert resp.command == "version" and resp.data["version"]
    assert resp.argv[:3] == [str(venv_python), "-m", "wintersar.cli"]
    assert "--json" in resp.argv and resp.argv[resp.argv.index("--lang") + 1] == "en"


def test_run_real_cli_init_writes_config(venv_python: Path, tmp_path: Path) -> None:
    client = WintersarClient(python_exe=venv_python)
    target = tmp_path / "config.yaml"
    resp = client.init_config(target)
    assert resp.ok and target.exists() and resp.data["path"] == str(target)


def test_run_real_cli_usage_error_names_the_command(venv_python: Path) -> None:
    """A click usage error under ``python -m wintersar.cli`` still yields an envelope whose
    ``command`` is the sub-command, not ``-m wintersar.cli plan`` (CLI-003, ADR-0091)."""
    client = WintersarClient(python_exe=venv_python, lang="en")
    resp = client.run(["plan", "--no-such-option"], timeout=60.0)
    assert resp.error is None and not resp.ok and resp.exit_code == 2, resp.raw_stderr
    assert resp.command == "plan"
    assert [f["rule_id"] for f in resp.findings] == ["CLI-003"]
    assert resp.findings[0]["params"]["command"] == "plan"


def test_cli_not_found_when_python_missing(tmp_path: Path) -> None:
    client = WintersarClient(python_exe=tmp_path / "nope" / "python")
    resp = client.version()
    assert not resp.ok and resp.error == "CLI_NOT_FOUND"
    f = resp.findings[0]
    assert f["rule_id"] == "CLI_NOT_FOUND" and f["severity"] == "FAIL"
    assert (
        f["message_key"] == "qgis.error.CLI_NOT_FOUND.cause"
        and f["fix_key"] == "qgis.error.CLI_NOT_FOUND.fix"
    )
    assert "detail" in f["params"]


def test_cli_not_found_when_nothing_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "_which", lambda _n, extra_dirs=(): None)
    resp = WintersarClient().version()
    assert resp.error == "CLI_NOT_FOUND" and resp.exit_code is None


def test_non_json_output_maps_to_invalid_json_or_cli_error(fake_cli) -> None:
    ok_garbage = fake_cli(stdout="hello\n", stderr="warn 1\nwarn 2\n", exit_code=0, name="p0")
    lines: list[str] = []
    resp = WintersarClient(python_exe=ok_garbage).run(["version"], on_line=lines.append)
    assert resp.error == "INVALID_JSON" and resp.exit_code == 0
    assert lines == ["warn 1", "warn 2"] and resp.raw_stdout.strip() == "hello"
    assert resp.findings[0]["params"]["exit_code"] == 0

    failing = fake_cli(stdout="", stderr="Traceback ...\n", exit_code=3, name="p3")
    resp = WintersarClient(python_exe=failing).run(["run", "--config", "c.yaml"])
    assert resp.error == "CLI_ERROR" and resp.exit_code == 3 and not resp.ok
    assert resp.findings[0]["params"]["command"] == "run"
    assert "Traceback" in resp.raw_stderr


def test_envelope_with_ok_false_and_exit_1_is_not_a_client_error(fake_cli) -> None:
    finding = {
        "rule_id": "SEL-01",
        "severity": "FAIL",
        "message_key": "select.SEL-01.fail",
        "params": {},
        "evidence": {},
        "fix_key": "select.SEL-01.fix",
        "refs": [],
        "scope": "T052D_VV",
    }
    exe = fake_cli(stdout=_env_text(ok=False, command="precheck", findings=[finding]), exit_code=1)
    resp = WintersarClient(python_exe=exe).precheck("c.json", "c.yaml")
    assert resp.error is None and not resp.ok and resp.exit_code == 1
    assert resp.command == "precheck" and resp.n_fail == 1 and resp.n_warn == 0
    assert resp.findings[0]["scope"] == "T052D_VV"


def test_timeout_kills_process(fake_cli) -> None:
    exe = fake_cli(stdout=_env_text(), sleep_s=5.0)
    t0 = time.monotonic()
    resp = WintersarClient(python_exe=exe, timeout=0.5).run(["version"])
    assert resp.error == "TIMEOUT" and time.monotonic() - t0 < 4.0
    assert resp.findings[0]["params"]["timeout_s"] == 0.5


def test_cancel_running_command(fake_cli) -> None:
    exe = fake_cli(stdout=_env_text(), sleep_s=5.0)
    client = WintersarClient(python_exe=exe, timeout=30.0)
    result: list[CliResponse] = []
    th = threading.Thread(target=lambda: result.append(client.run(["run"])))
    th.start()
    deadline = time.monotonic() + 5.0
    while not client.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.cancel()
    th.join(timeout=5.0)
    assert result and result[0].error == "CANCELLED"
    assert not client.running and not client.cancel()


# ------------------------------------------------------- engine grandchildren (process group)


def _spawning_cli(tmp_path: Path, pid_file: Path, sleep_s: int = 30) -> Path:
    """Fake CLI that starts an "engine" the way the adapters do, and records its pid.

    A ``/bin/sh`` script rather than a Python one: it reaches the ``echo $!`` in a
    millisecond, so the engine pid is on disk well before the 1 s timeout under test fires.
    """
    script = tmp_path / "fake_python_spawns_engine"
    script.write_text(
        f"#!/bin/sh\nsleep {sleep_s} &\necho $! > {str(pid_file)!r}\nwait\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not ours any more, but it exists
        return True
    return True


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _read_pid(pid_file: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text().strip())
        time.sleep(0.02)
    raise AssertionError(f"{pid_file} never appeared")


@pytest.fixture
def engine_pid_file(tmp_path: Path) -> Iterator[Path]:
    """``pid_file`` for the fake engine, SIGKILLed on teardown if a test leaks it."""
    pid_file = tmp_path / "engine.pid"
    yield pid_file
    if pid_file.exists() and pid_file.read_text().strip():
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid_file.read_text().strip()), 9)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_run_starts_the_cli_in_its_own_process_group() -> None:
    kwargs = cc.new_process_group_kwargs()
    assert kwargs == {"start_new_session": True}


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_cancel_kills_engine_grandchildren(tmp_path: Path, engine_pid_file: Path) -> None:
    """Cancel must stop SNAPHU/topsStack/MintPy too, not just the CLI pid."""
    exe = _spawning_cli(tmp_path, engine_pid_file)
    client = WintersarClient(python_exe=exe, timeout=60.0)
    result: list[CliResponse] = []
    th = threading.Thread(target=lambda: result.append(client.run(["run"])))
    th.start()
    engine_pid = _read_pid(engine_pid_file)
    assert _alive(engine_pid)
    assert client.cancel()
    th.join(timeout=15.0)
    assert result and result[0].error == "CANCELLED"
    assert _wait_gone(engine_pid), f"engine pid {engine_pid} survived cancel (orphaned)"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_terminate_process_tree_kills_engine_grandchildren(
    tmp_path: Path, engine_pid_file: Path
) -> None:
    """The kill path the Cancel button and the timeout share, without racing a timer."""
    exe = _spawning_cli(tmp_path, engine_pid_file)
    proc: subprocess.Popen[str] = subprocess.Popen(
        [str(exe)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **cc.new_process_group_kwargs(),
    )
    try:
        engine_pid = _read_pid(engine_pid_file)
        assert _alive(engine_pid)
        assert cc.terminate_process_tree(proc)
        proc.wait(timeout=15.0)
        assert _wait_gone(engine_pid), f"engine pid {engine_pid} survived (orphaned)"
    finally:
        if proc.poll() is None:  # pragma: no cover - only if the kill failed
            proc.kill()
            proc.wait()


def test_timeout_path_terminates_the_whole_tree(fake_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout must go through ``terminate_process_tree``, not ``Popen.kill``."""
    seen: list[int] = []
    real = cc.terminate_process_tree

    def spy(proc: subprocess.Popen[str], *args: Any, **kwargs: Any) -> bool:
        seen.append(proc.pid)
        return real(proc, *args, **kwargs)

    monkeypatch.setattr(cc, "terminate_process_tree", spy)
    exe = fake_cli(stdout=_env_text(), sleep_s=5.0)
    resp = WintersarClient(python_exe=exe, timeout=0.5).run(["version"])
    assert resp.error == "TIMEOUT"
    assert seen, "the timeout path killed only the CLI pid"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_terminate_process_tree_never_signals_the_callers_own_group() -> None:
    """Without a group of its own only the pid is killed — QGIS must survive a Cancel."""
    proc: subprocess.Popen[str] = subprocess.Popen(  # no new session: shares our group
        [sys.executable, "-c", "import time; time.sleep(30)"], text=True
    )
    try:
        assert cc._own_process_group(proc) is None
        assert cc.terminate_process_tree(proc)
        proc.wait(timeout=10.0)
        assert proc.returncode != 0
    finally:
        if proc.poll() is None:  # pragma: no cover - only if the kill failed
            proc.kill()
            proc.wait()


def test_helper_methods_build_expected_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(self: WintersarClient, args, timeout=None, on_line=None, **kw) -> CliResponse:
        calls.append(list(args))
        return CliResponse(ok=True, command=args[0], data={}, findings=[])

    monkeypatch.setattr(WintersarClient, "run", fake_run)
    c = WintersarClient(python_exe=sys.executable)
    c.search("c.yaml")
    c.plan("c.yaml", until="unwrap")
    c.run_pipeline("c.yaml", from_="unwrap", force=["unwrap"])
    c.diagnose("work/logs", engine="mintpy")
    c.validate("ts.h5", "lev.csv", "gnss.csv")
    c.refpoint("ts.h5", "aoi.geojson", 7)
    c.check_install(["fake"])
    assert calls == [
        ["search", "--config", "c.yaml"],
        ["plan", "--config", "c.yaml", "--until", "unwrap"],
        ["run", "--config", "c.yaml", "--from", "unwrap", "--force", "unwrap"],
        ["diagnose", "work/logs", "--engine", "mintpy"],
        ["validate", "--ts", "ts.h5", "--leveling", "lev.csv", "--gnss", "gnss.csv"],
        ["refpoint", "--ts", "ts.h5", "--aoi", "aoi.geojson", "--top", "7"],
        ["check-install", "--engine", "fake"],
    ]


def test_client_error_ids_have_cause_and_fix_in_both_languages() -> None:
    ko, en = load_catalog("ko"), load_catalog("en")
    for error_id in cc.CLIENT_ERROR_IDS:
        for suffix in ("cause", "fix"):
            key = f"qgis.error.{error_id}.{suffix}"
            assert key in ko and key in en, key
    with pytest.raises(ValueError, match="unknown client error"):
        CliResponse.failure("NOPE", "x")
