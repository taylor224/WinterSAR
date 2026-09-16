# wintersar — project rules (apply to every change)

Source of truth: `docs/plan/wintersar_implementation_plan.md` (Korean). Chapter 11 rules are
invariant and are repeated here. IDs `R-xx` (requirement), `SEL-xx` (precheck rule), `PERF-xx`
(performance item), `KB-xx` (diagnosis knowledge base) must appear in commit messages, tests
and docs that implement them.

## Environment

- Python 3.11 venv at `.venv` managed by **uv**. Run everything through it:
  `uv run pytest`, `uv run ruff check src tests`, `uv run ruff format src tests`,
  `uv run mypy` (config in `pyproject.toml`). Never `pip install` into the venv ad hoc;
  add dependencies to `pyproject.toml` and run `uv sync --extra dev`.
- No conda/pixi on this machine. External engines (ISCE2, SNAPHU, MintPy, tophu, dolphin,
  hyp3-sdk, sentineleof, sardem) are **not installed**; adapters must detect absence and
  return `ENV-001` findings, and tests for them use mocks/fakes (`@pytest.mark.engine`).
- Network tests: `@pytest.mark.network`; default CI runs `-m "not network"`.

## Invariant rules (plan §11)

1. Type hints everywhere; `ruff` clean; `mypy --strict` clean (engine adapters may relax
   untyped calls — see `[tool.mypy.overrides]`).
2. External engines only via subprocess or lazily imported permissive packages. **Never
   `import mintpy`** (GPL-3). Read MintPy HDF5 with `h5py`. GMTSAR/LiCSBAS: no code copying.
3. Never re-implement SNAPHU/MCF or the SBAS inversion core. Never guess URLs, CLI flags,
   API argument names or credit numbers: verify against installed package source
   (`.venv/lib/python3.11/site-packages/...`) or official docs (WebFetch), and leave a
   `# source: <url or path>` comment next to the usage. If it cannot be verified, add a row
   to `docs/open-questions.md` and code the safest fallback.
4. No algorithm change without a test. Fixtures ≤ a few MB; larger data via download scripts.
5. Commit messages reference the IDs. Each Phase's DoD checklist goes in the PR body.
6. **All user-facing strings live in `src/wintersar/i18n/{ko,en}.yaml` or
   `i18n/{ko,en}/<module>.yaml`** (deep-merged by `wintersar.i18n.load_catalog`). Code uses
   keys (`wintersar.i18n.t("select.sel01.fail", track_a=61, track_b=134)`). Diagnostic text
   is "cause → fix": keys `<ns>.<ID>.cause` and `<ns>.<ID>.fix`. Every key must exist in
   both languages (tests assert this).
7. Design decisions and "확인 필요" verification results go to `docs/adr/NNNN-title.md`
   (template `docs/adr/0000-template.md`). ADR number ranges are assigned per module in
   the module's task description to avoid collisions.
8. Never write performance numbers anywhere without a `bench_result.json`.
9. Open items go to `docs/open-questions.md` (item · owner · due) and do not block work.
10. Domain review checkpoints (Phase 1 rules table, Phase 4 sign conventions, Phase 6
    experiment design): do not change defaults before the researcher confirms.
11. Use `wintersar.util.masking.mask_text/mask_mapping` on anything that lands in logs,
    reports or JSON output (home paths, tokens).

## Shared contracts (do not fork them; extend in place)

- `wintersar.io.schemas`: `BurstRecord`, `Pair`, `StackCandidate`, `Finding`, `Resources`,
  `Artifact(s)`, `StageRecord`, `Plan`, `GroundTruthRecord`.
- `wintersar.engines.base`: `Engine` ABC (`detect_version`, `check_install`, `estimate`,
  `run(stage, inputs, params, log_dir) -> Artifacts`, `parse_log`), `@register_engine`,
  `get_engine(name)`, helper `python_module_version`, `executable_version`.
- `wintersar.pipeline.config`: `Config`/`load_config` (plan §4.4), `Config.stage_params(stage)`.
- `wintersar.util.hashing`: `hash_params`, `hash_path`, `combine_hashes`.
- `wintersar.util.output`: `emit_json(command, data, findings)`, `print_findings`,
  `findings_to_markdown`; `wintersar.util.clistate.state` for `--json/--lang`.
- Module CLIs: `wintersar/<module>/cli.py` exposes `register(app: typer.Typer) -> None`
  and is mounted by `wintersar/cli.py`. Every command must honour `state.json`.

## Layout

```
src/wintersar/{select,engines,pipeline,unwrap,diagnose,validate,research,bench,io,compute,i18n,util}
tests/{unit,integration,network,regression/golden,fixtures}
docs/{adr,concepts,kb,tutorials,research,plan}   benchmarks/sites   qgis_plugin   examples
```

Tests live in `tests/unit/<module>/test_*.py`; integration tests (fake engine, synthetic
data, no network) in `tests/integration/`.
