# wintersar

Open-source Sentinel-1 InSAR (SBAS) toolkit that wraps proven engines (ASF HyP3, ISCE2 topsStack,
SNAPHU / tophu, MintPy, dolphin) behind subprocess adapters and adds what the ecosystem lacks:

- **select** — burst-level search (ASF), automatic stack grouping, precheck rules `SEL-01..13`
  (same track? common bursts? polarization? baselines? looks? layover?) with cause → fix messages (ko/en)
- **pipeline** — hash-cached DAG: change one parameter, re-run only downstream stages (`PERF-03`); add one
  acquisition with `run --incremental`, recompute only the pairs that touch it (`PERF-06`)
- **unwrap** — scheduler: per-interferogram parallelism first, automatic tiling within a memory budget (`PERF-04`)
- **diagnose** — engine log parsers + knowledge base `KB-xx` (SNAPHU, ISCE2, MintPy, HyP3, auth, env)
- **validate** — reference-point recommendation, loop-closure dashboard, levelling/GNSS comparison (LOS projection), parameter sweeps
- **research** — synthetic interferogram generator, representative-phase & tile-stitching experiments (`R-07`, `R-15`)
- **bench** — before/after measurement protocol (`PERF-xx`); no performance claims without `bench_result.json`
- **QGIS plugin** — thin client over the CLI `--json` output

Design principles: reuse (adapters, no forks), engines behind subprocess boundaries (GPL isolation),
every stage a cacheable DAG node, messages in Korean and English, domain mistakes prevented in code.
Status of this version (which plan DoDs are met, which are not): [`docs/release-notes.md`](docs/release-notes.md).

## Install

Three paths — uv (core, HyP3 remote path), pixi (local engines), Docker (engines image) — are described in
[`docs/install.md`](docs/install.md). External engines are never bundled; `check-install` reports what is
missing (`ENV-001`) and how to install it.

```bash
uv sync --extra dev          # Python 3.11 venv in .venv
uv run wintersar --help
uv run wintersar check-install
uv run pytest -m "not network and not engine_real and not gpu"
```

## Quick start (synthetic, no network or engines)

```bash
wintersar init config.yaml           # then set engine.interferogram: fake, timeseries.engine: fake, drop validate:
printf '{"type":"FeatureCollection","features":[]}' > aoi.geojson
wintersar plan --config config.yaml
wintersar run  --config config.yaml
wintersar run  --config config.yaml --set unwrap.coherence_threshold=0.5   # only unwrap and downstream re-run
wintersar run  --config config.yaml --incremental                           # seeds the per-pair cache (PERF-06)
wintersar run  --config config.yaml --incremental --set interferogram.n_dates=7   # one more date: only its pairs are computed
wintersar diagnose work/             # logs live in work/<stage>/<hash>/logs (ADR-0032)
```

Real-data flow: `search` → `precheck` → `plan` → `run` → `diagnose` → `validate` → `refpoint` → `sweep`.
Commands: `version`, `check-install`, `init`, `search`, `precheck`, `plan`, `run`, `cache {ls,gc}`, `diagnose`,
`validate`, `refpoint`, `closure`, `sweep`, `unwrap {plan,run}`, `research {synth,repr-phase,stitch,experiment,experiments}`,
`bench` — see `wintersar --help`.

`--json` (stable envelope: `{"ok", "command", "data", "findings"}`) and `--lang ko|en` are **global**
options and go *before* the sub-command — `wintersar --json run --config config.yaml`. Placing them
after it (`wintersar run --json`) is a usage error (`No such option`, exit 2).

## Documentation

- [Getting started](docs/index.md) · [Install](docs/install.md) · [Concepts](docs/concepts/index.md)
- Tutorials: [HyP3 quick start](docs/tutorials/hyp3-quickstart.md) · [Local ISCE2](docs/tutorials/isce2-local.md) · [Validate & tune](docs/tutorials/validate-tune.md)
- [Diagnosis KB](docs/kb/index.md) ([overview](docs/kb/overview.md)) · [ADR index](docs/adr/README.md) · [Open questions](docs/open-questions.md)
- [Release notes](docs/release-notes.md) · [Roadmap](docs/roadmap.md) · [Contributing](docs/contributing.md)
- Full implementation plan (Korean): [`docs/plan/wintersar_implementation_plan.md`](docs/plan/wintersar_implementation_plan.md)

Pages are Korean with an English summary at the end (ADR-0072).

## Licence

Apache-2.0. Copernicus Sentinel data © ESA. SNAPHU, MintPy, ISCE2 and other engines keep their own licences
and are never bundled (component table: [`docs/adr/0001-license-and-engine-boundaries.md`](docs/adr/0001-license-and-engine-boundaries.md)).
