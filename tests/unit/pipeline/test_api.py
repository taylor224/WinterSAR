"""``wintersar.pipeline.api`` result assembly (findings are reported once)."""

from __future__ import annotations

from wintersar.io.schemas import Finding
from wintersar.pipeline.api import dedupe_findings


def _f(rule: str, scope: str = "validate", **params: object) -> Finding:
    return Finding(
        rule_id=rule,
        severity="INFO",
        message_key=f"pipeline.{rule}.cause",
        fix_key=f"pipeline.{rule}.fix",
        params=params,
        scope=scope,
    )


def test_dedupe_findings_keeps_the_first_of_each_repeat() -> None:
    """``run`` concatenates the plan's findings with the executor's, which rebuilds the
    same DAG nodes: every DAG-level finding used to be printed twice."""
    a, b = _f("PIPELINE-010"), _f("PIPELINE-010")
    other_scope = _f("PIPELINE-010", scope="unwrap")
    other_params = _f("PIPELINE-010", engine="fake")
    out = dedupe_findings([a, b, other_scope, other_params, b])
    assert out == [a, other_scope, other_params]
    assert out[0] is a  # order and identity of the first occurrence are preserved
    assert dedupe_findings([]) == []


def test_dedupe_findings_keeps_different_severities() -> None:
    warn = _f("PIPELINE-012")
    fail = warn.model_copy(update={"severity": "FAIL"})
    assert dedupe_findings([warn, fail, warn]) == [warn, fail]
