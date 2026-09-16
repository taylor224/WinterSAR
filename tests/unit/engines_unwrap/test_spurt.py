"""spurt adapter: absence (ENV-001), stack-only contract (UNW-009), CLI command builder
(spurt-emcf flags, ADR-0025), subprocess run() with a fake executable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wintersar.engines import spurt as sp
from wintersar.engines._unwrap_common import EngineRunError, StackOnlyEngineError, UnwrapError
from wintersar.engines.base import EngineNotAvailableError, get_engine
from wintersar.io.schemas import Artifact, Artifacts

pytestmark = pytest.mark.engine


def _engine() -> sp.SpurtEngine:
    eng = get_engine("spurt")
    assert isinstance(eng, sp.SpurtEngine)
    return eng


def test_absent_engine_reports_env_001(engines_absent: None, tmp_path: Path) -> None:
    eng = _engine()
    assert eng.detect_version() is None
    assert [f.rule_id for f in eng.check_install()] == ["ENV-001"]
    with pytest.raises(EngineNotAvailableError):
        eng.run("unwrap", Artifacts(), {"_out_dir": str(tmp_path)}, tmp_path / "logs")
    assert sp.find_spurt_command({}) is None


def test_class_metadata() -> None:
    eng = _engine()
    assert eng.name == "spurt" and eng.stages == ("unwrap",) and eng.stack_only is True
    assert eng.version_constraint == ">=0.1,<1"
    assert eng.install_hint.startswith("pip install spurt")
    assert eng.license_note.startswith("BSD-3-Clause OR Apache-2.0")


def test_single_igram_unwrap_is_stack_only_error(engines_absent: None, pair) -> None:
    eng = _engine()
    with pytest.raises(StackOnlyEngineError) as ei:
        eng.unwrap(pair.wrapped, pair.coherence, pair.mask, {})
    assert isinstance(ei.value, NotImplementedError)
    assert ei.value.finding.rule_id == "UNW-009"
    assert "spurt" in str(ei.value)


def test_build_spurt_command_flags(tmp_path: Path) -> None:
    cmd = sp.build_spurt_command(
        ["spurt-emcf"],
        tmp_path / "in",
        tmp_path / "out",
        tmp_path / "tmp",
        {
            "coherence_threshold": 0.3,  # spatial threshold: must NOT become --coh
            "_cores": 8,
            "spurt": {
                "temporal_coherence_threshold": 0.7,
                "s_workers": 2,
                "batchsize": 1000,
                "t_cost_type": "distance",
                "t_cost_scale": 50,
                "pts_per_tile": 100000,
                "max_tiles": 9,
                "merge_parallel_ifgs": 2,
                "unwrap_parallel_tiles": 3,
                "singletile": True,
            },
        },
        tmp_path / "spurt.log",
    )
    assert cmd[:7] == [
        "spurt-emcf",
        "-i",
        str(tmp_path / "in"),
        "-o",
        str(tmp_path / "out"),
        "--tempdir",
        str(tmp_path / "tmp"),
    ]
    rest = cmd[7:]
    pairs = dict(zip(rest[0::2], rest[1::2], strict=False))
    assert pairs["--t-workers"] == "8"  # from _cores
    assert pairs["--s-workers"] == "2"
    assert pairs["--batchsize"] == "1000"
    assert pairs["--coh"] == "0.7"
    assert pairs["--t-cost-type"] == "distance"
    assert pairs["--t-cost-scale"] == "50"
    assert pairs["--pts-per-tile"] == "100000"
    assert pairs["--max-tiles"] == "9"
    assert pairs["--merge-parallel-ifgs"] == "2"
    assert pairs["--unwrap-parallel-tiles"] == "3"
    assert "--singletile" in rest
    assert rest[-2:] == ["--log-file", str(tmp_path / "spurt.log")]
    assert "0.3" not in cmd


def test_build_spurt_command_minimal_and_validation(tmp_path: Path) -> None:
    cmd = sp.build_spurt_command(["x"], tmp_path, tmp_path / "o", tmp_path / "t", {})
    assert cmd == [
        "x",
        "-i",
        str(tmp_path),
        "-o",
        str(tmp_path / "o"),
        "--tempdir",
        str(tmp_path / "t"),
    ]
    with pytest.raises(ValueError, match="t_cost_type"):
        sp.build_spurt_command(["x"], tmp_path, tmp_path, tmp_path, {"t_cost_type": "bogus"})


def test_validate_phase_linked_dir(tmp_path: Path, phase_linked_dir: Path) -> None:
    assert sp.validate_phase_linked_dir(phase_linked_dir)["n_int_files"] == 2
    with pytest.raises(UnwrapError) as ei:
        sp.validate_phase_linked_dir(tmp_path / "nope")
    assert ei.value.finding.rule_id == "UNW-010"
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(UnwrapError):
        sp.validate_phase_linked_dir(empty)


def test_run_with_fake_executable(
    fake_spurt_exe: Path, phase_linked_dir: Path, tmp_path: Path
) -> None:
    eng = _engine()
    assert sp.find_spurt_command({}) == [str(fake_spurt_exe)]
    out = tmp_path / "unwrap"
    inputs = Artifacts().add(Artifact(name="phase_linked_stack", path=phase_linked_dir, kind="dir"))
    params = {
        "unwrap": {"method": "spurt"},
        "spurt": {"temporal_coherence_threshold": 0.65},
        "_out_dir": str(out),
        "_cores": 4,
    }
    arts = eng.run("unwrap", inputs, params, out / "logs")
    assert set(arts.items) == {"unw_stack", "unw_stats"}
    stack_dir = arts["unw_stack"].path
    assert stack_dir == out / "emcf" and arts["unw_stack"].kind == "dir"
    argv = json.loads((stack_dir / "argv.json").read_text())
    assert argv[:2] == ["-i", str(phase_linked_dir)]
    assert "--coh" in argv and argv[argv.index("--coh") + 1] == "0.65"
    assert argv[argv.index("--t-workers") + 1] == "4"
    assert argv[-2:] == ["--log-file", str(out / "logs" / "spurt-emcf.log")]
    assert (out / "logs" / "spurt-emcf.log").exists()
    log = (out / "logs" / "spurt.log").read_text()
    assert "spurt-emcf start" in log and "fake spurt-emcf done" in log and "cmd:" in log
    stats = json.loads(arts["unw_stats"].path.read_text())
    assert stats["engine"] == "spurt" and stats["n_int_files"] == 2 and stats["n_outputs"] >= 1
    assert stats["wall_time_s"] >= 0


def test_run_requires_phase_linked_stack(fake_spurt_exe: Path, tmp_path: Path) -> None:
    eng = _engine()
    with pytest.raises(UnwrapError) as ei:
        eng.run("unwrap", Artifacts(), {"_out_dir": str(tmp_path)}, tmp_path / "logs")
    assert ei.value.finding.rule_id == "UNW-010"


def test_run_failure_raises_engine_run_error(fake_spurt_exe: Path, tmp_path: Path) -> None:
    eng = _engine()
    d = tmp_path / "linked"
    d.mkdir()
    (d / "a.int.tif").write_bytes(b"x")
    (d / "temporal_coherence.tif").write_bytes(b"x")
    inputs = Artifacts().add(Artifact(name="phase_linked_stack", path=d, kind="dir"))
    # make the fake executable fail: remove the input dir after validation is impossible,
    # so instead point spurt at a bogus temp dir via a non-writable path -> use an exe that fails
    bad = tmp_path / "bin" / "spurt-fail"
    bad.write_text("#!/bin/sh\necho boom >&2\nexit 7\n")
    bad.chmod(0o755)
    with pytest.raises(EngineRunError) as ei:
        eng.run(
            "unwrap",
            inputs,
            {"_out_dir": str(tmp_path / "o"), "spurt_executable": str(bad)},
            tmp_path / "logs",
        )
    f = ei.value.finding
    assert f.rule_id == "UNW-003" and f.params["returncode"] == 7
    assert "boom" in (tmp_path / "logs" / "spurt.log").read_text()


def test_detect_version_via_executable(
    fake_spurt_exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: str(fake_spurt_exe) if name == "spurt-emcf" else None
    )
    eng = _engine()
    assert eng.detect_version() == "0.1.1"
    assert eng.check_install() == []
