"""Machine spec detection (cores, memory, GPU) used by the scheduler, MintPy template
auto-configuration (PERF-09) and ``check-install``."""

from __future__ import annotations

import importlib.util
import os
import platform
from dataclasses import dataclass


@dataclass(frozen=True)
class MachineSpec:
    cores: int
    memory_gb: float
    gpu: bool
    gpu_name: str | None
    python: str
    os: str

    def budget(
        self,
        cores: int | str = "auto",
        memory_gb: float | str = "auto",
        gpu: bool | str = "auto",
        memory_fraction: float = 0.8,
    ) -> MachineSpec:
        """Apply ``compute.*`` config overrides on top of the detected spec."""
        return MachineSpec(
            cores=self.cores if cores == "auto" else int(cores),
            memory_gb=(self.memory_gb * memory_fraction)
            if memory_gb == "auto"
            else float(memory_gb),
            gpu=self.gpu if gpu == "auto" else bool(gpu),
            gpu_name=self.gpu_name,
            python=self.python,
            os=self.os,
        )


def _gpu_info() -> tuple[bool, str | None]:
    if importlib.util.find_spec("cupy") is None:
        return False, None
    try:  # pragma: no cover - requires CUDA
        import cupy

        n = int(cupy.cuda.runtime.getDeviceCount())
        if n <= 0:
            return False, None
        props = cupy.cuda.runtime.getDeviceProperties(0)
        name = props.get("name", b"")
        return True, name.decode() if isinstance(name, bytes) else str(name)
    except Exception:
        return False, None


def detect() -> MachineSpec:
    try:
        import psutil

        mem = psutil.virtual_memory().total / 1e9
        cores = psutil.cpu_count(logical=True) or os.cpu_count() or 1
    except ImportError:  # pragma: no cover
        mem = 0.0
        cores = os.cpu_count() or 1
    gpu, gpu_name = _gpu_info()
    return MachineSpec(
        cores=int(cores),
        memory_gb=float(mem),
        gpu=gpu,
        gpu_name=gpu_name,
        python=platform.python_version(),
        os=f"{platform.system()} {platform.release()} ({platform.machine()})",
    )
