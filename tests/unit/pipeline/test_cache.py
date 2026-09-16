"""Work-dir cache: manifests, artifact hashing, gc (PERF-03; ADR-0032)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wintersar.io.schemas import Artifact, Artifacts, StageRecord
from wintersar.pipeline import cache


def _record(workdir: Path, stage: str, h: str, when: datetime, status: str = "ok") -> Path:
    d = cache.stage_dir(workdir, stage, h)
    out, _ = cache.prepare_node_dir(d)
    p = out / "data.bin"
    p.write_bytes(b"x" * 100)
    arts = cache.hash_artifacts(Artifacts().add(Artifact(name="igrams", path=p)))
    rec = StageRecord(
        stage=stage,
        node_hash=h,
        status=status,  # type: ignore[arg-type]
        started_at=when,
        finished_at=when,
        outputs={"igrams": str(p)},
        extra={cache.ARTIFACTS_KEY: cache.artifacts_to_extra(arts)},
    )
    cache.write_record(rec, d)
    return d


def test_write_load_roundtrip(workdir: Path) -> None:
    d = _record(workdir, "unwrap", "a" * 16, datetime.now(UTC))
    rec = cache.load_record(d)
    assert rec is not None and rec.stage == "unwrap" and rec.status == "ok"
    arts = cache.record_artifacts(rec)
    assert "igrams" in arts and arts["igrams"].sha256
    assert arts["igrams"].meta["hash_method"] == "fast"
    assert cache.load_record(workdir / "nope") is None
    (d / cache.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert cache.load_record(d) is None


def test_find_cached_requires_ok_and_intact_outputs(workdir: Path) -> None:
    h = "b" * 16
    d = _record(workdir, "unwrap", h, datetime.now(UTC))
    assert cache.find_cached(workdir, "unwrap", h) is not None
    assert cache.find_cached(workdir, "unwrap", "c" * 16) is None
    # failed record is never a hit
    _record(workdir, "unwrap", "d" * 16, datetime.now(UTC), status="failed")
    assert cache.find_cached(workdir, "unwrap", "d" * 16) is None
    # output modified -> stale
    p = d / "out" / "data.bin"
    time.sleep(0.01)
    p.write_bytes(b"y" * 100)
    assert cache.find_cached(workdir, "unwrap", h) is None
    assert cache.find_cached(workdir, "unwrap", h, verify=False) is not None
    # output deleted -> miss even without verification
    p.unlink()
    assert cache.find_cached(workdir, "unwrap", h) is None


def test_hash_artifacts_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        cache.hash_artifacts(Artifacts().add(Artifact(name="x", path=tmp_path / "missing")))
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.txt").write_text("a")
    arts = cache.hash_artifacts(Artifacts().add(Artifact(name="t", path=d, kind="dir")))
    assert arts["t"].sha256 and arts["t"].path.is_absolute()


def test_list_records_newest_first_and_sizes(workdir: Path) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i, h in enumerate(["1" * 16, "2" * 16, "3" * 16]):
        _record(workdir, "unwrap", h, t0 + timedelta(minutes=i))
    _record(workdir, "geocode", "9" * 16, t0 - timedelta(minutes=1))
    entries = cache.list_records(workdir)
    assert [e.node_hash for e in entries][:3] == ["3" * 16, "2" * 16, "1" * 16]
    assert all(e.size_bytes >= 100 for e in entries)
    assert set(cache.cache_size(workdir)) == {"unwrap", "geocode"}
    assert [e.stage for e in cache.list_records(workdir, "geocode")] == ["geocode"]
    latest = cache.latest_record(workdir, "unwrap")
    assert latest is not None and latest.node_hash == "3" * 16


def test_gc_keeps_latest(workdir: Path) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i, h in enumerate(["1" * 16, "2" * 16, "3" * 16]):
        _record(workdir, "unwrap", h, t0 + timedelta(minutes=i))
    dry = cache.gc(workdir, keep_latest=1, dry_run=True)
    assert [e.node_hash for e in dry.removed] == ["2" * 16, "1" * 16]
    assert dry.freed_bytes > 0 and dry.dry_run
    assert cache.stage_dir(workdir, "unwrap", "1" * 16).exists()  # dry-run deletes nothing
    report = cache.gc(workdir, keep_latest=1)
    assert [e.node_hash for e in report.kept] == ["3" * 16]
    assert not cache.stage_dir(workdir, "unwrap", "1" * 16).exists()
    assert cache.stage_dir(workdir, "unwrap", "3" * 16).exists()
    with pytest.raises(ValueError):
        cache.gc(workdir, keep_latest=-1)


def test_human_size() -> None:
    assert cache.human_size(512) == "512 B"
    assert cache.human_size(2048) == "2.0 KiB"
    assert cache.human_size(3 * 1024**3) == "3.0 GiB"
