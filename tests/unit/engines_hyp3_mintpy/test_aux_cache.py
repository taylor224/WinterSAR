"""PERF-02 auxiliary data cache: hit/miss, offline, locking, orbits via injected fetcher."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from wintersar.engines import aux_cache as ac


class Counting:
    def __init__(self, fail: bool = False, empty: bool = False) -> None:
        self.calls = 0
        self.fail = fail
        self.empty = empty

    def __call__(self, dest: Path) -> None:
        self.calls += 1
        if self.fail:
            msg = "network down"
            raise ConnectionError(msg)
        if not self.empty:
            (dest / "S1A_OPER_AUX_POEORB.EOF").write_text("orbit data")


def test_hit_miss_zero_network_on_rerun(tmp_path: Path) -> None:
    key = {"kind": "orbit", "mission": "S1A", "date": "2024-01-01", "orbit_type": "precise"}
    fetcher = Counting()
    e1 = ac.cached_fetch(tmp_path, key, fetcher, namespace="orbits")
    assert fetcher.calls == 1 and (e1 / ac.COMPLETE_MARKER).exists()
    assert ac.is_cached(tmp_path, key, "orbits")
    e2 = ac.cached_fetch(tmp_path, key, fetcher, namespace="orbits")
    assert e2 == e1 and fetcher.calls == 1  # offline re-run: zero fetches
    assert [p.name for p in ac.entry_files(e1)] == ["S1A_OPER_AUX_POEORB.EOF"]
    manifest = json.loads((e1 / ac.KEY_FILE).read_text())
    assert (
        manifest["key"] == key
        and manifest["files"] == ["S1A_OPER_AUX_POEORB.EOF"]
        and manifest["bytes"] == 10
    )
    assert e1.name == ac.cache_key(key) and len(e1.name) == 32
    # different key order -> same address
    assert ac.cache_key(dict(reversed(list(key.items())))) == e1.name
    assert not list(tmp_path.glob("**/*.lock")) and not list(tmp_path.glob("**/*.tmp-*"))


def test_offline_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = {"kind": "dem", "tile": "N37E126"}
    fetcher = Counting()
    monkeypatch.setenv("WINTERSAR_OFFLINE", "1")
    with pytest.raises(ac.CacheMissOfflineError):
        ac.cached_fetch(tmp_path, key, fetcher)
    assert fetcher.calls == 0
    monkeypatch.delenv("WINTERSAR_OFFLINE")
    entry = ac.cached_fetch(tmp_path, key, fetcher)
    assert ac.cached_fetch(tmp_path, key, fetcher, offline=True) == entry and fetcher.calls == 1


def test_failed_or_empty_fetch_leaves_no_entry(tmp_path: Path) -> None:
    key = {"kind": "x"}
    with pytest.raises(ConnectionError):
        ac.cached_fetch(tmp_path, key, Counting(fail=True))
    assert not ac.is_cached(tmp_path, key)
    with pytest.raises(ac.CacheFetchError):
        ac.cached_fetch(tmp_path, key, Counting(empty=True))
    assert not ac.is_cached(tmp_path, key)
    assert not list(tmp_path.glob("**/*.tmp-*")) and not list(tmp_path.glob("**/*.lock"))
    # a later successful fetch works
    assert ac.cached_fetch(tmp_path, key, Counting()).exists()


def test_stale_partial_entry_is_replaced(tmp_path: Path) -> None:
    key = {"kind": "y"}
    stale = ac.cache_entry_dir(tmp_path, key)
    stale.mkdir(parents=True)
    (stale / "partial.bin").write_bytes(b"x")
    entry = ac.cached_fetch(tmp_path, key, Counting())
    assert (
        entry == stale
        and not (entry / "partial.bin").exists()
        and (entry / ac.COMPLETE_MARKER).exists()
    )


def test_lock_timeout(tmp_path: Path) -> None:
    key = {"kind": "z"}
    entry = ac.cache_entry_dir(tmp_path, key)
    entry.parent.mkdir(parents=True)
    entry.with_name(entry.name + ".lock").write_text("held")
    with pytest.raises(ac.CacheLockTimeoutError):
        ac.cached_fetch(tmp_path, key, Counting(), lock_timeout_s=0.3)


def test_orbit_key_validation() -> None:
    from datetime import date

    k = ac.orbit_key("s1a", date(2024, 1, 1))
    assert k == {"kind": "orbit", "mission": "S1A", "date": "2024-01-01", "orbit_type": "precise"}
    with pytest.raises(ValueError):
        ac.orbit_key("S1A", date(2024, 1, 1), "bogus")


def test_fetch_orbits_without_sentineleof_is_env006(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ac, "sentineleof_available", lambda: False)
    monkeypatch.setattr(ac.shutil, "which", lambda name: None)
    res = ac.fetch_orbits(tmp_path, [("S1A", datetime(2024, 1, 1, 9, 20))])
    assert res.paths == {} and [f.rule_id for f in res.findings] == ["ENV-006"]
    assert res.findings[0].severity == "WARN" and res.findings[0].params["package"] == "sentineleof"
    assert res.ok


def test_fetch_orbits_with_injected_fetcher_is_cached(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str]] = []

    def factory(mission: str, when: datetime, orbit_type: str):
        def fetch(dest: Path) -> None:
            calls.append((mission, when.date().isoformat(), orbit_type))
            (dest / f"{mission}_OPER_AUX_POEORB_{when:%Y%m%d}.EOF").write_text("x")

        return fetch

    acq = [("S1A", datetime(2024, 1, 1, 9, 20)), ("S1A", datetime(2024, 1, 13, 9, 20))]
    r1 = ac.fetch_orbits(tmp_path, acq, fetcher_factory=factory)
    assert r1.n_fetched == 2 and r1.n_hits == 0 and len(calls) == 2
    assert sorted(r1.paths) == ["S1A_2024-01-01", "S1A_2024-01-13"]
    assert all(p.suffix == ".EOF" and p.exists() for p in r1.paths.values())
    r2 = ac.fetch_orbits(tmp_path, acq, fetcher_factory=factory)
    assert r2.n_hits == 2 and r2.n_fetched == 0 and len(calls) == 2  # zero network calls
    assert r2.paths == r1.paths


def test_fetch_orbits_reports_aux001_on_failure(tmp_path: Path) -> None:
    def factory(mission: str, when: datetime, orbit_type: str):
        def fetch(dest: Path) -> None:
            msg = "HTTP 503"
            raise ac.CacheFetchError(msg)

        return fetch

    res = ac.fetch_orbits(tmp_path, [("S1B", datetime(2021, 5, 5))], fetcher_factory=factory)
    assert [f.rule_id for f in res.findings] == ["AUX-001"] and res.findings[0].params[
        "mission"
    ] == "S1B"


def test_sentineleof_fetcher_without_package_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ac.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(ac.shutil, "which", lambda name: None)
    fetch = ac.sentineleof_fetcher("S1A", datetime(2024, 1, 1))
    with pytest.raises(ac.CacheFetchError, match="sentineleof"):
        fetch(tmp_path)
    assert ac.sentineleof_version() is None


def test_sentineleof_fetcher_cli_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ac.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(ac.shutil, "which", lambda name: "/usr/bin/eof")
    seen: dict[str, object] = {}

    class P:
        returncode = 0
        stderr = ""

    def fake_run(cmd: list[str], **kw: object) -> P:
        seen["cmd"] = cmd
        return P()

    monkeypatch.setattr(ac.subprocess, "run", fake_run)
    ac.sentineleof_fetcher("s1a", datetime(2024, 1, 1, 9, 20), "restituted")(tmp_path)
    # flags verified in sentineleof eof/cli.py
    assert seen["cmd"] == [
        "/usr/bin/eof",
        "--date",
        "2024-01-01",
        "--mission",
        "S1A",
        "--save-dir",
        str(tmp_path),
        "--orbit-type",
        "restituted",
    ]


def test_weather_dir_stats_prune(tmp_path: Path) -> None:
    w = ac.weather_dir(tmp_path, "era5")
    assert w == tmp_path / "weather" / "ERA5" and w.is_dir()
    for i in range(3):
        ac.cached_fetch(tmp_path, {"i": i}, Counting(), namespace="dem")
    stats = ac.cache_stats(tmp_path)
    assert stats["namespaces"]["dem"]["entries"] == 3 and stats["bytes"] == 30
    removed = ac.prune(tmp_path, max_bytes=15)
    assert len(removed) == 2 and ac.cache_stats(tmp_path)["namespaces"]["dem"]["entries"] == 1
    assert ac.cache_stats(tmp_path / "nope")["bytes"] == 0
