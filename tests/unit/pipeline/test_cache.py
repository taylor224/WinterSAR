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


# ---------------------------------------------------------------------- orphans / budget / lock


def test_orphan_node_dirs_are_listed_and_collected(workdir: Path) -> None:
    """A crash between prepare_node_dir() and the first manifest must not leak ``out/``.

    ``list_records`` used to drop such directories, so ``cache ls`` never showed them and
    ``cache gc`` never deleted them.
    """
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    _record(workdir, "unwrap", "1" * 16, t0)
    orphan = cache.stage_dir(workdir, "unwrap", "0" * 16)
    out, _ = cache.prepare_node_dir(orphan)
    (out / "big.bin").write_bytes(b"x" * 5000)
    truncated = _record(workdir, "unwrap", "2" * 16, t0 + timedelta(minutes=1))
    (truncated / cache.MANIFEST_NAME).write_text("{not json", encoding="utf-8")

    by_hash = {e.node_hash: e for e in cache.list_records(workdir, "unwrap")}
    assert set(by_hash) == {"0" * 16, "1" * 16, "2" * 16}
    assert by_hash["0" * 16].orphan and by_hash["0" * 16].record is None
    assert by_hash["0" * 16].status == cache.ORPHAN_STATUS
    assert by_hash["0" * 16].size_bytes >= 5000
    assert by_hash["0" * 16].finished_at is None
    assert by_hash["2" * 16].orphan
    assert not by_hash["1" * 16].orphan
    # an orphan is never a cache hit and never occupies a keep_latest slot
    assert cache.latest_record(workdir, "unwrap") is not None
    report = cache.gc(workdir, keep_latest=2)
    assert {e.node_hash for e in report.kept} == {"1" * 16}
    assert {e.node_hash for e in report.removed} == {"0" * 16, "2" * 16}
    assert not orphan.exists() and not truncated.exists()
    assert cache.stage_dir(workdir, "unwrap", "1" * 16).exists()


def test_gc_max_bytes_trims_the_oldest_survivors(workdir: Path) -> None:
    """PERF-03 cache budget: ``keep_latest`` alone cannot bound the work directory size."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i, h in enumerate(["1" * 16, "2" * 16, "3" * 16]):
        _record(workdir, "unwrap", h, t0 + timedelta(minutes=i))
    entries = cache.list_records(workdir, "unwrap")
    budget = max(e.size_bytes for e in entries) + 1  # room for exactly one entry
    dry = cache.gc(workdir, keep_latest=3, max_bytes=budget, dry_run=True)
    assert {e.node_hash for e in dry.removed} == {"1" * 16, "2" * 16}
    assert cache.stage_dir(workdir, "unwrap", "1" * 16).exists()  # dry-run deletes nothing
    report = cache.gc(workdir, keep_latest=3, max_bytes=budget)
    assert [e.node_hash for e in report.kept] == ["3" * 16]
    assert {e.node_hash for e in report.removed} == {"1" * 16, "2" * 16}
    assert not cache.stage_dir(workdir, "unwrap", "2" * 16).exists()
    assert cache.stage_dir(workdir, "unwrap", "3" * 16).exists()
    # a budget nothing fits into empties the stage; a generous one keeps everything
    _record(workdir, "unwrap", "4" * 16, t0 + timedelta(minutes=3))
    assert cache.gc(workdir, keep_latest=3, max_bytes=10**9).removed == []
    assert cache.gc(workdir, keep_latest=3, max_bytes=0).kept == []
    with pytest.raises(ValueError):
        cache.gc(workdir, max_bytes=-1)


def test_node_lock_is_exclusive_and_gc_spares_locked_entries(workdir: Path) -> None:
    """ADR-0032: two runs must not share one ``work/<stage>/<hash>/`` directory."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    held = _record(workdir, "unwrap", "5" * 16, t0)
    _record(workdir, "unwrap", "6" * 16, t0 + timedelta(minutes=1))
    with cache.node_lock(held):
        assert cache.is_locked(held)
        with pytest.raises(cache.NodeBusyError), cache.node_lock(held):
            pass  # a second run cannot take it
        report = cache.gc(workdir, keep_latest=1)  # would otherwise delete the older entry
        assert {e.node_hash for e in report.removed} == set()
        assert held.exists()
    assert not cache.is_locked(held)
    with cache.node_lock(held):  # released: available again
        pass
    assert {e.node_hash for e in cache.gc(workdir, keep_latest=1).removed} == {"5" * 16}
