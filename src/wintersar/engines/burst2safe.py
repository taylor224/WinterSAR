"""``burst2safe`` wrapper: rebuild ESA SAFE products from ASF burst granules (PERF-01).

topsStack (``stackSentinel.py -s <slc_dir>``) consumes SAFE zips/directories, while the
wintersar ``select`` path works on burst granules. `burst2safe <https://github.com/ASFHyP3/burst2safe>`_
(ASF, BSD-2-Clause; PyPI 2.0.3, 2026-08-21) downloads the bursts and assembles one SAFE per
date. It is **optional** (below the adoption threshold, ADR-0001): when it is not installed
the adapter emits ``ENV-006`` and expects ``slc_dir`` to be provided by the user.

Verified facts (ADR-0026):

* CLI ``burst2safe [granules ...] [--orbit N] [--extent W S E N | file] [--pols VV VH]
  [--swaths IW1 IW2 IW3] [--mode IW] [--min-bursts 1] [--all-anns] [--output-dir DIR]
  [--keep-files] [-v]``; python API ``burst2safe(granules, orbit, extent, polarizations,
  swaths, mode='IW', min_bursts=1, all_anns=False, keep_files=False, work_dir=None) -> Path``
  # source: https://github.com/ASFHyP3/burst2safe/blob/main/src/burst2safe/burst2safe.py
* "To use burst2safe, you must provide your Earthdata Login credentials via two environment
  variables (EARTHDATA_USERNAME and EARTHDATA_PASSWORD), or via your .netrc file."; an
  ``EARTHDATA_TOKEN`` variable is also accepted (priority token > .netrc > username/password).
  ISCE2 (including TopsStack) compatibility: "Yes | 2.6.3 | None" (no required flags;
  ``--all-anns`` is only needed for ISCE3/s1-reader).
  # source: https://github.com/ASFHyP3/burst2safe/blob/main/README.md
* CLI (argparse in ``main()``): positional ``granules`` (nargs='*'), ``--orbit`` (int),
  ``--extent`` (nargs='+'), ``--pols`` (nargs='+'), ``--swaths`` (nargs='+'), ``--mode``
  (default 'IW'), ``--min-bursts`` (int, default 1), ``--all-anns``, ``--output-dir``,
  ``--keep-files``, ``-v/--verbose``.
  # source: https://github.com/ASFHyP3/burst2safe/blob/main/src/burst2safe/burst2safe.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from wintersar.engines.base import python_module_version
from wintersar.io.schemas import Finding
from wintersar.util.masking import mask_text

EXECUTABLE = "burst2safe"
BURST2SAFE_VERIFIED_VERSION = "2.0.3"  # source: https://pypi.org/pypi/burst2safe/json (2026-09-16)
INSTALL_HINT = "pip install burst2safe  (or: conda install -c conda-forge burst2safe; BSD-2-Clause)"
CREDENTIAL_ENV = ("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD")  # source: burst2safe README
TOKEN_ENV = "EARTHDATA_TOKEN"  # source: burst2safe README ("token > .netrc > username/password")

Runner = Callable[[list[str], Path, Path], int]


def find_executable() -> str | None:
    return shutil.which(EXECUTABLE)


def detect_version() -> str | None:
    """Installed burst2safe version (``None`` when neither module nor executable exist)."""
    v = python_module_version("burst2safe")
    if v:
        return v
    return "unknown" if find_executable() else None


def credentials_available() -> bool:
    """Earthdata Login for burst2safe: token, both user/password env vars, or ``~/.netrc``."""
    if os.environ.get(TOKEN_ENV) or all(os.environ.get(k) for k in CREDENTIAL_ENV):
        return True
    netrc = Path(os.environ.get("NETRC") or (Path.home() / ".netrc"))
    return netrc.is_file()


def check_install() -> list[Finding]:
    """``ENV-006`` when burst2safe is absent (optional package); ``ISCE2-005`` without creds."""
    findings: list[Finding] = []
    if detect_version() is None:
        findings.append(
            Finding(
                rule_id="ENV-006",
                severity="WARN",
                message_key="env.ENV-006.cause",
                fix_key="env.ENV-006.fix",
                params={"package": EXECUTABLE, "install_hint": INSTALL_HINT},
                evidence={"executable": EXECUTABLE},
                scope="fetch",
            )
        )
        return findings
    if not credentials_available():
        findings.append(
            Finding(
                rule_id="ISCE2-005",
                severity="WARN",
                message_key="engines.isce2.ISCE2-005.cause",
                fix_key="engines.isce2.ISCE2-005.fix",
                params={"env_vars": "/".join((TOKEN_ENV, *CREDENTIAL_ENV))},
                scope="fetch",
            )
        )
    return findings


def build_argv(
    granules: Sequence[str],
    output_dir: Path,
    *,
    polarizations: Sequence[str] | None = None,
    swaths: Sequence[str] | None = None,
    mode: str | None = None,
    min_bursts: int | None = None,
    all_anns: bool = False,
    keep_files: bool = False,
    executable: str | None = None,
) -> list[str]:
    """``burst2safe`` argv from verified flags (``# source`` in the module docstring)."""
    if not granules:
        msg = "burst2safe: at least one burst granule is required"
        raise ValueError(msg)
    argv = [executable or EXECUTABLE, *granules, "--output-dir", str(output_dir)]
    if polarizations:
        argv += ["--pols", *polarizations]
    if swaths:
        argv += ["--swaths", *swaths]
    if mode:
        argv += ["--mode", mode]
    if min_bursts is not None:
        argv += ["--min-bursts", str(int(min_bursts))]
    if all_anns:
        argv.append("--all-anns")
    if keep_files:
        argv.append("--keep-files")
    return argv


def _subprocess_runner(argv: list[str], cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"# {datetime.now(UTC).isoformat(timespec='seconds')} $ {mask_text(' '.join(argv))}\n"
        )
        fh.flush()
        try:
            proc = subprocess.run(
                argv, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT, check=False, text=True
            )
        except OSError as e:
            fh.write(f"# could not start burst2safe: {mask_text(str(e))}\n")
            return 127
        fh.write(f"# exit={proc.returncode}\n")
        return int(proc.returncode)


def list_safes(directory: Path) -> list[Path]:
    """SAFE products (directories or zips) directly inside ``directory``."""
    if not directory.is_dir():
        return []
    out = [
        p for p in directory.iterdir() if p.name.endswith(".SAFE") or p.name.endswith(".SAFE.zip")
    ]
    return sorted(out)


def build_safes(
    granules_per_date: Mapping[str, Sequence[str]],
    output_dir: Path,
    log_dir: Path,
    *,
    polarizations: Sequence[str] | None = None,
    swaths: Sequence[str] | None = None,
    runner: Runner | None = None,
    executable: str | None = None,
) -> tuple[dict[str, Path], list[Finding]]:
    """One ``burst2safe`` call per date; returns ``{date: SAFE path}`` + findings.

    Dates whose SAFE already exists in ``output_dir`` (any ``*<YYYYMMDD>T*.SAFE``) are skipped
    (PERF-02: no re-download). A failed date yields an ``ISCE2-004`` FAIL finding.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run = runner or _subprocess_runner
    exe = executable or find_executable() or EXECUTABLE
    safes: dict[str, Path] = {}
    findings: list[Finding] = []
    for day, granules in sorted(granules_per_date.items()):
        ymd = day.replace("-", "")[:8]
        existing = [p for p in list_safes(output_dir) if f"_{ymd}T" in p.name]
        if existing:
            safes[day] = existing[0]
            continue
        before = set(list_safes(output_dir))
        argv = build_argv(
            list(granules), output_dir, polarizations=polarizations, swaths=swaths, executable=exe
        )
        log_path = Path(log_dir) / f"burst2safe_{ymd}.log"
        rc = run(argv, output_dir, log_path)
        created = sorted(set(list_safes(output_dir)) - before)
        if rc != 0 or not created:
            findings.append(
                Finding(
                    rule_id="ISCE2-004",
                    severity="FAIL",
                    message_key="engines.isce2.ISCE2-004.cause",
                    fix_key="engines.isce2.ISCE2-004.fix",
                    params={
                        "date": day,
                        "n_granules": len(granules),
                        "returncode": rc,
                        "log": mask_text(str(log_path)),
                    },
                    evidence={"granules": list(granules), "returncode": rc},
                    scope="fetch",
                )
            )
            continue
        safes[day] = created[0]
    return safes, findings
