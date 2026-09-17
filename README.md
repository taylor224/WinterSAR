# wintersar

Open-source Sentinel-1 InSAR (SBAS) toolkit that wraps proven engines (ASF HyP3, ISCE2 topsStack,
SNAPHU / tophu, MintPy, dolphin) and adds what the ecosystem lacks:

- **select** — burst-level search (ASF), automatic stack grouping, precheck rules `SEL-01..13`
  (same track? common bursts? polarization? baselines? looks? layover?) with cause → fix messages (ko/en)
- **pipeline** — hash-cached DAG: change one parameter, re-run only downstream stages (`PERF-03`)
- **unwrap** — scheduler: per-interferogram parallelism first, automatic tiling within a memory budget (`PERF-04`)
- **diagnose** — engine log parsers + knowledge base `KB-xx` (SNAPHU, ISCE2, MintPy, HyP3, auth, env)
- **validate** — reference-point recommendation, loop-closure dashboard, levelling/GNSS comparison (LOS projection), parameter sweeps
- **research** — synthetic interferogram generator, representative-phase & tile-stitching experiments (`R-07`, `R-15`)
- **bench** — before/after measurement protocol (`PERF-xx`); no performance claims without `bench_result.json`
- **QGIS plugin** — thin client over the CLI `--json` output

Design principles: reuse (adapters, no forks), engines behind subprocess boundaries (GPL isolation),
every stage a cacheable DAG node, messages in Korean and English, domain mistakes prevented in code.

The full implementation plan (Korean) is in [`docs/plan/wintersar_implementation_plan.md`](docs/plan/wintersar_implementation_plan.md).

## Install (development)

```bash
uv sync --extra dev          # Python 3.11 venv in .venv
uv run wintersar --help
uv run wintersar check-install
uv run pytest -m "not network"
```

External engines are optional and detected at runtime (see `docs/adr/0001-license-and-engine-boundaries.md`).

## Quick start

```bash
wintersar init config.yaml           # example config (plan §4.4)
wintersar search   --config config.yaml
wintersar precheck work/select/candidates.json
wintersar plan     --config config.yaml
wintersar run      --config config.yaml
wintersar diagnose work/                 # logs live in work/<stage>/<hash>/logs (ADR-0032)
wintersar validate --ts work/ts/timeseries.h5 --leveling data/leveling.csv
```

`--json` (stable envelope: `{"ok", "command", "data", "findings"}`) and `--lang ko|en` are **global**
options and go *before* the sub-command — `wintersar --json run --config config.yaml`. Placing them
after it (`wintersar run --json`) is a usage error (`No such option`, exit 2).

## Licence

Apache-2.0. Copernicus Sentinel data © ESA. SNAPHU, MintPy, ISCE2 and other engines keep their own licences and are never bundled.
