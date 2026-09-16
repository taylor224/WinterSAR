# ADR-0044: 파라미터 스윕 설계 — 그리드 정의, 지표, Pareto, DAG 캐시 재사용

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-08 / R-11 / PERF-03 / 플랜 §5.6 `sweep.py`, Phase 4 DoD
- 검증 출처(Sources):
  - `src/wintersar/pipeline/api.py::run(cfg, param_overrides=...) -> RunResult` (records·artifacts·cache_hit)
  - `src/wintersar/pipeline/config.py::Config` (`extra="forbid"`, `validate` alias, `stage_params`)
  - `src/wintersar/pipeline/stages.py` (단계별 config 섹션 매핑: unwrap→`unwrap`, timeseries→`timeseries`, interferogram/multilook→`engine`)
  - `src/wintersar/research/synth.py::PHASE_PER_M_LOS` (= −4π/λ, λ = 0.05546576 m)
  - ADR-0042 (폐합 지표), ADR-0031/0034 (해시·파라미터 계약)

## 맥락

플랜 §5.6: "그리드(코히어런스 임계 × looks × 필터 alpha × 언래퍼 × 대기 보정) 실행 → 지표(폐합 RMS, 시간적 코히어런스,
잔차 RMS, 대조군 RMSE, 실행 시간) 표 + Pareto 플롯. DAG 캐시로 변경 단계만 재실행." Phase 4 DoD: "스윕 8개 조합이
캐시 덕분에 전량 재실행 시간의 절반 이하로 완료(측정 기록)."

## 선택지

1. 단계 파라미터 오버라이드(`param_overrides`)로 그리드를 표현 — 엔진 파라미터(n_dates 등)에는 맞지만 `config.yaml` 키
   (`unwrap.coherence_threshold`)와 이름 체계가 다르다.
2. **`config.yaml`의 점 표기 키**로 그리드를 표현하고, 각 점을 `Config` 덤프에 deep-merge한 뒤 pydantic으로 재검증.
   DAG 해시는 `Config.stage_params`에서 나오므로 바뀐 섹션의 단계만 재실행된다.
3. 외부 실험 관리 도구(optuna 등) 도입 — 의존성 정책·범위 초과.

## 결정

선택지 2.

- 스윕 YAML: `grid: {dotted.key: [values]}`, 선택적 `objectives`, `max_points`(기본 256, 실수로 폭발하는 그리드 방지).
  `make_grid`는 데카르트 곱을 섹션별 중첩 dict로 돌려준다(첫 키가 가장 느리게 변함).
- `apply_point(cfg, point)`: `model_dump(by_alias=True)` → deep-merge → `Config.model_validate` (알 수 없는 키·잘못된 값은
  즉시 오류), `config_path` 유지. 원본 `cfg`는 변경하지 않는다.
- 러너 주입: `run_sweep(cfg, grid, gt, runner=None)`; 기본 러너는 `wintersar.pipeline.api.run`(지연 import). 테스트는
  `metrics` 매핑을 돌려주는 가짜 러너를 주입한다. 한 점의 실패는 `ok=False` + `VAL-016` 경고로 기록하고 스윕은 계속된다.
- 지표(`METRIC_KEYS`), RunResult에서 계산:

| 지표 | 정의 | 출처 산출물 |
|---|---|---|
| `closure_rms` (rad) | 픽셀별 폐합 RMS의 RMS (unw 모드, ADR-0042) | `igrams` + `unw` |
| `temporal_coherence` | `closure_coherence` 평균 — MintPy `temporalCoherence`의 **대용**(네트워크 역산 잔차가 없을 때) | `igrams` |
| `residual_rms` (m) | `unw_pair·(−λ/4π) − (disp_j − disp_i)`의 RMS(간섭도별 공간 중앙값 제거 후) — 역산 잔차의 근사 | `unw` + `timeseries` |
| `gt_rmse` (m) | `validate.metrics.compare` 전체 RMSE | `timeseries` + 대조군 CSV |
| `wall_time_s` | 매니페스트 `resources.wall_time_s` 합(캐시 단계는 ≈0) | `records` |
| `cache_hits` | `extra.cache_hit == True`인 단계 수 | `records` |

- Pareto: 목표별 방향(`min` 기본, `temporal_coherence`·`cache_hits`는 `max`)에 대해 비지배 행 집합. 목표 지표가 없는
  행은 제외. 표(Markdown/HTML)에 `*`로 표시, matplotlib이 있으면 첫 두 목표의 산점도(`sweep_pareto.png`).
- 결과 파일: `sweep.json`(행·지표·Pareto 인덱스), `sweep.md`, `sweep_pareto.png`(선택).

## Phase 4 DoD 측정 방식

규칙 11.8(성능 수치는 `bench_result.json` 없이는 적지 않음)에 따라 초 단위 수치는 기록하지 않고, 테스트
`test_real_fake_pipeline_sweep_reuses_cache`가 **재실행되는 단계 집합**을 고정한다: 2×2 그리드(`unwrap.coherence_threshold` ×
`timeseries.troposphere`)에서 첫 점만 엔진 8단계를 실행하고, 이후 점은 `fetch..multilook`을 캐시에서 받으며 troposphere만
바뀐 점은 `timeseries` 이후만 재실행한다(`cache_hits ≥ 4`). 동일 그리드 재실행은 전량 캐시(`cache_hits == 8`).
벤치(`wintersar bench`)가 실사이트 8개 조합의 wall time을 `bench_result.json`에 기록하면 이 ADR에 수치를 추가한다.

## 결과

- `temporal_coherence`·`residual_rms`는 대용 지표다. `io.formats`가 MintPy `temporalCoherence.h5`·`timeseriesResidual.h5`를
  읽게 되면 실제 값으로 교체하고 대용은 fake 경로에만 남긴다(open-questions 신규 행).
- 스윕은 `validate` 단계를 파이프라인 안에서 돌리지 않고(대조군은 `run_sweep(gt=...)`로 별도 전달) `timeseries` 산출물에서
  직접 `gt_rmse`를 계산한다 — 파이프라인 `validate` 단계는 `ValidateCfg`가 있을 때만 실행되는 선택 단계.
