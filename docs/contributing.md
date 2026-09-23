# 기여 안내 (Contributing)

이 문서는 저장소 루트 `CLAUDE.md` 와 플랜 §11(불변 규칙)을 기여자 관점에서 정리한 것입니다. 충돌하면
`CLAUDE.md` 가 우선합니다.

## 환경

- Python 3.11, **uv** 가 관리하는 `.venv`. 모든 명령은 venv 를 통해 실행합니다:
  `uv run pytest`, `uv run ruff check src tests`, `uv run ruff format src tests`, `uv run mypy`.
  `pip install` 로 venv 를 직접 건드리지 않습니다 — 의존성은 `pyproject.toml` 에 추가하고 `uv sync --extra dev`.
- 여러 에이전트/사람이 동시에 작업할 때는 `uv run`/`uv sync` 대신 venv 실행 파일을 직접 씁니다
  (`.venv/bin/python -m pytest tests/unit/<module>`, `.venv/bin/ruff check --fix <paths>`,
  `.venv/bin/mypy src/wintersar/<module>`). `uv run` 은 락파일을 다시 잠가 서로 충돌합니다.
- 외부 엔진은 설치되어 있지 않다고 가정합니다. 어댑터는 부재를 감지해 `ENV-001` Finding 을 내고, 어댑터
  테스트는 mock/fake 를 씁니다(`@pytest.mark.engine`). 네트워크 테스트는 `@pytest.mark.network`
  (기본 CI 는 `-m "not network"`).

## 새 패키지를 추가하기 전에

1. 공식 레지스트리(PyPI/npm/…)에서 이름을 확인합니다 — 타이포스쿼팅 방지.
2. 널리 쓰이고 유지되는지 확인합니다(다운로드 수, 저장소 활동, 유지자).
3. 사용자 약 1만 명(월 다운로드 기준) 미만인 패키지는 핵심 의존성으로 쓰지 않습니다. 대안이 없으면
   `docs/open-questions.md` 에 적고 발주자 승인을 받습니다([ADR-0001](adr/0001-license-and-engine-boundaries.md)).

## 불변 규칙 (플랜 §11)

1. 타입 힌트 필수, `ruff` 클린, `mypy --strict` 클린(엔진 어댑터는 `pyproject.toml` 의 override 로 완화).
2. 외부 엔진은 subprocess 또는 지연 import 하는 관용 라이선스 패키지로만. **`import mintpy` 금지**(GPL-3) —
   MintPy HDF5 는 `h5py` 로 읽습니다. GMTSAR/LiCSBAS 코드 복사 금지.
3. SNAPHU/MCF, SBAS 역산 코어를 재구현하지 않습니다. URL·CLI 플래그·API 인자명·크레딧 수치는 **추측하지
   않습니다**: 설치된 패키지 소스(`.venv/lib/python3.11/site-packages/...`)나 공식 문서로 확인하고
   `# source: <url|path>` 주석을 남깁니다. 확인할 수 없으면 `docs/open-questions.md` 에 행을 추가(추가만,
   재작성 금지)하고 가장 안전한 폴백을 코딩합니다.
4. 테스트 없는 알고리즘 변경 금지. 픽스처는 수 MB 이하, 그 이상은 다운로드 스크립트.
5. 커밋 메시지에 관련 ID(`R-xx`, `SEL-xx`, `PERF-xx`, `KB-xx`)를 씁니다. PR 본문에 해당 Phase 의 DoD 체크리스트.
6. 사용자에게 보이는 문구는 전부 `src/wintersar/i18n/{ko,en}.yaml` 또는 `i18n/{ko,en}/<module>.yaml` 에
   둡니다(`wintersar.i18n.load_catalog` 가 깊은 병합). 코드는 키만 씁니다:
   `t("select.sel01.fail", track_a=61, track_b=134)`. 진단 문구는 "원인 → 조치": `<ns>.<ID>.cause`,
   `<ns>.<ID>.fix`. **모든 키는 두 언어에 다 있어야** 하며 테스트가 이를 검사합니다.
7. 설계 결정과 "확인 필요" 검증 결과는 `docs/adr/NNNN-title.md`(템플릿 `0000-template.md`, 번호 대역은
   [ADR 색인](adr/README.md)).
8. `bench_result.json` 없이는 성능 수치를 어디에도 쓰지 않습니다.
9. 미확정 사항은 `docs/open-questions.md`(항목·담당·기한)에 남기고 진행을 막지 않습니다.
10. 도메인 검수 체크포인트(Phase 1 규칙표, Phase 4 부호 규약, Phase 6 실험 설계)에서는 연구자 확인 전에
    기본값을 바꾸지 않습니다.
11. 로그·리포트·JSON 에 들어가는 문자열은 `wintersar.util.masking.mask_text/mask_mapping` 으로 홈 경로·토큰을
    마스킹합니다.

## 공유 계약 (fork 하지 말고 제자리에서 확장)

- `wintersar.io.schemas`: `BurstRecord`, `Pair`, `StackCandidate`, `Finding`, `Resources`, `Artifact(s)`,
  `StageRecord`, `Plan`, `GroundTruthRecord`.
- `wintersar.engines.base`: `Engine` ABC(`detect_version`, `check_install`, `estimate`,
  `run(stage, inputs, params, log_dir) -> Artifacts`, `parse_log`), `@register_engine`, `get_engine(name)`.
- `wintersar.pipeline.config`: `Config`/`load_config`, `Config.stage_params(stage)`.
- `wintersar.util.hashing`, `wintersar.util.output`(`emit_json(command, data, findings)`,
  `print_findings`, `findings_to_markdown`), `wintersar.util.clistate.state`(`--json/--lang`).
- 모듈 CLI: `wintersar/<module>/cli.py` 의 `register(app: typer.Typer) -> None` 을 `wintersar/cli.py` 가
  마운트합니다. 모든 명령은 `state.json` 을 존중해 JSON 봉투를 냅니다(QGIS 플러그인이 파싱).

## 병렬 작업 규칙

- 자기 작업이 소유한 파일만 건드립니다. **절대 수정 금지**: `pyproject.toml`, `tests/conftest.py`,
  `src/wintersar/cli.py`, `src/wintersar/io/schemas.py`, `src/wintersar/engines/base.py`,
  `src/wintersar/pipeline/config.py`, `src/wintersar/i18n/ko.yaml`, `src/wintersar/i18n/en.yaml`, `CLAUDE.md`.
  변경이 필요하면 최종 보고의 "needs_from_others" 에 적고 로컬 우회를 코딩합니다.
- 모듈 문구는 `i18n/ko/<module>.yaml`, `en/<module>.yaml`(같은 키). 모듈 픽스처는
  `tests/unit/<module>/conftest.py`; 공용 `make_burst` 는 `tests.conftest` 에서 import.
- `git commit` 은 통합자가 합니다.

## 레이아웃

```
src/wintersar/{select,engines,pipeline,unwrap,diagnose,validate,research,bench,io,compute,i18n,util}
tests/{unit,integration,network,regression/golden,fixtures}
docs/{adr,concepts,kb,tutorials,research,plan}   benchmarks/sites   qgis_plugin   examples
```

`docs/kb/` 는 `scripts/render_kb_docs.py` 가 `src/wintersar/diagnose/kb/*.yaml` 에서 생성하므로 직접
편집하지 않습니다. 문서 언어 정책(한국어 본문 + English summary)은 [ADR-0072](adr/0072-docs-structure-and-i18n.md).

## English summary

Use the uv-managed Python 3.11 venv (`uv sync --extra dev`; in parallel sessions call `.venv/bin/*`
directly). Invariants: type hints + ruff + mypy strict; engines only via subprocess or lazily imported
permissive packages (never `import mintpy`); never re-implement SNAPHU/MCF or the SBAS inversion; never
guess URLs, flags, argument names or credit numbers — verify and leave a `# source:` comment, otherwise
add an open-questions row and code the safest fallback; every user-facing string lives in the i18n YAML
in both languages as `<ns>.<ID>.cause`/`.fix`; decisions go to numbered ADRs; no performance numbers
without `bench_result.json`; mask home paths and tokens in anything logged. Touch only the files your
task owns; the shared contracts and the listed shared files are edited by the integrator.

## 골든 통계 (Golden statistics, ADR-0100)

`tests/regression/golden/S_synthetic/stats.json` 은 합성 S 사이트를 fake 엔진으로 돌린 결과의
기계 독립 통계(속도장 분포·마스크 비율·폐합 RMS·Finding 목록 등)를 고정한다. 다음 파일을 바꾸면
값이 달라질 수 있으므로 **같은 커밋에서** `.venv/bin/python scripts/make_golden.py` 로 재생성하고
PR 본문에 그 사실을 적는다:

- `src/wintersar/engines/fake.py` (`rng_for`, `synth_params`, `fake_dates`: 쌍별 RNG 시드)
- `src/wintersar/research/synth.py` (`make_interferogram`, `turbulent_atmosphere`, `deformation_field`)
- `src/wintersar/bench/golden.py` 의 통계 정의, `benchmarks/sites/S_synthetic.yaml`

검증은 `scripts/make_golden.py --check` (CI 의 regression 테스트가 같은 비교를 수행한다).
성능 수치(시간·메모리)는 골든에 넣지 않는다(규칙 11.8).
