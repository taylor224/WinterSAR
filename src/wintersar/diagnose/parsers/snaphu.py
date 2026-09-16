"""SNAPHU log parser spec.

Marker strings are from the SNAPHU C sources vendored by snaphu-py
(https://github.com/gmgunter/snaphu, src/snaphu.c: "Starting first-round tile-mode
unwrapping", "Unwrapping tile at row %ld, column %ld", "Starting second-round single-tile
unwrapping") and the snaphu-py package name (``snaphu.unwrap``).
"""

from __future__ import annotations

import re

from wintersar.diagnose.parsers.generic import ABORT_LINE, ParserSpec

SPEC = ParserSpec(
    name="snaphu",
    filename_hints=("snaphu", "unwrap"),
    content_markers=(
        re.compile(r"\bsnaphu\b", re.IGNORECASE),
        re.compile(r"Starting (?:first-round tile-mode|second-round single-tile) unwrapping"),
        re.compile(r"Unwrapping tile at row \d+, column \d+"),
        re.compile(r"\b(?:TILECOSTTHRESH|MINREGIONSIZE|NTILEROW|NTILECOL)\b"),
        re.compile(r"snaphu\.conf|snaphu\.unwrap\("),
    ),
    event_patterns=(("abort", ABORT_LINE),),
)
