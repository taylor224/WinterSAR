#!/usr/bin/env python
"""Regenerate the golden statistics of a benchmark site (plan §8 "regression" row,
ADR-0100..0102). Default: ``benchmarks/sites/S_synthetic.yaml`` →
``tests/regression/golden/S_synthetic/stats.json``.

Usage (from the repo root, through the project venv)::

    .venv/bin/python scripts/make_golden.py            # regenerate + list what changed
    .venv/bin/python scripts/make_golden.py --check    # compare only, exit 1 on a mismatch
    .venv/bin/python scripts/make_golden.py --site benchmarks/sites/X.yaml --out path.json

Only regenerate after a *reviewed*, intended change of the synthetic data, the fake engine or
the pipeline (rule 11.4) and say why in the PR body; ``tests/regression/test_golden_s_synthetic.py``
and ``scripts/check_golden.py`` (CI) compare against this file with the ADR-0102 tolerances.
The file holds statistics only — never timings, memory, hashes, timestamps or paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wintersar.bench.golden import main_make  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main_make(sys.argv[1:], repo=REPO))
