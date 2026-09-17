"""Public diagnosis API (plan §5.5, R-02, R-14, PERF-13).

* :func:`diagnose_text` — diagnose one log text.
* :func:`diagnose_logs` — diagnose a file or every log file under a directory.
* :func:`diagnose_exception` — diagnose a caught exception (used by ``run`` on failure).
* :func:`attach_retry_hint` — machine-readable retry suggestion for the pipeline.

Flow: detect engine (name + content) → try the engine's KB entries (+ engine-independent
ones) → if nothing matched, try every entry → if still nothing but the generic parser saw
a failure (traceback / ERROR / OOM …) emit ``KB-UNKNOWN`` with a masked excerpt.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from wintersar.diagnose import parsers
from wintersar.diagnose.kb_loader import KB_ENGINES, KBEntry, entries_for_engine, kb_index
from wintersar.diagnose.matcher import match_entries, to_finding, unknown_finding
from wintersar.io.schemas import Finding, sort_findings
from wintersar.util.masking import mask_mapping, mask_text

LOG_SUFFIXES: frozenset[str] = frozenset(
    {".log", ".txt", ".out", ".err", ".stdout", ".stderr", ".e", ".o", ".json"}
)
DEFAULT_MAX_BYTES = 20_000_000

#: wintersar's own run records (ADR-0032): ``work/<stage>/<hash>/manifest.json``
#: (:class:`~wintersar.io.schemas.StageRecord`) and ``work/runs/<run_id>.json`` (run summary).
#: Both embed the failed stage's ``extra.error`` and log excerpt, so diagnosing them re-reports
#: a failure that the engine log already carries, under a second scope and with the raw JSON as
#: the excerpt. They are skipped when scanning a directory. Engine JSON in a log dir
#: (``runfiles_summary.json`` (ADR-0027), ``<stage>.findings.json``, HyP3 ``jobs.json``) is not a
#: record and is still diagnosed; an explicitly named file is always diagnosed.
#: Identified by their top-level keys, not by path, so a renamed/moved work dir still works.
RECORD_MARKER_KEYS: tuple[frozenset[str], ...] = (
    frozenset({"stage", "node_hash", "status"}),  # StageRecord (manifest.json)
    frozenset({"ok", "records", "plan"}),  # pipeline.api.RunResult.to_dict (runs/<id>.json)
)
#: JSON larger than this is never probed for record markers (it is not one of ours).
RECORD_PROBE_MAX_BYTES = 8_000_000


#: registry / config engine names that map onto a KB engine family (``None`` = auto-detect)
ENGINE_ALIASES: dict[str, str | None] = {
    "isce2_topsstack": "isce2",
    "compass_isce3": "isce2",
    "tophu": "snaphu",
    "fake": None,
    "spurt": None,
    "dolphin": None,
    "asf_search": "asf",
}


def _check_engine(engine: str | None) -> str | None:
    if engine is None:
        return None
    eng = engine.lower()
    if eng in ENGINE_ALIASES:
        return ENGINE_ALIASES[eng]
    if eng not in KB_ENGINES:
        msg = f"unknown engine {engine!r}; choose one of {KB_ENGINES}"
        raise ValueError(msg)
    return eng


def diagnose_text(
    text: str,
    engine: str | None = None,
    *,
    scope: str | None = None,
    context_lines: int = 3,
    assume_failed: bool = False,
) -> list[Finding]:
    """Diagnose one log text and return findings (FAIL first).

    ``engine`` restricts which KB entries are tried *first*; when it is ``None`` the engine is
    detected from ``scope`` (file name) and the content. ``assume_failed=True`` emits
    ``KB-UNKNOWN`` even when the generic parser sees no error marker (the caller knows the
    stage failed, e.g. a non-zero exit code).
    """
    requested = _check_engine(engine)
    detected = parsers.detect_engine(scope, text)
    eng = requested or detected
    matches = match_entries(text, entries_for_engine(eng), context_lines)
    if not matches and eng is not None:
        matches = match_entries(text, entries_for_engine(None), context_lines)
    findings = [to_finding(m, scope=scope, engine=eng) for m in matches]
    if not findings:
        events = parsers.events_for(eng, text, context_lines)
        if events or assume_failed:
            findings.append(unknown_finding(events, text, scope=scope, engine=eng))
    return sort_findings(findings)


def iter_log_files(path: Path) -> list[Path]:
    """Log files under ``path`` (a file is returned as-is).

    wintersar's own run records are skipped (:func:`is_run_record`) so that pointing
    ``diagnose`` at a whole work directory reports each failure once, from the engine log.
    """
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(path.rglob("*")):
        if not p.is_file():
            continue
        if (p.suffix.lower() in LOG_SUFFIXES or p.suffix == "") and _looks_text(p):
            if is_run_record(p):
                continue
            out.append(p)
    return out


def is_run_record(path: Path) -> bool:
    """``True`` for a wintersar ``StageRecord`` manifest or run summary (not engine output)."""
    if path.suffix.lower() != ".json":
        return False
    try:
        if path.stat().st_size > RECORD_PROBE_MAX_BYTES:
            return False
        with path.open("rb") as fh:
            data = json.loads(fh.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    keys = set(data)
    return any(marker <= keys for marker in RECORD_MARKER_KEYS)


def _looks_text(p: Path, probe: int = 4096) -> bool:
    try:
        with p.open("rb") as fh:
            chunk = fh.read(probe)
    except OSError:
        return False
    return b"\x00" not in chunk


def normalise_newlines(text: str) -> str:
    """CRLF/CR -> LF.

    Engine wrappers and downloaders redraw progress with a bare ``\r``; read in binary mode
    that is one enormous "line" for every ``^``/``$``-anchored or ``.``-based pattern. Folding
    it into ordinary lines keeps line numbers, excerpts and regex cost sane.
    """
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_log(path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Read a log as text (last ``max_bytes`` when larger; undecodable bytes replaced)."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        data = fh.read()
    return normalise_newlines(data.decode("utf-8", errors="replace"))


def diagnose_logs(
    path: Path,
    engine: str | None = None,
    *,
    assume_failed: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    context_lines: int = 3,
) -> list[Finding]:
    """Diagnose a log file or every log file under a directory.

    ``Finding.scope`` is the (masked) path relative to ``path``'s parent so that a report
    can say which job failed. Identical findings from different files are kept (they have
    different scopes); identical findings within one file are merged by the matcher.
    """
    path = Path(path)
    _check_engine(engine)
    files = iter_log_files(path)
    if not files:
        return []
    base = path.parent if path.is_file() else path
    findings: list[Finding] = []
    for f in files:
        text = read_log(f, max_bytes)
        try:
            rel = f.relative_to(base)
        except ValueError:
            rel = f
        scope = mask_text(str(rel))
        findings.extend(
            diagnose_text(
                text,
                engine,
                scope=scope,
                context_lines=context_lines,
                assume_failed=assume_failed and len(files) == 1,
            )
        )
    if assume_failed and not findings:
        # directory: nothing matched anywhere -> one KB-UNKNOWN with the last file's tail
        last = files[-1]
        text = read_log(last, max_bytes)
        findings.append(unknown_finding([], text, scope=mask_text(str(last.name)), engine=engine))
    return sort_findings(findings)


def diagnose_exception(
    exc: BaseException, engine: str | None = None, *, scope: str | None = None
) -> list[Finding]:
    """Diagnose a caught exception via its formatted traceback (``run`` failure path)."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return diagnose_text(text, engine, scope=scope, assume_failed=True)


def attach_retry_hint(
    findings: list[Finding], kb: dict[str, KBEntry] | None = None
) -> dict[str, Any] | None:
    """Retry suggestion from the most severe finding that carries a KB ``retry_hint``.

    Returns ``{"rule_id", "stage", "action", "params", "note", "scope", "extracted"}`` or
    ``None``. ``params`` are parameter overrides for the stage named in ``stage`` (names
    follow the engine adapter / config field they refer to); ``extracted`` carries the
    values captured from the log (e.g. credits cost/remaining) for the caller's own logic.
    """
    index = kb if kb is not None else kb_index()
    for f in sort_findings(findings):
        entry = index.get(f.rule_id)
        if entry is None or entry.retry_hint is None:
            continue
        hint = entry.retry_hint
        out: dict[str, Any] = mask_mapping(
            {
                "rule_id": f.rule_id,
                "stage": hint.stage,
                "action": hint.action,
                "params": dict(hint.params),
                "note": hint.note,
                "scope": f.scope,
                "extracted": dict(f.params),
            }
        )
        return out
    return None


__all__ = [
    "DEFAULT_MAX_BYTES",
    "LOG_SUFFIXES",
    "RECORD_MARKER_KEYS",
    "RECORD_PROBE_MAX_BYTES",
    "attach_retry_hint",
    "diagnose_exception",
    "diagnose_logs",
    "diagnose_text",
    "is_run_record",
    "iter_log_files",
    "normalise_newlines",
    "read_log",
]
