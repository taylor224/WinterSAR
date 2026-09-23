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
    """PERF-03 cache budget (ADR-0081): ``keep_latest`` alone cannot bound the work
    directory size; ``max_bytes`` evicts the oldest survivors first but never the newest
    ``ok`` entry of a stage (the one the next run resolves against)."""
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
    assert [e.node_hash for e in report.protected] == ["3" * 16]
    assert report.max_bytes == budget and not report.over_budget
    assert not cache.stage_dir(workdir, "unwrap", "2" * 16).exists()
    assert cache.stage_dir(workdir, "unwrap", "3" * 16).exists()
    # a generous budget keeps everything; a budget nothing fits into keeps only the newest
    # ok entry of the stage and reports that it is still over budget
    _record(workdir, "unwrap", "4" * 16, t0 + timedelta(minutes=3))
    assert cache.gc(workdir, keep_latest=3, max_bytes=10**9).removed == []
    zero = cache.gc(workdir, keep_latest=3, max_bytes=0)
    assert [e.node_hash for e in zero.kept] == ["4" * 16]
    assert {e.node_hash for e in zero.removed} == {"3" * 16}
    assert zero.over_budget and zero.kept_bytes > 0
    assert cache.stage_dir(workdir, "unwrap", "4" * 16).exists()
    with pytest.raises(ValueError):
        cache.gc(workdir, max_bytes=-1)


def test_gc_budget_evicts_oldest_across_stages_and_spares_failed_newest(workdir: Path) -> None:
    """Eviction order is ``finished_at`` across stages; a failed entry is never protected
    (only the newest *ok* one is), so the budget can still reclaim it."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    _record(workdir, "unwrap", "a" * 16, t0)  # oldest overall
    _record(workdir, "geocode", "b" * 16, t0 + timedelta(minutes=1))
    _record(workdir, "unwrap", "c" * 16, t0 + timedelta(minutes=2))
    _record(workdir, "unwrap", "d" * 16, t0 + timedelta(minutes=3), status="failed")
    sizes = {e.node_hash: e.size_bytes for e in cache.list_records(workdir)}
    # room for the two protected entries (newest ok of unwrap = c, of geocode = b) only
    budget = sizes["c" * 16] + sizes["b" * 16]
    report = cache.gc(workdir, keep_latest=5, max_bytes=budget)
    assert {e.node_hash for e in report.protected} == {"b" * 16, "c" * 16}
    assert [e.node_hash for e in report.removed] == ["a" * 16, "d" * 16]  # oldest first
    assert {e.node_hash for e in report.kept} == {"b" * 16, "c" * 16}
    assert not report.over_budget


def test_size_summary_accounts_for_orphans_and_protected_entries(workdir: Path) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    _record(workdir, "unwrap", "1" * 16, t0)
    _record(workdir, "unwrap", "2" * 16, t0 + timedelta(minutes=1))
    orphan = cache.stage_dir(workdir, "unwrap", "0" * 16)
    out, _ = cache.prepare_node_dir(orphan)
    (out / "big.bin").write_bytes(b"x" * 5000)
    summary = cache.size_summary(workdir, max_bytes=10)
    sizes = {e.node_hash: e.size_bytes for e in cache.list_records(workdir)}
    assert summary["total_bytes"] == sum(sizes.values()) == summary["by_stage"]["unwrap"]
    assert summary["n_entries"] == 3 and summary["n_orphans"] == 1
    assert summary["orphan_bytes"] == sizes["0" * 16] >= 5000
    assert summary["protected_bytes"] == sizes["2" * 16]  # newest ok entry of the stage
    assert summary["max_bytes"] == 10
    assert summary["over_budget_bytes"] == summary["total_bytes"] - 10
    assert "max_bytes" not in cache.size_summary(workdir)


def test_prepare_node_dir_keeps_pairs_only_when_asked(workdir: Path) -> None:
    """A full recompute must not inherit per-pair results; an incremental one must."""
    from wintersar.pipeline.incremental import pairs_dir

    node_dir = workdir / "unwrap" / ("x" * 16)
    out, _ = cache.prepare_node_dir(node_dir)
    pairs = pairs_dir(node_dir)
    pairs.mkdir()
    (pairs / "20240101_20240113.npz").write_bytes(b"pair")
    (out / "unw.npz").write_bytes(b"stack")
    cache.prepare_node_dir(node_dir, clean=True, keep_pairs=True)
    assert (pairs / "20240101_20240113.npz").exists() and not (out / "unw.npz").exists()
    cache.prepare_node_dir(node_dir, clean=True)
    assert not pairs.exists()
    cache.prepare_node_dir(node_dir, clean=False)  # no cleaning at all
    assert out.exists()


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


# ---------------------------------------------------------------------- gc robustness


def test_gc_reports_what_it_could_not_delete_and_never_counts_it_as_freed(
    workdir: Path, tmp_path: Path
) -> None:
    """A symlinked node directory is unlinked (target untouched); a directory that cannot
    be removed lands in ``failed`` — not in ``removed``, not in ``freed_bytes`` — and is
    listed again by the next gc instead of being "freed" forever."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    _record(workdir, "unwrap", "9" * 16, t0 + timedelta(minutes=9))  # newest, survives
    external = _record(tmp_path / "elsewhere", "unwrap", "e" * 16, t0)
    link = cache.stage_dir(workdir, "unwrap", "1" * 16)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(external, target_is_directory=True)
    stuck = _record(workdir, "unwrap", "2" * 16, t0 + timedelta(minutes=1))
    stuck.chmod(0o555)  # rmtree cannot delete the manifest/out inside
    try:
        report = cache.gc(workdir, keep_latest=1)
        assert {e.node_hash for e in report.removed} == {"1" * 16}
        assert {e.node_hash for e in report.failed} == {"2" * 16}
        assert report.freed_bytes == sum(e.size_bytes for e in report.removed)
        assert not link.exists() and not link.is_symlink()
        assert (external / cache.MANIFEST_NAME).exists()  # the link target was not followed
        assert stuck.exists() and (stuck / cache.MANIFEST_NAME).exists()
        # still there, still reported (not silently "freed" a second time)
        again = cache.gc(workdir, keep_latest=1)
        assert [e.node_hash for e in again.failed] == ["2" * 16] and again.removed == []
        assert again.freed_bytes == 0
        dry = cache.gc(workdir, keep_latest=1, dry_run=True)
        assert [e.node_hash for e in dry.removed] == ["2" * 16] and dry.failed == []
    finally:
        stuck.chmod(0o755)


def test_gc_budget_protects_the_newest_ok_entry_even_with_keep_zero(workdir: Path) -> None:
    """``--keep 0 --max-size X`` must not delete the entry the next run resolves against:
    the budget mode's promise (ADR-0081) beats ``keep_latest``. Without a budget
    ``keep_latest=0`` still means "wipe the stage"."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i, h in enumerate(["1" * 16, "2" * 16, "3" * 16]):
        _record(workdir, "unwrap", h, t0 + timedelta(minutes=i))
    _record(workdir, "unwrap", "4" * 16, t0 + timedelta(minutes=3), status="failed")
    dry = cache.gc(workdir, keep_latest=0, max_bytes=10**9, dry_run=True)
    assert [e.node_hash for e in dry.protected] == ["3" * 16]  # newest *ok*, not the failed
    assert [e.node_hash for e in dry.kept] == ["3" * 16]
    assert {e.node_hash for e in dry.removed} == {"1" * 16, "2" * 16, "4" * 16}
    assert not dry.over_budget
    zero = cache.gc(workdir, keep_latest=0, max_bytes=0)
    assert [e.node_hash for e in zero.kept] == ["3" * 16] and zero.over_budget
    assert cache.stage_dir(workdir, "unwrap", "3" * 16).exists()
    assert not cache.stage_dir(workdir, "unwrap", "2" * 16).exists()
    # no budget: keep 0 wipes
    wipe = cache.gc(workdir, keep_latest=0)
    assert [e.node_hash for e in wipe.removed] == ["3" * 16] and wipe.kept == []
    assert wipe.protected == [] and cache.list_records(workdir, "unwrap") == []
