"""asf_search log parser spec.

Markers: the ``asf_search`` package path / logger name (``asf_search/__init__.py``:
``ASF_LOGGER = logging.getLogger(__name__)``), its exception class names
(``asf_search/exceptions.py``) and CMR wording from ``search/search_generator.py``.
"""

from __future__ import annotations

import re

from wintersar.diagnose.parsers.generic import ParserSpec

SPEC = ParserSpec(
    name="asf",
    filename_hints=("asf", "search", "cmr"),
    content_markers=(
        re.compile(r"\basf_search\b"),
        re.compile(
            r"\bASF(?:Search(?:4xx|5xx)?|Authentication|Download|Baseline|WKT)Error\b|\bCMR(?:ConceptID|Incomplete)?Error\b"
        ),
        re.compile(r"\bCMR\b|cmr\.earthdata\.nasa\.gov|urs\.earthdata\.nasa\.gov"),
    ),
    event_patterns=(("error", re.compile(r"\bASF\w*Error\b|\bCMR\w*Error\b")),),
)
