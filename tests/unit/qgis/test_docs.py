"""Docs owned by the docs/qgis module must stay true to the tree (ADR-0072, ADR-0112, rules 11.7/11.9).

Every assertion here reads the published Markdown, so a page that drifts away from the real CLI,
the real workdir layout, the mkdocs nav or the open-questions table fails in CI instead of at a
reader's terminal. What is and is not checked is written down in ADR-0112.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import typer
import yaml
from typer.testing import CliRunner

from wintersar.cli import app
from wintersar.pipeline import cache
from wintersar.pipeline.incremental import INCREMENTAL_STAGES

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
OPEN_QUESTIONS = DOCS / "open-questions.md"
MKDOCS = REPO / "mkdocs.yml"
LOG_MANIFEST = REPO / "tests" / "fixtures" / "logs" / "manifest.yaml"

#: Pages this module owns (its task description); other owners' pages are checked by them.
OWNED_PAGES: tuple[Path, ...] = (
    REPO / "README.md",
    DOCS / "index.md",
    DOCS / "contributing.md",
    DOCS / "release-notes.md",
    DOCS / "roadmap.md",
    DOCS / "adr" / "README.md",
    DOCS / "kb" / "overview.md",
    DOCS / "tutorials" / "hyp3-quickstart.md",
    DOCS / "tutorials" / "isce2-local.md",
    DOCS / "tutorials" / "validate-tune.md",
    *sorted((DOCS / "concepts").glob("*.md")),
)
#: Pages that must carry the mandated ``## English summary`` section (ADR-0072 "언어").
ENGLISH_SUMMARY_PAGES: tuple[Path, ...] = (
    DOCS / "index.md",
    DOCS / "release-notes.md",
    DOCS / "roadmap.md",
    DOCS / "kb" / "overview.md",
    DOCS / "tutorials" / "hyp3-quickstart.md",
    DOCS / "tutorials" / "isce2-local.md",
    DOCS / "tutorials" / "validate-tune.md",
    *sorted((DOCS / "concepts").glob("*.md")),
)
#: Pages whose fenced ``wintersar …`` commands are checked against the Click tree (ADR-0112).
COMMAND_PAGES: tuple[Path, ...] = (
    REPO / "README.md",
    DOCS / "index.md",
    DOCS / "release-notes.md",
    DOCS / "kb" / "overview.md",
    *sorted((DOCS / "tutorials").glob("*.md")),
)
#: Pages whose relative Markdown links must resolve to files (only pages this module wrote).
LINK_PAGES: tuple[Path, ...] = (
    REPO / "README.md",
    DOCS / "index.md",
    DOCS / "release-notes.md",
    DOCS / "roadmap.md",
    DOCS / "adr" / "README.md",
    DOCS / "kb" / "overview.md",
    DOCS / "concepts" / "index.md",
    *sorted((DOCS / "tutorials").glob("*.md")),
)
QGIS_I18N = (
    REPO / "src" / "wintersar" / "i18n" / "ko" / "qgis.yaml",
    REPO / "src" / "wintersar" / "i18n" / "en" / "qgis.yaml",
)

runner = CliRunner()


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


# ------------------------------------------------------- open-questions cross references

#: ``file -> {row number: a phrase that row must still contain}``. The table is append-only, but a
#: stale ``#N`` would still point at an unrelated row if it were ever renumbered (rule 11.9 trail).
OPEN_QUESTION_REFS: dict[str, dict[int, str]] = {
    "docs/concepts/pixel-spacing-looks.md": {27: "IW 명목값"},
    "docs/concepts/unwrap-tiling-multiresolution.md": {
        8: "SNAPHU 메모리 상수",
        9: "타일 오버랩 기본값",
        21: "snaphu-py 경로에서 타일 임시 파일 보존 불가",
    },
    "docs/tutorials/hyp3-quickstart.md": {
        10: "의존성 정책 예외 승인",
        74: "PERF-06 실 엔진 참여",
        75: "PERF-06 before/after 측정",
    },
    "docs/tutorials/isce2-local.md": {
        8: "SNAPHU 메모리 상수",
        10: "의존성 정책 예외 승인",
        46: "stackSentinel.py 플래그",
    },
    "docs/tutorials/validate-tune.md": {
        40: "기준점 점수 가중치",
        42: "수준점 **시계열**",
    },
    "docs/release-notes.md": {
        4: "SNAPHU 라이선스 조건",
        10: "의존성 정책 예외 승인",
        22: "spurt conda-forge 패키지 부재",
        24: "looks 종횡비 상한",
        53: "R-15 A/B",
        67: "락파일 부재",
    },
}
_REF_RE = re.compile(r"(?:open-questions|오픈 항목|미확정 사항) #(\d+)")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")  # a ``\|`` inside a cell is not a separator


def _open_question_table() -> dict[int, dict[str, str]]:
    """``{row number: {"item": …, "owner": …, "status": …}}`` of docs/open-questions.md."""
    rows: dict[int, dict[str, str]] = {}
    for line in OPEN_QUESTIONS.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in _CELL_SPLIT_RE.split(line)]
        if len(cells) < 8 or not cells[1].isdigit():
            continue
        rows[int(cells[1])] = {"item": cells[2], "owner": cells[5], "status": cells[7]}
    return rows


def _open_question_rows() -> dict[int, str]:
    return {n: row["item"] for n, row in _open_question_table().items()}


def test_open_questions_table_is_numbered_consecutively() -> None:
    rows = _open_question_rows()
    assert rows, "no numbered rows parsed from docs/open-questions.md"
    assert sorted(rows) == list(range(1, max(rows) + 1))


@pytest.mark.parametrize("page", OWNED_PAGES, ids=_rel)
def test_open_question_references_point_at_the_right_row(page: Path) -> None:
    rows = _open_question_rows()
    found = {int(n) for n in _REF_RE.findall(page.read_text(encoding="utf-8"))}
    expected = OPEN_QUESTION_REFS.get(_rel(page), {})
    assert found == set(expected), (
        f"{_rel(page)}: open-question references changed; update OPEN_QUESTION_REFS "
        f"(found {sorted(found)}, expected {sorted(expected)})"
    )
    for number, phrase in expected.items():
        assert number in rows, f"{_rel(page)}: #{number} is not a row in open-questions.md"
        assert phrase in rows[number], (
            f"{_rel(page)}: #{number} now reads {rows[number]!r} — the table was renumbered, "
            f"the reference no longer lands on {phrase!r}"
        )


# ----------------------------------------------------------- roadmap mirrors the owners

_ROADMAP_ROW_RE = re.compile(r"^\| #(\d+) \|(.*)\|\s*$")
#: The word an open-questions status cell starts with; the roadmap must start its cell the same way.
_STATUS_WORD_RE = re.compile(r"^(완료|진행 중|대기|미착수)")


def _status_word(cell: str) -> str | None:
    m = _STATUS_WORD_RE.match(cell)
    return m.group(1) if m else None


def _roadmap_rows() -> dict[int, dict[str, str]]:
    """``{row number: {"status": …, "owner": …}}`` of the ``| #N | 요지 | 상태 | 담당 |`` rows."""
    rows: dict[int, dict[str, str]] = {}
    for line in (DOCS / "roadmap.md").read_text(encoding="utf-8").splitlines():
        m = _ROADMAP_ROW_RE.match(line)
        if not m:
            continue
        number = int(m.group(1))
        cells = [c.strip() for c in _CELL_SPLIT_RE.split(m.group(2))]
        assert number not in rows, f"docs/roadmap.md lists #{number} twice"
        rows[number] = {"status": cells[-2], "owner": cells[-1]}
    assert rows, "docs/roadmap.md no longer lists any `| #N |` rows"
    return rows


def test_roadmap_mirrors_every_open_question_row() -> None:
    """docs/roadmap.md promises every open-questions row, grouped by owner, with the table's status.

    A row added to open-questions but not to the roadmap (or a status that moved on in the source
    table only) is exactly the drift this page's header rules out (ADR-0112).
    """
    table = _open_question_table()
    roadmap = _roadmap_rows()
    assert set(roadmap) == set(table), (
        f"docs/roadmap.md rows differ from open-questions.md: missing {sorted(set(table) - set(roadmap))}, "
        f"unknown {sorted(set(roadmap) - set(table))}"
    )
    for number, row in roadmap.items():
        assert row["owner"] == table[number]["owner"], (
            f"docs/roadmap.md #{number}: owner {row['owner']!r} but open-questions.md says "
            f"{table[number]['owner']!r}"
        )
        source_word = _status_word(table[number]["status"])
        assert source_word is not None, (
            f"open-questions.md #{number}: status {table[number]['status']!r} does not start with "
            "완료 / 진행 중 / 대기 / 미착수"
        )
        assert _status_word(row["status"]) == source_word, (
            f"docs/roadmap.md #{number}: status {row['status']!r} but open-questions.md says "
            f"{table[number]['status']!r}"
        )


# ------------------------------------------------ countable facts quoted by the release notes

_FIXTURE_COUNT_RE = re.compile(r"manifest\.yaml`\(?\s*(\d+) 픽스처")
FIXTURE_COUNT_PAGES: tuple[Path, ...] = (
    DOCS / "release-notes.md",
    DOCS / "adr" / "0111-release-notes-dod-reporting-policy.md",
)


@pytest.mark.parametrize("page", FIXTURE_COUNT_PAGES, ids=_rel)
def test_quoted_log_fixture_count_matches_the_manifest(page: Path) -> None:
    """ADR-0111 rule 4 allows tree-countable facts only because they can be checked — so check them."""
    quoted = [int(n) for n in _FIXTURE_COUNT_RE.findall(page.read_text(encoding="utf-8"))]
    assert quoted, f"{_rel(page)}: no '`…manifest.yaml` N 픽스처' claim found"
    actual = len(yaml.safe_load(LOG_MANIFEST.read_text(encoding="utf-8")))
    assert set(quoted) == {actual}, (
        f"{_rel(page)} says {sorted(set(quoted))} fixtures but {_rel(LOG_MANIFEST)} has {actual}"
    )


# ------------------------------------------------------------------ mkdocs nav entries


def _nav_paths(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [p for v in node.values() for p in _nav_paths(v)]
    if isinstance(node, list):
        return [p for v in node for p in _nav_paths(v)]
    return []


def test_mkdocs_nav_entries_exist_on_disk() -> None:
    """Every page the nav points at must be a file under docs/ (ADR-0112; open-questions #65)."""
    config = yaml.safe_load(MKDOCS.read_text(encoding="utf-8"))
    paths = _nav_paths(config["nav"])
    assert paths, "mkdocs.yml has an empty nav"
    missing = [p for p in paths if not (DOCS / p).is_file()]
    assert not missing, f"mkdocs.yml nav points at missing pages: {missing}"
    for required in ("install.md", "release-notes.md", "roadmap.md"):
        assert required in paths, f"mkdocs.yml nav lacks {required}"
    assert all(p.startswith("tutorials/") or True for p in paths)
    assert {p for p in paths if p.startswith("tutorials/")} == {
        _rel(p).removeprefix("docs/") for p in sorted((DOCS / "tutorials").glob("*.md"))
    }


# ------------------------------------------------------------------ diagnose target path


def test_workdir_has_no_top_level_logs_dir(tmp_path: Path) -> None:
    """ADR-0032: logs live in ``<workdir>/<stage>/<hash>/logs`` — ``<workdir>/logs`` never exists."""
    node = cache.stage_dir(tmp_path, "unwrap", "deadbeef")
    assert cache.log_dir(node) == tmp_path / "unwrap" / "deadbeef" / "logs"
    assert cache.log_dir(node) != tmp_path / "logs"


_DIAGNOSE_RE = re.compile(r"diagnose\s+([\w<>/.-]+)")
#: Targets that do not exist: ``run`` never creates a logs dir directly under the workdir.
BAD_DIAGNOSE_TARGETS = ("work/logs", "<workdir>/logs")


def _bad_diagnose_targets(text: str) -> list[str]:
    return [t for t in _DIAGNOSE_RE.findall(text) if t.rstrip("/") in BAD_DIAGNOSE_TARGETS]


@pytest.mark.parametrize("page", OWNED_PAGES, ids=_rel)
def test_docs_never_send_diagnose_to_work_logs(page: Path) -> None:
    """``wintersar diagnose work/logs`` exits 2 ("path not found") — it is not a real directory."""
    bad = _bad_diagnose_targets(page.read_text(encoding="utf-8"))
    assert not bad, (
        f"{_rel(page)}: points diagnose at {bad}; use the workdir itself or "
        "<workdir>/<stage>/<hash>/logs (ADR-0032)"
    )


@pytest.mark.parametrize("path", QGIS_I18N, ids=_rel)
def test_qgis_messages_never_send_diagnose_to_work_logs(path: Path) -> None:
    """The plugin's Diagnose button uses the failed node's log dir, or the workdir — not both."""
    strings = _yaml_strings(yaml.safe_load(path.read_text(encoding="utf-8")))
    for text in strings:
        assert not _bad_diagnose_targets(text), f"{_rel(path)}: {text!r}"
        for bad in BAD_DIAGNOSE_TARGETS:
            assert bad not in text, f"{_rel(path)}: {text!r} mentions {bad!r}"


def _yaml_strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _yaml_strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _yaml_strings(v)]
    return []


# ------------------------------------------------------------- --json is a global option

_CMD_RE = re.compile(r"^\s*(?:\$ )?(wintersar\s+[^\n#]*)", re.MULTILINE)
_GLOBAL_OPTS = ("--json", "--lang")


@pytest.mark.parametrize("page", OWNED_PAGES, ids=_rel)
def test_global_options_precede_the_subcommand_in_examples(page: Path) -> None:
    for raw in _CMD_RE.findall(page.read_text(encoding="utf-8")):
        tokens = raw.split()[1:]  # drop "wintersar"
        opts = [i for i, tok in enumerate(tokens) if tok in _GLOBAL_OPTS]
        if not opts:
            continue
        values = {i + 1 for i, tok in enumerate(tokens) if tok == "--lang"}  # ``--lang ko``
        subcmd = next(
            (i for i, tok in enumerate(tokens) if not tok.startswith("-") and i not in values),
            len(tokens),
        )
        assert max(opts) < subcmd, (
            f"{_rel(page)}: `{raw.strip()}` puts a global option after the sub-command; "
            "the CLI only accepts `wintersar --json <command>`"
        )


def test_cli_rejects_json_after_the_subcommand() -> None:
    """Why the docs must use the global form (finding: README 'every command accepts --json')."""
    trailing = runner.invoke(app, ["version", "--json"])
    assert trailing.exit_code != 0
    assert runner.invoke(app, ["--json", "version"]).exit_code == 0


# ------------------------------------------- fenced commands exist in the Click tree (ADR-0112)

ROOT: Any = typer.main.get_command(app)
_FENCE_RE = re.compile(r"^```([^\n]*)\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_SHELL_INFO = {"", "bash", "sh", "shell", "zsh", "console"}
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:;|&&|\|\||\s\|\s)\s*")
_COMMENT_RE = re.compile(r"(?:^|\s)#")
_NUMBER_RE = re.compile(r"^-\d")


def _shell_blocks(text: str) -> list[str]:
    """Bodies of fenced blocks that hold shell examples (``text``/``yaml``/… blocks are output)."""
    out: list[str] = []
    for info, body in _FENCE_RE.findall(text):
        lang = info.strip().split(" ")[0] if info.strip() else ""
        if lang in _SHELL_INFO:
            out.append(body)
    return out


def _wintersar_invocations(block: str) -> list[list[str]]:
    """Token lists after ``wintersar`` for every command in a shell block.

    Handles ``\\`` line continuation, ``#`` comments, ``;``/``&&``/``||``/pipes, a ``$`` prompt,
    launcher prefixes (``uv run wintersar``, ``.venv/bin/wintersar``) and the ``[--opt value]``
    optional-argument notation used in synopses.
    """
    lines: list[str] = []
    buf = ""
    for raw in block.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        lines.append(buf + line)
        buf = ""
    if buf:
        lines.append(buf)
    out: list[list[str]] = []
    for line in lines:
        line = line.strip().removeprefix("$ ")
        line = _COMMENT_RE.split(line, maxsplit=1)[0]
        for segment in _SEGMENT_SPLIT_RE.split(line):
            try:
                tokens = shlex.split(segment)
            except ValueError:
                tokens = segment.split()
            tokens = [t.strip("[]") for t in tokens]
            tokens = [t for t in tokens if t]
            start = next(
                (i for i, t in enumerate(tokens) if t == "wintersar" or t.endswith("/wintersar")),
                None,
            )
            if start is not None:
                out.append(tokens[start + 1 :])
    return out


def _is_group(cmd: Any) -> bool:
    # ``isinstance(cmd, click.Group)`` is False for typer's TyperGroup under this Click build
    # (source: typer/core.py + typer/_click/core.py, checked 2026-09-23), so duck-type it.
    return hasattr(cmd, "list_commands") and hasattr(cmd, "get_command")


def _context(cmd: Any, parent: Any = None) -> Any:
    # Typer vendors Click (``typer._click``), so the real ``click.Context``/``click.Command`` are
    # not the classes these commands were built with; use the command's own context class.
    return cmd.context_class(cmd, parent=parent)


def _subcommand(cmd: Any, ctx: Any, name: str) -> Any | None:
    if not _is_group(cmd):
        return None
    sub = cmd.get_command(ctx, name)
    return sub if sub is not None and hasattr(sub, "get_params") else None


def _option_map(cmd: Any, ctx: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for param in cmd.get_params(ctx):
        if param.param_type_name != "option":
            continue
        for name in [*param.opts, *param.secondary_opts]:
            out[name] = param
    return out


def _takes_no_value(param: Any) -> bool:
    return bool(getattr(param, "is_flag", False) or getattr(param, "count", False))


def _invocation_error(tokens: list[str]) -> str | None:
    """Walk ``tokens`` down the Click tree; return a message for the first thing that is not real."""
    cmd: Any = ROOT
    ctx = _context(cmd)
    path = ["wintersar"]
    positional = 0
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-") and len(tok) > 1 and not _NUMBER_RE.match(tok):
            name = tok.partition("=")[0]
            param = _option_map(cmd, ctx).get(name)
            if param is None:
                return f"`{' '.join(path)}` has no option {name!r}"
            if "=" not in tok and not _takes_no_value(param):
                i += max(int(param.nargs), 1)
        else:
            sub = _subcommand(cmd, ctx, tok)
            if sub is not None:
                cmd, ctx = sub, _context(sub, parent=ctx)
                path.append(tok)
                positional = 0
            elif _is_group(cmd):
                return f"`{' '.join(path)}` has no sub-command {tok!r}"
            else:
                positional += 1
        i += 1
    arguments = [p for p in cmd.get_params(ctx) if p.param_type_name == "argument"]
    variadic = any(int(p.nargs) == -1 for p in arguments)
    if positional > len(arguments) and not variadic:
        return (
            f"`{' '.join(path)}` takes {len(arguments)} positional argument(s), "
            f"the example passes {positional}"
        )
    return None


@pytest.mark.parametrize("page", COMMAND_PAGES, ids=_rel)
def test_fenced_commands_name_existing_commands_and_options(page: Path) -> None:
    """Every ``wintersar …`` line in a shell block resolves against the real Click tree."""
    text = page.read_text(encoding="utf-8")
    invocations = [inv for block in _shell_blocks(text) for inv in _wintersar_invocations(block)]
    assert invocations, f"{_rel(page)}: no fenced `wintersar …` command found (page contract)"
    problems = [
        f"`wintersar {' '.join(inv)}`: {error}"
        for inv in invocations
        if (error := _invocation_error(inv)) is not None
    ]
    assert not problems, f"{_rel(page)}:\n  " + "\n  ".join(problems)


# ------------------------- the release notes' verification block mirrors CI (ADR-0111 rule 5)

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
#: ``uv run <tool> [<sub>]`` as CI spells it; the same phrase must appear in the release notes.
_UV_RUN_RE = re.compile(r"uv run (ruff \w+|mkdocs \w+|mypy|pytest|wintersar)")


def _ci_run_phrases() -> set[str]:
    config = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    phrases: set[str] = set()
    for job in config["jobs"].values():
        for step in job.get("steps", []):
            phrases.update(_UV_RUN_RE.findall(step.get("run") or ""))
    return phrases


def test_release_notes_verification_block_lists_what_ci_runs() -> None:
    """ADR-0111 rule 5: the '검증 방법' section is "the commands CI actually runs" — keep it so."""
    text = (DOCS / "release-notes.md").read_text(encoding="utf-8")
    section = text.split("## 검증 방법", 1)[1].split("\n## ", 1)[0]
    blocks = _shell_blocks(section)
    assert blocks, "docs/release-notes.md '검증 방법' has no fenced shell block"
    block = "\n".join(blocks)
    phrases = _ci_run_phrases()
    assert {"ruff check", "mypy", "pytest"} <= phrases, f"ci.yml parse looks wrong: {phrases}"
    missing = sorted(p for p in phrases if p not in block)
    assert not missing, (
        f"docs/release-notes.md '검증 방법' omits commands that {_rel(CI_WORKFLOW)} runs: {missing}"
    )


# ------------------------------- the docs/index.md command table lists every option (ADR-0112)

_TABLE_SYNOPSIS_RE = re.compile(r"^\| `([^`]+)` \|", re.MULTILINE)
_LONG_OPTION_RE = re.compile(r"(--[\w-]+)")


def _index_command_synopses() -> list[str]:
    """The first cell of every row of the "명령 목록" table in docs/index.md."""
    text = (DOCS / "index.md").read_text(encoding="utf-8")
    section = text.split("## 명령 목록", 1)[1].split("\n## ", 1)[0]
    return _TABLE_SYNOPSIS_RE.findall(section)


def _synopsis_command_path(synopsis: str) -> list[str] | None:
    """Command path of a synopsis, or None when the row is abbreviated (`` …``) or bundles commands."""
    if synopsis.endswith(" …"):
        return None
    path: list[str] = []
    for tok in synopsis.split():
        if tok.startswith(("-", "[")) or tok.isupper():
            break
        if "|" in tok:  # ``cache ls|gc``, ``research synth|repr-phase|…``
            return None
        path.append(tok)
    return path


def _click_long_options(path: list[str]) -> list[set[str]] | None:
    """One set of long names per option of the command at ``path`` (``--help`` excluded)."""
    cmd: Any = ROOT
    ctx = _context(cmd)
    for name in path:
        sub = _subcommand(cmd, ctx, name)
        if sub is None:
            return None
        cmd, ctx = sub, _context(sub, parent=ctx)
    out: list[set[str]] = []
    for param in cmd.get_params(ctx):
        if param.param_type_name != "option":
            continue
        names = {n for n in [*param.opts, *param.secondary_opts] if n.startswith("--")}
        if names and names != {"--help"}:
            out.append(names)
    return out


def test_index_command_table_lists_every_option_of_each_command() -> None:
    """A new CLI option must show up in the command table, not only in ``--help`` (PERF-06 lesson)."""
    synopses = _index_command_synopses()
    assert synopses, "docs/index.md no longer has a '명령 목록' table"
    checked = 0
    problems: list[str] = []
    for synopsis in synopses:
        path = _synopsis_command_path(synopsis)
        if path is None:
            continue
        options = _click_long_options(path)
        assert options is not None, f"docs/index.md: `{synopsis}` is not a command"
        listed = set(_LONG_OPTION_RE.findall(synopsis))
        known = {n for names in options for n in names}
        if unknown := listed - known:
            problems.append(f"`{synopsis}`: options {sorted(unknown)} do not exist")
        if missing := [sorted(names)[0] for names in options if not names & listed]:
            problems.append(f"`{synopsis}`: options {missing} exist but are not listed")
        checked += 1
    assert checked >= 10, f"only {checked} complete rows checked — did the table format change?"
    assert not problems, "docs/index.md command table:\n  " + "\n  ".join(problems)


def test_command_checker_rejects_unknown_commands_and_options() -> None:
    """The checker must fail on drift, otherwise the docs test is decorative."""
    assert _invocation_error(["run", "--config", "c.yaml"]) is None
    assert _invocation_error(["--json", "cache", "ls", "--config", "c.yaml"]) is None
    assert _invocation_error(["unwrap", "plan", "--shape", "4000", "6000", "--n", "30"]) is None
    assert _invocation_error(["validate", "--ts", "t.npz", "--heading", "-12"]) is None
    assert _invocation_error(["diagnose", "work/", "--engine", "isce2|snaphu"]) is None
    assert _invocation_error(["run", "--jsn"]) is not None
    assert _invocation_error(["runn", "--config", "c.yaml"]) is not None
    assert _invocation_error(["cache", "prune"]) is not None
    assert _invocation_error(["run", "extra_positional"]) is not None
    assert _invocation_error(["run", "--json"]) is not None  # global option after the command


def test_command_extraction_understands_shell_syntax() -> None:
    block = (
        "$ wintersar plan --config c.yaml   # dry run\n"
        "uv run wintersar --json run --config c.yaml | head -c 700\n"
        "wintersar cache ls --config c.yaml ; wintersar cache gc --config c.yaml --keep 3\n"
        "wintersar validate --ts t.npz \\\n    --leveling l.csv\n"
        "wintersar run --config c.yaml [--until unwrap] [--force STAGE]\n"
        'wintersar run --config c.yaml --set "interferogram.shape=[16,16]"\n'
        "printf '{\"a\":1}' > aoi.geojson\n"
        "#   --no-fail   comment only\n"
    )
    invocations = _wintersar_invocations(block)
    assert invocations == [
        ["plan", "--config", "c.yaml"],
        ["--json", "run", "--config", "c.yaml"],
        ["cache", "ls", "--config", "c.yaml"],
        ["cache", "gc", "--config", "c.yaml", "--keep", "3"],
        ["validate", "--ts", "t.npz", "--leveling", "l.csv"],
        ["run", "--config", "c.yaml", "--until", "unwrap", "--force", "STAGE"],
        ["run", "--config", "c.yaml", "--set", "interferogram.shape=[16,16"],
    ]
    assert _shell_blocks("```text\nwintersar nope\n```\n```bash\nwintersar version\n```") == [
        "wintersar version\n"
    ]


# --------------------------- the plan excerpt shows hashes for incremental stages (ADR-0080)

_PLAN_ROW_RE = re.compile(r"^│ (\w+)\s+│[^│]*│[^│]*│[^│]*│\s*([^│]*?)\s*│", re.MULTILINE)
_NEVER_HASHED = {"search", "precheck", "validate"}  # skipped on the fake path, so `-`


def test_hyp3_plan_excerpt_shows_hashes_for_incremental_stages_only() -> None:
    """ADR-0080: fetch…unwrap hash before they run; timeseries and later hash only after their inputs."""
    text = (DOCS / "tutorials" / "hyp3-quickstart.md").read_text(encoding="utf-8")
    section = text.split("## 5. 계획·견적 (plan)", 1)[1].split("\n## 6.", 1)[0]
    blocks = [
        body
        for info, body in _FENCE_RE.findall(section)
        if info.strip() == "text" and "실행 계획" in body
    ]
    assert len(blocks) == 1, "hyp3-quickstart.md §5 must hold exactly one `실행 계획` excerpt"
    cells = {stage: cell for stage, cell in _PLAN_ROW_RE.findall(blocks[0])}
    engine_stages = set(cells) - _NEVER_HASHED
    assert engine_stages >= set(INCREMENTAL_STAGES), f"excerpt rows {sorted(cells)} lack a stage"
    hashed = {stage for stage in engine_stages if cells[stage] != "-"}
    assert hashed == set(INCREMENTAL_STAGES), (
        f"hyp3-quickstart.md §5 excerpt shows a hash for {sorted(hashed)}; the stages hashed before "
        f"their first run are {sorted(INCREMENTAL_STAGES)} (ADR-0080)"
    )


# --------------------------------------------------------------------- relative links

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


@pytest.mark.parametrize("page", LINK_PAGES, ids=_rel)
def test_relative_links_resolve_to_files(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    broken: list[str] = []
    for target in _LINK_RE.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        rel = target.split("#", 1)[0]
        if rel and not (page.parent / rel).exists():
            broken.append(target)
    assert not broken, f"{_rel(page)}: links to missing files {broken}"


# ---------------------------------------------------------------------- English summary


@pytest.mark.parametrize("page", ENGLISH_SUMMARY_PAGES, ids=_rel)
def test_page_has_an_english_summary(page: Path) -> None:
    """ADR-0072: 본문 한국어 + 페이지 끝 ``## English summary``."""
    text = page.read_text(encoding="utf-8")
    assert "## English summary" in text, f"{_rel(page)}: missing '## English summary'"
    body = text.split("## English summary", 1)[1].strip()
    assert len(body.split()) >= 30, f"{_rel(page)}: English summary is too short to be useful"


# ------------------------------------------------- the documented failure injection works

_FAIL_SET_RE = re.compile(r"wintersar run[^\n]*?--set (\w+)\.fail_stage=(\w+)")


def _fake_config(tmp_path: Path) -> Path:
    (tmp_path / "aoi.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
    )
    data: dict[str, Any] = {
        "project": {"name": "docs-demo", "workdir": "work", "language": "ko"},
        "aoi": "aoi.geojson",
        "time_range": {"start": "2024-01-01", "end": "2024-06-30"},
        "engine": {"interferogram": "fake"},
        "timeseries": {"engine": "fake"},
        "unwrap": {"method": "auto", "coherence_threshold": 0.3},
        "compute": {"cores": 2, "memory_gb": 4},
    }
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return cfg


def test_documented_failure_injection_actually_fails(tmp_path: Path) -> None:
    """``--set <stage>.key`` only reaches *that* stage, so the doc must name the failing stage."""
    index = (DOCS / "index.md").read_text(encoding="utf-8")
    matches = _FAIL_SET_RE.findall(index)
    assert matches, "docs/index.md no longer documents the fail_stage example"
    cfg = _fake_config(tmp_path)
    for override_stage, failing_stage in matches:
        result = runner.invoke(
            app,
            [
                "--json",
                "run",
                "--config",
                str(cfg),
                "--set",
                f"{override_stage}.fail_stage={failing_stage}",
                "--set",
                "interferogram.n_dates=4",
                "--set",
                "interferogram.shape=[16, 16]",
            ],
        )
        envelope = json.loads(result.output)
        ids = {f.get("rule_id") for f in envelope.get("findings", [])}
        assert result.exit_code == 1, (
            f"docs/index.md documents `--set {override_stage}.fail_stage={failing_stage}` as a way "
            f"to reproduce a failure, but run exited {result.exit_code} (findings: {sorted(ids)})"
        )
        assert "PIPELINE-001" in ids


# --------------------------------------------- the documented incremental recipe reuses pairs

_INCREMENTAL_RUN_RE = re.compile(
    r"^\s*wintersar run\s+--config config\.yaml\s+--incremental\s+--set interferogram\.n_dates=(\d+)",
    re.MULTILINE,
)


def test_documented_incremental_recipe_reuses_pairs(tmp_path: Path) -> None:
    """hyp3-quickstart §6: run the ``--incremental`` lines in order; the last one must reuse pairs.

    The recipe exists to show PERF-06 working, so the doc test executes it: with the fake engine
    the second run reports ``incremental: true``, ``pairs.reused > 0`` and ``PIPELINE-015``.
    """
    page = (DOCS / "tutorials" / "hyp3-quickstart.md").read_text(encoding="utf-8")
    n_dates = [int(n) for n in _INCREMENTAL_RUN_RE.findall(page)]
    assert len(n_dates) >= 2 and n_dates == sorted(set(n_dates)), (
        "docs/tutorials/hyp3-quickstart.md no longer documents "
        "`wintersar run --config config.yaml --incremental --set interferogram.n_dates=N` "
        f"with a growing N (found {n_dates})"
    )
    cfg = _fake_config(tmp_path)
    envelope: dict[str, Any] = {}
    for n in n_dates:
        result = runner.invoke(
            app,
            [
                "--json",
                "run",
                "--config",
                str(cfg),
                "--incremental",
                "--set",
                f"interferogram.n_dates={n}",
                "--set",
                "interferogram.shape=[16, 16]",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
    data = envelope["data"]
    assert data["incremental"] is True
    assert data["pairs"]["reused"] > 0, data["pairs"]
    assert data["pairs"]["computed"] > 0, data["pairs"]
    assert "PIPELINE-015" in {f.get("rule_id") for f in envelope["findings"]}
