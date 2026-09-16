# Benchmarks (`wintersar bench`, plan §5.9 / §6.3)

Site definitions live in `benchmarks/sites/*.yaml` (schema: `wintersar.bench.sites.Site`).

| Site | Size | Data | Purpose |
|---|---|---|---|
| `S_synthetic.yaml` | S | synthetic (fake engine, no network) | CI regression gate on every PR |
| `S.yaml` | S | 2 bursts × ~30 dates, Korean site with levelling/GNSS control (template) | tool-chain before/after + ground-truth RMSE |
| `M.yaml` | M | 1 frame × ~60 dates (template) | throughput / memory |
| `L.yaml` | L | 2 frames × ~120 dates (template) | disk peak, unwrap scheduler |

Templates contain `<PLACEHOLDER>` values and are refused until filled in (`BENCH-004`);
real-data sites need `--allow-network` (`BENCH-002`).

## Protocol (plan §6.3)

1. Every stage is measured for wall time, peak RSS, disk peak (work directory), network
   bytes and CPU time (`wintersar.bench.profiler.StageProfiler`); the **median of 3 repeats**
   is reported, each repeat on a fresh work directory (no cache reuse).
2. Quality metrics: closure-phase RMS (wrapped triplets), unwrap error fraction (synthetic
   sites, against the truth phase) and ground-truth RMSE (sites with a control CSV).
3. Output is `bench_result.json` (`schema_version`, `site`, `git_sha`, `machine`, `stages`,
   `total`, `metrics`, `runs`, optional `compare`). Numbers are only ever quoted from such a
   file (project rule 11.8); this README links to tables, it does not carry numbers.
4. `--compare baseline.json` renders a before/after table with percentage deltas; a stage
   whose median wall time is more than 15 % slower than the baseline is a regression
   (`BENCH-001`; exit code 1 with `--fail-on-regression`).

## Commands

```bash
# CI gate (no network); writes bench_result.json
wintersar bench --site benchmarks/sites/S_synthetic.yaml --out bench_result.json

# compare with a stored baseline and fail the job on a >15 % slowdown
wintersar bench --site benchmarks/sites/S_synthetic.yaml \
  --compare benchmarks/baselines/S_synthetic.json --fail-on-regression --markdown bench.md

# quick smoke run that calls the fake engine directly (no DAG)
wintersar bench --site benchmarks/sites/S_synthetic.yaml --runner fake --repeats 1

# machine-readable envelope for the QGIS plugin / scripts
wintersar --json bench --site benchmarks/sites/S_synthetic.yaml
```

Store baselines under `benchmarks/baselines/<site>.json` (created with `--out`); update
them deliberately when a slowdown is intended, and say so in the PR body.

## Chunk-preset measurement (PERF-08)

`wintersar.io.zarr_store.benchmark_chunk_presets(stack, out_dir)` writes a stack once per
preset (`timeseries` = `(all, 512, 512)`, `display` = `(1, 2048, 2048)`) and times random
pixel-series reads versus single-layer reads. Its dictionary is meant to be embedded in a
`bench_result.json` (`extra`), not quoted from a terminal.
