"""Stage resource profiler for ``wintersar bench`` (plan §5.9 / §6.3, ADR-0054).

:class:`StageProfiler` is a context manager that measures one stage:

* ``wall_time_s`` — ``time.perf_counter`` delta,
* ``cpu_time_s`` — process user+system CPU time (children included) via
  ``psutil.Process.cpu_times()`` (fields ``user, system, children_user, children_system``,
  source: ``.venv/lib/python3.11/site-packages/psutil/__init__.py`` ``Process.cpu_times``),
* ``peak_rss_gb`` — maximum resident set size sampled by a daemon thread
  (``Process.memory_info().rss``, plus children when ``include_children``),
* ``disk_peak_gb`` / ``disk_delta_gb`` — peak and net growth of a watched directory
  (recursive ``os.scandir`` byte count, sampled),
* ``network_bytes`` — system-wide ``psutil.net_io_counters()`` ``bytes_sent + bytes_recv``
  delta (source: psutil ``net_io_counters`` docstring; note it is *machine*-wide, not
  per-process — concurrent downloads by other programs count too).

Sampling has a floor of ``interval_s`` between samples; the directory walk is skipped when
it took longer than the interval to keep overhead bounded on large work directories.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from wintersar.io.schemas import Resources

_GB = 1e9
T = TypeVar("T")


def dir_size_bytes(path: Path | None) -> int | None:
    """Recursive size of ``path`` in bytes (``None`` when the directory does not exist)."""
    if path is None:
        return None
    root = Path(path)
    if not root.exists():
        return None
    total = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


@dataclass
class Measurement:
    wall_time_s: float
    cpu_time_s: float
    peak_rss_gb: float
    baseline_rss_gb: float
    network_bytes: int
    disk_peak_gb: float | None = None
    disk_delta_gb: float | None = None
    n_samples: int = 0
    interval_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_time_s": self.wall_time_s,
            "cpu_time_s": self.cpu_time_s,
            "peak_rss_gb": self.peak_rss_gb,
            "baseline_rss_gb": self.baseline_rss_gb,
            "network_bytes": self.network_bytes,
            "disk_peak_gb": self.disk_peak_gb,
            "disk_delta_gb": self.disk_delta_gb,
            "n_samples": self.n_samples,
            "interval_s": self.interval_s,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Measurement:
        return cls(
            wall_time_s=float(d.get("wall_time_s", 0.0)),
            cpu_time_s=float(d.get("cpu_time_s", 0.0)),
            peak_rss_gb=float(d.get("peak_rss_gb", 0.0)),
            baseline_rss_gb=float(d.get("baseline_rss_gb", 0.0)),
            network_bytes=int(d.get("network_bytes", 0)),
            disk_peak_gb=None if d.get("disk_peak_gb") is None else float(d["disk_peak_gb"]),
            disk_delta_gb=None if d.get("disk_delta_gb") is None else float(d["disk_delta_gb"]),
            n_samples=int(d.get("n_samples", 0)),
            interval_s=float(d.get("interval_s", 0.0)),
        )

    def to_resources(self) -> Resources:
        return Resources(
            wall_time_s=self.wall_time_s,
            peak_rss_gb=self.peak_rss_gb,
            disk_gb=self.disk_peak_gb,
            network_gb=self.network_bytes / _GB,
        )


class StageProfiler:
    """``with StageProfiler(watch_dir=out) as prof: ...`` → ``prof.measurement``."""

    def __init__(
        self,
        watch_dir: Path | None = None,
        interval_s: float = 0.05,
        include_children: bool = True,
        measure_network: bool = True,
    ) -> None:
        self.watch_dir = None if watch_dir is None else Path(watch_dir)
        self.interval_s = max(float(interval_s), 0.001)
        self.include_children = include_children
        self.measure_network = measure_network
        self.measurement: Measurement | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._peak_rss = 0
        self._peak_disk: int | None = None
        self._n_samples = 0
        self._t0 = 0.0
        self._cpu0 = 0.0
        self._net0 = 0
        self._disk0: int | None = None
        self._baseline_rss = 0
        self._proc: Any = None

    # ------------------------------------------------------------------ sampling
    def _rss(self) -> int:
        import psutil

        try:
            rss = int(self._proc.memory_info().rss)
        except (psutil.Error, OSError):
            return self._peak_rss
        if self.include_children:
            try:
                for ch in self._proc.children(recursive=True):
                    try:
                        rss += int(ch.memory_info().rss)
                    except (psutil.Error, OSError):
                        continue
            except (psutil.Error, OSError):
                pass
        return rss

    def _cpu(self) -> float:
        import psutil

        try:
            t = self._proc.cpu_times()
        except (psutil.Error, OSError):
            return 0.0
        total = float(t.user + t.system)
        if self.include_children:
            total += float(getattr(t, "children_user", 0.0) + getattr(t, "children_system", 0.0))
        return total

    @staticmethod
    def _net() -> int:
        import psutil

        try:
            c = psutil.net_io_counters()
        except (psutil.Error, OSError, RuntimeError):
            return 0
        return int(c.bytes_sent + c.bytes_recv)

    def sample(self) -> None:
        self._peak_rss = max(self._peak_rss, self._rss())
        if self.watch_dir is not None:
            t0 = time.perf_counter()
            size = dir_size_bytes(self.watch_dir)
            if size is not None:
                self._peak_disk = size if self._peak_disk is None else max(self._peak_disk, size)
            # bound overhead: if walking took longer than the interval, back off
            took = time.perf_counter() - t0
            if took > self.interval_s:
                self._stop.wait(min(took, 5.0))
        self._n_samples += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.interval_s)

    # ------------------------------------------------------------------ context manager
    def __enter__(self) -> StageProfiler:
        import psutil

        self._proc = psutil.Process()
        self._t0 = time.perf_counter()
        self._cpu0 = self._cpu()
        self._net0 = self._net() if self.measure_network else 0
        self._disk0 = dir_size_bytes(self.watch_dir)
        self._baseline_rss = self._rss()
        self._peak_rss = self._baseline_rss
        self._peak_disk = self._disk0
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wintersar-profiler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, 10 * self.interval_s))
        self.sample()  # final sample (peak at the end of the stage)
        wall = time.perf_counter() - self._t0
        cpu = max(self._cpu() - self._cpu0, 0.0)
        net = max(self._net() - self._net0, 0) if self.measure_network else 0
        disk_end = dir_size_bytes(self.watch_dir)
        peak_disk = self._peak_disk
        if disk_end is not None:
            peak_disk = disk_end if peak_disk is None else max(peak_disk, disk_end)
        self.measurement = Measurement(
            wall_time_s=wall,
            cpu_time_s=cpu,
            peak_rss_gb=self._peak_rss / _GB,
            baseline_rss_gb=self._baseline_rss / _GB,
            network_bytes=net,
            disk_peak_gb=None if peak_disk is None else peak_disk / _GB,
            disk_delta_gb=None if disk_end is None else (disk_end - (self._disk0 or 0)) / _GB,
            n_samples=self._n_samples,
            interval_s=self.interval_s,
        )

    @property
    def n_samples(self) -> int:
        """Samples taken so far (readable while the profiler is running)."""
        return self._n_samples

    @property
    def result(self) -> Measurement:
        if self.measurement is None:
            msg = "StageProfiler has not finished (use it as a context manager)"
            raise RuntimeError(msg)
        return self.measurement


def measure(
    fn: Callable[..., T],
    *args: Any,
    watch_dir: Path | None = None,
    interval_s: float = 0.05,
    **kwargs: Any,
) -> tuple[T, Measurement]:
    """Run ``fn(*args, **kwargs)`` under a :class:`StageProfiler`; return ``(result, measurement)``."""
    with StageProfiler(watch_dir=watch_dir, interval_s=interval_s) as prof:
        out = fn(*args, **kwargs)
    return out, prof.result
