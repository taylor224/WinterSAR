"""``load_config`` error contract (ADR-0091, CLI-004 / CLI-005).

A YAML *syntax* error in ``config.yaml`` must be a ``ValueError`` like a pydantic error is,
so every CLI that maps ``ValueError`` -> CLI-005 (exit 2) does the same for a typo in the
file instead of reporting a crash (CLI-001, exit 1). Round-2 review finding: ``sweep``
answered CLI-001 while ``search`` answered CLI-005 for one and the same malformed file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from wintersar.cli import app
from wintersar.pipeline.config import ConfigError, load_config

MALFORMED = "project: {name: x\n  workdir: [\n"
runner = CliRunner()


@pytest.fixture
def malformed(tmp_path: Path) -> Path:
    p = tmp_path / "malformed.yaml"
    p.write_text(MALFORMED, encoding="utf-8")
    (tmp_path / "grid.yaml").write_text("grid: {}\n", encoding="utf-8")
    return p


def test_yaml_syntax_error_is_a_value_error(malformed: Path) -> None:
    assert issubclass(ConfigError, ValueError)
    with pytest.raises(ValueError, match="line 2") as info:
        load_config(malformed)
    assert isinstance(info.value, ConfigError)
    # the pyyaml error is kept for anyone who wants the mark/position details
    assert isinstance(info.value.__cause__, yaml.YAMLError)


def test_missing_file_stays_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_schema_error_stays_a_value_error(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("project: {name: x}\n", encoding="utf-8")  # parses, but aoi/time_range missing
    with pytest.raises(ValueError):
        load_config(p)


@pytest.mark.parametrize(
    ("argv", "rule_id"),
    [
        (["search"], "CLI-005"),
        (["sweep", "--grid", "grid.yaml"], "CLI-005"),
        (["plan"], "PIPELINE-014"),  # pipeline's documented usage-error id (ADR-0091)
    ],
    ids=["search", "sweep", "plan"],
)
def test_every_cli_reports_a_malformed_config_as_usage_error(
    malformed: Path, argv: list[str], rule_id: str
) -> None:
    args = [a if a != "grid.yaml" else str(malformed.parent / a) for a in argv]
    res = runner.invoke(app, ["--json", *args, "--config", str(malformed)])
    env = json.loads(res.stdout)
    assert res.exit_code == 2, res.stdout
    assert env["ok"] is False
    assert [f["rule_id"] for f in env["findings"]] == [rule_id]
