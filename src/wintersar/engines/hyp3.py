"""ASF HyP3 On-Demand adapter (plan §5.2 ``hyp3.py``; Phase 2 step ①; PERF-02/06; ADR-0020).

HyP3 performs fetch + coregistration + interferogram + multilook/filter + SNAPHU unwrapping as
**one cloud job** (``INSAR_ISCE_BURST`` for a single burst pair, ``INSAR_ISCE_MULTI_BURST``
for 2-15 bursts of the same date pair). The adapter is therefore registered for the
``interferogram`` stage only, but :meth:`Hyp3Engine.run` returns **both** the ``igrams`` and
the ``unw`` artifacts (same GeoTIFF directory), so the pipeline skips the local ``unwrap``
stage when ``engine.interferogram == "hyp3"`` (see :data:`Hyp3Engine.produces`).

Verified facts (all ``# source:`` comments below; recorded in ADR-0020):

* hyp3-sdk 7.7.8 (PyPI, 2026-09-02, BSD-3-Clause). ``HyP3(api_url, username, password,
  token, prompt)``; ``HyP3.submit_prepared_jobs(list[dict]) -> Batch``;
  ``HyP3.prepare_insar_isce_burst_job(granule1, granule2, name=None, apply_water_mask=False,
  looks='20x4'|'10x2'|'5x1')``; ``prepare_insar_isce_multi_burst_job(reference: list[str],
  secondary: list[str], name=None, apply_water_mask=False, looks=...)``;
  ``HyP3.get_job_by_id(job_id) -> Job``; ``HyP3.watch(batch, timeout=10800, interval=60)``;
  ``HyP3.check_credits() -> float | int | None``; ``HyP3.costs() -> dict``;
  ``Job.succeeded()/failed()/complete()/expired()``; ``Job.download_files(location) ->
  list[Path]``; ``Job.files = [{'filename', 'url', 's3', 'size'}]``; ``Job.credit_cost``.
  # source: https://github.com/ASFHyP3/hyp3-sdk/blob/main/src/hyp3_sdk/hyp3.py
  # source: https://github.com/ASFHyP3/hyp3-sdk/blob/main/src/hyp3_sdk/jobs.py
* Product contents and naming: burst InSAR product guide (file suffixes in
  :data:`REQUIRED_SUFFIXES` / :data:`RECOMMENDED_SUFFIXES`, looks → pixel spacing).
  # source: https://hyp3-docs.asf.alaska.edu/guides/burst_insar_product_guide/
* Credit costs: ``hyp3_costs.yaml`` (never hard-coded here).
  # source: https://hyp3-docs.asf.alaska.edu/using/credits/
* MintPy ``prep_hyp3`` expects ``<product>_<suffix>.tif`` next to ``<product>.txt`` and reads the
  metadata keys listed in :data:`REQUIRED_METADATA_KEYS` (spaces removed).
  # source: https://github.com/insarlab/MintPy/blob/main/src/mintpy/prep_hyp3.py

Every network call goes through a :class:`Hyp3Client`; tests inject a fake.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Protocol

import yaml

from wintersar.engines.base import (
    Engine,
    EngineNotAvailableError,
    python_module_version,
    register_engine,
)
from wintersar.io.schemas import Artifact, Artifacts, Finding, Plan, Resources, StackCandidate
from wintersar.util.masking import mask_mapping, mask_text

# ---------------------------------------------------------------------- verified constants

HYP3_SDK_VERIFIED_VERSION = "7.7.8"  # source: https://pypi.org/pypi/hyp3-sdk/json (2026-09-16)
PROD_API = "https://hyp3-api.asf.alaska.edu"  # source: hyp3_sdk/hyp3.py PROD_API
JOB_TYPE_BURST = "INSAR_ISCE_BURST"  # source: hyp3_sdk/hyp3.py prepare_insar_isce_burst_job
JOB_TYPE_MULTI_BURST = "INSAR_ISCE_MULTI_BURST"  # source: prepare_insar_isce_multi_burst_job
# looks -> pixel spacing (m); source: burst_insar_product_guide (20x4=80 m, 10x2=40 m, 5x1=20 m)
LOOKS_PIXEL_M: dict[str, int] = {"20x4": 80, "10x2": 40, "5x1": 20}
DEFAULT_LOOKS = "20x4"  # source: hyp3_sdk default + product guide "The default is 20x4 looks."
MAX_BURSTS_PER_JOB = 15  # source: product guide "Sets of bursts can contain 1-15 bursts"

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
_ACTIVE_STATUSES = frozenset({STATUS_PENDING, STATUS_RUNNING})

# product file suffixes; source: burst_insar_product_guide "Product files" table
REQUIRED_SUFFIXES: tuple[str, ...] = (
    "_unw_phase.tif",  # unwrapped phase, float32, radians
    "_corr.tif",  # coherence 0..1, float32
    "_dem.tif",  # DEM, metres (geoid corrected)
    "_lv_theta.tif",  # look-vector elevation angle, radians
    "_lv_phi.tif",  # look-vector orientation angle, radians
)
RECOMMENDED_SUFFIXES: tuple[str, ...] = (
    "_conncomp.tif",  # SNAPHU connected components, uint8 (MintPy unwrapError needs it)
    "_water_mask.tif",  # 1 = land, 0 = water, uint8
    "_wrapped_phase.tif",  # filtered wrapped phase
    "_amp.tif",  # amplitude
)
METADATA_SUFFIX = ".txt"  # "<product>.txt includes processing parameters"
README_SUFFIX = ".README.md.txt"
# keys MintPy prep_hyp3 reads from <product>.txt after removing all spaces
# source: mintpy/prep_hyp3.py add_hyp3_metadata
REQUIRED_METADATA_KEYS: tuple[str, ...] = (
    "UTCtime",
    "Azimuthlooks",
    "Rangelooks",
    "Earthradiusatnadir",
    "Spacecraftheight",
    "Slantrangenear",
    "Heading",
    "Baseline",
    "Unwrappingtype",
)
# product name patterns; source: mintpy/prep_hyp3.py _get_product_name_and_type
PRODUCT_NAME_RE_BURST = re.compile(r"S1_\d{6}_IW[123](_\d{8}){2}_(VV|HH)_INT\d{2}_[0-9A-F]{4}")
PRODUCT_NAME_RE_MULTI_BURST = re.compile(
    r"S1_\d{3}_\d{6}s1n\d{2}-\d{6}s2n\d{2}-\d{6}s3n\d{2}_IW(_\d{8}){2}_(VV|HH)_INT\d{2}_[0-9A-F]{4}"
)
# MintPy template load keys -> HyP3 file suffix (source: MintPy docs dir_structure.md "HyP3")
MINTPY_LOAD_SUFFIXES: dict[str, str] = {
    "unwFile": "_unw_phase",
    "corFile": "_corr",
    "connCompFile": "_conncomp",
    "demFile": "_dem",
    "incAngleFile": "_lv_theta",
    "azAngleFile": "_lv_phi",
    "waterMaskFile": "_water_mask",
}
CLIP_SUFFIX = "_clip"
SUBMIT_BATCH_SIZE = 200  # source: hyp3_sdk/util.py chunk(itr, n=200)
POLL_TIMEOUT_S = 10800.0  # source: hyp3_sdk HyP3.watch(timeout=10800, interval=60)
POLL_INTERVAL_S = 30.0
POLL_MAX_INTERVAL_S = 300.0
POLL_BACKOFF = 1.5

_COSTS_PATH = Path(__file__).with_name("hyp3_costs.yaml")


class Hyp3RunError(RuntimeError):
    """Raised by :meth:`Hyp3Engine.run` when a FAIL finding was produced.

    State (``jobs.json``) and findings are written before raising so a re-run resumes.
    """

    def __init__(self, message: str, findings: list[Finding]) -> None:
        super().__init__(message)
        self.findings = findings


# ---------------------------------------------------------------------- cost table


@dataclass(frozen=True)
class CostTable:
    tiers: dict[str, tuple[tuple[int, float], ...]]  # looks -> ((max_pairs, credits), ...)
    pixel_m: dict[str, int]
    monthly_free_credits: float
    max_bursts_per_job: int
    source_url: str
    fetched: str


@lru_cache(maxsize=4)
def load_cost_table(path: Path | None = None) -> CostTable:
    p = path or _COSTS_PATH
    with p.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    tiers: dict[str, tuple[tuple[int, float], ...]] = {}
    pixel: dict[str, int] = {}
    for looks, spec in (raw.get("burst_insar") or {}).items():
        tiers[str(looks)] = tuple(
            (int(t["max_pairs"]), float(t["credits"]))
            for t in sorted(spec.get("tiers", []), key=lambda t: int(t["max_pairs"]))
        )
        pixel[str(looks)] = int(spec.get("pixel_m", 0))
    return CostTable(
        tiers=tiers,
        pixel_m=pixel,
        monthly_free_credits=float(raw.get("monthly_free_credits", 0) or 0),
        max_bursts_per_job=int(raw.get("max_bursts_per_job", MAX_BURSTS_PER_JOB)),
        source_url=str(raw.get("source_url", "")),
        fetched=str(raw.get("fetched", "")),
    )


def job_credits(looks: str, n_bursts: int = 1, table: CostTable | None = None) -> float | None:
    """Credits of one burst InSAR job with ``n_bursts`` burst pairs, or ``None`` if unknown."""
    table = table or load_cost_table()
    tiers = table.tiers.get(looks)
    if not tiers or n_bursts < 1:
        return None
    for max_pairs, credits in tiers:
        if n_bursts <= max_pairs:
            return credits
    return None


def estimate_credits(n_pairs: int, n_bursts: int = 1, looks: str = DEFAULT_LOOKS) -> Resources:
    """Credit estimate for ``n_pairs`` jobs (used by ``plan`` and SEL-13)."""
    table = load_cost_table()
    per_job = job_credits(looks, n_bursts, table)
    credits = None if per_job is None else per_job * int(n_pairs)
    return Resources(
        credits=credits,
        n_jobs=int(n_pairs),
        notes={
            "looks": looks,
            "n_bursts": int(n_bursts),
            "credits_per_job": per_job,
            "pixel_m": table.pixel_m.get(looks),
            "monthly_free_credits": table.monthly_free_credits,
            "source": table.source_url,
            "fetched": table.fetched,
        },
    )


# ---------------------------------------------------------------------- looks


def resolve_looks(looks: Any, target_pixel_m: float | None = None) -> tuple[str, Finding | None]:
    """Map config ``engine.looks`` / ``target_pixel_m`` to a HyP3 looks option.

    Accepts ``'20x4'``, ``[rg, az]``, ``'auto'``/``None``. Unsupported values fall back to the
    closest supported pixel spacing with a ``HYP3-003`` WARN finding.
    """
    if isinstance(looks, str) and looks in LOOKS_PIXEL_M:
        return looks, None
    requested: str
    wanted_px: float
    if looks in (None, "auto", ""):
        if target_pixel_m is None:
            return DEFAULT_LOOKS, None
        wanted_px = float(target_pixel_m)
        requested = f"auto ({wanted_px:g} m)"
    elif isinstance(looks, list | tuple) and len(looks) == 2:
        rg, az = int(looks[0]), int(looks[1])
        candidate = f"{rg}x{az}"
        if candidate in LOOKS_PIXEL_M:
            return candidate, None
        # HyP3 pixel spacing = 4 m x range looks (20->80, 10->40, 5->20)
        wanted_px = 4.0 * rg
        requested = candidate
    else:
        requested = str(looks)
        wanted_px = float(LOOKS_PIXEL_M[DEFAULT_LOOKS])
    used = min(LOOKS_PIXEL_M, key=lambda k: (abs(LOOKS_PIXEL_M[k] - wanted_px), -LOOKS_PIXEL_M[k]))
    if looks in (None, "auto", "") and float(LOOKS_PIXEL_M[used]) == wanted_px:
        return used, None
    return used, Finding(
        rule_id="HYP3-003",
        severity="WARN",
        message_key="engines.hyp3.HYP3-003.cause",
        fix_key="engines.hyp3.HYP3-003.fix",
        params={"requested": requested, "used": used},
        evidence={"requested": requested, "used": used, "pixel_m": LOOKS_PIXEL_M[used]},
        scope="interferogram",
    )


# ---------------------------------------------------------------------- job model


@dataclass(frozen=True)
class JobSpec:
    """One HyP3 job to submit (one date pair; 1..15 bursts)."""

    pair: str  # "YYYYMMDD_YYYYMMDD"
    reference: tuple[str, ...]  # burst granule names of the reference date
    secondary: tuple[str, ...]
    looks: str = DEFAULT_LOOKS
    apply_water_mask: bool = False
    name: str | None = None

    @property
    def n_bursts(self) -> int:
        return len(self.reference)

    @property
    def job_type(self) -> str:
        return JOB_TYPE_BURST if self.n_bursts == 1 else JOB_TYPE_MULTI_BURST

    def to_prepared(self) -> dict[str, Any]:
        """Exact payload of ``HyP3.prepare_insar_isce_[multi_]burst_job`` (hyp3_sdk 7.7.8).

        # source: hyp3_sdk/hyp3.py prepare_insar_isce_burst_job ->
        #   {'job_parameters': {'granules': [g1, g2], 'apply_water_mask': ..., 'looks': ...},
        #    'job_type': 'INSAR_ISCE_BURST', 'name': name (only when not None)}
        # source: prepare_insar_isce_multi_burst_job ->
        #   {'job_parameters': {'reference': [...], 'secondary': [...], 'apply_water_mask': ...,
        #    'looks': ...}, 'job_type': 'INSAR_ISCE_MULTI_BURST', 'name': ...}
        """
        if self.n_bursts == 1:
            params: dict[str, Any] = {
                "granules": [self.reference[0], self.secondary[0]],
                "apply_water_mask": self.apply_water_mask,
                "looks": self.looks,
            }
        else:
            params = {
                "reference": list(self.reference),
                "secondary": list(self.secondary),
                "apply_water_mask": self.apply_water_mask,
                "looks": self.looks,
            }
        job: dict[str, Any] = {"job_parameters": params, "job_type": self.job_type}
        if self.name is not None:
            job["name"] = self.name
        return job


@dataclass
class JobInfo:
    """Snapshot of a HyP3 ``Job`` (the subset of fields the adapter needs)."""

    job_id: str
    status_code: str
    job_type: str = JOB_TYPE_BURST
    name: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    credit_cost: float | None = None
    job_parameters: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    expiration_time: str | None = None
    request_time: str | None = None
    processing_times: list[float] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status_code == STATUS_SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.status_code == STATUS_FAILED

    @property
    def complete(self) -> bool:
        return self.succeeded or self.failed

    def expired(self, now: datetime | None = None) -> bool:
        if not self.expiration_time:
            return False
        try:
            exp = datetime.fromisoformat(self.expiration_time.replace("Z", "+00:00"))
        except ValueError:
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return (now or datetime.now(UTC)) >= exp

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> JobInfo:
        """Build from the HyP3 API job dict (``Job.to_dict()`` / ``/jobs`` response)."""
        return cls(
            job_id=str(d["job_id"]),
            status_code=str(d["status_code"]),
            job_type=str(d.get("job_type", JOB_TYPE_BURST)),
            name=d.get("name"),
            files=list(d.get("files") or []),
            credit_cost=None if d.get("credit_cost") is None else float(d["credit_cost"]),
            job_parameters=dict(d.get("job_parameters") or {}),
            logs=list(d.get("logs") or []),
            expiration_time=_iso_or_none(d.get("expiration_time")),
            request_time=_iso_or_none(d.get("request_time")),
            processing_times=list(d.get("processing_times") or []),
        )

    @classmethod
    def from_sdk(cls, job: Any) -> JobInfo:
        """Build from a ``hyp3_sdk.Job`` (attribute names verified in jobs.py)."""
        return cls.from_dict(job.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso_or_none(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    return str(v)


class Hyp3Client(Protocol):
    """Every network interaction of the adapter (tests inject a fake)."""

    def submit(self, specs: Sequence[JobSpec]) -> list[JobInfo]: ...

    def refresh(self, job_ids: Sequence[str]) -> list[JobInfo]: ...

    def download(self, job: JobInfo, dest: Path) -> list[Path]: ...

    def check_credits(self) -> float | None: ...

    def costs(self) -> dict[str, Any]: ...


class SdkHyp3Client:
    """``hyp3_sdk``-backed client (lazy import; the package is an optional extra)."""

    def __init__(
        self,
        api_url: str = PROD_API,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        sdk = importlib.import_module("hyp3_sdk")
        # source: hyp3_sdk/hyp3.py HyP3.__init__(api_url, username, password, token, prompt)
        # "If username and password are not provided, attempts to use credentials from .netrc"
        self._api = sdk.HyP3(api_url=api_url, username=username, password=password, token=token)
        self.version = str(getattr(sdk, "__version__", "") or "")

    def submit(self, specs: Sequence[JobSpec]) -> list[JobInfo]:
        # source: hyp3_sdk/hyp3.py HyP3.submit_prepared_jobs(prepared_jobs: dict | list[dict])
        batch = self._api.submit_prepared_jobs([s.to_prepared() for s in specs])
        return [JobInfo.from_sdk(j) for j in batch]

    def refresh(self, job_ids: Sequence[str]) -> list[JobInfo]:
        # source: hyp3_sdk/hyp3.py HyP3.get_job_by_id(job_id) -> Job
        return [JobInfo.from_sdk(self._api.get_job_by_id(jid)) for jid in job_ids]

    def download(self, job: JobInfo, dest: Path) -> list[Path]:
        # source: hyp3_sdk/jobs.py Job.download_files(location, create=True) -> list[Path]
        sdk_job = self._api.get_job_by_id(job.job_id)
        return [Path(p) for p in sdk_job.download_files(dest)]

    def check_credits(self) -> float | None:
        # source: hyp3_sdk/hyp3.py HyP3.check_credits() -> info['remaining_credits']
        v = self._api.check_credits()
        return None if v is None else float(v)

    def costs(self) -> dict[str, Any]:
        # source: hyp3_sdk/hyp3.py HyP3.costs() -> GET /costs
        return dict(self._api.costs())


# ---------------------------------------------------------------------- credentials


@dataclass(frozen=True)
class EarthdataCredentials:
    token: str | None = None
    netrc: bool = False

    @property
    def available(self) -> bool:
        return bool(self.token) or self.netrc


def resolve_credentials(
    env: Mapping[str, str] | None = None, netrc_path: Path | None = None
) -> EarthdataCredentials:
    """``EARTHDATA_TOKEN`` (bearer token) or a ``~/.netrc`` entry for urs.earthdata.nasa.gov.

    # source: hyp3_sdk/util.py get_authenticated_session(username, password, token):
    #   token -> 'Authorization: Bearer <token>'; otherwise .netrc credentials.
    """
    env = os.environ if env is None else env
    token = env.get("EARTHDATA_TOKEN") or None
    netrc = netrc_path or Path.home() / ".netrc"
    has_netrc = False
    if netrc.exists():
        try:
            has_netrc = "urs.earthdata.nasa.gov" in netrc.read_text(
                encoding="utf-8", errors="ignore"
            )
        except OSError:
            has_netrc = False
    return EarthdataCredentials(token=token, netrc=has_netrc)


def credentials_findings(creds: EarthdataCredentials) -> list[Finding]:
    if creds.available:
        return []
    return [
        Finding(
            rule_id="ENV-003",
            severity="WARN",
            message_key="env.ENV-003.cause",
            fix_key="env.ENV-003.fix",
            evidence={"token": False, "netrc": False},
            scope="hyp3",
        )
    ]


# ---------------------------------------------------------------------- state (resume)


@dataclass
class PairState:
    pair: str
    job_id: str | None = None
    status: str = "NEW"
    job_type: str | None = None
    name: str | None = None
    product_dir: str | None = None  # relative to the data dir
    credit_cost: float | None = None
    submitted_at: str | None = None
    n_bursts: int = 1


class JobState:
    """``<data_dir>/jobs.json``: pair → job id/status/product (PERF-06 resumable runs)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pairs: dict[str, PairState] = {}

    @classmethod
    def load(cls, path: Path) -> JobState:
        st = cls(path)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for k, v in (raw.get("pairs") or {}).items():
                st.pairs[k] = PairState(
                    **{f: v.get(f) for f in PairState.__dataclass_fields__ if f in v}
                )
        return st

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "updated": datetime.now(UTC).isoformat(timespec="seconds"),
            "pairs": {k: asdict(v) for k, v in sorted(self.pairs.items())},
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, pair: str) -> PairState:
        return self.pairs.setdefault(pair, PairState(pair=pair))


# ---------------------------------------------------------------------- products


@dataclass
class ProductFiles:
    product_dir: Path
    name: str
    files: dict[str, Path]  # suffix -> file
    metadata: dict[str, str]  # keys with spaces removed (prep_hyp3 convention)


def parse_metadata_txt(path: Path) -> dict[str, str]:
    """Parse ``<product>.txt`` like MintPy prep_hyp3 (``key, value = line.replace(' ','').split(':')[:2]``)."""
    meta: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if ":" not in line:
            continue
        key, value = line.strip().replace(" ", "").split(":")[:2]
        if key:
            meta[key] = value
    return meta


def product_name_of(name: str) -> tuple[str, str] | None:
    """``(product_name, job_type)`` from a file/dir name, or ``None`` if unrecognised."""
    m = PRODUCT_NAME_RE_BURST.match(name)
    if m:
        return m.group(), JOB_TYPE_BURST
    m = PRODUCT_NAME_RE_MULTI_BURST.match(name)
    if m:
        return m.group(), JOB_TYPE_MULTI_BURST
    return None


def find_product_dir(pair_dir: Path) -> Path | None:
    """The directory holding ``*_unw_phase.tif`` under ``pair_dir`` (itself or one level down)."""
    if not pair_dir.is_dir():
        return None
    candidates = [pair_dir, *sorted(p for p in pair_dir.iterdir() if p.is_dir())]
    for d in candidates:
        if any(f.name.endswith(REQUIRED_SUFFIXES[0]) for f in d.iterdir() if f.is_file()):
            return d
    return None


def extract_product_zip(zip_path: Path, dest: Path, delete: bool = True) -> Path:
    """Extract a HyP3 product zip into ``dest`` and return the product folder.

    # source: hyp3_sdk/util.py extract_zipped_product: extractall(parent); return parent/stem
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():  # guard against path traversal
            if member.startswith(("/", "..")) or ".." in Path(member).parts:
                msg = f"unsafe member in {zip_path.name}: {member}"
                raise ValueError(msg)
        z.extractall(dest)
    if delete:
        zip_path.unlink(missing_ok=True)
    folder = dest / zip_path.stem
    return folder if folder.is_dir() else dest


def validate_product(product_dir: Path, pair: str) -> tuple[ProductFiles | None, list[Finding]]:
    """Check required/recommended files and MintPy metadata keys of one product folder."""
    findings: list[Finding] = []
    files = {p.name: p for p in product_dir.iterdir() if p.is_file()}
    unw = next((p for n, p in files.items() if n.endswith(REQUIRED_SUFFIXES[0])), None)
    stem = unw.name[: -len(REQUIRED_SUFFIXES[0])] if unw else product_dir.name
    parsed = product_name_of(stem)
    name = parsed[0] if parsed else stem
    if parsed is None:
        findings.append(
            Finding(
                rule_id="HYP3-010",
                severity="WARN",
                message_key="engines.hyp3.HYP3-010.cause",
                fix_key="engines.hyp3.HYP3-010.fix",
                params={"name": stem},
                evidence={"dir": mask_text(str(product_dir))},
                scope=pair,
            )
        )
    by_suffix: dict[str, Path] = {}
    for suffix in (*REQUIRED_SUFFIXES, *RECOMMENDED_SUFFIXES):
        p = files.get(f"{name}{suffix}")
        if p is not None:
            by_suffix[suffix] = p
    meta_file = files.get(f"{name}{METADATA_SUFFIX}")
    missing_required = [s for s in REQUIRED_SUFFIXES if s not in by_suffix]
    metadata: dict[str, str] = {}
    if meta_file is None:
        missing_required.append(METADATA_SUFFIX)
    else:
        by_suffix[METADATA_SUFFIX] = meta_file
        metadata = parse_metadata_txt(meta_file)
        missing_required.extend(
            f"{METADATA_SUFFIX}:{k}" for k in REQUIRED_METADATA_KEYS if k not in metadata
        )
    if missing_required:
        findings.append(
            Finding(
                rule_id="HYP3-002",
                severity="FAIL",
                message_key="engines.hyp3.HYP3-002.cause",
                fix_key="engines.hyp3.HYP3-002.fix",
                params={
                    "pair": pair,
                    "missing": ", ".join(missing_required),
                    "product_dir": mask_text(str(product_dir)),
                },
                evidence={"missing": missing_required, "present": sorted(files)},
                scope=pair,
            )
        )
        return None, findings
    missing_rec = [s for s in RECOMMENDED_SUFFIXES if s not in by_suffix]
    if missing_rec:
        findings.append(
            Finding(
                rule_id="HYP3-008",
                severity="WARN",
                message_key="engines.hyp3.HYP3-008.cause",
                fix_key="engines.hyp3.HYP3-008.fix",
                params={"pair": pair, "missing": ", ".join(missing_rec)},
                evidence={"missing": missing_rec},
                scope=pair,
            )
        )
    return ProductFiles(
        product_dir=product_dir, name=name, files=by_suffix, metadata=metadata
    ), findings


# ---------------------------------------------------------------------- clipping (common extent)


def raster_bounds(path: Path) -> tuple[float, float, float, float]:
    import rasterio

    with rasterio.open(path) as src:
        b = src.bounds
        return (float(b.left), float(b.bottom), float(b.right), float(b.top))


def clip_to_common_extent(
    product_dirs: Sequence[Path], suffix: str = CLIP_SUFFIX, tol: float = 1e-9
) -> tuple[bool, list[Finding]]:
    """Write ``<name>_<x>_clip.tif`` copies cropped to the common extent of all products.

    MintPy loads every interferogram into one 3-D stack and requires identical sizes; HyP3
    burst products of the same burst ID differ by a few pixels between dates. Returns
    ``(clipped, findings)``; ``clipped`` is ``False`` when all extents already agree.
    """
    import numpy as np
    import rasterio
    from rasterio.windows import Window, from_bounds
    from rasterio.windows import transform as window_transform

    unw_files: list[Path] = []
    for d in product_dirs:
        f = next((p for p in d.iterdir() if p.name.endswith(REQUIRED_SUFFIXES[0])), None)
        if f is not None:
            unw_files.append(f)
    if len(unw_files) < 2:
        return False, []
    bounds = [raster_bounds(f) for f in unw_files]
    if all(all(abs(b[i] - bounds[0][i]) <= tol for i in range(4)) for b in bounds):
        return False, []
    left = max(b[0] for b in bounds)
    bottom = max(b[1] for b in bounds)
    right = min(b[2] for b in bounds)
    top = min(b[3] for b in bounds)
    if right - left <= 0 or top - bottom <= 0:
        return False, [
            Finding(
                rule_id="HYP3-013",
                severity="FAIL",
                message_key="engines.hyp3.HYP3-013.cause",
                fix_key="engines.hyp3.HYP3-013.fix",
                params={"n_products": len(unw_files)},
                evidence={"bounds": bounds},
                scope="interferogram",
            )
        ]
    # windows per product (same pixel size for all products of one looks option)
    windows: dict[Path, Window] = {}
    for f in unw_files:
        with rasterio.open(f) as src:
            w = from_bounds(left, bottom, right, top, transform=src.transform)
            windows[f.parent] = w.round_offsets().round_lengths()
    height = int(min(w.height for w in windows.values()))
    width = int(min(w.width for w in windows.values()))
    n_written = 0
    for d in product_dirs:
        w0 = windows.get(d)
        if w0 is None:
            continue
        win = Window(int(w0.col_off), int(w0.row_off), width, height)
        for tif in sorted(d.glob("*.tif")):
            if tif.stem.endswith(suffix):
                continue
            out = tif.with_name(f"{tif.stem}{suffix}.tif")
            with rasterio.open(tif) as src:
                data = src.read(window=win)
                profile = src.profile.copy()
                profile.update(
                    height=height, width=width, transform=window_transform(win, src.transform)
                )
            with rasterio.open(out, "w", **profile) as dst:
                dst.write(np.asarray(data))
            n_written += 1
    return True, [
        Finding(
            rule_id="HYP3-009",
            severity="INFO",
            message_key="engines.hyp3.HYP3-009.cause",
            fix_key="engines.hyp3.HYP3-009.fix",
            params={"n_products": len(unw_files)},
            evidence={
                "common_bounds": [left, bottom, right, top],
                "size": [height, width],
                "n_written": n_written,
            },
            scope="interferogram",
        )
    ]


# ---------------------------------------------------------------------- job specs from inputs


def load_candidates(
    inputs: Artifacts, params: Mapping[str, Any]
) -> tuple[StackCandidate, dict[str, dict[str, str]]]:
    """Read ``candidates.json`` (``wintersar search`` output) and the granule mapping.

    Accepted shapes: a ``StackCandidate`` dict, a list of them, or ``{"stacks": [...]}`` /
    ``{"candidates": [...]}``. Granule names per date come from ``stack.notes["granules"]``
    (``{date_iso: {burst_id: granule}}``) or from a top-level ``"granules": {stack_id: ...}``.
    The stack is chosen with ``params["stack_id"]``, else the top-level ``"recommended"``
    id, else the first entry.
    """
    path_v = params.get("candidates")
    if path_v is None and "candidates" in inputs:
        path_v = inputs["candidates"].path
    if path_v is None:
        msg = "hyp3: no candidates.json (inputs['candidates'] or params['candidates']) and no params['jobs']"
        raise FileNotFoundError(msg)
    path = Path(path_v)
    raw = json.loads(path.read_text(encoding="utf-8"))
    stacks_raw: list[dict[str, Any]]
    top: dict[str, Any] = {}
    if isinstance(raw, list):
        stacks_raw = list(raw)
    elif isinstance(raw, dict) and ("stacks" in raw or "candidates" in raw):
        top = raw
        stacks_raw = list(raw.get("stacks") or raw.get("candidates") or [])
    elif isinstance(raw, dict):
        stacks_raw = [raw]
    else:
        msg = f"hyp3: unrecognised candidates.json structure in {mask_text(str(path))}"
        raise ValueError(msg)
    if not stacks_raw:
        msg = f"hyp3: candidates.json has no stacks: {mask_text(str(path))}"
        raise ValueError(msg)
    stacks = [StackCandidate.model_validate(s) for s in stacks_raw]
    wanted = params.get("stack_id") or top.get("recommended")
    stack = next((s for s in stacks if wanted is None or s.stack_id == wanted), None)
    if stack is None:
        msg = (
            f"hyp3: stack {wanted!r} not in candidates.json (known: {[s.stack_id for s in stacks]})"
        )
        raise ValueError(msg)
    granules_raw = (
        stack.notes.get("granules") or (top.get("granules") or {}).get(stack.stack_id) or {}
    )
    granules: dict[str, dict[str, str]] = {}
    for day, per_burst in granules_raw.items():
        if isinstance(per_burst, Mapping):
            granules[str(day)] = {str(b): str(g) for b, g in per_burst.items()}
        else:  # list of granules -> infer burst id from the granule name S1_<burst>_<IW>_...
            granules[str(day)] = {_burst_id_from_granule(str(g)): str(g) for g in per_burst}
    return stack, granules


def _burst_id_from_granule(granule: str) -> str:
    # burst granule: S1_136231_IW2_20200604T022312_VV_7C85-BURST (product guide example)
    parts = granule.split("_")
    return f"{parts[1]}_{parts[2]}" if len(parts) > 3 else granule


def _granule_lookup(per_date: Mapping[str, str], burst_id: str) -> str | None:
    """Tolerant lookup: full id ('052_109903_IW2'), '109903_IW2' or bare '109903'."""
    if burst_id in per_date:
        return per_date[burst_id]
    parts = burst_id.split("_")
    short = "_".join(parts[-2:]) if len(parts) >= 2 else burst_id
    for k, v in per_date.items():
        kp = k.split("_")
        if (
            k == short
            or "_".join(kp[-2:]) == short
            or (len(parts) >= 2 and kp[-2:] and kp[-2] == parts[-2])
        ):
            return v
    return None


def specs_from_stack(
    stack: StackCandidate,
    granules: Mapping[str, Mapping[str, str]],
    *,
    looks: str = DEFAULT_LOOKS,
    apply_water_mask: bool = False,
    name_prefix: str = "wintersar",
) -> tuple[list[JobSpec], list[Finding]]:
    """One :class:`JobSpec` per pair: single-burst job when the stack has one burst id,
    multi-burst job (all bursts of the date, sorted) otherwise."""
    findings: list[Finding] = []
    bursts = sorted(stack.burst_ids)
    if not bursts or len(bursts) > MAX_BURSTS_PER_JOB:
        findings.append(
            Finding(
                rule_id="HYP3-004",
                severity="FAIL",
                message_key="engines.hyp3.HYP3-004.cause",
                fix_key="engines.hyp3.HYP3-004.fix",
                params={"n_bursts": len(bursts), "max_bursts": MAX_BURSTS_PER_JOB},
                evidence={"burst_ids": bursts},
                scope=stack.stack_id,
            )
        )
        return [], findings
    specs: list[JobSpec] = []
    for pair in stack.pairs:
        ref_day, sec_day = pair.reference.isoformat(), pair.secondary.isoformat()
        ref: list[str] = []
        sec: list[str] = []
        missing: tuple[str, str] | None = None
        for b in bursts:
            g_ref = _granule_lookup(granules.get(ref_day, {}), b)
            g_sec = _granule_lookup(granules.get(sec_day, {}), b)
            if g_ref is None:
                missing = (ref_day, b)
                break
            if g_sec is None:
                missing = (sec_day, b)
                break
            ref.append(g_ref)
            sec.append(g_sec)
        if missing is not None:
            findings.append(
                Finding(
                    rule_id="HYP3-007",
                    severity="WARN",
                    message_key="engines.hyp3.HYP3-007.cause",
                    fix_key="engines.hyp3.HYP3-007.fix",
                    params={"pair": pair.key, "date": missing[0], "burst_id": missing[1]},
                    evidence={"date": missing[0], "burst_id": missing[1]},
                    scope=pair.key,
                )
            )
            continue
        specs.append(
            JobSpec(
                pair=pair.key,
                reference=tuple(ref),
                secondary=tuple(sec),
                looks=looks,
                apply_water_mask=apply_water_mask,
                name=f"{name_prefix}_{stack.stack_id}_{pair.key}",
            )
        )
    return specs, findings


def build_job_specs(
    inputs: Artifacts, params: Mapping[str, Any], *, looks: str, apply_water_mask: bool
) -> tuple[list[JobSpec], list[Finding]]:
    """Explicit ``params["jobs"]`` (``[{pair, reference:[...], secondary:[...]}]``) or
    ``candidates.json`` (see :func:`load_candidates`)."""
    explicit = params.get("jobs")
    if explicit:
        specs: list[JobSpec] = []
        findings: list[Finding] = []
        for j in explicit:
            ref = tuple(str(g) for g in j["reference"])
            sec = tuple(str(g) for g in j["secondary"])
            if not ref or len(ref) != len(sec) or len(ref) > MAX_BURSTS_PER_JOB:
                findings.append(
                    Finding(
                        rule_id="HYP3-004",
                        severity="FAIL",
                        message_key="engines.hyp3.HYP3-004.cause",
                        fix_key="engines.hyp3.HYP3-004.fix",
                        params={
                            "n_bursts": max(len(ref), len(sec)),
                            "max_bursts": MAX_BURSTS_PER_JOB,
                        },
                        evidence={"reference": len(ref), "secondary": len(sec)},
                        scope=str(j.get("pair")),
                    )
                )
                continue
            specs.append(
                JobSpec(
                    pair=str(j["pair"]),
                    reference=ref,
                    secondary=sec,
                    looks=str(j.get("looks") or looks),
                    apply_water_mask=bool(j.get("apply_water_mask", apply_water_mask)),
                    name=j.get("name") or f"{params.get('name_prefix', 'wintersar')}_{j['pair']}",
                )
            )
        return specs, findings
    stack, granules = load_candidates(inputs, params)
    return specs_from_stack(
        stack,
        granules,
        looks=looks,
        apply_water_mask=apply_water_mask,
        name_prefix=str(params.get("name_prefix", "wintersar")),
    )


# ---------------------------------------------------------------------- logging helper


class _Log:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} {mask_text(msg)}\n")


def _mapping(params: Mapping[str, Any], key: str) -> dict[str, Any]:
    v = params.get(key)
    return dict(v) if isinstance(v, Mapping) else {}


def _chunks(items: Sequence[JobSpec], n: int) -> Iterable[Sequence[JobSpec]]:
    for i in range(0, len(items), max(1, n)):
        yield items[i : i + n]


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(mask_mapping(obj), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


# ---------------------------------------------------------------------- engine


@register_engine
class Hyp3Engine(Engine):
    """HyP3 burst InSAR adapter: submit → poll → download → validate → MintPy-ready layout."""

    name: ClassVar[str] = "hyp3"
    version_constraint: ClassVar[str] = ">=7.0,<8"  # verified against hyp3-sdk 7.7.8 API
    stages: ClassVar[tuple[str, ...]] = ("interferogram",)
    #: artifacts produced by the single ``interferogram`` stage — the pipeline skips the local
    #: ``unwrap`` stage when the interferogram engine is hyp3 (ADR-0020).
    produces: ClassVar[tuple[str, ...]] = ("igrams", "unw")
    install_hint: ClassVar[str] = (
        "pip install 'wintersar[hyp3]'  (hyp3-sdk>=7, BSD-3-Clause) + Earthdata Login"
    )
    license_note: ClassVar[str] = (
        "hyp3-sdk BSD-3-Clause; products: Copernicus Sentinel data, ASF processing"
    )

    def __init__(
        self,
        client: Hyp3Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self.findings: list[Finding] = []

    # ------------------------------------------------------------------ discovery
    def detect_version(self) -> str | None:
        return python_module_version("hyp3_sdk", "hyp3-sdk")

    def check_install(self) -> list[Finding]:
        findings = super().check_install()
        findings.extend(credentials_findings(resolve_credentials()))
        return findings

    # ------------------------------------------------------------------ estimate
    def estimate(self, plan: Plan) -> Resources:
        total = Resources()
        for rec in plan.stages:
            if rec.stage != "interferogram" or rec.engine not in (None, self.name):
                continue
            p = rec.params
            eng = _mapping(p, "engine")
            n_pairs = int(p.get("n_pairs") or len(p.get("pairs") or p.get("jobs") or []))
            n_bursts = int(p.get("n_bursts", 1))
            looks, _ = resolve_looks(
                p.get("looks", eng.get("looks")), p.get("target_pixel_m", eng.get("target_pixel_m"))
            )
            total = total + estimate_credits(n_pairs, n_bursts, looks)
        return total

    # ------------------------------------------------------------------ run
    def _get_client(self, params: Mapping[str, Any]) -> Hyp3Client:
        if self._client is not None:
            return self._client
        if self.detect_version() is None:
            self.require_available()  # raises EngineNotAvailableError
            msg = "hyp3_sdk not importable"  # pragma: no cover
            raise EngineNotAvailableError(msg)
        creds = resolve_credentials()
        return SdkHyp3Client(api_url=str(params.get("api_url", PROD_API)), token=creds.token)

    def run(
        self, stage: str, inputs: Artifacts, params: dict[str, Any], log_dir: Path
    ) -> Artifacts:
        if stage != "interferogram":
            msg = f"hyp3 engine only implements the 'interferogram' stage, got {stage!r}"
            raise ValueError(msg)
        out_root = Path(params.get("_out_dir") or log_dir.parent)
        data_dir = out_root / "hyp3"
        data_dir.mkdir(parents=True, exist_ok=True)
        log = _Log(log_dir / f"{stage}.log")
        findings: list[Finding] = []
        eng = _mapping(params, "engine")
        looks, lf = resolve_looks(
            params.get("looks", eng.get("looks")),
            params.get("target_pixel_m", eng.get("target_pixel_m")),
        )
        if lf is not None:
            findings.append(lf)
        water = bool(params.get("apply_water_mask", False))
        log(f"START hyp3 interferogram looks={looks} water_mask={water} data_dir={data_dir}")

        specs, spec_findings = build_job_specs(inputs, params, looks=looks, apply_water_mask=water)
        findings.extend(spec_findings)
        if not specs:
            findings.append(
                Finding(
                    rule_id="HYP3-011",
                    severity="FAIL",
                    message_key="engines.hyp3.HYP3-011.cause",
                    fix_key="engines.hyp3.HYP3-011.fix",
                    scope="interferogram",
                )
            )
            return self._finish(stage, data_dir, log_dir, log, findings, [], {}, looks, water)

        state = JobState.load(data_dir / "jobs.json")
        client = self._get_client(params)
        spec_by_pair = {s.pair: s for s in specs}

        # ---- resume (PERF-06): reuse validated products and in-flight job ids
        todo: list[JobSpec] = []
        n_cached = 0
        for spec in specs:
            ps = state.get(spec.pair)
            ps.n_bursts = spec.n_bursts
            if ps.product_dir and find_product_dir(data_dir / ps.product_dir) is not None:
                n_cached += 1
                continue
            ps.product_dir = None
            if ps.job_id and ps.status in _ACTIVE_STATUSES | {STATUS_SUCCEEDED}:
                continue  # poll / download the existing job
            todo.append(spec)
        log(
            f"RESUME cached={n_cached} inflight={len(specs) - n_cached - len(todo)} to_submit={len(todo)}"
        )

        # ---- credits (SEL-13 / HYP3-005)
        if todo and not params.get("ignore_credits", False):
            needed = sum(job_credits(s.looks, s.n_bursts) or 0.0 for s in todo)
            remaining = client.check_credits()
            log(f"CREDITS needed={needed} remaining={remaining}")
            if remaining is not None and needed > remaining:
                findings.append(
                    Finding(
                        rule_id="HYP3-005",
                        severity="FAIL",
                        message_key="engines.hyp3.HYP3-005.cause",
                        fix_key="engines.hyp3.HYP3-005.fix",
                        params={"needed": needed, "remaining": remaining, "n_jobs": len(todo)},
                        evidence={"needed": needed, "remaining": remaining},
                        scope="interferogram",
                    )
                )
                state.save()
                return self._finish(stage, data_dir, log_dir, log, findings, [], {}, looks, water)

        # ---- submit in batches
        batch_size = int(params.get("submit_batch_size", SUBMIT_BATCH_SIZE))
        for chunk in _chunks(todo, batch_size):
            infos = client.submit(chunk)
            for spec, info in zip(chunk, infos, strict=True):
                ps = state.get(spec.pair)
                ps.job_id = info.job_id
                ps.status = info.status_code
                ps.job_type = info.job_type
                ps.name = info.name or spec.name
                ps.submitted_at = datetime.now(UTC).isoformat(timespec="seconds")
                ps.credit_cost = info.credit_cost
                log(
                    f"SUBMITTED pair={spec.pair} job_id={info.job_id} type={info.job_type} bursts={spec.n_bursts}"
                )
            state.save()

        # ---- poll with backoff
        jobs, poll_findings = self._poll(client, state, params, log)
        findings.extend(poll_findings)

        # ---- download + validate
        products: dict[str, ProductFiles] = {}
        for pair, ps in state.pairs.items():
            if pair not in spec_by_pair:
                continue
            if ps.product_dir:
                pd = find_product_dir(data_dir / ps.product_dir)
                if pd is not None:
                    pf, vf = validate_product(pd, pair)
                    findings.extend(f for f in vf if f.severity != "WARN")
                    if pf is not None:
                        products[pair] = pf
                        continue
            if ps.status == STATUS_FAILED:
                failed = jobs.get(ps.job_id or "")
                reason = "; ".join(failed.logs) if failed and failed.logs else "see HyP3 job logs"
                log(f"JOB_FAILED pair={pair} job_id={ps.job_id} reason={reason}")
                findings.append(
                    Finding(
                        rule_id="HYP3-001",
                        severity="FAIL",
                        message_key="engines.hyp3.HYP3-001.cause",
                        fix_key="engines.hyp3.HYP3-001.fix",
                        params={
                            "pair": pair,
                            "job_id": ps.job_id,
                            "reason": reason,
                            "log_url": (failed.logs[0] if failed and failed.logs else "-"),
                        },
                        evidence={"job_id": ps.job_id, "job_type": ps.job_type},
                        scope=pair,
                    )
                )
                continue
            if ps.status != STATUS_SUCCEEDED or not ps.job_id:
                continue
            jinfo: JobInfo | None = jobs.get(ps.job_id)
            if jinfo is None:
                jinfo = next(iter(client.refresh([ps.job_id])), None)
            if jinfo is None:
                continue
            if jinfo.expired():
                log(f"JOB_EXPIRED pair={pair} job_id={ps.job_id}")
                findings.append(
                    Finding(
                        rule_id="HYP3-012",
                        severity="WARN",
                        message_key="engines.hyp3.HYP3-012.cause",
                        fix_key="engines.hyp3.HYP3-012.fix",
                        params={"pair": pair, "job_id": ps.job_id},
                        scope=pair,
                    )
                )
                ps.job_id, ps.status = None, "EXPIRED"
                continue
            pair_dir = data_dir / pair
            pair_dir.mkdir(parents=True, exist_ok=True)
            downloaded = client.download(jinfo, pair_dir)
            for p in downloaded:
                if p.suffix.lower() == ".zip" and p.exists():
                    extract_product_zip(p, pair_dir)
            pd = find_product_dir(pair_dir)
            if pd is None:
                findings.append(
                    Finding(
                        rule_id="HYP3-002",
                        severity="FAIL",
                        message_key="engines.hyp3.HYP3-002.cause",
                        fix_key="engines.hyp3.HYP3-002.fix",
                        params={
                            "pair": pair,
                            "missing": REQUIRED_SUFFIXES[0],
                            "product_dir": mask_text(str(pair_dir)),
                        },
                        evidence={"downloaded": [mask_text(str(x)) for x in downloaded]},
                        scope=pair,
                    )
                )
                continue
            pf, vf = validate_product(pd, pair)
            findings.extend(vf)
            if pf is None:
                continue
            ps.product_dir = pd.relative_to(data_dir).as_posix()
            ps.credit_cost = jinfo.credit_cost if jinfo.credit_cost is not None else ps.credit_cost
            products[pair] = pf
            log(
                f"DOWNLOADED pair={pair} product={pf.name} files={len(pf.files)} credits={ps.credit_cost}"
            )
            state.save()
        state.save()

        # ---- clip to common extent (MintPy needs identical sizes)
        clipped = False
        if params.get("clip_to_common_extent", True) and len(products) > 1:
            clipped, cf = clip_to_common_extent([p.product_dir for p in products.values()])
            findings.extend(cf)
        return self._finish(
            stage,
            data_dir,
            log_dir,
            log,
            findings,
            list(products.values()),
            state.pairs,
            looks,
            water,
            clipped,
        )

    def _poll(
        self, client: Hyp3Client, state: JobState, params: Mapping[str, Any], log: _Log
    ) -> tuple[dict[str, JobInfo], list[Finding]]:
        timeout = float(params.get("poll_timeout_s", POLL_TIMEOUT_S))
        interval = float(params.get("poll_interval_s", POLL_INTERVAL_S))
        max_interval = float(params.get("poll_max_interval_s", POLL_MAX_INTERVAL_S))
        jobs: dict[str, JobInfo] = {}
        pending = [
            ps.job_id for ps in state.pairs.values() if ps.job_id and ps.status in _ACTIVE_STATUSES
        ]
        start = self._clock()
        findings: list[Finding] = []
        while pending:
            for info in client.refresh(pending):
                jobs[info.job_id] = info
                for ps in state.pairs.values():
                    if ps.job_id == info.job_id:
                        ps.status = info.status_code
                        if info.credit_cost is not None:
                            ps.credit_cost = info.credit_cost
            state.save()
            pending = [jid for jid in pending if jid in jobs and not jobs[jid].complete]
            if not pending:
                break
            elapsed = self._clock() - start
            if elapsed >= timeout:
                log(f"POLL_TIMEOUT pending={len(pending)} timeout_s={timeout:g}")
                findings.append(
                    Finding(
                        rule_id="HYP3-006",
                        severity="FAIL",
                        message_key="engines.hyp3.HYP3-006.cause",
                        fix_key="engines.hyp3.HYP3-006.fix",
                        params={"n_pending": len(pending), "timeout_s": int(timeout)},
                        evidence={"pending_job_ids": pending},
                        scope="interferogram",
                    )
                )
                break
            log(f"POLL pending={len(pending)} elapsed_s={elapsed:.0f} next_in_s={interval:.0f}")
            self._sleep(interval)
            interval = min(max_interval, interval * POLL_BACKOFF)
        # succeeded jobs that were already complete before this run
        done_ids = [
            ps.job_id
            for ps in state.pairs.values()
            if ps.job_id
            and ps.status == STATUS_SUCCEEDED
            and ps.job_id not in jobs
            and not ps.product_dir
        ]
        if done_ids:
            for info in client.refresh(done_ids):
                jobs[info.job_id] = info
        return jobs, findings

    def _finish(
        self,
        stage: str,
        data_dir: Path,
        log_dir: Path,
        log: _Log,
        findings: list[Finding],
        products: list[ProductFiles],
        pairs: Mapping[str, PairState],
        looks: str,
        water: bool,
        clipped: bool = False,
    ) -> Artifacts:
        self.findings = findings
        _write_json(
            log_dir / f"{stage}.findings.json", [f.model_dump(mode="json") for f in findings]
        )
        clip = CLIP_SUFFIX if clipped else ""
        patterns = {k: f"*/*/*{suffix}{clip}.tif" for k, suffix in MINTPY_LOAD_SUFFIXES.items()}
        credits_used = sum(ps.credit_cost or 0.0 for ps in pairs.values() if ps.product_dir)
        manifest = {
            "engine": self.name,
            "hyp3_sdk_version": self.detect_version(),
            "hyp3_sdk_verified": HYP3_SDK_VERIFIED_VERSION,
            "looks": looks,
            "pixel_m": LOOKS_PIXEL_M[looks],
            "apply_water_mask": water,
            "clipped": clipped,
            "patterns": patterns,
            "n_pairs": len(products),
            "pairs": sorted(p.product_dir.parent.name for p in products),
            "credits_used": credits_used,
            "jobs": [asdict(ps) for _, ps in sorted(pairs.items())],
            "products": [
                {
                    "pair": p.product_dir.parent.name,
                    "name": p.name,
                    "dir": str(p.product_dir),
                    "metadata": p.metadata,
                }
                for p in products
            ],
            "findings": [f.rule_id for f in findings],
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _write_json(data_dir / "manifest.json", manifest)
        n_fail = sum(1 for f in findings if f.is_fail)
        log(
            f"END products={len(products)} credits_used={credits_used} findings={len(findings)} fail={n_fail}"
        )
        if n_fail:
            msg = f"hyp3 interferogram stage failed: {[f.rule_id for f in findings if f.is_fail]}"
            raise Hyp3RunError(msg, findings)
        meta = {
            "format": "hyp3_geotiff",
            "processor": "hyp3",
            "looks": looks,
            "pixel_m": LOOKS_PIXEL_M[looks],
            "n_pairs": len(products),
            "pairs": manifest["pairs"],
            "patterns": patterns,
            "clipped": clipped,
            "credits_used": credits_used,
            "unwrapped": True,
            "conncomp": all("_conncomp.tif" in p.files for p in products),
            "findings": [f.rule_id for f in findings],
        }
        arts = Artifacts()
        arts.add(Artifact(name="igrams", path=data_dir, kind="dir", meta=meta))
        arts.add(
            Artifact(
                name="unw",
                path=data_dir,
                kind="dir",
                meta={**meta, "source_stage": "interferogram"},
            )
        )
        return arts

    # ------------------------------------------------------------------ logs
    def parse_log(self, log_path: Path) -> list[Finding]:
        """Findings from the adapter's own ``interferogram.log`` (JOB_FAILED / POLL_TIMEOUT lines)."""
        findings: list[Finding] = []
        if not log_path.exists():
            return findings
        for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.search(r"JOB_FAILED pair=(\S+) job_id=(\S+) reason=(.*)$", line)
            if m:
                findings.append(
                    Finding(
                        rule_id="HYP3-001",
                        severity="FAIL",
                        message_key="engines.hyp3.HYP3-001.cause",
                        fix_key="engines.hyp3.HYP3-001.fix",
                        params={
                            "pair": m.group(1),
                            "job_id": m.group(2),
                            "reason": m.group(3),
                            "log_url": "-",
                        },
                        evidence={"line": mask_text(line)},
                        scope=m.group(1),
                    )
                )
                continue
            m = re.search(r"POLL_TIMEOUT pending=(\d+) timeout_s=(\S+)", line)
            if m:
                findings.append(
                    Finding(
                        rule_id="HYP3-006",
                        severity="FAIL",
                        message_key="engines.hyp3.HYP3-006.cause",
                        fix_key="engines.hyp3.HYP3-006.fix",
                        params={"n_pending": int(m.group(1)), "timeout_s": m.group(2)},
                        evidence={"line": mask_text(line)},
                        scope="interferogram",
                    )
                )
        return findings
