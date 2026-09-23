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

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
OPEN_QUESTIONS = DOCS / "open-questions.md"
MKDOCS = REPO / "mkdocs.yml"

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
    "docs/tutorials/hyp3-quickstart.md": {10: "의존성 정책 예외 승인"},
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
    """``{row number: {"item": …, "owner": …}}`` of docs/open-questions.md."""
    rows: dict[int, dict[str, str]] = {}
    for line in OPEN_QUESTIONS.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in _CELL_SPLIT_RE.split(line)]
        if len(cells) < 8 or not cells[1].isdigit():
            continue
        rows[int(cells[1])] = {"item": cells[2], "owner": cells[5]}
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


def test_roadmap_rows_cite_existing_open_questions_with_the_same_owner() -> None:
    """docs/roadmap.md groups open questions by owner; the owner cell must equal the source table's."""
    table = _open_question_table()
    seen: list[int] = []
    for line in (DOCS / "roadmap.md").read_text(encoding="utf-8").splitlines():
        m = _ROADMAP_ROW_RE.match(line)
        if not m:
            continue
        number = int(m.group(1))
        cells = [c.strip() for c in m.group(2).split("|")]
        assert number in table, f"docs/roadmap.md cites #{number}, not a row in open-questions.md"
        owner = cells[-1]
        assert owner == table[number]["owner"], (
            f"docs/roadmap.md #{number}: owner {owner!r} but open-questions.md says "
            f"{table[number]['owner']!r}"
        )
        seen.append(number)
    assert seen, "docs/roadmap.md no longer lists any `| #N |` rows"
    assert len(seen) == len(set(seen)), f"docs/roadmap.md lists a row twice: {sorted(seen)}"


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
