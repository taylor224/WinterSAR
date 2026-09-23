"""Golden *statistics* of the synthetic S site (plan §8 "regression" row, ADR-0100..0102).

The golden file ``tests/regression/golden/<site>/stats.json`` is a small JSON of statistics
taken from one deterministic fake-engine pipeline run (``benchmarks/sites/S_synthetic.yaml``,
interferogram ``seed`` = :data:`GOLDEN_SEED`, i.e. bench repeat 0): velocity-map
min/max/mean/std/percentiles, masked fractions, ``closure_rms`` / ``unwrap_error_fraction``
(the bench metric definitions from :mod:`wintersar.bench.runner`), the sorted ``Finding``
rule ids per stage, ``n_pairs`` / ``n_dates`` and the artifact array shapes. It never holds
arrays, timings, memory, git hashes, timestamps, hostnames or paths (rule 11.8 / 11.11), so
it is machine-independent and diffable in a PR.

Three layers use it:

* :func:`compute_golden` runs the pipeline (``wintersar.pipeline.api.run``) on a fresh work
  directory and reduces the result with :func:`golden_stats`.
* :func:`compare_golden` compares two statistics mappings with the ADR-0102 tolerance policy
  (exact for everything that is not a float, ``rtol``/``atol`` for floats, a wider absolute
  tolerance for ``*_fraction`` keys); :func:`mismatches_to_findings` turns the differences
  into ``GOLDEN-00x`` findings.
* :func:`main_make` / :func:`main_check` back ``scripts/make_golden.py`` (regenerate, or
  ``--check``) and ``scripts/check_golden.py`` (CI entry point, exit 1 on mismatch).
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from numpy.typing import NDArray

from wintersar.bench.runner import compute_metrics
from wintersar.bench.sites import Site, build_config, load_site
from wintersar.i18n import t
from wintersar.io.schemas import Artifact, Finding, Severity
from wintersar.util.masking import mask_mapping, mask_text
from wintersar.util.output import (
    console,
    emit_json,
    err_console,
    findings_to_markdown,
    print_findings,
    to_jsonable,
)

if TYPE_CHECKING:
    from wintersar.pipeline.api import RunResult

GOLDEN_SCHEMA_VERSION = 1
GENERATOR = "wintersar.bench.golden"
# source: src/wintersar/bench/runner.py::pipeline_runner — ``seed`` defaults to the repeat
# index, so the golden is bench repeat 0 of the same site definition.
GOLDEN_SEED = 0
GOLDEN_FILENAME = "stats.json"
GOLDEN_DIRNAME = Path("tests") / "regression" / "golden"
DEFAULT_SITE = Path("benchmarks") / "sites" / "S_synthetic.yaml"
MAX_GOLDEN_BYTES = 50_000
PERCENTILES: tuple[int, ...] = (5, 25, 50, 75, 95)
# Site fields that must be identical between the golden and the current YAML (exact).
SITE_KEYS: tuple[str, ...] = (
    "name",
    "size",
    "runner",
    "stages",
    "metrics",
    "config",
    "param_overrides",
)
# Keys that would make the golden machine- or time-dependent; the unit tests assert none of
# them appears anywhere in the file.
FORBIDDEN_KEYS: tuple[str, ...] = (
    "wall_time_s",
    "cpu_time_s",
    "peak_rss_gb",
    "disk_peak_gb",
    "network_bytes",
    "created_at",
    "git_sha",
    "machine",
    "hostname",
    "path",
    "node_hash",
    "sha256",
)

MismatchKind = Literal["missing", "extra", "type", "value", "float", "length"]


# ============================================================================ tolerances
@dataclass(frozen=True)
class Tolerances:
    """ADR-0102 tolerance policy.

    * anything that is not a float (ints, bools, strings, ``None``, list lengths): exact;
    * floats: ``|a - b| <= atol + rtol * |b|`` (``numpy.isclose`` semantics with ``b`` the
      golden value);
    * floats whose leaf key ends with ``fraction_suffix`` (pixel fractions): absolute
      ``fraction_atol`` only — a one-ulp change of a coherence value at the threshold flips
      one pixel, which must not fail the layer, whereas an algorithm change moves fractions
      by orders of magnitude more.
    """

    rtol: float = 1e-6
    atol: float = 1e-9
    fraction_atol: float = 1e-4
    fraction_suffix: str = "_fraction"

    def for_path(self, path: str) -> tuple[float, float]:
        leaf = path.rsplit(".", 1)[-1].split("[", 1)[0]
        if leaf.endswith(self.fraction_suffix):
            return 0.0, self.fraction_atol
        return self.rtol, self.atol

    def describe(self, path: str) -> str:
        rtol, atol = self.for_path(path)
        return f"atol={atol:g}" if rtol == 0.0 else f"rtol={rtol:g} atol={atol:g}"

    def close(self, path: str, a: float, b: float) -> bool:
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        if math.isinf(a) or math.isinf(b):
            return a == b
        rtol, atol = self.for_path(path)
        return abs(a - b) <= atol + rtol * abs(b)


DEFAULT_TOLERANCES = Tolerances()


@dataclass(frozen=True)
class Mismatch:
    path: str
    kind: MismatchKind
    expected: Any
    actual: Any
    tolerance: str = "exact"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "expected": self.expected,
            "actual": self.actual,
            "tolerance": self.tolerance,
        }


def _is_float(v: Any) -> bool:
    return isinstance(v, float) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def compare_golden(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    tol: Tolerances = DEFAULT_TOLERANCES,
) -> list[Mismatch]:
    """All differences between the golden ``expected`` and the freshly computed ``actual``.

    Missing and extra keys are mismatches too (a schema change must regenerate the golden);
    lists are compared element-wise after an exact length check.
    """
    out: list[Mismatch] = []
    _walk("", expected, actual, tol, out)
    return out


def _walk(path: str, e: Any, a: Any, tol: Tolerances, out: list[Mismatch]) -> None:
    if isinstance(e, Mapping) and isinstance(a, Mapping):
        for k in e:
            sub = f"{path}.{k}" if path else str(k)
            if k not in a:
                out.append(Mismatch(sub, "missing", e[k], None))
            else:
                _walk(sub, e[k], a[k], tol, out)
        for k in a:
            if k not in e:
                sub = f"{path}.{k}" if path else str(k)
                out.append(Mismatch(sub, "extra", None, a[k]))
        return
    if isinstance(e, list) and isinstance(a, list):
        if len(e) != len(a):
            out.append(Mismatch(path, "length", len(e), len(a)))
            return
        for i, (x, y) in enumerate(zip(e, a, strict=True)):
            _walk(f"{path}[{i}]", x, y, tol, out)
        return
    if _is_number(e) and _is_number(a):
        if _is_float(e) or _is_float(a):
            if not tol.close(path, float(a), float(e)):
                out.append(Mismatch(path, "float", e, a, tol.describe(path)))
        elif e != a:
            out.append(Mismatch(path, "value", e, a))
        return
    if type(e) is not type(a):
        out.append(Mismatch(path, "type", e, a))
        return
    if e != a:
        out.append(Mismatch(path, "value", e, a))


def _short(v: Any, limit: int = 120) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _finding(rule_id: str, severity: Severity, scope: str | None = None, **params: Any) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message_key=f"golden.{rule_id}.cause",
        fix_key=f"golden.{rule_id}.fix",
        params=params,
        evidence=dict(params),
        scope=scope,
    )


def mismatches_to_findings(mismatches: Sequence[Mismatch]) -> list[Finding]:
    """``GOLDEN-003`` for differences under ``site.*`` (the site YAML changed since the golden
    was made), ``GOLDEN-001`` for every other difference."""
    out: list[Finding] = []
    for m in mismatches:
        rule = "GOLDEN-003" if m.path == "site" or m.path.startswith("site.") else "GOLDEN-001"
        f = _finding(
            rule,
            "FAIL",
            scope=m.path,
            path=m.path,
            kind=m.kind,
            expected=_short(m.expected),
            actual=_short(m.actual),
            tolerance=m.tolerance,
        )
        f.evidence["expected_value"] = m.expected
        f.evidence["actual_value"] = m.actual
        out.append(f)
    return out


def format_mismatches(mismatches: Sequence[Mismatch], limit: int = 40) -> str:
    """Plain-text listing for assertion messages (one line per mismatch)."""
    lines = [
        f"{m.path}: {m.kind} expected={_short(m.expected)} actual={_short(m.actual)}"
        + (f" ({m.tolerance})" if m.kind == "float" else "")
        for m in mismatches[:limit]
    ]
    if len(mismatches) > limit:
        lines.append(f"... {len(mismatches) - limit} more")
    return "\n".join(lines)


# ============================================================================ statistics
def _load_arrays(path: Path | None) -> dict[str, NDArray[Any]] | None:
    if path is None or not path.exists():
        return None
    if path.suffix == ".npz":
        with np.load(path, allow_pickle=False) as z:
            return {k: z[k] for k in z.files}
    if path.suffix == ".npy":
        return {path.stem: np.load(path, allow_pickle=False)}
    return None


def array_stats(a: NDArray[Any]) -> dict[str, Any]:
    """min/max/mean/std/percentiles over the finite values of ``a`` (float64 accumulation
    so that float32 inputs give the same numbers on every platform up to ~1e-9)."""
    x = np.asarray(a, dtype=np.float64).ravel()
    finite = x[np.isfinite(x)]
    out: dict[str, Any] = {
        "n": int(x.size),
        "n_finite": int(finite.size),
        "nan_fraction": float(1.0 - finite.size / x.size) if x.size else 0.0,
    }
    if finite.size == 0:
        return out
    out.update(
        {
            "min": float(finite.min()),
            "max": float(finite.max()),
            "mean": float(finite.mean()),
            "std": float(finite.std()),
        }
    )
    pct = np.percentile(finite, PERCENTILES)
    out.update({f"p{p:02d}": float(v) for p, v in zip(PERCENTILES, pct, strict=True)})
    return out


def _array_shapes(arrays: Mapping[str, NDArray[Any]]) -> dict[str, dict[str, Any]]:
    return {k: {"shape": list(v.shape), "dtype": str(v.dtype)} for k, v in sorted(arrays.items())}


def _artifact_block(art: Artifact) -> dict[str, Any]:
    block: dict[str, Any] = {"kind": art.kind}
    arrays = _load_arrays(art.path)
    if arrays is not None:
        block["arrays"] = _array_shapes(arrays)
    elif art.path.suffix == ".json" and art.path.exists():
        try:
            data = json.loads(art.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            block["keys"] = sorted(str(k) for k in data)
    return block


def site_block(site: Site) -> dict[str, Any]:
    """The part of the site definition the golden depends on (exact comparison)."""
    return {k: copy.deepcopy(getattr(site, k)) for k in SITE_KEYS}


def golden_stats(result: RunResult, site: Site, seed: int = GOLDEN_SEED) -> dict[str, Any]:
    """Reduce a pipeline :class:`RunResult` to the machine-independent golden statistics."""
    arts = dict(result.artifacts.items)
    stats: dict[str, Any] = {
        "schema_version": GOLDEN_SCHEMA_VERSION,
        "generator": GENERATOR,
        "site": site_block(site),
        "seed": int(seed),
        "run": {
            "ok": bool(result.ok),
            "failed_stage": result.failed_stage,
            "findings": sorted(f.rule_id for f in result.findings),
        },
        "stages": {
            rec.stage: {
                "status": rec.status,
                "engine": rec.engine,
                "findings": sorted(f.rule_id for f in rec.findings),
            }
            for rec in result.records
        },
        "artifacts": {name: _artifact_block(a) for name, a in sorted(arts.items())},
    }
    ig = _load_arrays(arts["igrams"].path) if "igrams" in arts else None
    if ig is not None and "pairs" in ig and "dates" in ig:
        pairs = [str(p) for p in ig["pairs"]]
        dates = [str(d) for d in ig["dates"]]
        stats["n_pairs"] = len(pairs)
        stats["n_dates"] = len(dates)
        igrams: dict[str, Any] = {"pairs": pairs, "dates": dates}
        if "coherence" in ig:
            igrams["coherence"] = array_stats(ig["coherence"])
        if "wrapped" in ig:
            igrams["wrapped_abs_mean"] = float(
                np.mean(np.abs(np.asarray(ig["wrapped"], dtype=np.float64)))
            )
        if "mask" in ig:
            igrams["mask_fraction"] = float(np.mean(np.asarray(ig["mask"], dtype=np.float64)))
        stats["igrams"] = igrams
    un = _load_arrays(arts["unw"].path) if "unw" in arts else None
    if un is not None and "unw" in un:
        unw = np.asarray(un["unw"], dtype=np.float64)
        block: dict[str, Any] = {
            "masked_fraction": float(np.mean(~np.isfinite(unw))),
            "unw": array_stats(unw),
        }
        if "conncomp" in un:
            cc = np.asarray(un["conncomp"])
            block["conncomp_n_labels"] = int(np.unique(cc).size)
            block["conncomp_nonzero_fraction"] = float(np.mean(cc != 0))
        stats["unwrap"] = block
    ts = _load_arrays(arts["timeseries"].path) if "timeseries" in arts else None
    if ts is not None and "displacement_m" in ts:
        disp = np.asarray(ts["displacement_m"], dtype=np.float64)
        stats["timeseries"] = {
            "n_dates": int(disp.shape[0]),
            "displacement_last_epoch": array_stats(disp[-1]),
        }
    vel = _load_arrays(arts["velocity"].path) if "velocity" in arts else None
    if vel is not None:
        v = next(iter(vel.values()))
        stats["velocity"] = {"shape": list(v.shape), **array_stats(v)}
    metrics = compute_metrics({n: a.path for n, a in arts.items()}, site)
    stats["metrics"] = {k: (None if v is None else float(v)) for k, v in metrics.items()}
    return _plain(stats)


def _plain(stats: dict[str, Any]) -> dict[str, Any]:
    masked = mask_mapping(to_jsonable(stats))
    return dict(masked) if isinstance(masked, Mapping) else {}


# ============================================================================ run / io
def run_golden_pipeline(site: Site, workdir: Path | str, seed: int = GOLDEN_SEED) -> RunResult:
    """``wintersar.pipeline.api.run`` on a fresh work directory — the same construction as
    ``bench.runner.pipeline_runner`` (``build_config`` + ``site.param_overrides``, seed only
    when the site does not fix one), with the aux cache kept inside ``workdir`` so a golden
    run never touches ``~/.cache``."""
    from wintersar.pipeline import api

    wd = Path(workdir)
    cfg = build_config(site, wd)
    cfg.compute.cache_dir = wd / "cache"
    overrides = copy.deepcopy(site.param_overrides)
    overrides.setdefault("interferogram", {}).setdefault("seed", int(seed))
    until = site.stages[-1] if site.stages else None
    return api.run(cfg, param_overrides=overrides, until=until)


def golden_seed(site: Site, seed: int = GOLDEN_SEED) -> int:
    """The seed :func:`run_golden_pipeline` effectively uses (a site may fix its own)."""
    fixed = site.param_overrides.get("interferogram", {}).get("seed")
    return int(seed if fixed is None else fixed)


def compute_golden(
    site: Site, workdir: Path | str, seed: int = GOLDEN_SEED
) -> tuple[dict[str, Any], RunResult]:
    result = run_golden_pipeline(site, workdir, seed)
    return golden_stats(result, site, golden_seed(site, seed)), result


def golden_path(repo: Path | str, site_name: str) -> Path:
    return Path(repo) / GOLDEN_DIRNAME / site_name / GOLDEN_FILENAME


def dump_golden(stats: Mapping[str, Any]) -> str:
    """Canonical text of a golden (sorted keys, 2-space indent, trailing newline)."""
    return json.dumps(stats, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_golden(stats: Mapping[str, Any], path: Path | str) -> int:
    """Write ``stats`` and return the byte size; refuses a file over :data:`MAX_GOLDEN_BYTES`
    (a golden that large contains arrays, not statistics)."""
    text = dump_golden(stats)
    n = len(text.encode("utf-8"))
    if n > MAX_GOLDEN_BYTES:
        msg = f"golden would be {n} bytes (> {MAX_GOLDEN_BYTES}); store statistics, not arrays"
        raise ValueError(msg)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return n


def load_golden(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"{p}: golden must be a JSON object"
        raise ValueError(msg)
    return data


def count_leaves(obj: Any) -> int:
    if isinstance(obj, Mapping):
        return sum(count_leaves(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_leaves(v) for v in obj)
    return 1


def forbidden_keys(obj: Any, keys: Sequence[str] = FORBIDDEN_KEYS) -> list[str]:
    """Dotted paths of any key in ``keys`` found anywhere in ``obj`` (should be empty)."""
    found: list[str] = []

    def rec(o: Any, path: str) -> None:
        if isinstance(o, Mapping):
            for k, v in o.items():
                sub = f"{path}.{k}" if path else str(k)
                if str(k) in keys:
                    found.append(sub)
                rec(v, sub)
        elif isinstance(o, list):
            for i, v in enumerate(o):
                rec(v, f"{path}[{i}]")

    rec(obj, "")
    return found


# ============================================================================ check
@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    stats: dict[str, Any] | None = None
    golden: dict[str, Any] | None = None
    seed: int = GOLDEN_SEED

    @property
    def ok(self) -> bool:
        return not any(f.is_fail for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "seed": self.seed,
            "n_mismatches": len(self.mismatches),
            "mismatches": [m.to_dict() for m in self.mismatches],
            "n_leaves": None if self.golden is None else count_leaves(self.golden),
        }


def check_golden(
    site: Site,
    golden_file: Path | str,
    workdir: Path | str,
    tol: Tolerances = DEFAULT_TOLERANCES,
    seed: int = GOLDEN_SEED,
) -> CheckResult:
    """Run the pipeline and compare with the stored golden; never raises for a mismatch."""
    res = CheckResult(seed=golden_seed(site, seed))
    gp = Path(golden_file)
    if not gp.exists():
        res.findings.append(_finding("GOLDEN-002", "FAIL", path=str(gp)))
        return res
    res.golden = load_golden(gp)
    stats, result = compute_golden(site, workdir, seed)
    res.stats = stats
    if not result.ok:
        res.findings.append(
            _finding(
                "GOLDEN-004",
                "FAIL",
                scope=result.failed_stage,
                stage=result.failed_stage or "?",
                error=mask_text(result.error or "")[:300],
            )
        )
        return res
    res.mismatches = compare_golden(res.golden, stats, tol)
    res.findings.extend(mismatches_to_findings(res.mismatches))
    return res


# ============================================================================ script mains
def _parser(prog: str, check_only: bool) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog)
    p.add_argument("--site", type=Path, default=None, help="site YAML (default S_synthetic)")
    p.add_argument(
        "--golden" if check_only else "--out",
        dest="golden",
        type=Path,
        default=None,
        help="golden stats.json (default tests/regression/golden/<site>/stats.json)",
    )
    p.add_argument("--workdir", type=Path, default=None, help="keep the run directory here")
    p.add_argument("--seed", type=int, default=GOLDEN_SEED)
    p.add_argument("--rtol", type=float, default=DEFAULT_TOLERANCES.rtol)
    p.add_argument("--atol", type=float, default=DEFAULT_TOLERANCES.atol)
    p.add_argument("--fraction-atol", type=float, default=DEFAULT_TOLERANCES.fraction_atol)
    p.add_argument("--lang", default=None, help="ko | en (default: WINTERSAR_LANG or ko)")
    p.add_argument("--json", action="store_true", help="print a JSON envelope on stdout")
    p.add_argument("--markdown", type=Path, default=None, help="write the findings as Markdown")
    if not check_only:
        p.add_argument(
            "--check",
            action="store_true",
            help="do not write; compare with the stored golden and exit 1 on a mismatch",
        )
    return p


def _resolve(args: argparse.Namespace, repo: Path) -> tuple[Site, Path, Tolerances]:
    site_path = args.site if args.site is not None else repo / DEFAULT_SITE
    site = load_site(site_path)
    golden_file = args.golden if args.golden is not None else golden_path(repo, site.name)
    tol = Tolerances(rtol=args.rtol, atol=args.atol, fraction_atol=args.fraction_atol)
    return site, golden_file, tol


def _report(
    command: str, res: CheckResult, golden_file: Path, args: argparse.Namespace, lang: str | None
) -> None:
    if args.markdown is not None:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(findings_to_markdown(res.findings, lang), encoding="utf-8")
    if args.json:
        emit_json(command, {**res.to_dict(), "golden": str(golden_file)}, res.findings, ok=res.ok)
        return
    if res.ok and res.golden is not None:
        tol = Tolerances(rtol=args.rtol, atol=args.atol, fraction_atol=args.fraction_atol)
        console.print(
            t(
                "golden.check.ok",
                lang,
                n_leaves=count_leaves(res.golden),
                rtol=tol.rtol,
                atol=tol.atol,
                fraction_atol=tol.fraction_atol,
            )
        )
    else:
        print_findings(res.findings, lang)
        err_console.print(t("golden.check.regenerate", lang))


def main_check(argv: Sequence[str] | None = None, repo: Path | str | None = None) -> int:
    """``scripts/check_golden.py``: exit 0 when the fresh statistics match the golden, 1 when
    they do not (or the golden is missing / the run failed), 2 on bad input."""
    root = Path(repo) if repo is not None else Path.cwd()
    args = _parser("check_golden.py", check_only=True).parse_args(list(argv or []))
    try:
        site, golden_file, tol = _resolve(args, root)
    except (OSError, ValueError) as e:
        err_console.print(mask_text(f"{type(e).__name__}: {e}"))
        return 2
    lang = args.lang
    if not args.json:
        console.print(
            t(
                "golden.check.start",
                lang,
                site=site.name,
                path=str(golden_file),
                seed=golden_seed(site, args.seed),
            )
        )
    with tempfile.TemporaryDirectory(prefix="wintersar-golden-") as tmp:
        wd = args.workdir if args.workdir is not None else Path(tmp)
        res = check_golden(site, golden_file, wd, tol, args.seed)
    _report("check-golden", res, golden_file, args, lang)
    return 0 if res.ok else 1


def main_make(argv: Sequence[str] | None = None, repo: Path | str | None = None) -> int:
    """``scripts/make_golden.py``: regenerate the golden (exit 0), or with ``--check`` behave
    like :func:`main_check` without writing."""
    root = Path(repo) if repo is not None else Path.cwd()
    args = _parser("make_golden.py", check_only=False).parse_args(list(argv or []))
    try:
        site, golden_file, tol = _resolve(args, root)
    except (OSError, ValueError) as e:
        err_console.print(mask_text(f"{type(e).__name__}: {e}"))
        return 2
    lang = args.lang
    with tempfile.TemporaryDirectory(prefix="wintersar-golden-") as tmp:
        wd = args.workdir if args.workdir is not None else Path(tmp)
        if args.check:
            res = check_golden(site, golden_file, wd, tol, args.seed)
            _report("check-golden", res, golden_file, args, lang)
            return 0 if res.ok else 1
        stats, result = compute_golden(site, wd, args.seed)
    if not result.ok:
        f = _finding(
            "GOLDEN-004",
            "FAIL",
            scope=result.failed_stage,
            stage=result.failed_stage or "?",
            error=mask_text(result.error or "")[:300],
        )
        if args.json:
            emit_json("make-golden", {"golden": str(golden_file)}, [f], ok=False)
        else:
            print_findings([f], lang)
        return 1
    previous = load_golden(golden_file) if golden_file.exists() else None
    changes = compare_golden(previous, stats, tol) if previous is not None else []
    n_bytes = write_golden(stats, golden_file)
    data = {
        "golden": str(golden_file),
        "n_bytes": n_bytes,
        "n_leaves": count_leaves(stats),
        "seed": golden_seed(site, args.seed),
        "n_changed": len(changes),
        "changed": [m.to_dict() for m in changes],
    }
    if args.markdown is not None:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(
            findings_to_markdown(mismatches_to_findings(changes), lang), encoding="utf-8"
        )
    if args.json:
        emit_json("make-golden", data, [], ok=True)
        return 0
    console.print(
        t(
            "golden.make.written",
            lang,
            path=str(golden_file),
            n_bytes=n_bytes,
            n_leaves=count_leaves(stats),
        )
    )
    if previous is None:
        return 0
    if changes:
        console.print(t("golden.make.changed", lang, n=len(changes)))
        console.print(format_mismatches(changes))
    else:
        console.print(t("golden.make.unchanged", lang, n_leaves=count_leaves(stats)))
    return 0
