"""Engine adapter contract (plan §5.2) and registry.

Every external processor (HyP3, ISCE2 topsStack, SNAPHU, tophu, spurt, MintPy, dolphin)
is wrapped by an :class:`Engine`. Engines are invoked through subprocess or a lazily
imported permissively-licensed python package; GPL code (MintPy, GMTSAR) is never
imported (plan §9, rule 11.2).

Adapters must:

* pin a supported version range (``version_constraint``) and implement
  :meth:`Engine.check_install`, which returns ``ENV-001``/``ENV-002`` findings when the
  engine is absent or the version is out of range,
* keep parameters explicit and documented with the source URL of the upstream option
  (no guessed CLI flags),
* write all engine stdout/stderr to ``log_dir`` so that :mod:`wintersar.diagnose` can parse it.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, ClassVar

from wintersar.io.schemas import Artifacts, Finding, Plan, Resources


class EngineNotAvailableError(RuntimeError):
    """Raised when ``run`` is called on an engine whose ``check_install`` failed."""


class Engine(ABC):
    """Base class for engine adapters.

    Subclasses set ``name`` (registry key), ``version_constraint`` (PEP 440 specifier as a
    human-readable string, e.g. ``">=2.6,<3"``) and ``stages`` (which pipeline stages the
    engine can execute).
    """

    name: ClassVar[str]
    version_constraint: ClassVar[str] = "*"
    stages: ClassVar[tuple[str, ...]] = ()
    install_hint: ClassVar[str] = ""
    license_note: ClassVar[str] = ""

    # ------------------------------------------------------------------ discovery
    @abstractmethod
    def detect_version(self) -> str | None:
        """Return the installed version string, or ``None`` when not installed."""

    def check_install(self) -> list[Finding]:
        """Default implementation: ENV-001 when missing, ENV-002 when out of range."""
        version = self.detect_version()
        if version is None:
            return [
                Finding(
                    rule_id="ENV-001",
                    severity="FAIL",
                    message_key="env.ENV-001.cause",
                    fix_key="env.ENV-001.fix",
                    params={"engine": self.name, "install_hint": self.install_hint},
                    evidence={"engine": self.name},
                )
            ]
        if not version_satisfies(version, self.version_constraint):
            return [
                Finding(
                    rule_id="ENV-002",
                    severity="WARN",
                    message_key="env.ENV-002.cause",
                    fix_key="env.ENV-002.fix",
                    params={
                        "engine": self.name,
                        "found": version,
                        "constraint": self.version_constraint,
                    },
                    evidence={"engine": self.name, "version": version},
                )
            ]
        return []

    def is_available(self) -> bool:
        return not any(f.is_fail for f in self.check_install())

    # ------------------------------------------------------------------ contract
    def estimate(self, plan: Plan) -> Resources:
        """Estimate time/memory/disk/credits for ``plan``. Override per engine."""
        return Resources()

    @abstractmethod
    def run(
        self,
        stage: str,
        inputs: Artifacts,
        params: dict[str, Any],
        log_dir: Path,
    ) -> Artifacts:
        """Execute ``stage`` and return the produced artifacts.

        ``params`` is the canonical stage parameter mapping. The executor adds private keys
        prefixed with ``_``: ``_out_dir`` (stage output directory, always present),
        ``_workdir``, ``_cache_dir``, ``_cores``, ``_memory_gb``, ``_gpu``. Private keys are
        excluded from the node hash. All engine stdout/stderr must be written under
        ``log_dir`` (one ``<stage>.log`` or per-job files) for :mod:`wintersar.diagnose`.
        """

    def parse_log(self, log_path: Path) -> list[Finding]:
        """Engine-specific log parsing; by default delegate to :mod:`wintersar.diagnose`."""
        return []

    # ------------------------------------------------------------------ helpers
    def require_available(self) -> None:
        findings = self.check_install()
        if any(f.is_fail for f in findings):
            msg = f"engine {self.name!r} is not available: {[f.rule_id for f in findings]}"
            raise EngineNotAvailableError(msg)


# ---------------------------------------------------------------------- utilities


def python_module_version(module: str, dist: str | None = None) -> str | None:
    """Version of an importable module without importing heavy engines eagerly.

    Tries ``importlib.metadata`` (distribution name) first, then ``module.__version__``.
    """
    for candidate in filter(None, [dist, module, module.replace("_", "-")]):
        try:
            return importlib.metadata.version(candidate)
        except importlib.metadata.PackageNotFoundError:
            continue
    try:
        mod = importlib.import_module(module)
    except Exception:
        return None
    v = getattr(mod, "__version__", None)
    return str(v) if v else None


def executable_version(
    exe: str,
    args: Iterable[str] = ("--version",),
    parser: Callable[[str], str | None] | None = None,
    timeout: float = 20.0,
) -> str | None:
    """Run ``exe *args`` and extract a version string (``None`` if not on PATH)."""
    path = shutil.which(exe)
    if path is None:
        return None
    try:
        proc = subprocess.run(
            [path, *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    out = (proc.stdout or "") + (proc.stderr or "")
    if parser is not None:
        return parser(out) or "unknown"
    return _first_version_token(out) or "unknown"


def _first_version_token(text: str) -> str | None:
    import re

    m = re.search(r"\b(\d+\.\d+(?:\.\d+)?(?:[a-zA-Z0-9.+-]*)?)\b", text)
    return m.group(1) if m else None


def version_satisfies(version: str, constraint: str) -> bool:
    """Check ``version`` against a comma-separated PEP 440-like ``constraint``.

    ``"*"`` or ``""`` means any version. ``"unknown"`` versions always satisfy so that a
    detectable-but-unparseable engine is reported as available (with WARN elsewhere).
    """
    if constraint.strip() in {"", "*"} or version == "unknown":
        return True
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover - packaging ships with pip/uv envs
        return True
    try:
        v = Version(version.split("+")[0])
    except InvalidVersion:
        return True
    return v in SpecifierSet(constraint, prereleases=True)


# ---------------------------------------------------------------------- registry

_REGISTRY: dict[str, type[Engine]] = {}


def register_engine(cls: type[Engine]) -> type[Engine]:
    """Class decorator: ``@register_engine`` registers ``cls.name``."""
    if not getattr(cls, "name", None):
        msg = f"{cls.__name__} must define a class attribute 'name'"
        raise ValueError(msg)
    _REGISTRY[cls.name] = cls
    return cls


def _ensure_builtin_engines_loaded() -> None:
    """Import adapter modules so their ``@register_engine`` decorators run."""
    for mod in (
        "wintersar.engines.fake",
        "wintersar.engines.hyp3",
        "wintersar.engines.isce2_topsstack",
        "wintersar.engines.snaphu",
        "wintersar.engines.tophu",
        "wintersar.engines.spurt",
        "wintersar.engines.mintpy",
        "wintersar.engines.dolphin",
    ):
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as e:  # adapter not written yet / optional
            if e.name != mod:
                raise


def get_engine(name: str) -> Engine:
    _ensure_builtin_engines_loaded()
    try:
        return _REGISTRY[name]()
    except KeyError as e:
        msg = f"unknown engine {name!r}; known: {sorted(_REGISTRY)}"
        raise KeyError(msg) from e


def list_engines() -> dict[str, type[Engine]]:
    _ensure_builtin_engines_loaded()
    return dict(_REGISTRY)


def engines_for_stage(stage: str) -> list[str]:
    return [n for n, c in list_engines().items() if stage in c.stages]
