"""Rule 11.6 on the command line (ADR-0090): every help text comes from the catalogue.

* ``wintersar.util.clihelp.help_lang`` resolves the help language before the app callback
  runs (``--lang`` parsed → ``sys.argv`` scan → ``WINTERSAR_LANG`` → ``ko``).
* Every command / option / argument of the mounted app renders a ``cli_help.*`` key, never
  a literal, and ``i18n/{ko,en}/cli_help.yaml`` carry identical key sets.
* ``--help`` follows ``--lang`` in both languages.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import typer
import yaml
from typer.testing import CliRunner

from wintersar.cli import app
from wintersar.i18n import SUPPORTED, load_catalog, t
from wintersar.util import clihelp
from wintersar.util.clistate import state

runner = CliRunner()
I18N = Path(__file__).resolve().parents[2] / "src" / "wintersar" / "i18n"
OWNED_CLIS = [
    Path(__file__).resolve().parents[2] / "src" / "wintersar" / rel
    for rel in (
        "cli.py",
        "select/cli.py",
        "pipeline/cli.py",
        "unwrap/cli.py",
        "diagnose/cli.py",
        "validate/cli.py",
        "research/cli.py",
        "bench/cli.py",
    )
]
# typer adds these itself (add_completion) and click adds --help: not catalogue texts
TYPER_OWN_PARAMS = {"help", "install_completion", "show_completion"}
HANGUL = re.compile("[가-힣]")


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        else:
            flat[key] = str(v)
    return flat


def _cli_help_file(lang: str) -> dict[str, str]:
    with (I18N / lang / "cli_help.yaml").open(encoding="utf-8") as fh:
        return _flatten(yaml.safe_load(fh))


def _walk(cmd: Any, path: str = "wintersar") -> list[tuple[str, Any]]:
    out = [(path, cmd)]
    for name, sub in getattr(cmd, "commands", {}).items():
        out.extend(_walk(sub, f"{path} {name}"))
    return out


# ------------------------------------------------------------------ help_lang resolution


def test_help_lang_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "lang_explicit", False)
    monkeypatch.setattr(sys, "argv", ["wintersar", "--help"])
    monkeypatch.setenv("WINTERSAR_LANG", "en")
    assert clihelp.help_lang() == "en"
    monkeypatch.delenv("WINTERSAR_LANG")
    assert clihelp.help_lang() == "ko"  # DEFAULT_LANG
    monkeypatch.setattr(sys, "argv", ["wintersar", "--help", "--lang", "en"])
    assert clihelp.help_lang() == "en"
    monkeypatch.setattr(sys, "argv", ["wintersar", "--lang=EN", "plan", "--help"])
    assert clihelp.help_lang() == "en"
    monkeypatch.setattr(sys, "argv", ["wintersar", "--lang", "xx", "--help"])
    monkeypatch.setenv("WINTERSAR_LANG", "ko")
    assert clihelp.help_lang() == "ko"  # unsupported value ignored
    monkeypatch.setattr(state, "lang", "en")
    monkeypatch.setattr(state, "lang_explicit", True)
    assert clihelp.help_lang() == "en"  # a parsed --lang wins over argv/env


def test_lang_from_argv_edge_cases() -> None:
    assert clihelp.lang_from_argv([]) is None
    assert clihelp.lang_from_argv(["--lang"]) is None
    assert clihelp.lang_from_argv(["--lang", "--json"]) is None
    assert clihelp.lang_from_argv(["--json", "--lang", "ko"]) == "ko"
    assert clihelp.lang_from_argv(["--lang=", "--lang", "en"]) == "en"


def test_h_registers_both_languages_and_falls_back_to_the_key() -> None:
    text = clihelp.h("cli_help.plan.help")
    assert clihelp.key_for(text) == "cli_help.plan.help"
    for lang in SUPPORTED:
        assert clihelp.key_for(t("cli_help.plan.help", lang)) == "cli_help.plan.help"
    assert clihelp.key_for("not a help text") is None
    assert clihelp.key_for(None) is None


# ------------------------------------------------------------------ catalogue parity


def test_cli_help_catalogues_have_identical_keys_and_placeholders() -> None:
    ko, en = _cli_help_file("ko"), _cli_help_file("en")
    assert set(ko) == set(en), {
        "ko_only": sorted(set(ko) - set(en)),
        "en_only": sorted(set(en) - set(ko)),
    }
    ph = re.compile(r"\{(\w+)\}")
    for key in ko:
        assert set(ph.findall(ko[key])) == set(ph.findall(en[key])), key
        assert ko[key].strip() and en[key].strip(), key
    # every diagnostic id carries cause and fix in both files (rule 11.6 "cause -> fix")
    ids = {k.split(".")[1] for k in ko if k.startswith("cli.CLI-")}
    assert ids >= {f"CLI-{n:03d}" for n in range(3, 11)}
    for rid in ids:
        assert f"cli.{rid}.cause" in ko and f"cli.{rid}.fix" in ko, rid
    # the merged catalogue sees them, and nothing in cli_help.yaml shadows a root key
    for lang in SUPPORTED:
        merged = load_catalog(lang)
        assert all(k in merged for k in ko)
    assert clihelp.missing_help_keys() == []


# ------------------------------------------------------------------ every help is a key


def test_every_command_and_option_help_comes_from_the_catalogue() -> None:
    root = typer.main.get_command(app)
    seen_keys: set[str] = set()
    for path, cmd in _walk(root):
        assert isinstance(cmd, clihelp.HelpCommand | clihelp.HelpGroup), path
        key = clihelp.key_for(cmd.help)
        assert key and key.startswith("cli_help."), (path, cmd.help)
        seen_keys.add(key)
        for param in cmd.params:
            if param.name in TYPER_OWN_PARAMS:
                continue
            help_text = getattr(param, "help", None)
            assert help_text, f"{path} {param.name}: option without help"
            key = clihelp.key_for(help_text)
            assert key and key.startswith("cli_help."), (path, param.name, help_text)
            seen_keys.add(key)
    assert seen_keys <= clihelp.registered_keys()
    assert clihelp.missing_help_keys() == []


def test_no_literal_help_strings_in_the_cli_sources() -> None:
    literal = re.compile(r"""help\s*=\s*(?:\(\s*)?["'f]""")
    for py in OWNED_CLIS:
        text = py.read_text(encoding="utf-8")
        hits = [m.group(0) for m in literal.finditer(text)]
        assert not hits, (py.name, hits)
        assert "help=h(" in text, py.name


# ------------------------------------------------------------------ rendered --help


@pytest.mark.parametrize(
    "args, needle_ko, needle_en",
    [
        (["--help"], "표준 출력에 JSON", "Emit a JSON envelope"),
        (["plan", "--help"], "DAG 를 구성해", "Build the DAG"),
        (["unwrap", "--help"], "언래핑 전략", "unwrap strategy"),
        (["unwrap", "run", "--help"], "출력 디렉터리", "Output directory"),
        (["research", "synth", "--help"], "난수 시드", "Random seed"),
        (["validate", "--help"], "수준측량 CSV", "Levelling CSV"),
    ],
)
def test_help_follows_lang(args: list[str], needle_ko: str, needle_en: str) -> None:
    ko = runner.invoke(app, ["--lang", "ko", *args])
    en = runner.invoke(app, ["--lang", "en", *args])
    assert ko.exit_code == 0 and en.exit_code == 0, (ko.output, en.output)
    ko_text = re.sub(r"\s+", " ", ko.output)
    en_text = re.sub(r"\s+", " ", en.output)
    assert needle_ko in ko_text, ko.output
    assert needle_en in en_text and not HANGUL.search(en.output), en.output


def test_top_level_help_reads_lang_after_help_from_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``wintersar --help --lang en``: ``--help`` is processed first, so the scan decides."""
    monkeypatch.setattr(sys, "argv", ["wintersar", "--help", "--lang", "en"])
    r = runner.invoke(app, ["--help", "--lang", "en"])
    assert r.exit_code == 0 and "Emit a JSON envelope" in re.sub(r"\s+", " ", r.output)
    assert not HANGUL.search(r.output)


def test_help_without_lang_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["pytest"])
    monkeypatch.setenv("WINTERSAR_LANG", "en")
    assert "Build the DAG" in re.sub(r"\s+", " ", runner.invoke(app, ["plan", "--help"]).output)
    monkeypatch.setenv("WINTERSAR_LANG", "ko")
    assert "DAG 를 구성해" in re.sub(r"\s+", " ", runner.invoke(app, ["plan", "--help"]).output)


def test_lang_does_not_leak_between_invocations(monkeypatch: pytest.MonkeyPatch) -> None:
    """The root callback exports WINTERSAR_LANG; HelpGroup.main puts it back (ADR-0090)."""
    monkeypatch.setenv("WINTERSAR_LANG", "ko")
    assert runner.invoke(app, ["--lang", "en", "version"]).exit_code == 0
    import os

    assert os.environ["WINTERSAR_LANG"] == "ko"
    assert state.lang_explicit is True  # last invocation chose a language
    r = runner.invoke(app, ["plan", "--help"])
    assert state.lang_explicit is False
    assert "DAG 를 구성해" in re.sub(r"\s+", " ", r.output)
