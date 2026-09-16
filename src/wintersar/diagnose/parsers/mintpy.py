"""MintPy log parser spec (MintPy runs as a subprocess; only its stdout is read, rule 11.2).

Markers: script names printed by smallbaselineApp.py (``reference_point.py``,
``generate_mask.py``, ``tropo_pyaps3.py``, ``timeseries2velocity.py``), template keys
``mintpy.*`` (src/mintpy/defaults/smallbaselineApp.cfg) and the ``mintpy`` package path.
"""

from __future__ import annotations

import re

from wintersar.diagnose.parsers.generic import ParserSpec

SPEC = ParserSpec(
    name="mintpy",
    filename_hints=("mintpy", "smallbaselineapp", "timeseries", "tropo"),
    content_markers=(
        re.compile(r"\bmintpy\b", re.IGNORECASE),
        re.compile(r"smallbaselineApp\.py|reference_point\.py|generate_mask\.py|tropo_pyaps3?\.py"),
        re.compile(r"\bmintpy\.(?:reference|networkInversion|troposphericDelay|network)\.\w+"),
        re.compile(r"\bPyAPS\b|\bPYAPS\b"),
    ),
    event_patterns=(("error", re.compile(r"^ERROR: ", re.MULTILINE)),),
)
