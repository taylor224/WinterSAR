"""Executor helpers: log tails, KB engine names, the unwrap backend actually used."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.pipeline._support import write_fake_config
from wintersar.engines.base import list_engines
from wintersar.i18n import t
from wintersar.io.schemas import Artifact, Artifacts, StageRecord
from wintersar.pipeline.dag import Dag
from wintersar.pipeline.executor import (
    LOG_EXCERPT_TAIL_BYTES,
    Executor,
    kb_engine,
    log_excerpt,
    tail_lines,
)

GIANT_LINE = "A" * (LOG_EXCERPT_TAIL_BYTES + 10_000)
TAIL = ["ERROR: out of memory", "engine exited with 137", "bye"]


def _log(tmp_path: Path) -> Path:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "unwrap.log").write_text("\n".join([GIANT_LINE, *TAIL]) + "\n", encoding="utf-8")
    return log_dir


def test_tail_lines_reads_only_the_end_of_the_file(tmp_path: Path) -> None:
    """The failed stage may have died of OOM: never pull a whole log into memory."""
    p = _log(tmp_path) / "unwrap.log"
    assert tail_lines(p) == TAIL  # the truncated giant line is dropped
    assert tail_lines(p, n_lines=1) == TAIL[-1:]
    small = tmp_path / "small.log"
    small.write_text("a\nb\nc\n", encoding="utf-8")
    assert tail_lines(small) == ["a", "b", "c"]  # shorter than max_bytes: nothing dropped
    assert tail_lines(small, max_bytes=3) == ["c"]
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    assert tail_lines(empty) == []


def test_log_excerpt_keeps_only_the_tail(tmp_path: Path) -> None:
    excerpt = log_excerpt(_log(tmp_path))
    assert excerpt == {"unwrap.log": "\n".join(TAIL)}
    assert "A" * 100 not in excerpt["unwrap.log"]
    assert log_excerpt(tmp_path / "nope") == {}


def test_kb_engine_maps_registry_names_onto_the_knowledge_base() -> None:
    assert kb_engine("isce2_topsstack") == "isce2"  # KB file is kb/isce2.yaml
    assert kb_engine("snaphu") == "snaphu"
    assert kb_engine("hyp3") == "hyp3"
    for unknown in ("fake", "dolphin", "tophu", "spurt", "python", None, ""):
        assert kb_engine(unknown) is None


def test_pipeline_001_fix_never_suggests_an_engine_diagnose_rejects() -> None:
    """``wintersar diagnose --engine X`` exits 2 for anything outside ``KB_ENGINES``."""
    kb_engines = pytest.importorskip("wintersar.diagnose.kb_loader").KB_ENGINES
    for engine in [*list_engines(), "python", None]:
        kb = kb_engine(engine)
        key = f"pipeline.PIPELINE-001.{'fix' if kb else 'fix_auto'}"
        for lang in ("ko", "en"):
            text = t(
                key,
                lang,
                stage="unwrap",
                engine=engine or "python",
                kb_engine=kb or "",
                error="boom",
                log_dir="work/unwrap/abc/logs",
            )
            suggested = re.search(r"--engine ([A-Za-z0-9_]+)", text)
            assert (suggested is not None) == (kb is not None), (engine, lang, text)
            if suggested is not None:
                assert suggested.group(1) in kb_engines, (engine, lang, text)


def test_unwrap_record_names_the_backend_that_actually_ran(tmp_path: Path, cache_dir: Path) -> None:
    """``unwrap.method: auto`` hashes as snaphu, but the scheduler may pick tophu."""
    cfg = write_fake_config(tmp_path, engine={"interferogram": "isce2_topsstack"})
    dag = Dag(cfg)
    dag.build(until="unwrap")
    node = dag.node("unwrap")
    assert node.engine == "snaphu"  # auto -> snaphu for the cache key
    ex = Executor(cfg, dag)
    record = StageRecord(stage="unwrap", node_hash="h", engine=node.engine, status="ok")
    outputs = Artifacts().add(
        Artifact(name="unw", path=tmp_path / "unw.npz", meta={"method": "tophu"})
    )
    ex._record_actual_unwrap_engine(node, record, outputs)
    assert record.engine == "tophu"
    assert record.extra["engine_planned"] == "snaphu"
    assert record.engine_version == dag.engine_version("tophu")
    # the planned backend is what ran: nothing is rewritten
    same = StageRecord(stage="unwrap", node_hash="h", engine="snaphu", status="ok")
    ex._record_actual_unwrap_engine(
        node,
        same,
        Artifacts().add(Artifact(name="unw", path=tmp_path / "unw.npz", meta={"method": "snaphu"})),
    )
    assert same.engine == "snaphu" and "engine_planned" not in same.extra
