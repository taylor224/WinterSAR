"""bench.profiler: StageProfiler measures wall time, CPU, peak RSS, disk and network."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from wintersar.bench.profiler import Measurement, StageProfiler, dir_size_bytes, measure


def test_profiler_measures_sleep_and_allocation() -> None:
    with StageProfiler(interval_s=0.01) as prof:
        time.sleep(0.2)
        big = np.ones(16 * 1024 * 1024, dtype=np.float64)  # 128 MB, touched
        big += 1.0
        time.sleep(0.05)
    m = prof.result
    assert m.wall_time_s >= 0.2
    assert m.n_samples >= 2 and m.interval_s == 0.01
    assert m.cpu_time_s >= 0.0 and m.network_bytes >= 0
    assert m.peak_rss_gb >= m.baseline_rss_gb
    # How much of the 128 MB array shows up in RSS is the allocator's business (freed pages may
    # be reused): the peak/baseline bookkeeping itself is asserted exactly in
    # test_profiler_peak_and_disk_use_every_sample, which scripts the samples.
    assert m.disk_peak_gb is None and m.disk_delta_gb is None
    del big


def test_profiler_watches_directory_growth(tmp_path: Path) -> None:
    watch = tmp_path / "work"
    watch.mkdir()
    (watch / "seed.bin").write_bytes(b"\0" * 1000)
    with StageProfiler(watch_dir=watch, interval_s=0.005) as prof:
        (watch / "sub").mkdir()
        (watch / "sub" / "big.bin").write_bytes(b"\1" * (3 * 1024 * 1024))
        time.sleep(0.03)
    m = prof.result
    assert m.disk_peak_gb is not None and m.disk_peak_gb >= (3 * 1024 * 1024 + 1000) / 1e9
    assert m.disk_delta_gb is not None and abs(m.disk_delta_gb - 3 * 1024 * 1024 / 1e9) < 1e-6
    assert dir_size_bytes(watch) == 3 * 1024 * 1024 + 1000
    assert dir_size_bytes(tmp_path / "missing") is None and dir_size_bytes(None) is None


def test_profiler_peak_and_disk_use_every_sample(tmp_path: Path, monkeypatch) -> None:
    """Scripted RSS/disk samples (no real timing, no allocator luck): the peak is the maximum
    over all samples including the final one taken in __exit__, the delta is end - start."""
    import psutil

    from wintersar.bench import profiler as profiler_mod

    state = {"rss": 100_000_000, "disk": 1_000}
    monkeypatch.setattr(
        psutil.Process, "memory_info", lambda self: SimpleNamespace(rss=state["rss"])
    )
    monkeypatch.setattr(psutil.Process, "children", lambda self, recursive=False: [])
    monkeypatch.setattr(
        profiler_mod, "dir_size_bytes", lambda path: None if path is None else state["disk"]
    )

    # interval 5 s: the sampler thread takes exactly one automatic sample (waited for below),
    # then sleeps past the end of the block, so the samples asserted here are the only ones.
    with StageProfiler(watch_dir=tmp_path, interval_s=5.0) as prof:
        deadline = time.perf_counter() + 5.0
        while prof.n_samples < 1 and time.perf_counter() < deadline:
            time.sleep(0.001)
        state["rss"], state["disk"] = 500_000_000, 9_000
        prof.sample()
        state["rss"], state["disk"] = 300_000_000, 4_000
    m = prof.result
    assert m.baseline_rss_gb == pytest.approx(0.1)
    assert m.peak_rss_gb == pytest.approx(0.5)  # peak, not the value at exit
    assert m.disk_peak_gb == pytest.approx(9_000 / 1e9)
    assert m.disk_delta_gb == pytest.approx((4_000 - 1_000) / 1e9)
    assert m.n_samples >= 1 and m.interval_s == 5.0


def test_measure_helper_and_serialisation(tmp_path: Path) -> None:
    out, m = measure(lambda a, b: a + b, 2, b=3, interval_s=0.01)
    assert out == 5 and isinstance(m, Measurement)
    d = m.to_dict()
    assert set(d) >= {"wall_time_s", "cpu_time_s", "peak_rss_gb", "network_bytes", "disk_peak_gb"}
    back = Measurement.from_dict(d)
    assert back == m
    r = m.to_resources()
    assert r.wall_time_s == m.wall_time_s and r.peak_rss_gb == m.peak_rss_gb
    assert r.network_gb == m.network_bytes / 1e9


def test_profiler_result_before_exit_raises() -> None:
    import pytest

    p = StageProfiler()
    with pytest.raises(RuntimeError):
        _ = p.result
