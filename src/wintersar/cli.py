"""``wintersar`` command line (plan §4.5).

Top-level commands defined here: ``check-install``, ``init``, ``version``.
Every module registers its own commands through ``wintersar.<module>.cli.register(app)``
so that modules stay independent; a module whose CLI is not importable is skipped
(with ``-v`` the import error is shown).

Global options: ``--json`` (machine-readable envelope parsed by the QGIS plugin),
``--lang ko|en``, ``-v``.

Rule 11.6 on the command line (ADR-0090/0091): every help text is a catalogue key rendered
by :func:`wintersar.util.clihelp.h`, ``--lang`` is *eager* so ``wintersar --lang en --help``
is already English, and every error path ends in a finding (envelope under ``--json``),
never a traceback on stdout.
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
from wintersar.util import clihelp
from wintersar.util.clihelp import h
from wintersar.util.clistate import state
from wintersar.util.output import (
    CLI_EXISTS,
    cli_finding,
    console,
    emit_json,
    err_console,
    exit_with_findings,
    print_findings,
    report_unexpected,
)

app = typer.Typer(
    name="wintersar",
    cls=clihelp.HelpGroup,
    help=h("cli_help.root.help"),
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


def _lang_callback(value: str | None) -> str | None:
    """Eager ``--lang``: adopt the language before ``--help`` is rendered (ADR-0090).

    Eager parameters are processed first, in command-line order, so ``--lang en --help``
    sees the choice; ``--help --lang en`` still works through the ``sys.argv`` scan.
    # source: .venv/lib/python3.11/site-packages/typer/_click/core.py iter_params_for_processing
    """
    state.set_lang(value)
    return value


@app.callback()
def _main_callback(
    json_out: Annotated[bool, typer.Option("--json", help=h("cli_help.root.json"))] = False,
    lang: Annotated[
        str | None,
        typer.Option(
            "--lang",
            help=h("cli_help.root.lang"),
            is_eager=True,
            callback=_lang_callback,
            show_default=False,
        ),
    ] = None,
    verbose: Annotated[
        int, typer.Option("-v", "--verbose", count=True, help=h("cli_help.root.verbose"))
    ] = 0,
) -> None:
    state.json = json_out
    state.set_lang(lang)
    state.verbose = verbose
    state.apply()


@app.command(help=h("cli_help.version.help"))
def version() -> None:
    """Print the wintersar version."""
    if state.json:
        emit_json("version", {"version": __version__})
    else:
        console.print(f"wintersar {__version__}")


@app.command("check-install", help=h("cli_help.check_install.help"))
def check_install(
    engines: Annotated[
        list[str] | None,
        typer.Option("--engine", "-e", help=h("cli_help.check_install.engine")),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict", help=h("cli_help.check_install.strict")),
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


@app.command(help=h("cli_help.init.help"))
def init(
    path: Annotated[Path, typer.Argument(help=h("cli_help.init.path"))] = Path("config.yaml"),
    force: Annotated[bool, typer.Option("--force", help=h("cli_help.init.force"))] = False,
) -> None:
    """Write an example ``config.yaml`` (plan §4.4)."""
    from wintersar.pipeline.config import EXAMPLE_CONFIG
    from wintersar.util.masking import mask_text

    if path.exists() and not force:
        masked = mask_text(str(path))
        raise exit_with_findings(
            "init", [cli_finding(CLI_EXISTS, path=masked)], code=1, data={"path": masked}
        )
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
    # every command/group renders its help in the language of the invocation (ADR-0090)
    clihelp.install(app)


_mount_module_clis()


def main() -> None:
    """Entry point: ``HelpGroup.main`` already keeps the output contract for click usage
    errors and exceptions raised inside commands; this guards what escapes typer itself
    (``Typer.__call__`` re-raises, rule 11.11: no unmasked traceback)."""
    try:
        app()
    except Exception as exc:
        if state.verbose:
            raise
        report_unexpected(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
