"""Every error path of every CLI keeps the output contract (CLAUDE.md, ADR-0091).

Under ``--json`` a broken input yields exactly one envelope on stdout (``ok: false``, a
finding with a ``CLI-xxx`` / module rule id whose cause and fix keys exist in both
languages) and exit 2 (usage) or 1 (action failed); never a traceback. Without ``--json``
the same run renders translated text in ``ko`` and ``en`` with no raw i18n key in it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tests.conftest import make_burst
from wintersar.cli import app
from wintersar.i18n import SUPPORTED, has_key

runner = CliRunner()
ENVELOPE_KEYS = {"ok", "command", "data", "findings"}
RAW_KEY = re.compile(r"[a-z_]+\.[A-Za-z0-9_-]+\.(cause|fix)")
Case = tuple[str, Callable[[Path], list[str]], str | None, int]


@pytest.fixture
def ws(tmp_path: Path, aoi_geojson: Path) -> Path:
    """A workspace with a valid config, a candidates file, an empty grid and a bad config."""
    cfg = {
        "project": {"name": "t", "workdir": str(tmp_path / "work"), "language": "ko"},
        "aoi": str(aoi_geojson),
        "time_range": {"start": "2024-01-01", "end": "2024-12-31"},
        "data": {"polarization": "VV"},
        "engine": {"interferogram": "fake"},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("project: {name: x}\n", encoding="utf-8")
    (tmp_path / "candidates.json").write_text(
        json.dumps({"records": [make_burst("2024-01-01").model_dump(mode="json")]}),
        encoding="utf-8",
    )
    (tmp_path / "empty.json").write_text(json.dumps({"records": []}), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "grid.yaml").write_text("grid: {}\n", encoding="utf-8")
    (tmp_path / "ts.npz").write_bytes(b"")
    (tmp_path / "logs").mkdir()
    return tmp_path


def _cfg(p: Path) -> str:
    return str(p / "config.yaml")


# (id, argv builder, expected rule id or None, expected exit code)
CASES: list[Case] = [
    ("root-unknown-option", lambda p: ["--bogus"], "CLI-003", 2),
    ("init-exists", lambda p: ["init", _cfg(p)], "CLI-002", 1),
    ("search-config-missing", lambda p: ["search", "--config", str(p / "nope.yaml")], "CLI-004", 2),
    ("search-config-invalid", lambda p: ["search", "--config", str(p / "bad.yaml")], "CLI-005", 2),
    (
        "precheck-candidates-missing",
        lambda p: ["precheck", str(p / "nope.json"), "--config", _cfg(p)],
        "CLI-006",
        2,
    ),
    (
        "precheck-candidates-broken",
        lambda p: ["precheck", str(p / "broken.json"), "--config", _cfg(p)],
        "CLI-007",
        2,
    ),
    (
        "precheck-candidates-empty",
        lambda p: ["precheck", str(p / "empty.json"), "--config", _cfg(p)],
        "CLI-009",
        2,
    ),
    (
        "precheck-bad-baseline",
        lambda p: ["precheck", str(p / "candidates.json"), "--config", _cfg(p), "--baseline", "x"],
        "CLI-008",
        2,
    ),
    (
        "plan-config-missing",
        lambda p: ["plan", "--config", str(p / "nope.yaml")],
        "PIPELINE-014",
        2,
    ),
    (
        "plan-bad-stage",
        lambda p: ["plan", "--config", _cfg(p), "--until", "nope"],
        "PIPELINE-014",
        2,
    ),
    ("run-bad-set", lambda p: ["run", "--config", _cfg(p), "--set", "novalue"], "PIPELINE-014", 2),
    ("cache-no-args", lambda p: ["cache"], "CLI-003", 2),
    (
        "cache-ls-config-missing",
        lambda p: ["cache", "ls", "--config", str(p / "nope.yaml")],
        "PIPELINE-014",
        2,
    ),
    ("unwrap-no-args", lambda p: ["unwrap"], "CLI-003", 2),
    ("unwrap-plan-missing-shape", lambda p: ["unwrap", "plan"], "CLI-003", 2),
    (
        "unwrap-plan-bad-method",
        lambda p: ["unwrap", "plan", "--shape", "8", "8", "--method", "x"],
        "CLI-008",
        2,
    ),
    (
        "unwrap-plan-bad-tiles",
        lambda p: ["unwrap", "plan", "--shape", "8", "8", "--tiles", "ab"],
        "CLI-008",
        2,
    ),
    (
        "unwrap-run-input-missing",
        lambda p: ["unwrap", "run", str(p / "gone.npz"), "--out", str(p / "o")],
        "UNW-005",
        1,
    ),
    ("diagnose-no-path", lambda p: ["diagnose"], "CLI-003", 2),
    ("diagnose-path-missing", lambda p: ["diagnose", str(p / "nope.log")], "CLI-006", 2),
    (
        "diagnose-bad-engine",
        lambda p: ["diagnose", str(p / "logs"), "--engine", "gamma"],
        "CLI-008",
        2,
    ),
    ("validate-missing-ts", lambda p: ["validate"], "CLI-003", 2),
    ("validate-ts-missing", lambda p: ["validate", "--ts", str(p / "nope.npz")], "CLI-006", 2),
    (
        "validate-bad-method",
        lambda p: ["validate", "--ts", str(p / "ts.npz"), "--method", "x"],
        "CLI-008",
        2,
    ),
    ("refpoint-ts-missing", lambda p: ["refpoint", "--ts", str(p / "nope.npz")], "CLI-006", 2),
    (
        "refpoint-dem-missing",
        lambda p: ["refpoint", "--ts", str(p / "ts.npz"), "--dem", str(p / "no.npy")],
        "CLI-006",
        2,
    ),
    (
        "sweep-grid-missing",
        lambda p: ["sweep", "--config", _cfg(p), "--grid", str(p / "nope.yaml")],
        "CLI-006",
        2,
    ),
    (
        "sweep-config-missing",
        lambda p: ["sweep", "--config", str(p / "nope.yaml"), "--grid", str(p / "grid.yaml")],
        "CLI-004",
        2,
    ),
    (
        "closure-igrams-missing",
        lambda p: ["closure", "--igrams", str(p / "nope.npz")],
        "CLI-006",
        2,
    ),
    ("bench-site-missing", lambda p: ["bench", "--site", str(p / "nope.yaml")], "BENCH-004", 2),
    (
        "bench-bad-runner",
        lambda p: ["bench", "--site", str(p / "nope.yaml"), "--runner", "warp"],
        "CLI-008",
        2,
    ),
    ("research-no-args", lambda p: ["research"], "CLI-003", 2),
    (
        "research-experiment-unknown",
        lambda p: ["research", "experiment", "nope", "--out", str(p / "x")],
        "RES-010",
        1,
    ),
    (
        "research-synth-bad-kind",
        lambda p: ["research", "synth", "--out", str(p / "s.npz"), "--kind", "x"],
        "RES-006",
        1,
    ),
    (
        "research-repr-input-missing",
        lambda p: [
            "research",
            "repr-phase",
            "--igram",
            str(p / "no.npz"),
            "--out",
            str(p / "r.npz"),
        ],
        None,
        1,
    ),
    (
        "research-stitch-input-missing",
        lambda p: ["research", "stitch", "--tiles", str(p / "no.npz"), "--out", str(p / "m.npz")],
        None,
        1,
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_json_error_path_is_one_envelope(ws: Path, case: Case) -> None:
    _, build, rule_id, code = case
    r = runner.invoke(app, ["--json", *build(ws)])
    assert r.exit_code == code, (r.exit_code, r.output)
    assert "Traceback" not in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit), repr(r.exception)
    stdout = r.stdout.strip()
    assert stdout.startswith("{") and stdout.endswith("}"), r.output
    payload = json.loads(stdout)  # exactly one JSON document
    assert set(payload) == ENVELOPE_KEYS and payload["ok"] is False
    first_word = next((a for a in build(ws) if not a.startswith("-")), "wintersar")
    assert payload["command"].split()[0] == first_word, payload["command"]
    assert r.stderr == "", r.stderr  # nothing else on the machine channel
    ids = [f["rule_id"] for f in payload["findings"]]
    if rule_id is not None:
        assert rule_id in ids, ids
    for f in payload["findings"]:
        assert f["severity"] == "FAIL"
        for lang in SUPPORTED:
            assert has_key(f["message_key"], lang), (f["message_key"], lang)
            assert f["fix_key"] and has_key(f["fix_key"], lang), (f["fix_key"], lang)


@pytest.mark.parametrize("lang", ["ko", "en"])
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_text_error_path_is_translated(ws: Path, case: Case, lang: str) -> None:
    _, build, _rule, code = case
    r = runner.invoke(app, ["--lang", lang, *build(ws)])
    assert r.exit_code == code, (r.exit_code, r.output)
    assert "Traceback" not in r.output
    assert not r.stdout.strip().startswith("{"), r.stdout  # no envelope without --json
    raw = RAW_KEY.findall(r.output)
    assert not raw, (RAW_KEY.search(r.output), r.output)


def test_unexpected_exception_inside_a_command_becomes_cli_001(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wintersar.pipeline import api

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError(f"boom {Path.home()}/secret.yaml")

    monkeypatch.setattr(api, "plan", boom)
    r = runner.invoke(app, ["--json", "plan", "--config", _cfg(ws)])
    assert r.exit_code == 1 and r.stderr == "", r.output
    payload = json.loads(r.stdout)
    assert payload["command"] == "plan" and payload["ok"] is False
    assert [f["rule_id"] for f in payload["findings"]] == ["CLI-001"]
    assert str(Path.home()) not in r.stdout  # rule 11.11
    text = runner.invoke(app, ["--lang", "en", "plan", "--config", _cfg(ws)])
    assert text.exit_code == 1 and "Traceback" not in text.output
    assert "CLI-001" not in text.stdout and "Unexpected error" in text.output
    # -v keeps the developer path: the exception propagates
    verbose = runner.invoke(app, ["-v", "plan", "--config", _cfg(ws)])
    assert isinstance(verbose.exception, RuntimeError)


def test_usage_error_command_name_includes_the_group(ws: Path) -> None:
    r = runner.invoke(app, ["--json", "unwrap", "run"])  # missing IGRAMS / --out
    payload = json.loads(r.stdout)
    assert payload["command"] == "unwrap run"
    finding = payload["findings"][0]
    assert finding["rule_id"] == "CLI-003" and finding["scope"] == "unwrap run"
    assert finding["params"]["command"] == "unwrap run" and finding["params"]["detail"]
