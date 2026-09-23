#!/usr/bin/env python
"""Guard against drift between ``pyproject.toml`` and ``pixi.toml`` (PERF-12, ADR-0107).

``pyproject.toml`` (uv) is the source of truth for the dependency lists; ``pixi.toml`` (engine
environments) mirrors them because a stand-alone pixi manifest cannot reference another
file's dependency table. This script compares the two (names *and* specifiers) so they
cannot silently diverge. It needs nothing outside the standard library so it can run before
any environment exists.

Usage (from the repo root)::

    .venv/bin/python scripts/check_env.py            # exit 1 and list every difference
    .venv/bin/python scripts/check_env.py --group core --group dev

Groups (``pyproject`` side -> ``pixi`` table)::

    core   [project].dependencies                    -> [pypi-dependencies]
    dev    optional-dependencies.dev                 -> [feature.dev.pypi-dependencies]
    hyp3   optional-dependencies.hyp3                -> [feature.hyp3.pypi-dependencies]
    unwrap optional-dependencies.unwrap              -> [feature.unwrap.dependencies]  (conda)
    aux    optional-dependencies.orbits + .dem       -> [feature.aux.dependencies]     (conda)
    plots  optional-dependencies.plots               -> subset of [feature.dev.pypi-dependencies]

The conda-side tables hold conda-forge packages (snaphu, sentineleof, sardem — ADR-0105); the
guard compares the package name (PEP 503 normalised, so conda's ``hyp3_sdk`` would match PyPI's
``hyp3-sdk``) and the specifier text, which means the manifest has to keep the PEP 440
spelling that both tools accept (``>=0.4,<1``). ``plots`` has no feature of its own: its
packages are part of the ``dev`` extra, so the dev table only has to *contain* them.

Extras that deliberately have no pixi counterpart are declared in ``UV_ONLY_EXTRAS``
(``spurt``: no conda-forge package, ADR-0025; ``gpu``: the engine environments and
``Dockerfile.engines`` are CPU-only, docs/install.md; ``docs``: mkdocs is the uv/CI path).
pixi features without a pyproject side are declared in ``PIXI_ONLY_FEATURES`` (the conda-only
engines and ``geo``). When all groups are compared, an extra or feature that is neither mapped
nor declared is reported as drift, so adding one forces a decision here.

Exit codes: 0 in sync, 1 drift found, 2 unreadable or invalid input.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, NamedTuple

REPO = Path(__file__).resolve().parents[1]
PROJECT_NAME = "wintersar"
#: pseudo-extra naming ``[project].dependencies``
CORE = "core"


class Group(NamedTuple):
    """One comparison: the union of ``extras`` against the pixi table at ``table``."""

    extras: tuple[str, ...]
    table: tuple[str, ...]
    #: the pixi table may hold more packages than the extras (only one direction is checked)
    subset: bool = False


#: pyproject group -> pixi table (see the module docstring)
GROUPS: dict[str, Group] = {
    CORE: Group((CORE,), ("pypi-dependencies",)),
    "dev": Group(("dev",), ("feature", "dev", "pypi-dependencies")),
    "hyp3": Group(("hyp3",), ("feature", "hyp3", "pypi-dependencies")),
    "unwrap": Group(("unwrap",), ("feature", "unwrap", "dependencies")),
    "aux": Group(("orbits", "dem"), ("feature", "aux", "dependencies")),
    "plots": Group(("plots",), ("feature", "dev", "pypi-dependencies"), subset=True),
}
#: pyproject extras that intentionally have no pixi mirror
UV_ONLY_EXTRAS: frozenset[str] = frozenset({"spurt", "gpu", "docs"})
#: pixi features that intentionally have no pyproject extra
PIXI_ONLY_FEATURES: frozenset[str] = frozenset({"geo", "tophu", "isce2", "mintpy", "dolphin"})

# PEP 508 subset: ``name[extras] specifier ; marker`` (markers are dropped before matching).
_REQ_RE = re.compile(
    r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[^\]]*\])?\s*(.*?)\s*$"
)


class InputError(ValueError):
    """Unreadable or malformed input (exit 2), as opposed to drift (exit 1)."""


def display_path(path: Path) -> str:
    """Path for messages with the home directory shortened to ``~`` (rule 11.11)."""
    try:
        return "~/" + path.resolve().relative_to(Path.home()).as_posix()
    except (ValueError, OSError):
        return str(path)


def normalize_name(name: str) -> str:
    """PEP 503 normalisation (``types-PyYAML`` == ``types_pyyaml``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def normalize_spec(spec: str | None) -> str:
    """Canonical specifier: no whitespace, clauses sorted; ``*``/empty mean 'any version'."""
    if spec is None:
        return ""
    spec = spec.strip()
    if spec in ("", "*"):
        return ""
    clauses = sorted(c.replace(" ", "") for c in spec.split(",") if c.strip())
    return ",".join(clauses)


def parse_requirement(req: str) -> tuple[str, str]:
    """``'numpy>=1.26; python_version>"3"'`` -> ``('numpy', '>=1.26')``."""
    head = req.split(";", 1)[0]
    m = _REQ_RE.match(head)
    if m is None:
        msg = f"cannot parse requirement {req!r}"
        raise InputError(msg)
    return normalize_name(m.group(1)), normalize_spec(m.group(3))


def load_toml(path: Path) -> dict[str, Any]:
    """Parse ``path``; :class:`InputError` when it is unreadable or not TOML."""
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        msg = f"cannot read {display_path(path)}: {exc.strerror or exc}"
        raise InputError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"{display_path(path)} is not valid TOML: {exc}"
        raise InputError(msg) from exc


def _extra_requirements(data: dict[str, Any], extra: str) -> dict[str, str]:
    project = data.get("project", {})
    reqs: list[str]
    if extra == CORE:
        reqs = list(project.get("dependencies", []))
    else:
        reqs = list(project.get("optional-dependencies", {}).get(extra, []))
    return dict(parse_requirement(r) for r in reqs)


def pyproject_group(data: dict[str, Any], group: str) -> dict[str, str]:
    """Requirements of a pyproject group as ``{normalised name: canonical specifier}``.

    Groups that merge several extras (``aux`` = orbits + dem) return the union; a package the
    extras disagree on is reported by :func:`compare`, here the last extra wins.
    """
    out: dict[str, str] = {}
    for extra in GROUPS[group].extras:
        out.update(_extra_requirements(data, extra))
    return out


def pixi_group(data: dict[str, Any], group: str) -> dict[str, str] | None:
    """Entries of the pixi table mapped to ``group`` (``None`` when the table is absent).

    The project's own editable path entry (``wintersar = { path = "." }``) is skipped; any
    other non-version source (``path``/``git``/``url``) is reported as ``"<non-version>"`` so
    it shows up as a difference.
    """
    node: Any = data
    for key in GROUPS[group].table:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    if not isinstance(node, dict):
        return None
    out: dict[str, str] = {}
    for raw_name, value in node.items():
        name = normalize_name(str(raw_name))
        if name == PROJECT_NAME:
            continue
        if isinstance(value, str):
            out[name] = normalize_spec(value)
        elif isinstance(value, dict) and "version" in value:
            out[name] = normalize_spec(str(value["version"]))
        elif isinstance(value, dict):
            out[name] = "<non-version>"
        else:
            out[name] = "<unparseable>"
    return out


def _compare_group(pyproject: dict[str, Any], pixi: dict[str, Any], group: str) -> list[str]:
    spec = GROUPS[group]
    table = ".".join(spec.table)
    problems: list[str] = []
    # extras merged into one group must agree with each other before they are mirrored
    seen: dict[str, tuple[str, str]] = {}
    for extra in spec.extras:
        for name, version in _extra_requirements(pyproject, extra).items():
            if name in seen and seen[name][1] != version:
                problems.append(
                    f"[{group}] {name}: pyproject.toml extras {seen[name][0]!r} "
                    f"{seen[name][1] or '*'!r} != {extra!r} {version or '*'!r}"
                )
            seen.setdefault(name, (extra, version))
    want = pyproject_group(pyproject, group)
    have = pixi_group(pixi, group)
    if have is None:
        problems.append(f"[{group}] pixi.toml has no [{table}] table")
        return problems
    for name in sorted(want.keys() - have.keys()):
        problems.append(f"[{group}] {name}{want[name]!s}: in pyproject.toml, missing in [{table}]")
    if not spec.subset:
        for name in sorted(have.keys() - want.keys()):
            problems.append(
                f"[{group}] {name}{have[name]!s}: in [{table}], missing in pyproject.toml"
            )
    for name in sorted(want.keys() & have.keys()):
        if want[name] != have[name]:
            problems.append(
                f"[{group}] {name}: pyproject.toml {want[name] or '*'!r} != pixi.toml "
                f"{have[name] or '*'!r}"
            )
    return problems


def _classify(pyproject: dict[str, Any], pixi: dict[str, Any]) -> list[str]:
    """Every extra and every feature must be mapped by ``GROUPS`` or declared one-sided."""
    problems: list[str] = []
    mapped_extras = {e for g in GROUPS.values() for e in g.extras} - {CORE}
    extras = pyproject.get("project", {}).get("optional-dependencies", {})
    for extra in sorted(set(extras) - mapped_extras - UV_ONLY_EXTRAS):
        problems.append(
            f"[extras] {extra}: pyproject extra is neither mapped to a pixi table by GROUPS nor "
            "declared in UV_ONLY_EXTRAS (scripts/check_env.py)"
        )
    mapped_features = {g.table[1] for g in GROUPS.values() if g.table[0] == "feature"}
    features = pixi.get("feature", {})
    for feature in sorted(set(features) - mapped_features - PIXI_ONLY_FEATURES):
        problems.append(
            f"[features] {feature}: pixi feature is neither mapped to a pyproject extra by "
            "GROUPS nor declared in PIXI_ONLY_FEATURES (scripts/check_env.py)"
        )
    return problems


def compare(
    pyproject: dict[str, Any], pixi: dict[str, Any], groups: list[str] | None = None
) -> list[str]:
    """Return one human-readable line per difference (empty list == in sync).

    With ``groups`` given only those are compared; without it every group is compared and the
    extras/features classification (:func:`_classify`) runs as well.
    """
    problems: list[str] = []
    for group in groups or list(GROUPS):
        problems.extend(_compare_group(pyproject, pixi, group))
    if groups is None:
        problems.extend(_classify(pyproject, pixi))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pyproject", type=Path, default=REPO / "pyproject.toml")
    parser.add_argument("--pixi", type=Path, default=REPO / "pixi.toml")
    parser.add_argument(
        "--group",
        action="append",
        choices=sorted(GROUPS),
        help="compare only this group (repeatable; default: all groups + extras/features check)",
    )
    args = parser.parse_args(argv)
    try:
        problems = compare(load_toml(args.pyproject), load_toml(args.pixi), args.group)
    except InputError as exc:
        sys.stderr.write(f"check_env.py: {exc}\n")
        return 2
    if problems:
        sys.stdout.write("pyproject.toml / pixi.toml drift:\n")
        for line in problems:
            sys.stdout.write(f"  - {line}\n")
        return 1
    groups = ", ".join(args.group or GROUPS)
    sys.stdout.write(f"pyproject.toml and pixi.toml agree ({groups})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
