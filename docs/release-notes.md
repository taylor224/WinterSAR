# 릴리스 노트 — wintersar v0.1.0-dev (`0.1.0.dev0`)

- 기준 트리: `main`, 2026-09-23. 버전 문자열은 `wintersar version` 이 출력하는 값(`src/wintersar/__init__.py`).
- 무엇을 "충족" 이라 부를 수 있는지는 [ADR-0111](adr/0111-release-notes-dod-reporting-policy.md) 의 기준을 따릅니다:
  **코드가 있다는 것과 DoD 를 충족했다는 것은 다릅니다.** 실데이터·외부 엔진·자격증명이 필요한 항목은 개발 환경에
  그것이 없으므로 "미충족(환경)" 으로 적고, 근거(테스트 파일·ADR·`bench_result.json`)가 있는 것만 "충족" 으로 적습니다.
  성능 수치는 `bench_result.json` 없이는 쓰지 않습니다(규칙 11.8) — 이 문서에는 수치가 없습니다.

## 한 줄 요약

플랜 §7 의 Phase 0–8 이 모두 **코드·테스트·ADR 수준**으로 트리에 있고, CI 는 lint·형식·`mypy --strict`·단위+통합
테스트(합성, 네트워크 없음)·합성 벤치마크·Docker 스모크를 돌립니다. 반면 **실제 위성 자료를 한 번도 처리하지
않았습니다**: 외부 엔진(ISCE2·SNAPHU·tophu·MintPy·dolphin·hyp3-sdk·sentineleof·sardem)이 개발 환경에 없고, 국내
대조군 데이터도 없습니다. 따라서 실사이트 DoD 는 전부 미충족이며, v0.1.0 정식 릴리스 전에 그 항목들을 채워야 합니다
([로드맵](roadmap.md)).

## 이번 버전에 들어 있는 것

| 모듈 | 있는 것 | 진입점 | 근거 |
|---|---|---|---|
| `select` | ASF burst 검색(BURST 우선, SLC fallback), 스택 그룹핑·커버리지·네트워크·참조일 추천, 수직 기선(ASF stack API → 궤도 fallback), 규칙 `SEL-01…13`, 자동 looks, 레이오버·셰도우 마스크, 리포트 md/html/json | `wintersar search`, `wintersar precheck` | `tests/unit/select*`, ADR-0010…0019 |
| `engines` | 어댑터 `hyp3`, `mintpy`, `snaphu`, `tophu`, `spurt`, `isce2_topsstack`, `dolphin`, `fake`; 크레딧 표 `hyp3_costs.yaml`; MintPy 템플릿 생성(PERF-09); run_files 병렬 실행기; burst2safe; 보조 데이터 캐시 | `wintersar check-install`, config `engine.*` | `tests/unit/engines_*`(`@pytest.mark.engine`, mock), ADR-0020…0029 |
| `pipeline` | 해시 캐시 DAG(`search … validate` 11단계), `plan`/`run`/`cache ls|gc`, `--set/--until/--from/--force`, 실패 시 `diagnose` 자동 첨부, 작업 디렉터리 규약 | `wintersar plan`, `run`, `cache` | `tests/integration/test_pipeline_fake.py`, `tests/integration/test_incremental.py`, ADR-0030…0034, 0080…0082 |
| `unwrap` | 스케줄러(간섭도 병렬 우선, 메모리 예산 내 자동 타일, 단차 검출·병합), `unwrap plan/run` | `wintersar unwrap plan|run` | `tests/unit/unwrap`, `tests/integration/test_unwrap_scheduler.py`, ADR-0045…0048 |
| `diagnose` | 로그 파서(isce2·snaphu·mintpy·hyp3·asf·generic), KB YAML 20항목, `KB-UNKNOWN` 미분류, 리소스 추정 모델, `docs/kb/` 렌더러 | `wintersar diagnose`, `--list-kb` | `tests/fixtures/logs/manifest.yaml`(26 픽스처), ADR-0035…0037 |
| `validate` | 대조군 CSV 임포트·LOS 투영·RMSE/bias 리포트, 기준점 추천, 폐합 대시보드, 파라미터 스윕(Pareto) | `wintersar validate`, `refpoint`, `closure`, `sweep` | `tests/unit/validate`, ADR-0040…0044 |
| `research` | 합성 생성기(간섭도·SLC 스택·타일), 대표위상 5종, 스티칭 2종, 지표, YAML 실험 5종, 결과표 | `wintersar research synth|repr-phase|stitch|experiment|experiments` | `docs/research/results/*.md`, ADR-0060…0064 |
| `io` / `compute` / `bench` | Zarr v3 스택 저장소, COG 내보내기, ISCE2/HyP3/MintPy 포맷 리더, CuPy 선택 백엔드, 벤치마크 프로토콜(`bench_result.json`, `--compare`) | `wintersar bench` | `tests/unit/{io,compute,bench}`, ADR-0050…0054, 0095…0097(GPU 경로는 CUDA 머신 미실행, #72), 0100…0102 |
| QGIS 플러그인 | CLI `--json` 위의 얇은 클라이언트(패널 6종, Processing 공급자, ZIP 빌드), 환경 탐색 | `qgis_plugin/build_zip.py` | `tests/unit/qgis`, ADR-0070/0071 |
| 문서 | 개념 9편, 튜토리얼 3편(ko + English summary), KB 생성 페이지, ADR 색인, 이 릴리스 노트, 로드맵 | `mkdocs.yml` | `tests/unit/qgis/test_docs.py`, ADR-0072, 0110–0112 |

CLI 도움말은 `--lang` 을 먼저 읽어 두 언어로 나오고 오류는 항상 Finding/봉투로 끝납니다(ADR-0090/0091). CLI 명령 전체 목록은 `wintersar --help` (현재: `version`, `check-install`, `init`, `search`, `precheck`, `plan`,
`run`, `diagnose`, `validate`, `refpoint`, `sweep`, `closure`, `bench`, `cache {ls,gc}`, `unwrap {plan,run}`,
`research {repr-phase,stitch,synth,experiment,experiments}`). `--json`/`--lang` 은 전역 옵션입니다.

## Phase 별 DoD 상태

상태 값: **충족** = 근거가 트리에 있고 이 환경에서 재현됨 · **부분** = 일부 항목만 · **미충족(환경)** = 필요한
엔진/데이터/자격증명이 없어 확인 불가 · **미충족** = 아직 하지 않음.

### Phase 0 — 스캐폴딩 (PERF-12): 부분

| DoD 항목 | 상태 | 근거 / 이유 |
|---|---|---|
| CI 녹색 | 부분 | `.github/workflows/ci.yml`(ruff·mypy·pytest·합성 bench·core Docker 스모크)과 `nightly.yml`(골든 통계 회귀 게이트, ADR-0100/0101/0102). 로컬에서는 ruff/mypy/pytest 만 실행했고 원격 CI 결과는 이 환경에서 확인하지 않음 |
| Docker 빌드 | 미충족(환경) | core 이미지 `Dockerfile` 은 CI job 이 빌드; 엔진 이미지 `Dockerfile.engines` 는 CI 에서 빌드하지 않음(ADR-0106, #71). 이 환경에서는 둘 다 빌드하지 않음 |
| `check-install` 이 미설치 엔진을 Finding 으로 보고 | 충족 | 실행 확인: 미설치 엔진마다 `ENV-001`, 종료 코드 0, `--strict` 로 1 |
| 합성 파이프라인 end-to-end 테스트 | 충족 | `tests/integration/test_pipeline_fake.py`, fake 엔진 |
| `pixi.toml` + 락파일 | 부분 | `pixi.toml` 은 있음(ADR-0105/0107, 문서·레지스트리로만 검증); `pixi.lock` 은 pixi 가 없는 머신이라 생성하지 못함(#70, #67 의 구현 단계) |

### Phase 1 — select (R-01~04, PERF-01, PERF-13): 부분

| DoD 항목 | 상태 | 근거 / 이유 |
|---|---|---|
| 실제 AOI 3곳에서 후보 스택·FAIL/WARN | 미충족(환경) | 네트워크 테스트(`-m network`)는 CI 에서 제외, 실 AOI 실행 기록 없음 |
| 규칙마다 단위테스트(양성·음성) | 충족 | `tests/unit/select/test_rules*.py` |
| looks 고정값 테스트 | 충족 | `tests/unit/select` (ADR-0015) |
| 마스크 상승/하강 골든 파일 + 수동 검수 1회 | 부분 | 골든 `tests/regression/golden/geometry` 있음, 연구자 수동 검수 미실시(ADR-0019 제안 상태) |
| 규칙표 연구자 검수(규칙 11.10) | 미충족 | open-questions #24, #25 대기 — 기본값 변경 금지 상태 |

### Phase 2 — engines + pipeline + unwrap 스케줄러: 부분

| DoD 항목 | 상태 | 근거 / 이유 |
|---|---|---|
| S 사이트를 HyP3 경로로 설치 후 속도 지도 | 미충족(환경) | `hyp3-sdk` 미설치(의존성 정책 예외 대기 #10), Earthdata 자격증명·크레딧 없음. 어댑터는 SDK 소스로 검증(ADR-0020) |
| 같은 사이트를 ISCE2 경로로 재현·차이 통계 | 미충족(환경) | ISCE2 미설치; 플래그·run_files 는 상류 소스로만 검증(ADR-0026/0027, #46) |
| 파라미터 1개 변경 시 하류만 재실행 | 충족 | `tests/integration/test_pipeline_fake.py`, 튜토리얼에서 재현(`--set unwrap.coherence_threshold=0.5` → 4단계만 실행) |
| 스케줄러가 예산 초과 입력을 자동 타일링·OOM 없이 완료 | 부분 | 타일 결정은 `tests/integration/test_unwrap_scheduler.py` 로 확인; 실제 SNAPHU/tophu 실행은 미설치(`ENV-001`) |
| bench S 기준선 수치 기록 | 부분 | 합성 S(`S_synthetic.yaml`)만 CI 아티팩트로 생성; 기준선 파일·러너 정책 미정(#58), 실데이터 S 없음 |

### Phase 3 — diagnose (R-02, R-14): 충족(픽스처 기준)

| DoD 항목 | 상태 | 근거 / 이유 |
|---|---|---|
| KB 시드 12개 이상 | 충족 | 20항목(`wintersar diagnose --list-kb`), 전부 `pattern_verified: true` + 출처 |
| 알려진 실패 케이스 10개(로그 픽스처)에서 정확한 KB 매칭 | 충족 | `tests/fixtures/logs/manifest.yaml` 26 픽스처의 기대 ID 검사. 단 픽스처는 상류 소스 문자열로 만든 **합성 발췌**이지 현장 로그가 아님(#29, #31) |
| 매칭 실패 시 미분류 Finding + 로그 발췌 | 충족 | 실행 확인: fake 실패 주입 → `KB-UNKNOWN` + 마스킹 발췌 |
| `run` 실패 시 자동 첨부, `docs/kb/` 렌더링 | 충족 | ADR-0033, `scripts/render_kb_docs.py --check` |

### Phase 4 — validate (R-09, R-10, R-11): 부분

| DoD 항목 | 상태 | 근거 / 이유 |
|---|---|---|
| 국내 사이트 1곳 대조군 RMSE/bias 리포트 | 미충족(환경) | 국토지리정보원 자료 접근 방식 미확정(#6, #42), 실데이터 없음. 리포트 생성 자체는 합성 시계열 + 픽스처 CSV 로 재현됨 |
| LOS 투영 부호 테스트(상승/하강) 통과 | 충족 | `tests/unit/validate`(ADR-0040); 실데이터 부호 검수는 #39 대기 |
| 스윕 8조합이 캐시 덕분에 전량 재실행의 절반 이하 | 미충족 | 측정 기록(`bench_result.json`) 없음. 캐시 적중은 통합 테스트·`sweep.md` 의 "캐시 적중" 열로만 확인 |

### Phase 5 — 성능 (PERF-05/06/10/11): 미충족(환경)

| DoD 항목 | 상태 | 근거 / 이유 |
|---|---|---|
| PERF 항목별 before/after 측정치 | 미충족(환경) | 실데이터·엔진 없음. dolphin 어댑터(ADR-0028/0029), CuPy 백엔드(ADR-0053), 증분 모드(PERF-11)는 코드·테스트만 |
| 채택/기각 ADR | 부분 | 설계·사실 확인 ADR 은 있으나 측정 기반 채택/기각 결정은 없음(#53 A/B 대기) |

### Phase 6 — research (R-07, R-15): 부분

| DoD 항목 | 상태 | 근거 / 이유 |
|---|---|---|
| 합성 3종 결과표 | 충족 | `docs/research/results/S_synth_{repr_phase,steep_ramp,strong_atmosphere,stitching}.md` |
| 실데이터 2 사이트 | 미충족(환경) | 실 SLC 스택 없음 |
| 나은 조합의 스케줄러 옵션 승격 | 미충족 | 연구자 검수 전 기본값 변경 금지(ADR-0060, 규칙 11.10); R-15 A/B 는 dolphin·MintPy 필요(#53) |

### Phase 7 — QGIS 플러그인 (R-12): 부분

| DoD 항목 | 상태 | 근거 / 이유 |
|---|---|---|
| 패널 6종·Processing Provider·ZIP·설치 문서 | 충족 | `qgis_plugin/`, `tests/unit/qgis`, `qgis_plugin/README.md` |
| 플러그인만으로 검색 → 검증 리포트 완료 | 미충족(환경) | QGIS 미설치, HyP3 경로 미실행 |
| QGIS LTR 2종에서 동작 확인 | 미충족(환경) | #60 (3.44 LTR / 4.x 수동 검수 필요) |

### Phase 8 — 릴리스 v0.1: 부분

| DoD 항목 | 상태 | 근거 / 이유 |
|---|---|---|
| 튜토리얼 3종(ko/en) | 충족(정책 범위) | 본문 한국어 + `## English summary`; 완전한 영어판은 아님(ADR-0072/0110) |
| KB 정리 | 충족 | 생성 페이지 `docs/kb/index.md` + [KB 개요](kb/overview.md) |
| 라이선스 표 확정(§9) | 부분 | ADR-0001 표: COMPASS·isce3·LiCSBAS·tophu 확인 완료; SNAPHU C 코어·snaphu-py·dolphin·spurt·sardem 원문 확인은 #4 미착수 |
| 릴리스 노트 | 충족 | 이 문서 |
| 벤치마크 표 링크 | 부분 | 합성 S 결과는 CI 아티팩트(`bench_result.json`)로만 존재, 커밋된 기준선 없음(#58). 링크할 표가 아직 없어 `benchmarks/README.md` 만 가리킴 |

## 알려진 제한

1. **실데이터 미검증.** Sentinel-1 자료를 실제로 내려받아 처리한 기록이 없습니다. 어댑터의 API·플래그는 설치된
   패키지 소스와 공식 문서로 검증했지만(`# source:` 주석), 실행 경로는 mock/fake 로만 시험했습니다.
2. **엔진 비번들.** ISCE2·SNAPHU·tophu·MintPy·dolphin·spurt 는 사용자가 설치합니다([설치 안내](install.md):
   uv core · pixi engines · Docker engines 세 경로, `pixi.lock` 은 아직 없음).
   `hyp3-sdk`·`sentineleof`·`sardem`·`snaphu`·`burst2safe` 는 의존성 정책 임계 미달로 optional extra 이며
   발주자 승인 전에는 개발 환경에도 설치하지 않습니다(open-questions #10).
3. **성능 미측정.** PERF-xx 항목의 효과는 가설입니다. 합성 S 벤치마크만 CI 에서 돌며, 회귀 게이트는 기준선 정책(#58)이
   정해질 때까지 측정만 합니다.
4. **영어 문서는 요약뿐.** 본문은 한국어이고 각 페이지 끝에 English summary 가 있습니다(ADR-0072).
5. **데이터 소스는 ASF 만.** `data.source: cdse` 는 설정 스키마에 예약만 되어 있고 검색은 구현되지 않았습니다.
   GACOS 는 MintPy 템플릿 매핑만 있고 자동 다운로드·캐시는 없습니다. KOMPSAT-5 리더는 없습니다([로드맵](roadmap.md)).
6. **spurt 는 기본값이 아님.** 어댑터는 있으나 conda-forge 패키지 부재·`ortools` 정책 예외(#22)로 실행 미검증,
   스케줄러는 스택 전용 백엔드로만 취급합니다.
7. **QGIS 미실행.** 플러그인의 Qt 부분은 순수 파이썬 단위 테스트로만 검증했습니다(#60).
8. **HyP3 실패 사유.** `hyp3_sdk.Job` 에는 실패 사유 필드가 없어 런타임 실패는 `KB-UNKNOWN` 으로 떨어질 수 있습니다(#33).
9. **문서 빌드 미실행.** `mkdocs`(docs extra)가 개발 환경에 설치되어 있지 않아 `mkdocs build` 는 돌리지 않았고,
   nav 항목의 존재·링크·명령 유효성만 테스트로 검사합니다(ADR-0112).

## 라이선스

프로젝트는 Apache-2.0 이고 GPL 구성요소(MintPy 등)는 subprocess 경계 뒤에 둡니다. 구성요소별 라이선스 표와
확인 출처는 [ADR-0001](adr/0001-license-and-engine-boundaries.md) 에 있으며, 아직 원문 확인이 남은 행은
open-questions #4 입니다. ISCE2·isce3 의 EAR99 고지는 배포물에 포함해야 합니다. Copernicus Sentinel 자료 ©
ESA — 산출물에 출처를 표기합니다.

## 의존성 정책 예외 (승인 대기)

전역 정책은 월 다운로드 약 1만 미만 패키지를 핵심 의존성으로 두지 않습니다. 다음 항목은 발주자 승인이 있어야
개발 환경에 설치합니다 — 승인 전까지는 mock 테스트만 돕니다.

| 항목 | 내용 | 행 |
|---|---|---|
| `hyp3-sdk`, `sentineleof`, `sardem`, `snaphu`, `burst2safe` | HyP3 경로·궤도·DEM·언래핑 실행 테스트에 필요 | open-questions #10 |
| `spurt` (`pip`, `ortools` 의존) | conda-forge 패키지 없음 | open-questions #22 |
| `dolphin`(pip), `mintpy`(GPL-3, subprocess 전용) | R-15 A/B(`S_synth_seq_estimator_ab`) 실행에 필요 | open-questions #53 |
| `pixi.lock` 생성·커밋 | conda 전용 엔진 환경의 재현(`pixi.toml` 은 있음) | open-questions #67, #70 |

## 검증 방법 (이 릴리스가 통과한 것)

```bash
uv sync --extra dev
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run pytest -m "not network and not engine_real and not gpu"
uv run wintersar --json bench --site benchmarks/sites/S_synthetic.yaml --out bench_result.json
```

`engine` 마커(mock 어댑터 계약 테스트)는 CI 에 포함되고, `engine_real`(실제 엔진 필요)·`network`·`gpu` 만 제외됩니다.
nightly 워크플로는 합성 S 사이트의 골든 통계(`tests/regression/golden`, ADR-0100/0102)를 게이트로, 기준선 대비
before/after 표는 보고로만 돌립니다(ADR-0101; 기준선 파일 정책은 #58, 플랫폼 간 허용 오차 실측은 #68).

## 다음 릴리스에서 채워야 할 것

[로드맵](roadmap.md) — 백로그(KOMPSAT-5, GACOS, spurt 기본화, CDSE)와 담당자별 미확정 사항.

## English summary

wintersar 0.1.0.dev0 ships every plan phase (0–8) as code, tests and ADRs: burst-level selection with
`SEL-01..13`, adapters for HyP3, MintPy, SNAPHU/tophu/spurt, ISCE2 topsStack and dolphin, a hash-cached DAG,
the unwrap scheduler, a 20-entry diagnosis KB with `KB-UNKNOWN` fallback, ground-truth validation, reference
point, closure and sweep tools, the research module, Zarr/COG IO, the bench protocol, a thin QGIS plugin and
this documentation set. CI runs ruff, `mypy --strict`, unit + integration tests on synthetic data, the synthetic
S benchmark and a Docker smoke test. What is **not** met, honestly: no real Sentinel-1 data has been processed
(no engines, credentials or credits in the development environment), so every real-site DoD in Phases 1, 2, 4,
5, 6 and 7 is unmet; no performance number exists (rule 11.8); the QGIS plugin has not run inside QGIS; the
licence table still has unverified rows (open-questions #4); several packages wait for a dependency-policy
exception (#10, #22, #53, #67); English readers get summaries, not full pages (ADR-0072). Claims in this page
follow the reporting policy of ADR-0111 (code existing is not DoD met).
