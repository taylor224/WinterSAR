"""Docs owned by the docs/qgis module must stay true to the tree (ADR-0072, rules 11.7/11.9).

Every assertion here reads the published Markdown, so a page that drifts away from the real CLI,
the real workdir layout or the open-questions table fails in CI instead of at a reader's terminal.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from wintersar.cli import app
from wintersar.pipeline import cache

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
OPEN_QUESTIONS = DOCS / "open-questions.md"

#: Pages this module owns (its task description); other owners' pages are checked by them.
OWNED_PAGES: tuple[Path, ...] = (
    REPO / "README.md",
    DOCS / "index.md",
    DOCS / "contributing.md",
    DOCS / "adr" / "README.md",
    DOCS / "tutorials" / "hyp3-quickstart.md",
    DOCS / "tutorials" / "isce2-local.md",
    *sorted((DOCS / "concepts").glob("*.md")),
)
#: Pages that must carry the mandated ``## English summary`` section (ADR-0072 "언어").
ENGLISH_SUMMARY_PAGES: tuple[Path, ...] = (
    DOCS / "index.md",
    DOCS / "tutorials" / "hyp3-quickstart.md",
    DOCS / "tutorials" / "isce2-local.md",
    *sorted((DOCS / "concepts").glob("*.md")),
)
QGIS_I18N = (
    REPO / "src" / "wintersar" / "i18n" / "ko" / "qgis.yaml",
    REPO / "src" / "wintersar" / "i18n" / "en" / "qgis.yaml",
)

runner = CliRunner()


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


# ------------------------------------------------------- open-questions cross references

#: ``file -> {row number: a phrase that row must still contain}``. The table is renumbered when
#: rows are added, so a stale ``#N`` silently points at an unrelated row (rule 11.9 trail).
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
        46: "stackSentinel.py 플래그",
    },
}
_REF_RE = re.compile(r"(?:open-questions|오픈 항목|미확정 사항) #(\d+)")


def _open_question_rows() -> dict[int, str]:
    rows: dict[int, str] = {}
    for line in OPEN_QUESTIONS.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 4 or not cells[1].isdigit():
            continue
        rows[int(cells[1])] = cells[2]
    return rows


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
        subcmd = next(
            (i for i, tok in enumerate(tokens) if not tok.startswith("-")),
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
