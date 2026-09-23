"""Help-text language and click-level error envelopes for the ``wintersar`` CLI.

Two problems of rule 11.6 on the command line are solved here (ADR-0090, ADR-0091):

1. **Help text language.** Typer renders ``--help`` *before* the app callback that parses
   ``--lang`` runs, and the ``help="..."`` strings are evaluated when the CLI modules are
   imported. :func:`h` therefore resolves the language early (:func:`help_lang`:
   ``--lang`` already parsed → ``sys.argv`` scan → ``WINTERSAR_LANG`` → ``ko``) and
   remembers every rendered text in a registry, so that :class:`HelpGroup` /
   :class:`HelpCommand` can re-translate the help of a command right before rendering it
   (when ``--lang`` *has* been parsed, e.g. ``wintersar --lang en plan --help``).
2. **Usage errors as envelopes.** Click reports a missing option or an unknown value on
   stderr with exit 2 and no JSON at all. :meth:`HelpGroup.main` runs click in
   non-standalone mode and turns every :class:`ClickException` into a ``CLI-003`` finding
   (envelope under ``--json``, typer's rich error panel otherwise) with the same exit code.

# source: .venv/lib/python3.11/site-packages/typer/main.py (get_command_from_info: ``cls``
#   per command; get_group_from_info: ``cls`` per group; Typer.__call__ re-raises)
# source: .venv/lib/python3.11/site-packages/typer/core.py (``_main``: non-standalone mode
#   re-raises ClickException / returns Exit.exit_code; TyperGroup.parse_args raises
#   NoArgsIsHelpError whose constructor already printed the help page)
# source: .venv/lib/python3.11/site-packages/typer/_click/core.py
#   (iter_params_for_processing: eager params first, in command-line order)
"""

from __future__ import annotations

import inspect
import os
import sys
from collections.abc import Sequence
from typing import Any

import typer

# typer 0.27 ships its own copy of click: typer/_click ("adapted from Click 8.3.1"). The
# exceptions typer raises are those classes, not the ones of the separately installed
# ``click`` package (docs/open-questions.md: minimum typer pin).
from typer._click import Context, HelpFormatter
from typer._click import exceptions as _exc
from typer.core import TyperCommand, TyperGroup
from typer.models import DefaultPlaceholder

from wintersar.i18n import SUPPORTED, current_lang, has_key, t
from wintersar.util.clistate import state
from wintersar.util.masking import mask_text
from wintersar.util.output import (
    CLI_USAGE,
    cli_finding,
    emit_json,
    err_console,
    invoked_command,
    report_unexpected,
)

# Parameters typer/click add themselves (``add_completion``, ``add_help_option``): their
# help is a literal in the library, so it is replaced from the catalogue at render time.
# source: .venv/lib/python3.11/site-packages/typer/completion.py
#   (_install_completion_placeholder_function: help="Install completion for the current shell.")
# source: .venv/lib/python3.11/site-packages/typer/_click/decorators.py
#   (help_option: help="Show this message and exit.")
BUILTIN_PARAM_KEYS: dict[str, str] = {
    "help": "cli_help.root.help_option",
    "install_completion": "cli_help.root.install_completion",
    "show_completion": "cli_help.root.show_completion",
}

# ------------------------------------------------------------------ language resolution


def lang_from_argv(argv: Sequence[str]) -> str | None:
    """``--lang ko`` / ``--lang=ko`` anywhere in ``argv`` (first valid occurrence wins)."""
    it = iter(argv)
    for arg in it:
        if arg == "--lang":
            value: str | None = next(it, None)
        elif arg.startswith("--lang="):
            value = arg.partition("=")[2]
        else:
            continue
        if value and value.lower() in SUPPORTED:
            return value.lower()
    return None


def help_lang() -> str:
    """Language of help text, resolvable before the app callback has run (ADR-0090).

    Order: a ``--lang`` that the root callback already parsed → ``--lang`` found by
    scanning ``sys.argv`` (top-level ``wintersar --help --lang en``, and the import-time
    resolution in :func:`h`) → ``WINTERSAR_LANG`` → ``ko``.
    """
    if state.lang_explicit and state.lang in SUPPORTED:
        return state.lang
    # an embedding interpreter (QGIS, a C host) may never have set sys.argv at all
    return lang_from_argv(getattr(sys, "argv", [])[1:]) or current_lang()


# ------------------------------------------------------------------ catalogue-backed help

# rendered text (any language) -> (key, params); lets format_help re-translate at render time
_REGISTRY: dict[str, tuple[str, dict[str, Any]]] = {}
_KEYS: set[str] = set()


def h(key: str, **params: Any) -> str:
    """Help text for ``key`` (``cli_help.<command>.<option>``) in :func:`help_lang`.

    Every ``help=`` of the CLIs goes through here; the text is also registered for each
    supported language so the rendered help can follow a later ``--lang``.
    """
    _KEYS.add(key)
    for lang in SUPPORTED:
        text = t(key, lang, **params)
        _REGISTRY.setdefault(text, (key, dict(params)))
        _REGISTRY.setdefault(inspect.cleandoc(text), (key, dict(params)))
    return t(key, help_lang(), **params)


def registered_keys() -> frozenset[str]:
    """Every catalogue key that :func:`h` has been asked for (tests)."""
    return frozenset(_KEYS)


def key_for(text: str | None) -> str | None:
    """The catalogue key behind a rendered help text, or ``None`` for a literal (tests)."""
    if not text:
        return None
    entry = _REGISTRY.get(text) or _REGISTRY.get(inspect.cleandoc(text))
    return entry[0] if entry else None


def missing_help_keys() -> list[str]:
    """Registered keys that are absent from either catalogue (must be empty)."""
    return sorted(k for k in _KEYS if not all(has_key(k, lang) for lang in SUPPORTED))


def _retranslate(text: str | None, lang: str) -> str | None:
    if not text:
        return text
    entry = _REGISTRY.get(text) or _REGISTRY.get(inspect.cleandoc(text))
    if entry is None:
        return text
    key, params = entry
    return t(key, lang, **params)


def localize_help(cmd: Any) -> None:
    """Re-translate the help of ``cmd``, its parameters and its sub-commands in place."""
    lang = help_lang()
    cmd.help = _retranslate(cmd.help, lang)
    for param in cmd.params:
        builtin = BUILTIN_PARAM_KEYS.get(str(param.name))
        if builtin is not None:
            param.help = t(builtin, lang)
            continue
        help_text = getattr(param, "help", None)
        if isinstance(help_text, str):
            param.help = _retranslate(help_text, lang)
    for sub in getattr(cmd, "commands", {}).values():
        sub.help = _retranslate(sub.help, lang)
        if isinstance(getattr(sub, "short_help", None), str):
            sub.short_help = _retranslate(sub.short_help, lang)


# ------------------------------------------------------------------ click-level errors


def json_requested(argv: Sequence[str]) -> bool:
    """``--json`` among the global options (before the first command word).

    Needed when the root callback has not run yet (``Missing command.`` / ``No such
    command`` are raised by ``TyperGroup.invoke`` before it). The value of ``--lang`` is
    not a command word, so ``--lang en --json`` counts like ``--json --lang en``.
    # source: .venv/lib/python3.11/site-packages/typer/core.py TyperGroup.invoke
    #   (ctx.fail("Missing command.") / resolve_command before super().invoke(ctx))
    """
    skip_value = False
    for arg in argv:
        if skip_value:
            skip_value = False
            continue
        if arg == "--json":
            return True
        if arg == "--lang":
            skip_value = True
            continue
        if not arg.startswith("-"):
            return False
    return False


def _command_of(exc: BaseException, argv: Sequence[str]) -> str:
    """``plan`` / ``unwrap run`` for the envelope, whatever the program name was.

    ``ctx.command_path`` starts with the root ``info_name``: one word for the console
    script, three for ``python -m wintersar.cli`` (how the QGIS client starts the CLI).
    # source: .venv/lib/python3.11/site-packages/typer/_click/core.py Context.command_path
    """
    ctx = getattr(exc, "ctx", None)
    path = getattr(ctx, "command_path", None)
    if ctx is not None and isinstance(path, str) and path:
        prog = str(getattr(ctx.find_root(), "info_name", None) or "")
        words = path[len(prog) :].split() if prog and path.startswith(prog) else path.split()[1:]
        if words:
            return " ".join(words)
    return invoked_command(argv)


def report_click_error(exc: Any, argv: Sequence[str]) -> int:
    """``CLI-003`` for a click usage error; returns the exit code to use (ADR-0091).

    ``NoArgsIsHelpError`` carries the whole help page as its message, so the usage line
    is reported instead.
    """
    command = _command_of(exc, argv)
    ctx = getattr(exc, "ctx", None)
    if isinstance(exc, _exc.NoArgsIsHelpError) and ctx is not None:
        detail = str(ctx.get_usage())
    else:
        detail = str(exc.format_message())
    detail = mask_text(detail)
    finding = cli_finding(CLI_USAGE, scope=command, command=command, detail=detail)
    if state.json or json_requested(argv):
        emit_json(command, None, [finding], ok=False)
    else:
        _show_click_error(exc)
    code = getattr(exc, "exit_code", 1)
    return int(code) if isinstance(code, int) else 1


def _show_click_error(exc: Any) -> None:
    """Typer's rich error panel when available (what standalone mode prints), else click's."""
    try:
        from typer import rich_utils

        rich_utils.rich_format_error(exc)
    except Exception:  # pragma: no cover - rich missing or a foreign exception type
        exc.show()


def _show_abort() -> None:
    """Ctrl-C / declined prompt: catalogue text instead of typer's English panel."""
    err_console.print(t("cli.aborted", state.lang), style="red", markup=False)


# ------------------------------------------------------------------ click classes


class HelpCommand(TyperCommand):
    """A typer command whose help follows :func:`help_lang` at render time."""

    def format_help(self, ctx: Context, formatter: HelpFormatter) -> None:
        localize_help(self)
        super().format_help(ctx, formatter)

    def get_help_option(self, ctx: Context) -> Any:
        option = super().get_help_option(ctx)
        if option is not None:
            option.help = t(BUILTIN_PARAM_KEYS["help"], help_lang())
        return option


class HelpGroup(TyperGroup):
    """A typer group whose help is localised at render time and whose ``main`` keeps the
    output contract for click-level errors (missing option, unknown value, no arguments).
    """

    def format_help(self, ctx: Context, formatter: HelpFormatter) -> None:
        localize_help(self)
        super().format_help(ctx, formatter)

    def get_help_option(self, ctx: Context) -> Any:
        option = super().get_help_option(ctx)
        if option is not None:
            option.help = t(BUILTIN_PARAM_KEYS["help"], help_lang())
        return option

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        # NoArgsIsHelpError prints the help page while it is being constructed; under
        # --json that would put a second document on stdout, so raise a plain usage error.
        if not args and self.no_args_is_help and not ctx.resilient_parsing and state.json:
            raise _exc.UsageError(ctx.get_usage(), ctx=ctx)
        return super().parse_args(ctx, args)

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        if not standalone_mode:
            return super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        argv = list(sys.argv[1:] if args is None else args)
        # One invocation = one state. CliRunner reuses the process, and the root callback
        # exports WINTERSAR_LANG for library code (``state.apply``): put it back afterwards
        # so a ``--lang en`` run does not turn the next ``--lang``-less run English.
        state.json = False
        state.lang_explicit = False
        saved_lang = os.environ.get("WINTERSAR_LANG")
        try:
            rv = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except _exc.ClickException as e:
            sys.exit(report_click_error(e, argv))
        except typer.Abort:
            _show_abort()
            sys.exit(1)
        except Exception as e:
            if state.verbose:
                raise
            report_unexpected(e, argv)
            sys.exit(1)
        finally:
            if saved_lang is None:
                os.environ.pop("WINTERSAR_LANG", None)
            else:
                os.environ["WINTERSAR_LANG"] = saved_lang
        sys.exit(rv if isinstance(rv, int) else 0)


def install(app: typer.Typer) -> None:
    """Give every command/group of ``app`` (recursively) the localising click classes.

    Explicit ``cls=`` choices are kept; only the defaults are replaced.
    # source: .venv/lib/python3.11/site-packages/typer/main.py Typer.command (``cls = TyperCommand``
    #   when None), typer/models.py CommandInfo/TyperInfo (``cls`` defaults)
    """
    # Typer.command() / Typer() fill ``cls`` with the plain typer classes when none is
    # given, so "default" means TyperCommand / TyperGroup here, not None.
    if isinstance(app.info.cls, DefaultPlaceholder) or app.info.cls in (None, TyperGroup):
        app.info.cls = HelpGroup
    for command in app.registered_commands:
        if command.cls in (None, TyperCommand):
            command.cls = HelpCommand
    for group in app.registered_groups:
        sub = group.typer_instance
        if sub is not None:
            install(sub)
