"""Thin subprocess client over the ``wintersar --json`` CLI (plan §5.8, ADR-0070/0071).

The plugin never imports ``wintersar``: the QGIS Python has its own dependency set and the
toolkit lives in a separate conda/pixi/uv environment. Every action becomes

    <python of that environment> -m wintersar.cli --json --lang <ko|en> <command> [args]

and the JSON envelope ``{"ok", "command", "data", "findings"}`` written by
``wintersar.util.output.emit_json`` is parsed into :class:`CliResponse`.
# source: src/wintersar/util/output.py emit_json (envelope), src/wintersar/cli.py
#   (global options --json/--lang precede the sub-command; console script `wintersar`)

Client-side failures (CLI missing, timeout, no envelope, non-zero exit) are reported as
Finding-shaped dicts with rule ids ``CLI_NOT_FOUND``/``TIMEOUT``/``INVALID_JSON``/``CLI_ERROR``
and i18n keys ``qgis.error.<ID>.cause`` / ``.fix`` so the UI renders them like any other
finding (cause -> fix).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

CLI_MODULE = "wintersar.cli"
CONSOLE_SCRIPT = "wintersar"
ENVELOPE_KEYS: tuple[str, ...] = ("ok", "command", "data", "findings")
CLIENT_ERROR_IDS: tuple[str, ...] = (
    "CLI_NOT_FOUND",
    "TIMEOUT",
    "INVALID_JSON",
    "CLI_ERROR",
    "CANCELLED",
)
# source: src/wintersar/pipeline/stages.py STAGE_ORDER (copied; tests assert equality)
STAGE_ORDER: tuple[str, ...] = (
    "search",
    "precheck",
    "fetch",
    "coregister",
    "interferogram",
    "multilook",
    "unwrap",
    "timeseries",
    "corrections",
    "geocode",
    "validate",
)
ENV_HINT_KINDS: tuple[str, ...] = ("conda", "pixi", "uv", "venv", "python")

LineCallback = Callable[[str], None]


# ----------------------------------------------------------------------------- response


@dataclass
class CliResponse:
    """Parsed ``--json`` envelope plus process details."""

    ok: bool
    command: str
    data: Any
    findings: list[dict[str, Any]]
    raw_stdout: str = ""
    raw_stderr: str = ""
    exit_code: int | None = None
    argv: list[str] = field(default_factory=list)
    error: str | None = None  # one of CLIENT_ERROR_IDS when the client itself failed

    @property
    def n_fail(self) -> int:
        return sum(1 for f in self.findings if f.get("severity") == "FAIL")

    @property
    def n_warn(self) -> int:
        return sum(1 for f in self.findings if f.get("severity") == "WARN")

    @property
    def n_info(self) -> int:
        return sum(1 for f in self.findings if f.get("severity") == "INFO")

    @property
    def client_error(self) -> bool:
        return self.error is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "command": self.command,
            "data": self.data,
            "findings": self.findings,
            "exit_code": self.exit_code,
            "error": self.error,
        }

    @classmethod
    def failure(
        cls,
        error_id: str,
        command: str,
        argv: Sequence[str] = (),
        *,
        exit_code: int | None = None,
        raw_stdout: str = "",
        raw_stderr: str = "",
        **params: Any,
    ) -> CliResponse:
        if error_id not in CLIENT_ERROR_IDS:
            msg = f"unknown client error id {error_id!r}"
            raise ValueError(msg)
        finding = client_finding(error_id, command=command, exit_code=exit_code, **params)
        return cls(
            ok=False,
            command=command,
            data=None,
            findings=[finding],
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            exit_code=exit_code,
            argv=list(argv),
            error=error_id,
        )


def client_finding(error_id: str, **params: Any) -> dict[str, Any]:
    """Finding-shaped dict (same keys as ``wintersar.io.schemas.Finding``)."""
    return {
        "rule_id": error_id,
        "severity": "FAIL",
        "message_key": f"qgis.error.{error_id}.cause",
        "fix_key": f"qgis.error.{error_id}.fix",
        "params": {k: v for k, v in params.items() if v is not None},
        "evidence": {},
        "refs": [],
        "scope": None,
    }


# ----------------------------------------------------------------------------- envelope


def parse_envelope(stdout: str) -> dict[str, Any] | None:
    """Find the ``{"ok", "command", "data", "findings"}`` object in ``stdout``.

    The envelope is the last JSON object on stdout (``emit_json`` writes it with
    ``indent=2``); anything printed before it by third-party libraries is ignored.
    """
    text = stdout.strip()
    if not text:
        return None
    candidate = _try_load(text)
    if candidate is not None:
        return candidate
    decoder = json.JSONDecoder()
    starts = [i for i, ch in enumerate(text) if ch == "{" and (i == 0 or text[i - 1] == "\n")]
    for start in reversed(starts):
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if _is_envelope(obj):
            return obj  # type: ignore[no-any-return]
    return None


def _try_load(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if _is_envelope(obj) else None


def _is_envelope(obj: Any) -> bool:
    return isinstance(obj, dict) and all(k in obj for k in ENVELOPE_KEYS)


# ----------------------------------------------------------------------------- environment


@dataclass(frozen=True)
class EnvHint:
    kind: str  # conda | pixi | uv | venv | python
    target: str


def parse_env_hint(hint: str | None) -> EnvHint | None:
    """``"conda:wintersar"`` -> ``EnvHint("conda", "wintersar")``; ``None``/blank -> ``None``."""
    if not hint or not hint.strip():
        return None
    kind, sep, target = hint.strip().partition(":")
    kind = kind.strip().lower()
    if not sep or kind not in ENV_HINT_KINDS or not target.strip():
        msg = f"invalid environment hint {hint!r}; expected one of " + ", ".join(
            f"{k}:<target>" for k in ENV_HINT_KINDS
        )
        raise ValueError(msg)
    return EnvHint(kind, target.strip())


def _python_in_prefix(prefix: Path) -> Path | None:
    """Interpreter of a venv/conda prefix (``bin/python`` or ``python.exe``/``Scripts``)."""
    for rel in ("bin/python", "python.exe", "Scripts/python.exe"):
        p = prefix / rel
        if p.exists():
            return p
    return None


def _script_in_prefix(prefix: Path, name: str = CONSOLE_SCRIPT) -> Path | None:
    for rel in (f"bin/{name}", f"Scripts/{name}.exe", f"Scripts/{name}"):
        p = prefix / rel
        if p.exists():
            return p
    return None


def _which(name: str, extra_dirs: Iterable[Path] = ()) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in extra_dirs:
        p = d / name
        if p.exists():
            return str(p)
        pe = d / f"{name}.exe"
        if pe.exists():
            return str(pe)
    return None


def conda_prefixes(environments_txt: Path | None = None) -> list[Path]:
    """Environment prefixes registered by conda.

    # source: https://docs.conda.io/projects/conda/en/stable/user-guide/tasks/manage-environments.html
    #   "conda info --envs" lists environments from ~/.conda/environments.txt
    """
    p = environments_txt or (Path.home() / ".conda" / "environments.txt")
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[Path] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(Path(s))
    return out


def _conda_prefix_for_name(name: str, prefixes: Sequence[Path]) -> Path | None:
    for p in prefixes:
        if p.name == name and p.is_dir():
            return p
    return None


def resolve_command(
    python_exe: str | Path | None,
    env_hint: str | None,
    *,
    conda_envs: Sequence[Path] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[str] | None:
    """Argv prefix that starts the wintersar CLI, or ``None`` when nothing was found.

    Resolution order (ADR-0071):
    1. explicit ``python_exe`` -> ``[python_exe, "-m", "wintersar.cli"]``
    2. ``env_hint``: ``conda:<name|prefix>`` / ``pixi:<manifest dir>`` / ``uv:<project dir>`` /
       ``venv:<dir>`` / ``python:<exe>``. A direct interpreter is preferred when the
       environment's layout exposes one (no launcher overhead, works without the launcher on
       PATH); otherwise the launcher is used:
       # source: https://docs.conda.io/projects/conda/en/stable/commands/run.html
       #   conda run [-n ENVIRONMENT | -p PATH] [--no-capture-output] executable_call
       # source: https://pixi.prefix.dev/latest/reference/cli/pixi/run/
       #   pixi run --manifest-path <pixi.toml> <command>
       # source: https://docs.astral.sh/uv/reference/cli/#uv-run
       #   uv run --project <dir> --no-sync <command>  (".venv" is the project environment)
    3. ``wintersar`` console script on PATH.
    """
    which_fn = which or _which
    if python_exe:
        return [str(python_exe), "-m", CLI_MODULE]
    hint = parse_env_hint(env_hint)
    if hint is not None:
        prefix = _prefix_from_hint(hint, conda_envs, which_fn)
        if prefix is not None:
            return prefix
    script = which_fn(CONSOLE_SCRIPT)
    if script:
        return [script]
    return None


def _prefix_from_hint(
    hint: EnvHint,
    conda_envs: Sequence[Path] | None,
    which_fn: Callable[[str], str | None],
) -> list[str] | None:
    target = Path(hint.target).expanduser()
    if hint.kind == "python":
        return [str(target), "-m", CLI_MODULE]
    if hint.kind == "venv":
        py = _python_in_prefix(target)
        return [str(py), "-m", CLI_MODULE] if py else None
    if hint.kind == "conda":
        if target.is_absolute() or target.exists():
            py = _python_in_prefix(target)
            if py:
                return [str(py), "-m", CLI_MODULE]
            conda = which_fn("conda")
            return (
                [conda, "run", "-p", str(target), "--no-capture-output", "python", "-m", CLI_MODULE]
                if conda
                else None
            )
        envs = list(conda_envs) if conda_envs is not None else conda_prefixes()
        prefix = _conda_prefix_for_name(hint.target, envs)
        if prefix is not None:
            py = _python_in_prefix(prefix)
            if py:
                return [str(py), "-m", CLI_MODULE]
        conda = which_fn("conda")
        return (
            [conda, "run", "-n", hint.target, "--no-capture-output", "python", "-m", CLI_MODULE]
            if conda
            else None
        )
    if hint.kind == "pixi":
        manifest = target if target.suffix == ".toml" else target / "pixi.toml"
        pixi = which_fn("pixi")
        if pixi is None:
            return None
        return [pixi, "run", "--manifest-path", str(manifest), "python", "-m", CLI_MODULE]
    if hint.kind == "uv":
        py = _python_in_prefix(target / ".venv")
        if py:
            return [str(py), "-m", CLI_MODULE]
        uv = which_fn("uv")
        if uv is None:
            return None
        return [uv, "run", "--project", str(target), "--no-sync", "python", "-m", CLI_MODULE]
    return None


@dataclass(frozen=True)
class EnvCandidate:
    label: str
    python_exe: str
    hint: str


def discover_environments(
    home: Path | None = None,
    extra_prefixes: Iterable[Path] = (),
) -> list[EnvCandidate]:
    """Environments on this machine where the ``wintersar`` console script exists.

    Heuristic and file-system only (no subprocesses): conda registry
    (``~/.conda/environments.txt``), common conda/mamba install roots, ``$CONDA_PREFIX``,
    ``$VIRTUAL_ENV``, ``.venv`` of the current directory, plus ``extra_prefixes``.
    """
    home = home or Path.home()
    prefixes: list[Path] = []
    prefixes.extend(conda_prefixes(home / ".conda" / "environments.txt"))
    for root in ("miniconda3", "anaconda3", "miniforge3", "mambaforge", "micromamba"):
        envs = home / root / "envs"
        if envs.is_dir():
            prefixes.extend(sorted(p for p in envs.iterdir() if p.is_dir()))
    for var in ("CONDA_PREFIX", "VIRTUAL_ENV"):
        v = os.environ.get(var)
        if v:
            prefixes.append(Path(v))
    prefixes.append(Path.cwd() / ".venv")
    prefixes.extend(extra_prefixes)
    out: list[EnvCandidate] = []
    seen: set[str] = set()
    for prefix in prefixes:
        try:
            key = str(prefix.resolve())
        except OSError:
            continue
        if key in seen or not prefix.is_dir():
            continue
        seen.add(key)
        py = _python_in_prefix(prefix)
        if py is None or _script_in_prefix(prefix) is None:
            continue
        kind = "venv" if (prefix / "pyvenv.cfg").exists() else "conda"
        out.append(EnvCandidate(label=prefix.name, python_exe=str(py), hint=f"{kind}:{prefix}"))
    return out


# ----------------------------------------------------------------------------- argv builders


def _opt(flag: str, value: Any) -> list[str]:
    return [] if value in (None, "", False) else [flag, str(value)]


def _repeat(flag: str, values: Iterable[str] | None) -> list[str]:
    return [x for v in (values or []) for x in (flag, str(v))]


def search_args(config: str | Path) -> list[str]:
    # source: src/wintersar/select/cli.py search(--config/-c)
    return ["search", "--config", str(config)]


def precheck_args(
    candidates: str | Path,
    config: str | Path,
    *,
    out: str | Path | None = None,
    geometry: str | Path | None = None,
    no_fail: bool = False,
    baseline: str | None = None,
) -> list[str]:
    # source: src/wintersar/select/cli.py precheck(CANDIDATES --config --out --geometry --no-fail --baseline)
    args = ["precheck", str(candidates), "--config", str(config)]
    args += _opt("--out", out) + _opt("--geometry", geometry)
    if no_fail:
        args.append("--no-fail")
    args += _opt("--baseline", baseline)
    return args


def _pipeline_args(
    command: str,
    config: str | Path,
    until: str | None,
    from_: str | None,
    force: Iterable[str] | None,
    set_: Iterable[str] | None,
) -> list[str]:
    for s in (until, from_, *(force or [])):
        if s is not None and s not in STAGE_ORDER:
            msg = f"unknown stage {s!r}; known: {', '.join(STAGE_ORDER)}"
            raise ValueError(msg)
    args = [command, "--config", str(config)]
    args += _opt("--until", until) + _opt("--from", from_)
    args += _repeat("--force", force) + _repeat("--set", set_)
    return args


def plan_args(
    config: str | Path,
    *,
    until: str | None = None,
    from_: str | None = None,
    force: Iterable[str] | None = None,
    set_: Iterable[str] | None = None,
) -> list[str]:
    # source: src/wintersar/pipeline/cli.py plan_cmd(--config --until --from --force* --set*)
    return _pipeline_args("plan", config, until, from_, force, set_)


def run_args(
    config: str | Path,
    *,
    until: str | None = None,
    from_: str | None = None,
    force: Iterable[str] | None = None,
    set_: Iterable[str] | None = None,
    dry_run: bool = False,
) -> list[str]:
    # source: src/wintersar/pipeline/cli.py run_cmd(... --dry-run)
    args = _pipeline_args("run", config, until, from_, force, set_)
    if dry_run:
        args.append("--dry-run")
    return args


def diagnose_args(
    path: str | Path,
    *,
    engine: str | None = None,
    out: str | Path | None = None,
    assume_failed: bool = False,
) -> list[str]:
    # source: src/wintersar/diagnose/cli.py diagnose_cmd(PATH --engine --out --assume-failed)
    args = ["diagnose", str(path), *_opt("--engine", engine), *_opt("--out", out)]
    if assume_failed:
        args.append("--assume-failed")
    return args


def validate_args(
    ts: str | Path,
    leveling: str | Path | None = None,
    gnss: str | Path | None = None,
    *,
    out: str | Path | None = None,
    radius_m: float | None = None,
) -> list[str]:
    # source: src/wintersar/validate/cli.py validate_cmd(--ts --leveling --gnss --out --radius
    #   --method --align --max-gap-days --heading --incidence --no-plots)
    if leveling is None and gnss is None:
        msg = "validate needs --leveling and/or --gnss"
        raise ValueError(msg)
    return [
        "validate",
        "--ts",
        str(ts),
        *_opt("--leveling", leveling),
        *_opt("--gnss", gnss),
        *_opt("--out", out),
        *_opt("--radius", radius_m),
    ]


def refpoint_args(
    ts: str | Path,
    aoi: str | Path | None = None,
    top: int = 5,
    *,
    out: str | Path | None = None,
) -> list[str]:
    # source: src/wintersar/validate/cli.py refpoint_cmd(--ts --aoi --top --coherence --conncomp
    #   --dem --weights --min-coherence --mintpy-threshold --out); data["candidates"] =
    #   RefPointCandidate.to_dict() list
    if top < 1:
        msg = "top must be >= 1"
        raise ValueError(msg)
    return [
        "refpoint",
        "--ts",
        str(ts),
        *_opt("--aoi", aoi),
        "--top",
        str(top),
        *_opt("--out", out),
    ]


def check_install_args(engines: Iterable[str] | None = None) -> list[str]:
    # source: src/wintersar/cli.py check_install(--engine/-e repeatable)
    return ["check-install", *_repeat("--engine", engines)]


def init_args(path: str | Path, force: bool = False) -> list[str]:
    # source: src/wintersar/cli.py init(PATH --force)
    return ["init", str(path), *(["--force"] if force else [])]


def cache_ls_args(config: str | Path | None = None, workdir: str | Path | None = None) -> list[str]:
    # source: src/wintersar/pipeline/cli.py cache_ls(--config --workdir)
    return ["cache", "ls", *_opt("--config", config), *_opt("--workdir", workdir)]


# ----------------------------------------------------------------------------- client


def _pump(stream: IO[str] | None, sink: list[str], on_line: LineCallback | None) -> None:
    if stream is None:
        return
    try:
        for line in stream:
            text = line.rstrip("\r\n")
            sink.append(text)
            if on_line is not None:
                with contextlib.suppress(Exception):  # a UI callback must not kill the reader
                    on_line(text)
    finally:
        with contextlib.suppress(OSError):
            stream.close()


class WintersarClient:
    """Runs ``wintersar`` sub-commands in a separate environment and parses ``--json``."""

    def __init__(
        self,
        python_exe: str | Path | None = None,
        env_activate: str | None = None,
        *,
        lang: str = "ko",
        timeout: float | None = 3600.0,
        cwd: str | Path | None = None,
    ) -> None:
        self.python_exe = Path(python_exe) if python_exe else None
        self.env_activate = env_activate or None
        self.lang = lang if lang in ("ko", "en") else "ko"
        self.timeout = timeout
        self.cwd = Path(cwd) if cwd else None
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._cancel_requested = False

    # ------------------------------------------------------------------ resolution
    def command_prefix(self) -> list[str] | None:
        return resolve_command(self.python_exe, self.env_activate)

    def resolved_command(self) -> str | None:
        prefix = self.command_prefix()
        return " ".join(prefix) if prefix else None

    def describe_env(self) -> str:
        if self.python_exe:
            return f"python:{self.python_exe}"
        if self.env_activate:
            return self.env_activate
        return "PATH"

    # ------------------------------------------------------------------ execution
    def cancel(self) -> bool:
        """Kill the running command (if any); returns True when a process was signalled."""
        with self._lock:
            proc = self._proc
            self._cancel_requested = proc is not None
        if proc is None:
            return False
        try:
            proc.kill()
        except OSError:
            return False
        return True

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def run(
        self,
        args: Sequence[str],
        timeout: float | None = None,
        on_line: LineCallback | None = None,
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> CliResponse:
        """Execute ``wintersar --json --lang L <args>``; never raises for CLI failures."""
        command = (
            " ".join(args[:2])
            if args and args[0] in ("cache", "unwrap")
            else (args[0] if args else "")
        )
        prefix = self.command_prefix()
        if prefix is None:
            return CliResponse.failure(
                "CLI_NOT_FOUND", command, detail=f"no interpreter for {self.describe_env()}"
            )
        argv = [*prefix, "--json", "--lang", self.lang, *args]
        proc_env = os.environ.copy()
        proc_env.update(env or {})
        proc_env["WINTERSAR_LANG"] = self.lang
        proc_env["PYTHONIOENCODING"] = "utf-8"
        # source: .venv/lib/python3.11/site-packages/rich/console.py (NO_COLOR honoured)
        proc_env.setdefault("NO_COLOR", "1")
        popen_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":  # no console window from inside QGIS
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(cwd or self.cwd) if (cwd or self.cwd) else None,
                env=proc_env,
                **popen_kwargs,
            )
        except OSError as exc:
            return CliResponse.failure("CLI_NOT_FOUND", command, argv, detail=str(exc))
        with self._lock:
            self._proc = proc
            self._cancel_requested = False
        out_lines: list[str] = []
        err_lines: list[str] = []
        t_out = threading.Thread(target=_pump, args=(proc.stdout, out_lines, None), daemon=True)
        t_err = threading.Thread(target=_pump, args=(proc.stderr, err_lines, on_line), daemon=True)
        t_out.start()
        t_err.start()
        limit = self.timeout if timeout is None else timeout
        timed_out = False
        try:
            proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait()
        finally:
            t_out.join()
            t_err.join()
            with self._lock:
                self._proc = None
                cancelled = self._cancel_requested
                self._cancel_requested = False
        stdout = "\n".join(out_lines)
        stderr = "\n".join(err_lines)
        code = proc.returncode
        if cancelled:
            return CliResponse.failure(
                "CANCELLED", command, argv, exit_code=code, raw_stdout=stdout, raw_stderr=stderr
            )
        if timed_out:
            return CliResponse.failure(
                "TIMEOUT",
                command,
                argv,
                exit_code=code,
                raw_stdout=stdout,
                raw_stderr=stderr,
                timeout_s=float(limit or 0.0),
            )
        envelope = parse_envelope(stdout)
        if envelope is None:
            error_id = "INVALID_JSON" if code == 0 else "CLI_ERROR"
            return CliResponse.failure(
                error_id, command, argv, exit_code=code, raw_stdout=stdout, raw_stderr=stderr
            )
        findings = envelope.get("findings") or []
        if not isinstance(findings, list):
            findings = []
        return CliResponse(
            ok=bool(envelope.get("ok")),
            command=str(envelope.get("command") or command),
            data=envelope.get("data"),
            findings=[f for f in findings if isinstance(f, dict)],
            raw_stdout=stdout,
            raw_stderr=stderr,
            exit_code=code,
            argv=argv,
        )

    # ------------------------------------------------------------------ helpers
    def version(self, timeout: float | None = 60.0) -> CliResponse:
        return self.run(["version"], timeout=timeout)

    def check_install(
        self, engines: Iterable[str] | None = None, timeout: float | None = 300.0
    ) -> CliResponse:
        return self.run(check_install_args(engines), timeout=timeout)

    def init_config(self, path: str | Path, force: bool = False) -> CliResponse:
        return self.run(init_args(path, force), timeout=60.0)

    def search(self, config: str | Path, on_line: LineCallback | None = None) -> CliResponse:
        return self.run(search_args(config), on_line=on_line)

    def precheck(
        self,
        candidates: str | Path,
        config: str | Path,
        *,
        out: str | Path | None = None,
        geometry: str | Path | None = None,
        no_fail: bool = False,
        baseline: str | None = None,
        on_line: LineCallback | None = None,
    ) -> CliResponse:
        return self.run(
            precheck_args(
                candidates, config, out=out, geometry=geometry, no_fail=no_fail, baseline=baseline
            ),
            on_line=on_line,
        )

    def plan(
        self,
        config: str | Path,
        *,
        until: str | None = None,
        from_: str | None = None,
        force: Iterable[str] | None = None,
        set_: Iterable[str] | None = None,
        on_line: LineCallback | None = None,
    ) -> CliResponse:
        return self.run(
            plan_args(config, until=until, from_=from_, force=force, set_=set_), on_line=on_line
        )

    def run_pipeline(
        self,
        config: str | Path,
        *,
        until: str | None = None,
        from_: str | None = None,
        force: Iterable[str] | None = None,
        set_: Iterable[str] | None = None,
        dry_run: bool = False,
        on_line: LineCallback | None = None,
        timeout: float | None = None,
    ) -> CliResponse:
        """``wintersar run`` with stderr streamed line by line to ``on_line``."""
        return self.run(
            run_args(config, until=until, from_=from_, force=force, set_=set_, dry_run=dry_run),
            timeout=timeout,
            on_line=on_line,
        )

    def diagnose(
        self,
        logdir: str | Path,
        *,
        engine: str | None = None,
        out: str | Path | None = None,
        assume_failed: bool = False,
        on_line: LineCallback | None = None,
    ) -> CliResponse:
        return self.run(
            diagnose_args(logdir, engine=engine, out=out, assume_failed=assume_failed),
            on_line=on_line,
        )

    def validate(
        self,
        ts: str | Path,
        leveling: str | Path | None = None,
        gnss: str | Path | None = None,
        on_line: LineCallback | None = None,
    ) -> CliResponse:
        return self.run(validate_args(ts, leveling, gnss), on_line=on_line)

    def refpoint(
        self,
        ts: str | Path,
        aoi: str | Path | None = None,
        top: int = 5,
        on_line: LineCallback | None = None,
    ) -> CliResponse:
        return self.run(refpoint_args(ts, aoi, top), on_line=on_line)

    def cache_ls(
        self, config: str | Path | None = None, workdir: str | Path | None = None
    ) -> CliResponse:
        return self.run(cache_ls_args(config, workdir), timeout=120.0)
