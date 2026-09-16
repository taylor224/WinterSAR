"""Processing algorithms: declarative specs (pure python) + QGIS class factory (ADR-0070).

``ALGORITHMS`` describes search / precheck / run / validate as parameter lists;
:func:`build_cli_args` turns a value mapping into the CLI argv and
:func:`outputs_from_response` maps a :class:`CliResponse` to Processing outputs. Both are
unit tested without QGIS. :func:`make_algorithm_classes` creates the
``QgsProcessingAlgorithm`` subclasses lazily (only inside QGIS).

# source: https://qgis.org/pyqgis/3.44/core/QgsProcessingAlgorithm.html
#   abstract name()/displayName()/createInstance()/initAlgorithm(configuration)/
#   processAlgorithm(parameters, context, feedback); virtual group()/groupId()/shortHelpString()
#   parameterAsString/parameterAsBool/parameterAsFile/parameterAsEnum/parameterAsInt(parameters, name, context)
#   addParameter(parameterDefinition, createOutput=True), addOutput(outputDefinition)
# source: https://qgis.org/pyqgis/3.44/core/QgsProcessingParameterFile.html
#   QgsProcessingParameterFile(name, description='', behavior=...File, extension='',
#   defaultValue=None, optional=False, fileFilter='') ; Behavior.File / Behavior.Folder
# source: https://qgis.org/pyqgis/3.44/core/QgsProcessingParameterEnum.html
#   QgsProcessingParameterEnum(name, description='', options=[], allowMultiple=False,
#   defaultValue=None, optional=False, usesStaticStrings=False)
# source: https://qgis.org/pyqgis/3.44/core/QgsProcessingFeedback.html
#   pushInfo(info), pushWarning(warning), reportError(error, fatalError=False);
#   QgsFeedback.isCanceled() -> bool, setProgress(progress: float)
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import cli_client
from .cli_client import STAGE_ORDER, CliResponse, WintersarClient
from .i18n import render_finding, t

STAGE_OPTIONS: tuple[str, ...] = ("", *STAGE_ORDER)  # "" = not set
OUTPUT_NAMES: tuple[str, ...] = ("OK", "COMMAND", "DATA", "FINDINGS", "N_FAIL", "N_WARN")


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label_key: str
    kind: str  # file | folder | string | bool | enum | int
    optional: bool = False
    default: Any = None
    options: tuple[str, ...] = ()
    extension: str = ""


@dataclass(frozen=True)
class AlgorithmSpec:
    name: str
    display_key: str
    help_key: str
    params: tuple[ParamSpec, ...]


ALGORITHMS: tuple[AlgorithmSpec, ...] = (
    AlgorithmSpec(
        "search",
        "qgis.processing.search",
        "qgis.processing.help_search",
        (ParamSpec("CONFIG", "qgis.processing.param_config", "file", extension="yaml"),),
    ),
    AlgorithmSpec(
        "precheck",
        "qgis.processing.precheck",
        "qgis.processing.help_precheck",
        (
            ParamSpec("CANDIDATES", "qgis.processing.param_candidates", "file", extension="json"),
            ParamSpec("CONFIG", "qgis.processing.param_config", "file", extension="yaml"),
            ParamSpec("OUT", "qgis.processing.param_out", "folder", optional=True),
            ParamSpec("NO_FAIL", "qgis.processing.param_no_fail", "bool", default=False),
        ),
    ),
    AlgorithmSpec(
        "run",
        "qgis.processing.run",
        "qgis.processing.help_run",
        (
            ParamSpec("CONFIG", "qgis.processing.param_config", "file", extension="yaml"),
            ParamSpec(
                "UNTIL", "qgis.processing.param_until", "enum", options=STAGE_OPTIONS, default=0
            ),
            ParamSpec(
                "FROM", "qgis.processing.param_from", "enum", options=STAGE_OPTIONS, default=0
            ),
            ParamSpec("FORCE", "qgis.processing.param_force", "string", optional=True),
            ParamSpec("DRY_RUN", "qgis.processing.param_dry_run", "bool", default=False),
        ),
    ),
    AlgorithmSpec(
        "validate",
        "qgis.processing.validate",
        "qgis.processing.help_validate",
        (
            ParamSpec("TS", "qgis.processing.param_ts", "file", extension="h5"),
            ParamSpec(
                "LEVELING", "qgis.processing.param_leveling", "file", optional=True, extension="csv"
            ),
            ParamSpec("GNSS", "qgis.processing.param_gnss", "file", optional=True, extension="csv"),
        ),
    ),
)

ALGORITHMS_BY_NAME: dict[str, AlgorithmSpec] = {a.name: a for a in ALGORITHMS}


def _stage(value: Any) -> str | None:
    """Enum index or stage name -> stage name (``None`` for "not set")."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if 0 <= value < len(STAGE_OPTIONS):
            return STAGE_OPTIONS[value] or None
        msg = f"stage index {value} out of range"
        raise ValueError(msg)
    s = str(value).strip()
    return s or None


def parse_force(value: Any) -> list[str]:
    """``"unwrap, timeseries"`` -> ``["unwrap", "timeseries"]`` (validated stage names)."""
    if value is None:
        return []
    if isinstance(value, list | tuple):
        items = [str(v).strip() for v in value]
    else:
        items = [s.strip() for s in str(value).split(",")]
    stages = [s for s in items if s]
    for s in stages:
        if s not in STAGE_ORDER:
            msg = f"unknown stage {s!r}; known: {', '.join(STAGE_ORDER)}"
            raise ValueError(msg)
    return stages


def build_cli_args(algorithm: str, values: Mapping[str, Any]) -> list[str]:
    """Processing parameter values -> ``wintersar`` argv (without the global options)."""
    if algorithm == "search":
        return cli_client.search_args(str(values["CONFIG"]))
    if algorithm == "precheck":
        return cli_client.precheck_args(
            str(values["CANDIDATES"]),
            str(values["CONFIG"]),
            out=values.get("OUT") or None,
            no_fail=bool(values.get("NO_FAIL", False)),
        )
    if algorithm == "run":
        return cli_client.run_args(
            str(values["CONFIG"]),
            until=_stage(values.get("UNTIL")),
            from_=_stage(values.get("FROM")),
            force=parse_force(values.get("FORCE")),
            dry_run=bool(values.get("DRY_RUN", False)),
        )
    if algorithm == "validate":
        return cli_client.validate_args(
            str(values["TS"]), values.get("LEVELING") or None, values.get("GNSS") or None
        )
    msg = f"unknown algorithm {algorithm!r}"
    raise KeyError(msg)


def outputs_from_response(resp: CliResponse) -> dict[str, Any]:
    """Processing outputs (JSON text for the structured parts)."""
    return {
        "OK": bool(resp.ok),
        "COMMAND": resp.command,
        "DATA": json.dumps(resp.data, ensure_ascii=False),
        "FINDINGS": json.dumps(resp.findings, ensure_ascii=False),
        "N_FAIL": resp.n_fail,
        "N_WARN": resp.n_warn,
    }


def findings_text(resp: CliResponse, lang: str | None = None) -> list[str]:
    """One ``[SEV] ID: cause -> fix`` line per finding (for the Processing log)."""
    lines: list[str] = []
    for f in resp.findings:
        sev, rid, cause, fix = render_finding(f, lang)
        lines.append(f"[{sev}] {rid}: {cause}" + (f" -> {fix}" if fix else ""))
    return lines


# ----------------------------------------------------------------------------- QGIS classes


def make_algorithm_classes(client_factory: Callable[[], WintersarClient]) -> list[type]:
    """Create one ``QgsProcessingAlgorithm`` subclass per :data:`ALGORITHMS` entry."""
    from qgis.core import (
        QgsProcessingAlgorithm,
        QgsProcessingException,
        QgsProcessingOutputBoolean,
        QgsProcessingOutputNumber,
        QgsProcessingOutputString,
        QgsProcessingParameterBoolean,
        QgsProcessingParameterEnum,
        QgsProcessingParameterFile,
        QgsProcessingParameterString,
    )

    class _WintersarAlgorithm(QgsProcessingAlgorithm):  # type: ignore[misc]
        spec: AlgorithmSpec

        def createInstance(self) -> Any:  # noqa: N802
            return type(self)()

        def name(self) -> str:
            return self.spec.name

        def displayName(self) -> str:  # noqa: N802
            return t(self.spec.display_key)

        def group(self) -> str:
            return t("qgis.plugin.group")

        def groupId(self) -> str:  # noqa: N802
            return "wintersar"

        def shortHelpString(self) -> str:  # noqa: N802
            return t(self.spec.help_key)

        def initAlgorithm(self, configuration: dict[str, Any] | None = None) -> None:  # noqa: N802
            for p in self.spec.params:
                label = t(p.label_key)
                if p.kind == "file":
                    self.addParameter(
                        QgsProcessingParameterFile(
                            p.name,
                            label,
                            behavior=QgsProcessingParameterFile.Behavior.File,
                            extension=p.extension,
                            optional=p.optional,
                        )
                    )
                elif p.kind == "folder":
                    self.addParameter(
                        QgsProcessingParameterFile(
                            p.name,
                            label,
                            behavior=QgsProcessingParameterFile.Behavior.Folder,
                            optional=p.optional,
                        )
                    )
                elif p.kind == "bool":
                    self.addParameter(
                        QgsProcessingParameterBoolean(p.name, label, defaultValue=bool(p.default))
                    )
                elif p.kind == "enum":
                    self.addParameter(
                        QgsProcessingParameterEnum(
                            p.name,
                            label,
                            options=list(p.options),
                            allowMultiple=False,
                            defaultValue=p.default,
                            optional=p.optional,
                        )
                    )
                else:
                    self.addParameter(
                        QgsProcessingParameterString(
                            p.name, label, defaultValue=p.default, optional=p.optional
                        )
                    )
            self.addOutput(QgsProcessingOutputBoolean("OK", t("qgis.processing.out_ok")))
            self.addOutput(QgsProcessingOutputString("COMMAND", t("qgis.processing.out_command")))
            self.addOutput(QgsProcessingOutputString("DATA", t("qgis.processing.out_data")))
            self.addOutput(QgsProcessingOutputString("FINDINGS", t("qgis.processing.out_findings")))
            self.addOutput(QgsProcessingOutputNumber("N_FAIL", t("qgis.processing.out_n_fail")))
            self.addOutput(QgsProcessingOutputNumber("N_WARN", t("qgis.processing.out_n_warn")))

        def _values(self, parameters: dict[str, Any], context: Any) -> dict[str, Any]:
            values: dict[str, Any] = {}
            for p in self.spec.params:
                if p.kind in ("file", "folder"):
                    values[p.name] = self.parameterAsFile(parameters, p.name, context)
                elif p.kind == "bool":
                    values[p.name] = self.parameterAsBool(parameters, p.name, context)
                elif p.kind == "enum":
                    values[p.name] = self.parameterAsEnum(parameters, p.name, context)
                else:
                    values[p.name] = self.parameterAsString(parameters, p.name, context)
            return values

        def processAlgorithm(  # noqa: N802
            self, parameters: dict[str, Any], context: Any, feedback: Any
        ) -> dict[str, Any]:
            values = self._values(parameters, context)
            try:
                args = build_cli_args(self.spec.name, values)
            except (KeyError, ValueError) as exc:
                raise QgsProcessingException(str(exc)) from exc
            client = client_factory()
            feedback.pushInfo(f"wintersar {' '.join(args)}")

            def on_line(line: str) -> None:
                feedback.pushInfo(line)
                if feedback.isCanceled():
                    client.cancel()

            resp = client.run(args, on_line=on_line)
            for line in findings_text(resp):
                if line.startswith("[" + t("common.severity.FAIL")):
                    feedback.reportError(line)
                else:
                    feedback.pushWarning(line)
            if resp.client_error and resp.error != "CLI_ERROR":
                raise QgsProcessingException("\n".join(findings_text(resp)) or str(resp.error))
            return outputs_from_response(resp)

    classes: list[type] = []
    for spec in ALGORITHMS:
        cls = type(
            f"Wintersar{spec.name.capitalize()}Algorithm", (_WintersarAlgorithm,), {"spec": spec}
        )
        classes.append(cls)
    return classes
