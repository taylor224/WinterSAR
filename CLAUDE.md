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
- Markers: `@pytest.mark.engine` = adapter contract test driven by stubs/mocks — it runs
  everywhere **including CI** (that is how an engine API change breaks the adapter first);
  `@pytest.mark.engine_real` = needs the engine actually installed and is the only engine
  marker CI deselects. `@pytest.mark.network` needs internet/credentials.
- CI runs `-m "not network and not engine_real and not gpu"`.

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
  `load_config` raises `FileNotFoundError` (missing file) or `ValueError` (`ConfigError` for YAML
  syntax, pydantic `ValidationError` for schema) — CLI loaders catch exactly those two
  (CLI-004 / CLI-005), never `yaml.YAMLError` or bare `Exception`.
- `wintersar.util.hashing`: `hash_params`, `hash_path`, `combine_hashes`.
- `wintersar.util.output`: `emit_json(command, data, findings)`, `print_findings`,
  `findings_to_markdown`; `wintersar.util.clistate.state` for `--json/--lang`.
- Module CLIs: `wintersar/<module>/cli.py` exposes `register(app: typer.Typer) -> None`
  and is mounted by `wintersar/cli.py`. Every command must honour `state.json`.
- Envelope vs exit code: `ok` is "the subject has no FAIL finding", the exit code is "what
  happened to the command" — `0` ran, `1` the requested action failed, `2` bad input/usage.
  *Report* commands (`check-install`, `search`, `diagnose`, `validate`) therefore exit 0
  with `ok:false` and let the caller read the findings (`check-install --strict`,
  `precheck --no-fail` opt in/out); *action* commands (`plan`, `run`, `precheck`, `bench`)
  exit 1 when not `ok`. An envelope is always emitted in `--json` mode, including for an
  unexpected exception (`cli.main` → `CLI-001`).

## Layout

```
src/wintersar/{select,engines,pipeline,unwrap,diagnose,validate,research,bench,io,compute,i18n,util}
tests/{unit,integration,network,regression/golden,fixtures}
docs/{adr,concepts,kb,tutorials,research,plan}   benchmarks/sites   qgis_plugin   examples
```

Tests live in `tests/unit/<module>/test_*.py`; integration tests (fake engine, synthetic
data, no network) in `tests/integration/`.

## Working in parallel (multiple agents on this repo at once)

- Use the venv binaries directly, never `uv run`/`uv sync` (they re-lock the project and
  race with other agents): `.venv/bin/python -m pytest tests/unit/<module>`,
  `.venv/bin/ruff check --fix <paths>`, `.venv/bin/ruff format <paths>`,
  `.venv/bin/mypy src/wintersar/<module>`.
- Only touch files you own (your task lists them). **Never edit** `pyproject.toml`,
  `tests/conftest.py`, `src/wintersar/cli.py`, `src/wintersar/io/schemas.py`,
  `src/wintersar/engines/base.py`, `src/wintersar/pipeline/config.py`,
  `src/wintersar/i18n/ko.yaml`, `src/wintersar/i18n/en.yaml`, `CLAUDE.md`. If you need a
  change there (new dependency, new shared model, new config field), report it in your
  final output under "needs_from_others" and code a local workaround.
- Module strings go in `src/wintersar/i18n/ko/<module>.yaml` and `en/<module>.yaml`
  (same keys in both files).
- Module fixtures go in `tests/unit/<module>/conftest.py`; the shared `make_burst` factory
  is importable from `tests.conftest`.
- Do not `git commit`; the integrator commits.
