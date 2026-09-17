"""``wintersar`` command line (plan §4.5).

Top-level commands defined here: ``check-install``, ``init``, ``version``.
Every module registers its own commands through ``wintersar.<module>.cli.register(app)``
so that modules stay independent; a module whose CLI is not importable is skipped
(with ``-v`` the import error is shown).

Global options: ``--json`` (machine-readable envelope parsed by the QGIS plugin),
``--lang ko|en``, ``-v``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Annotated, cast

import typer

from wintersar import __version__
from wintersar.i18n import t
from wintersar.io.schemas import Finding
from wintersar.util.clistate import state
from wintersar.util.output import console, emit_json, err_console, print_findings

app = typer.Typer(
    name="wintersar",
    help="Open-source Sentinel-1 InSAR (SBAS) toolkit: select · precheck · run · diagnose · validate.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)

# module -> import path of the CLI registrar
_MODULE_CLIS: tuple[str, ...] = (
    "wintersar.select.cli",
    "wintersar.pipeline.cli",
    "wintersar.unwrap.cli",
    "wintersar.diagnose.cli",
    "wintersar.validate.cli",
    "wintersar.research.cli",
    "wintersar.bench.cli",
)


@app.callback()
def _main_callback(
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit a JSON envelope on stdout.")
    ] = False,
    lang: Annotated[str, typer.Option("--lang", help="Message language: ko | en.")] = state.lang,
    verbose: Annotated[int, typer.Option("-v", "--verbose", count=True)] = 0,
) -> None:
    state.json = json_out
    state.lang = lang if lang in ("ko", "en") else "ko"
    state.verbose = verbose
    state.apply()


@app.command()
def version() -> None:
    """Print the wintersar version."""
    if state.json:
        emit_json("version", {"version": __version__})
    else:
        console.print(f"wintersar {__version__}")


@app.command("check-install")
def check_install(
    engines: Annotated[
        list[str] | None,
        typer.Option("--engine", "-e", help="Only check these engines (default: all)."),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Exit 1 when a FAIL finding exists (default: always 0)."),
    ] = False,
) -> None:
    """Report engine availability, versions, credentials and hardware (PERF-12).

    Like the other *report* commands (``search``, ``diagnose``, ``validate``) this exits 0
    as long as the report could be produced; the envelope's ``ok`` is false when a FAIL
    finding (e.g. a missing engine) is present. ``--strict`` turns that into exit 1 so a
    script can gate on it without parsing the envelope.
    """
    from wintersar.engines.base import list_engines
    from wintersar.util import sysinfo

    spec = sysinfo.detect()
    findings: list[Finding] = []
    rows: list[dict[str, object]] = []
    for name, cls in sorted(list_engines().items()):
        if engines and name not in engines:
            continue
        eng = cls()
        version = eng.detect_version()
        fs = eng.check_install()
        findings.extend(fs)
        rows.append(
            {
                "engine": name,
                "version": version,
                "constraint": cls.version_constraint,
                "available": not any(f.is_fail for f in fs),
                "stages": list(cls.stages),
                "license": cls.license_note,
            }
        )
    # Engines that need Earthdata (hyp3) already report ENV-003; do not say it twice.
    if not any(f.rule_id == "ENV-003" for f in findings):
        findings.extend(_check_credentials())
    findings.extend(_check_geo_libs())
    findings.extend(_check_optional_packages())
    if not spec.gpu:
        findings.append(
            Finding(
                rule_id="ENV-005",
                severity="INFO",
                message_key="env.ENV-005.cause",
                fix_key="env.ENV-005.fix",
            )
        )
    data = {
        "python": spec.python,
        "os": spec.os,
        "cores": spec.cores,
        "memory_gb": round(spec.memory_gb, 1),
        "gpu": spec.gpu,
        "gpu_name": spec.gpu_name,
        "engines": rows,
    }
    ok = not any(f.is_fail for f in findings)
    if state.json:
        emit_json("check-install", data, findings, ok=ok)
        if strict and not ok:
            raise typer.Exit(code=1)
        return
    from rich.table import Table

    console.print(f"[bold]{t('cli.check_install.title')}[/] — wintersar {__version__}")
    console.print(t("cli.check_install.python", version=spec.python) + f" · {spec.os}")
    console.print(
        t(
            "cli.check_install.hardware",
            cores=spec.cores,
            memory_gb=spec.memory_gb,
            gpu=spec.gpu_name or t("common.no"),
        )
    )
    table = Table(expand=True)
    table.add_column(t("cli.check_install.engine"))
    table.add_column(t("cli.check_install.version"))
    table.add_column("constraint")
    table.add_column(t("cli.check_install.status"))
    table.add_column("stages")
    for r in rows:
        ok = bool(r["available"])
        status = (
            f"[green]{t('cli.check_install.ok')}[/]"
            if ok
            else f"[red]{t('cli.check_install.missing')}[/]"
        )
        table.add_row(
            str(r["engine"]),
            str(r["version"] or "-"),
            str(r["constraint"]),
            status,
            ", ".join(str(s) for s in cast(list[str], r["stages"])),
        )
    console.print(table)
    print_findings(findings)
    if strict and not ok:
        raise typer.Exit(code=1)


def _check_optional_packages() -> list[Finding]:
    """ENV-006 for optional helper packages (DEM via sardem, orbits via sentineleof)."""
    out: list[Finding] = []
    try:
        from wintersar.select import dem as _dem

        out.extend(_dem.check_install())
    except Exception:
        pass
    try:
        from wintersar.engines import aux_cache as _aux

        fn = getattr(_aux, "check_install", None)
        if callable(fn):
            out.extend(fn())
    except Exception:
        pass
    return out


def _check_credentials() -> list[Finding]:
    import os

    if os.environ.get("EARTHDATA_TOKEN"):
        return []
    netrc = Path.home() / ".netrc"
    if netrc.exists():
        try:
            if "urs.earthdata.nasa.gov" in netrc.read_text(encoding="utf-8", errors="ignore"):
                return []
        except OSError:
            pass
    return [
        Finding(
            rule_id="ENV-003",
            severity="WARN",
            message_key="env.ENV-003.cause",
            fix_key="env.ENV-003.fix",
        )
    ]


def _check_geo_libs() -> list[Finding]:
    try:
        import pyproj
        import rasterio

        gdal_v = getattr(rasterio, "__gdal_version__", "?")
        proj_v = getattr(pyproj, "proj_version_str", "?")
        proj_from_gdal = getattr(rasterio, "__proj_version__", None)
        if (
            proj_from_gdal
            and proj_v != "?"
            and proj_from_gdal.split(".")[0] != proj_v.split(".")[0]
        ):
            return [
                Finding(
                    rule_id="ENV-004",
                    severity="WARN",
                    message_key="env.ENV-004.cause",
                    fix_key="env.ENV-004.fix",
                    params={
                        "rasterio_gdal": f"GDAL {gdal_v}/PROJ {proj_from_gdal}",
                        "pyproj_proj": proj_v,
                    },
                    evidence={
                        "gdal": gdal_v,
                        "proj_rasterio": proj_from_gdal,
                        "proj_pyproj": proj_v,
                    },
                )
            ]
    except ImportError:
        pass
    return []


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Where to write the example config.yaml")] = Path(
        "config.yaml"
    ),
    force: Annotated[bool, typer.Option("--force", help="Overwrite if exists.")] = False,
) -> None:
    """Write an example ``config.yaml`` (plan §4.4)."""
    from wintersar.pipeline.config import EXAMPLE_CONFIG
    from wintersar.util.masking import mask_text

    if path.exists() and not force:
        masked = mask_text(str(path))
        if state.json:
            emit_json(
                "init",
                {"path": masked},
                [
                    Finding(
                        rule_id="CLI-002",
                        severity="FAIL",
                        message_key="cli.CLI-002.cause",
                        fix_key="cli.CLI-002.fix",
                        params={"path": masked},
                    )
                ],
                ok=False,
            )
        else:
            err_console.print(f"[red]{t('cli.CLI-002.cause', path=masked)}[/]")
            err_console.print(t("cli.CLI-002.fix", path=masked))
        raise typer.Exit(code=1)
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    if state.json:
        emit_json("init", {"path": str(path)})
    else:
        console.print(t("cli.wrote", path=str(path)))


def _mount_module_clis() -> None:
    for mod_path in _MODULE_CLIS:
        try:
            mod = importlib.import_module(mod_path)
        except ModuleNotFoundError as e:
            if e.name and (
                mod_path.startswith(e.name) or e.name.startswith(mod_path.rsplit(".", 1)[0])
            ):
                continue  # module not implemented yet
            if "-v" in sys.argv or "--verbose" in sys.argv:
                err_console.print(f"[yellow]skip {mod_path}: {e}[/]")
            continue
        except Exception as e:
            if "-v" in sys.argv or "--verbose" in sys.argv:
                err_console.print(f"[yellow]skip {mod_path}: {e!r}[/]")
            continue
        register = getattr(mod, "register", None)
        if callable(register):
            register(app)


_mount_module_clis()


def _invoked_command() -> str:
    """Best-effort command name for the error envelope (``wintersar --json plan …`` → 'plan')."""
    skip_value = False
    for arg in sys.argv[1:]:
        if skip_value:
            skip_value = False
            continue
        if arg.startswith("-"):
            skip_value = arg == "--lang"
            continue
        return arg
    return "wintersar"


def _report_unexpected(exc: BaseException) -> None:
    """Turn an exception that escaped a sub-command into the normal output contract.

    Typer re-raises unexpected exceptions instead of converting them to an exit code
    (``Typer.__call__``), so without this the process ends in a traceback: no ``--json``
    envelope at all, and unmasked home paths in the stderr frames (rule 11.11).
    # source: .venv/lib/python3.11/site-packages/typer/main.py (Typer.__call__)
    """
    from wintersar.util.masking import mask_text

    detail = mask_text(f"{type(exc).__name__}: {exc}")
    finding = Finding(
        rule_id="CLI-001",
        severity="FAIL",
        message_key="cli.CLI-001.cause",
        fix_key="cli.CLI-001.fix",
        params={"error": detail},
        evidence={"error": detail},
    )
    if state.json:
        emit_json(_invoked_command(), {"error": detail}, [finding], ok=False)
    else:
        err_console.print(f"[red]{t('cli.CLI-001.cause', error=detail)}[/]")
        err_console.print(t("cli.CLI-001.fix", error=detail))


def main() -> None:
    try:
        app()
    except Exception as exc:
        if state.verbose:
            raise
        _report_unexpected(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
