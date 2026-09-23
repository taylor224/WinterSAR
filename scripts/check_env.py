#!/usr/bin/env python
"""Guard against drift between ``pyproject.toml`` and ``pixi.toml`` (PERF-12, ADR-0107).

``pyproject.toml`` (uv) is the source of truth for the core dependency list; ``pixi.toml``
(engine environments) mirrors that list under ``[pypi-dependencies]`` because a stand-alone
pixi manifest cannot reference another file's dependency table. This script compares the two
(names *and* specifiers) so they cannot silently diverge. It needs nothing outside the
standard library so it can run before any environment exists.

Usage (from the repo root)::

    .venv/bin/python scripts/check_env.py            # exit 1 and list every difference
    .venv/bin/python scripts/check_env.py --group core --group dev

Groups (``pyproject`` extra -> ``pixi`` table)::

    core -> [pypi-dependencies]                       ([project].dependencies)
    dev  -> [feature.dev.pypi-dependencies]           ([project.optional-dependencies].dev)
    hyp3 -> [feature.hyp3.pypi-dependencies]          ([project.optional-dependencies].hyp3)

The ``orbits``/``dem``/``unwrap`` extras are *not* compared: in pixi those packages come from
conda-forge features (sentineleof, sardem, snaphu), see ADR-0105.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PROJECT_NAME = "wintersar"

#: pyproject group -> key path inside pixi.toml
GROUPS: dict[str, tuple[str, ...]] = {
    "core": ("pypi-dependencies",),
    "dev": ("feature", "dev", "pypi-dependencies"),
    "hyp3": ("feature", "hyp3", "pypi-dependencies"),
}

# PEP 508 subset: ``name[extras] specifier ; marker`` (markers are dropped before matching).
_REQ_RE = re.compile(
    r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[^\]]*\])?\s*(.*?)\s*$"
)


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
        raise ValueError(msg)
    return normalize_name(m.group(1)), normalize_spec(m.group(3))


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def pyproject_group(data: dict[str, Any], group: str) -> dict[str, str]:
    """Requirements of a pyproject group as ``{normalised name: canonical specifier}``."""
    project = data.get("project", {})
    reqs: list[str]
    if group == "core":
        reqs = list(project.get("dependencies", []))
    else:
        reqs = list(project.get("optional-dependencies", {}).get(group, []))
    return dict(parse_requirement(r) for r in reqs)


def pixi_group(data: dict[str, Any], group: str) -> dict[str, str] | None:
    """PyPI entries of the pixi table mapped to ``group`` (``None`` when the table is absent).

    The project's own editable path entry (``wintersar = { path = "." }``) is skipped; any
    other non-version source (``path``/``git``/``url``) is reported as ``"<non-version>"`` so
    it shows up as a difference.
    """
    node: Any = data
    for key in GROUPS[group]:
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


def compare(
    pyproject: dict[str, Any], pixi: dict[str, Any], groups: list[str] | None = None
) -> list[str]:
    """Return one human-readable line per difference (empty list == in sync)."""
    problems: list[str] = []
    for group in groups or list(GROUPS):
        want = pyproject_group(pyproject, group)
        have = pixi_group(pixi, group)
        table = ".".join(GROUPS[group])
        if have is None:
            problems.append(f"[{group}] pixi.toml has no [{table}] table")
            continue
        for name in sorted(want.keys() - have.keys()):
            problems.append(
                f"[{group}] {name}{want[name]!s}: in pyproject.toml, missing in [{table}]"
            )
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pyproject", type=Path, default=REPO / "pyproject.toml")
    parser.add_argument("--pixi", type=Path, default=REPO / "pixi.toml")
    parser.add_argument(
        "--group",
        action="append",
        choices=sorted(GROUPS),
        help="compare only this group (repeatable; default: all)",
    )
    args = parser.parse_args(argv)
    problems = compare(load_toml(args.pyproject), load_toml(args.pixi), args.group)
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
