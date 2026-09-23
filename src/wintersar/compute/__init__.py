"""wintersar.compute — numpy / CuPy pixel kernels with CPU fallback (PERF-10).

* :mod:`wintersar.compute.xp` — backend selection (``resolve_backend``, ``stage_gpu``,
  ``to_numpy``; ADR-0095) and host/device helpers.
* :mod:`wintersar.compute.kernels` — multilook, Goldstein filter, coherence estimate, box
  sums (ADR-0096 tolerances).
* :func:`bench_kernels` — CPU vs GPU wall times, numbers returned only (rule 11.8).
"""

from wintersar.compute.bench import bench_kernels
from wintersar.compute.xp import (
    Backend,
    GpuNotAvailableError,
    GpuRequest,
    get_xp,
    resolve_backend,
    stage_gpu,
    to_numpy,
)

__all__ = [
    "Backend",
    "GpuNotAvailableError",
    "GpuRequest",
    "bench_kernels",
    "get_xp",
    "resolve_backend",
    "stage_gpu",
    "to_numpy",
]
