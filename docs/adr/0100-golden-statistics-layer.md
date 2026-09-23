# ADR-0100: 합성 S 사이트 골든 통계 층 (`tests/regression/golden/S_synthetic/stats.json`)

- 상태(Status): 채택
- 날짜(Date): 2026-09-23
- 관련 ID: 플랜 §8 테스트 전략 표 "regression" 행(골든 산출물: 속도 지도 통계, Finding 목록), §6.3 항목 5,
  §5.9, 규칙 11.4 / 11.8 / 11.11, R-13
- 검증 출처(Sources):
  - 구현 `src/wintersar/bench/golden.py`, `scripts/make_golden.py`, `scripts/check_golden.py`,
    `tests/regression/test_golden_s_synthetic.py`, `tests/unit/bench/test_golden.py`
  - 파이프라인 실행 경로 `src/wintersar/bench/runner.py::pipeline_runner`(`build_config` + `site.param_overrides`,
    `seed` = 반복 번호) — 골든은 같은 구성의 반복 0 이다
  - 지표 정의 `src/wintersar/bench/runner.py::compute_metrics` (`closure_rms`, `unwrap_error_fraction`, ADR-0054)
  - fake engine 산출물 `src/wintersar/engines/fake.py` (`igrams.npz`, `unw.npz`, `timeseries.npz`, `velocity.npy`)
  - 재현성 확인: 이 저장소에서 `scripts/make_golden.py` 를 두 번 실행해 `diff` 로 비교 — 바이트 단위 동일
    (`tests/regression/test_golden_s_synthetic.py::test_golden_run_is_reproducible`,
    `tests/unit/bench/test_golden.py::test_golden_stats_are_bit_identical_across_runs` 가 고정)
  - 기존 골든 층 `tests/regression/golden/geometry/` (R-04 마스크, `make_golden.py` + `ridge_masks.npz`)

## 맥락 (Context)

플랜 §8 은 "regression" 층을 골든 산출물(속도 지도 통계, Finding 목록)로 정의했지만 지금까지 select 의 기하
마스크 골든만 있었다(리뷰 지적: 속도 지도 골든 층 부재). 합성 S 사이트(`benchmarks/sites/S_synthetic.yaml`)는
매 PR 에서 fake engine 으로 전체 DAG 를 돌리므로, 합성 데이터 생성기·fake engine·파이프라인·bench 지표 정의의
*결과* 변화를 잡는 층을 여기에 얹는 것이 가장 싸다. 결정할 것: 무엇을 저장할지(배열 vs 통계), 어디에, 어떤 크기로,
어떻게 재생성하는지, 그리고 부동소수 재현성.

## 선택지 (Options)

1. 산출물 배열 자체(`velocity.npy` 등)를 골든으로 커밋 — 정확하지만 96×96×14 스택으로도 수 MB, 플랫폼 간 ulp
   차이로 바이트 비교가 깨지고 diff 가 읽히지 않는다.
2. 배열의 해시 — 작지만 ulp 차이 하나로 실패하고 무엇이 변했는지 알 수 없다.
3. **통계 JSON** — 속도 지도 min/max/mean/std/백분위, 마스크 비율, bench 지표, 단계별 Finding rule_id, 쌍·날짜 수,
   산출물 배열 shape/dtype. 작고(수 kB) 사람이 읽을 수 있으며 허용 오차 비교(ADR-0102)가 가능하다.

## 결정 (Decision)

선택지 3.

- **파일**: `tests/regression/golden/<site.name>/stats.json` (S_synthetic 은 약 7 kB, 상한 50 kB —
  `write_golden` 이 초과 시 거부: 그 크기면 통계가 아니라 배열이 들어간 것). 정렬된 키·2칸 들여쓰기·끝 개행의
  정규 텍스트(`dump_golden`)로만 쓰며 테스트가 "스크립트로 쓴 파일인가"를 텍스트 동일성으로 확인한다.
- **내용**(`golden_stats`): `schema_version`, `generator`, `seed`, `site`(name/size/runner/stages/metrics/config/
  param_overrides — 사이트 YAML 이 바뀌면 골든이 무효), `run`(ok/failed_stage/정렬된 rule_id), `stages{status,
  engine, 정렬된 rule_id}`, `artifacts{kind, arrays{shape,dtype} | json keys}`, `n_pairs`, `n_dates`,
  `igrams{pairs, dates, coherence 통계, wrapped_abs_mean, mask_fraction}`, `unwrap{masked_fraction, conncomp
  라벨 수·비영 비율, unw 통계}`, `timeseries{n_dates, 마지막 epoch 변위 통계}`, `velocity{shape + 통계}`,
  `metrics{closure_rms, unwrap_error_fraction}`. 통계는 유한값만 float64 로 누적해 계산한다(float32 입력의
  플랫폼별 합산 차이를 1e-9 수준으로 억제).
- **제외**: wall time·CPU·RSS·디스크·네트워크(규칙 11.8), git SHA·타임스탬프·버전(파일이 코드+사이트의
  순수 함수여야 두 번 실행의 diff 가 비어야 한다), 경로·호스트명·노드 해시·sha256(규칙 11.11, 머신 식별 금지).
  `FORBIDDEN_KEYS` 를 테스트가 검사한다.
- **실행**: `run_golden_pipeline` = `bench.sites.build_config` + `site.param_overrides` + `interferogram.seed`
  (사이트가 고정하지 않으면 `GOLDEN_SEED = 0`, 즉 bench 반복 0) + `until = site.stages[-1]`; 보조 데이터 캐시는
  작업 디렉터리 안(`~/.cache` 를 건드리지 않는다).
- **재현성·반올림**: 같은 머신에서 생성기를 두 번 실행한 결과가 바이트 단위로 같았으므로 **반올림하지 않는다**.
  플랫폼 간(macOS arm64 에서 생성, Linux x86_64 CI 에서 비교) 마지막 자리 차이는 ADR-0102 의 허용 오차가
  흡수한다. 만약 향후 같은 머신에서 diff 가 생기면(예: 스레드 수에 따라 달라지는 합산) 그때 유효 자릿수 반올림을
  도입하고 이 ADR 을 대체한다.
- **재생성 절차**: `.venv/bin/python scripts/make_golden.py --check` 로 차이를 본 뒤 `scripts/make_golden.py`
  로 다시 쓴다(이전 골든과 달라진 값 목록을 출력). 검토된 의도적 변경 후에만(규칙 11.4), PR 본문에 이유를 적는다.
  실패 메시지(`GOLDEN-001/003`, `golden.check.regenerate`)가 이 절차를 안내한다.
- **스키마 2 (2026-09-23, 리뷰 2차)**: `array_stats` 는 `n`(배열 크기)·`nan_fraction`·min/max/mean/std/백분위만 담고
  `n_finite` 는 담지 않는다 — `n · (1 − nan_fraction)` 과 같은 정보인데 정수 정확 비교가 `*_fraction` 허용 오차
  (ADR-0102)를 무효화했다. 골든은 `scripts/make_golden.py` 로 재생성했고 커밋본과의 diff 는 `n_finite` 4줄 삭제와
  `schema_version` 1→2 뿐이었다(다른 값은 바이트 동일).
- **스크립트 오류 경로**: `make_golden.py` / `check_golden.py` 는 CLI 와 같은 계약(ADR-0091)을 따른다 — `--site` 파일
  없음 `CLI-006`, 읽기·검증 실패(`yaml.YAMLError` 포함 — `ValueError` 의 하위가 아니라 별도로 잡는다) `GOLDEN-006`
  → 종료 2; 골든 파일이 JSON 이 아니거나 객체가 아니면 `GOLDEN-005`(check: FAIL 종료 1, make: WARN 후 덮어씀);
  쓰기 실패 `GOLDEN-007`, 그 밖의 예외 `CLI-001` → 종료 1. `--json` 이면 봉투가 stdout 의 유일한 출력이고(nightly
  가 stdout 을 `golden_check.json` 으로 받는다) 아니면 stderr 에 원인 → 조치; `--markdown` 은 오류 경로에서도 쓴다.
  `--help` 문구와 불일치 목록은 `i18n/{ko,en}/golden.yaml` 의 `golden.args.*` / `golden.mismatch.*` 이며 `--lang` 은
  argparse 가 도움말을 그리기 전에 argv 에서 먼저 읽는다(ADR-0090 과 같은 문제). **예외**: argparse 자체의 문구
  (`usage:`, `show this help message and exit`, 알 수 없는 옵션·`--lang fr` 거부 메시지)는 표준 라이브러리 영어
  그대로다 — 개발자용 스크립트라 gettext 를 붙이지 않는다.
- geometry 골든(`ridge_masks.npz`, 배열 골든)은 그대로 둔다: 마스크는 불리언이라 정확 비교가 맞고 이미 작다.

## 결과 (Consequences)

- 합성 생성기(`research.synth`), fake engine, 파이프라인 단계 순서·Finding, bench 지표 정의 중 하나라도 결과를
  바꾸면 `tests/regression` 과 nightly 의 `check_golden.py` 가 실패한다. 의도한 변경은 골든 재생성 커밋을 동반한다.
- 골든은 S_synthetic 한 사이트만 다룬다. 다른 합성 사이트를 추가하면 `make_golden.py --site` 로 같은 형식의
  파일을 `tests/regression/golden/<name>/stats.json` 에 만들고 회귀 테스트를 매개변수화한다.
- `stats.json` 의 스키마가 바뀌면(`schema_version` 증가) 키 누락/추가가 불일치로 잡히므로 반드시 재생성한다.
- 실데이터 S 사이트 골든(플랜 §8 "S 사이트")은 네트워크·엔진 설치가 필요해 이 층에 포함하지 않았다
  (`docs/open-questions.md` 참조).
