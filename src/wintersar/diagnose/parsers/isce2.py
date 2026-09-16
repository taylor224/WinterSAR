"""ISCE2 / topsStack log parser spec.

Markers: ISCE2 logger names (``isce.topsinsar.*`` in components/isceobj/TopsProc/*.py,
``isce.insar.VerifyDEM`` in runVerifyDEM.py), topsStack script names
(contrib/stack/topsStack: stackSentinel.py, Stack.py, run_files, SentinelWrapper.py) and
the ``isceobj`` package path that appears in tracebacks.
"""

from __future__ import annotations

import re

from wintersar.diagnose.parsers.generic import ParserSpec

SPEC = ParserSpec(
    name="isce2",
    filename_hints=("isce", "topsstack", "topsapp", "stacksentinel", "run_", "sentinelwrapper"),
    content_markers=(
        re.compile(r"\bisce\.(?:topsinsar|insar|stripmapinsar)\b"),
        re.compile(r"\bisceobj\b|\bisce\b"),
        re.compile(r"stackSentinel\.py|topsApp\.py|SentinelWrapper\.py|run_files|topsStack"),
        re.compile(r"\bIW[123]\b.*\bburst\b|\bburst\b.*\bIW[123]\b", re.IGNORECASE),
        re.compile(r"ESD|azimuth misregistration|geo2rdr|rdr2geo|topo\.py", re.IGNORECASE),
    ),
    event_patterns=(
        ("exception", re.compile(r"^\s*raise Exception\(", re.MULTILINE)),
        ("error", re.compile(r"^\*{5,}\s*$\n^ERROR:", re.MULTILINE)),
    ),
)
