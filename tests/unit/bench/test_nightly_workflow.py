"""``.github/workflows/nightly.yml`` structure (ADR-0101: gates vs reports) and the
least-privilege ``GITHUB_TOKEN`` (``permissions: contents: read``).

The workflow is data, so a YAML-level test is the cheapest way to keep the two policies
from drifting: the golden check must stay a gate with ``--json``/``--markdown`` output, the
baseline comparison must stay report-only, and no job may widen the token scope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "nightly.yml"


@pytest.fixture(scope="module")
def wf() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_token_is_read_only(wf: dict[str, Any]) -> None:
    """Scheduled runs need nothing beyond checking out the code and exchanging artifacts
    inside the run, so the workflow pins ``contents: read`` (every other scope → none)."""
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        assert "permissions" not in job, f"job {name} must not widen the workflow token"


def test_gates_and_reports_match_adr_0101(wf: dict[str, Any]) -> None:
    jobs = wf["jobs"]
    assert set(jobs) == {"regression", "bench", "compare-baseline"}
    # gates: a failure makes the workflow red
    for gate in ("regression", "bench"):
        assert not jobs[gate].get("continue-on-error"), gate
    # report only: needs the bench artifact, never fails the workflow
    compare = jobs["compare-baseline"]
    assert compare["needs"] == "bench" and compare["continue-on-error"] is True
    # the golden check is the CI entry point with both machine- and human-readable output
    check = next(s for s in jobs["regression"]["steps"] if "check_golden.py" in s.get("run", ""))
    assert "--json" in check["run"] and "--markdown" in check["run"]
    assert "continue-on-error" not in check
    # the runner class the baseline is keyed on is the one the jobs run on (ADR-0101)
    assert all(job["runs-on"] == wf["env"]["RUNNER_CLASS"] for job in jobs.values())
    # scheduled + manual; PyYAML reads the bare `on` key as the boolean True
    triggers = next(v for k, v in wf.items() if k in ("on", True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
