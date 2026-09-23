#!/usr/bin/env python
"""CI entry point of the golden layer (plan §8 "regression" row, ADR-0101): run the
synthetic S site through the pipeline and compare its statistics with
``tests/regression/golden/S_synthetic/stats.json`` using the ADR-0102 tolerances.

Usage (from the repo root)::

    .venv/bin/python scripts/check_golden.py                       # human-readable table
    .venv/bin/python scripts/check_golden.py --json > check.json   # envelope for scripts
    .venv/bin/python scripts/check_golden.py --markdown summary.md --lang en

Exit codes follow the *action* convention: 0 = statistics match, 1 = mismatch, golden
missing or the run failed (the job must go red), 2 = bad input. Regenerate deliberately with
``scripts/make_golden.py`` after an intended change.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wintersar.bench.golden import main_check  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main_check(sys.argv[1:], repo=REPO))
