"""Every error path of every CLI keeps the output contract (CLAUDE.md, ADR-0091).

Under ``--json`` a broken input yields exactly one envelope on stdout (``ok: false``, a
finding with a ``CLI-xxx`` / module rule id whose cause and fix keys exist in both
languages) and exit 2 (usage) or 1 (action failed); never a traceback. Without ``--json``
the same run renders translated text in ``ko`` and ``en`` with no raw i18n key in it.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from tests.conftest import make_burst
from tests.unit.pipeline._support import write_fake_config
from wintersar.cli import app
from wintersar.i18n import SUPPORTED, has_key, t
from wintersar.io.schemas import Finding
from wintersar.util import output

runner = CliRunner()
ENVELOPE_KEYS = {"ok", "command", "data", "findings"}
RAW_KEY = re.compile(r"[a-z_]+\.[A-Za-z0-9_-]+\.(cause|fix)")
Case = tuple[str, Callable[[Path], list[str]], str | None, int]
SET_SMALL = ["--set", "interferogram.n_dates=5", "--set", "interferogram.shape=[24, 24]"]


def _squash(text: str) -> str:
    """Rich wraps at 80 columns (and folds CJK text anywhere): compare without whitespace."""
    return re.sub(r"\s+", "", text)


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
    (tmp_path / "dir").mkdir()  # a directory where a config file is expected
    return tmp_path


def _cfg(p: Path) -> str:
    return str(p / "config.yaml")


# (id, argv builder, expected rule id or None, expected exit code)
CASES: list[Case] = [
    ("root-unknown-option", lambda p: ["--bogus"], "CLI-003", 2),
    ("init-exists", lambda p: ["init", _cfg(p)], "CLI-002", 1),
    ("check-install-bad-engine", lambda p: ["check-install", "--engine", "nosuch"], "CLI-008", 2),
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
    # --config pointing at a directory (IsADirectoryError) is bad input, not CLI-001
    ("plan-config-dir", lambda p: ["plan", "--config", str(p / "dir")], "PIPELINE-014", 2),
    ("run-config-dir", lambda p: ["run", "--config", str(p / "dir")], "PIPELINE-014", 2),
    ("run-bad-set", lambda p: ["run", "--config", _cfg(p), "--set", "novalue"], "PIPELINE-014", 2),
    ("cache-no-args", lambda p: ["cache"], "CLI-003", 2),
    (
        "cache-ls-config-missing",
        lambda p: ["cache", "ls", "--config", str(p / "nope.yaml")],
        "PIPELINE-014",
        2,
    ),
    (
        "cache-ls-config-dir",
        lambda p: ["cache", "ls", "--config", str(p / "dir")],
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
        # an argument outside its allowed set is bad input: exit 2 like CLI-008 elsewhere
        "research-synth-bad-kind",
        lambda p: ["research", "synth", "--out", str(p / "s.npz"), "--kind", "x"],
        "RES-006",
        2,
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


@pytest.mark.parametrize("argv", [["--lang", "en", "--json", "nosuch"], ["--lang", "en", "--json"]])
def test_group_level_usage_error_keeps_the_envelope_after_a_lang_value(argv: list[str]) -> None:
    """``No such command`` / ``Missing command.`` are raised by ``TyperGroup.invoke`` before
    the root callback parses ``--json``; the argv scan must not stop at the *value* of
    ``--lang`` (it is not a command word)."""
    r = runner.invoke(app, argv)
    assert r.exit_code == 2 and r.stderr == "", r.output
    payload = json.loads(r.stdout)  # exactly one document
    assert set(payload) == ENVELOPE_KEYS and payload["ok"] is False
    assert [f["rule_id"] for f in payload["findings"]] == ["CLI-003"]
    assert payload["command"] == ("nosuch" if "nosuch" in argv else "wintersar")


def test_usage_error_command_name_drops_the_whole_program_name() -> None:
    """``python -m wintersar.cli`` (how the QGIS client starts the CLI) is a three-word
    program name; the envelope's ``command`` must still be the sub-command."""
    prog = "python -m wintersar.cli"
    r = runner.invoke(app, ["--json", "plan", "--lang", "en"], prog_name=prog)
    assert r.exit_code == 2, r.output
    payload = json.loads(r.stdout)
    assert payload["command"] == "plan"
    assert payload["findings"][0]["params"]["command"] == "plan"
    r = runner.invoke(app, ["--json", "--lang", "en"], prog_name=prog)  # missing command
    assert json.loads(r.stdout)["command"] == "wintersar"


# ------------------------------------------------------------------ text-mode rendering


def test_findings_renderers_keep_bracketed_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scopes and catalogue text are data: ``[T052D]`` / ``[dry-run: …]`` must not be taken
    for rich style tags and vanish."""
    buf = io.StringIO()
    monkeypatch.setattr(output, "console", Console(file=buf, width=200))
    monkeypatch.setattr(output, "err_console", Console(file=buf, width=200))
    f = Finding(
        rule_id="CLI-003",
        severity="FAIL",
        message_key="cli.CLI-003.cause",
        fix_key="cli.CLI-003.fix",
        params={"command": "plan", "detail": "[dry-run: nothing deleted]"},
        scope="T052D",
    )
    output.print_findings([f], "en")
    output.print_error_findings([f], "en")
    text = buf.getvalue()
    assert text.count("[T052D]") == 1, text
    assert text.count("[dry-run: nothing deleted]") == 2, text


def test_cache_gc_dry_run_is_labelled_in_text_mode(tmp_path: Path, cache_dir: Path) -> None:
    """``cache gc --dry-run`` must say so (the ``[dry-run: …]`` suffix is not markup)."""
    cfg = str(write_fake_config(tmp_path).config_path)
    assert runner.invoke(app, ["run", "--config", cfg, *SET_SMALL]).exit_code == 0
    again = ["run", "--config", cfg, *SET_SMALL, "--set", "unwrap.coherence_threshold=0.5"]
    assert runner.invoke(app, again).exit_code == 0

    def n_entries() -> int:
        r = runner.invoke(app, ["--json", "cache", "ls", "--config", cfg])
        return len(json.loads(r.stdout)["data"]["entries"])

    before = n_entries()
    for lang in SUPPORTED:
        r = runner.invoke(
            app, ["--lang", lang, "cache", "gc", "--config", cfg, "--keep", "1", "--dry-run"]
        )
        assert r.exit_code == 0, r.output
        assert _squash(t("pipeline.cli.gc_dry_suffix", lang)) in _squash(r.output), r.output
    assert n_entries() == before  # nothing was deleted
    r = runner.invoke(app, ["--lang", "en", "cache", "gc", "--config", cfg, "--keep", "1"])
    assert r.exit_code == 0 and "dry-run" not in r.output
    assert n_entries() < before


def test_fully_cached_incremental_run_does_not_claim_pair_work(
    tmp_path: Path, cache_dir: Path
) -> None:
    """The PERF-06 line ("only pairs touching new dates are computed … re-inverted") is
    printed only when a stage really ran with the per-pair cache."""
    cfg = str(write_fake_config(tmp_path).config_path)
    six = ["--set", "interferogram.n_dates=6", "--set", "interferogram.shape=[24, 24]"]
    base = ["--lang", "en", "run", "--config", cfg, "--incremental"]
    assert runner.invoke(app, [*base, *SET_SMALL]).exit_code == 0
    grown = runner.invoke(app, [*base, *six])  # one more date: pairs reused + new ones
    assert grown.exit_code == 0, grown.output
    mode = _squash(t("pipeline.cli.incremental_mode", "en"))
    assert mode in _squash(grown.output)
    cached = runner.invoke(app, [*base, *six])  # identical: every stage is a cache hit
    assert cached.exit_code == 0, cached.output
    flat = _squash(cached.output)
    assert mode not in flat and "pairscached" not in flat, cached.output
    assert _squash("Done: 0 stage(s) ran") in flat


def test_text_error_path_prints_the_cause_once_not_the_raw_exception(ws: Path) -> None:
    """When a finding carries the message, the raw (English) exception text is not printed
    in front of it: text mode is ``ID: cause`` then ``fix`` (ADR-0091)."""
    for lang in SUPPORTED:
        r = runner.invoke(
            app, ["--lang", lang, "research", "synth", "--out", str(ws / "s.npz"), "--kind", "x"]
        )
        assert r.exit_code == 2, r.output
        cause = t(
            "research.RES-006.cause", lang, name="kind", value="x", allowed="igrams | tiles | slc"
        )
        assert _squash(r.stderr).count(_squash(cause)) == 1, r.stderr
        assert r.stderr.lstrip().startswith("RES-006: "), r.stderr
        r = runner.invoke(
            app, ["--lang", lang, "unwrap", "run", str(ws / "gone.npz"), "--out", str(ws / "o")]
        )
        assert r.exit_code == 1, r.output
        assert r.stderr.lstrip().startswith("UNW-005: "), r.stderr
        assert has_key("cli.action_failed", lang)  # the line used when no finding exists
