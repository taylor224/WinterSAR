# ADR-0082: 실 엔진이 증분 모드(PERF-06)에 참여하기 위한 계약 — hyp3 · isce2_topsstack · 언래핑 백엔드

- 상태(Status): 채택(계약) / 어댑터 구현은 미착수(문서화만)
- 날짜(Date): 2026-09-23
- 관련 ID: PERF-06, PERF-11, PERF-07, PERF-03
- 검증 출처(Sources):
  - `src/wintersar/pipeline/incremental.py`(계약), `src/wintersar/pipeline/executor.py::_execute_locked`/`_record_pairs`
  - `src/wintersar/engines/hyp3.py`: `JobState`/`PairState`(`<data_dir>/jobs.json`, "PERF-06 resumable runs"),
    `run()`의 "resume (PERF-06)" 블록 — `product_dir`가 있고 `find_product_dir()`가 찾으면 `n_cached += 1; continue`,
    진행 중/성공 job id가 있으면 재제출하지 않고 폴링·다운로드(`continue  # poll / download the existing job`),
    실행 전에 이미 SUCCEEDED였던 job은 `client.refresh(done_ids)`로 회수
  - `src/wintersar/engines/isce2_topsstack.py`: `STAGE_WINDOWS = {"coregister": (None, "merge_reference_secondary_slc"),
    "interferogram": ("generate_burst_igram", "filter_coherence")}`, `_run_window()` →
    `runfiles.select_steps(...)` + `runfiles.pending_steps(...)`(`runfiles_state.json`의 `completed`),
    coregister의 업데이트 모드 분기(`coreg_secondarys/`에 없는 새 날짜가 있으면 run_files 재생성, ISCE2-008)
  - ADR-0029(topsStack 업데이트 모드에서 참조 기하·burst overlap 재사용, 새 날짜·쌍에 대해서만 run file 생성),
    ADR-0026/0027(run_files 단계·병렬 안전성), ADR-0020(HyP3 어댑터)
  - `src/wintersar/unwrap/api.py::run_unwrap`(언래핑 실행기 — 백엔드 공통, 이미 참여)

## 맥락 (Context)

ADR-0080의 실행기는 증분 단계에 `_pairs_dir`/`_pairs_done`을 넘기고 산출물 meta의 `pairs_reused`/
`pairs_computed`를 회계한다. fake 엔진과 `run_unwrap`(snaphu/tophu 공통 실행기)은 계약을 구현했다. 실 간섭도
엔진(hyp3, isce2_topsstack)은 같은 키를 받지만 아직 읽지 않는다 — 요청했는데 보고가 없으면 실행기가
`PIPELINE-016`(INFO, "전체를 다시 계산했습니다")을 낸다. 각 엔진이 실제로 무엇을 해야 참여가 되는지,
이미 갖고 있는 재개(resume) 기능과 어떻게 맞물리는지 적어 둔다. **이 ADR은 구현하지 않는다**(Phase 5, ISCE2
설치 환경 필요: open-questions #46, #74).

## 계약 (요약, `wintersar.pipeline.incremental`)

| 항목 | 내용 |
|---|---|
| 대상 | **쌍 단계**(`PAIR_STAGES` = interferogram · multilook · unwrap)만. fetch/coregister는 해시 규칙(ADR-0080 §1)만 따르고 이 키를 받지 않으며 `PIPELINE-016`도 나지 않는다(작업이 날짜 단위이고 엔진 작업 폴더 안에 있음) |
| 입력 | `params["_pairs_dir"]`(노드 디렉터리의 `pairs/`), `params["_pairs_done"] = {key: {hash, path, file_hash, meta}}` |
| 판단 | 쌍 `key`의 식별자 `pair_identity(params_hash, input_hash)`가 `_pairs_done[key].hash`와 같고 파일이 온전(`file_hash` 일치; `file_hash`가 없으면 검증 불가 → 재사용 안 함)하면 재사용. 온전해 보여도 읽을 수 없으면 `PairCache.discard(key)` 후 그 쌍만 다시 계산(단계 실패 아님) |
| 출력 | `PairCache.store()`로 새 쌍 등록 + `save()`; 산출물 meta에 `pairs`(스택 순서), `pairs_reused`, `pairs_computed` |
| 무시 | 키를 읽지 않아도 동작은 종전과 같다. 실행기는 `supported: false`로 기록하고 `PIPELINE-016`(INFO) |
| 해시 | 노드 해시는 이미 날짜 집합·`stack` 입력을 빼고 계산되므로(ADR-0080) 어댑터가 해시를 신경 쓸 필요는 없다 |

## 엔진별 — 해야 할 일

### hyp3 (interferogram, unw 동시 산출)

- **이미 하는 것**(검증): `jobs.json`의 `PairState.product_dir`가 가리키는 검증된 산출물 디렉터리가 있으면 그 쌍은
  제출·다운로드를 건너뛰고(`n_cached`), 진행 중 job은 재제출 없이 폴링한다. 즉 *다운로드된 쌍 건너뛰기*는
  어댑터 자체 상태로 이미 성립한다 — 단, `jobs.json`은 `data_dir`(엔진 작업 폴더) 기준이라 DAG 노드
  디렉터리가 바뀌면 잃는다. ADR-0080의 해시 규칙으로 노드 디렉터리가 날짜 추가에 안정되므로 이 문제는
  사라진다.
- **참여를 위해 남은 것**: (1) `_pairs_done`의 `path`(쌍 산출물 디렉터리)를 `PairState.product_dir` 대신/함께
  인정해 `jobs.json`이 없어도 재사용, (2) 쌍 식별자 = `hash(HyP3 job 옵션(looks, apply_water_mask, phase_filter …
  ADR-0020의 제출 파라미터), 쌍 키 + granule 목록)` — 같은 쌍이라도 옵션이 바뀌면 새 job, (3) meta에
  `pairs`/`pairs_reused`/`pairs_computed`(= 이번에 제출·다운로드한 쌍) 보고, (4) `PairCache.store(key, ident,
  product_dir)`는 디렉터리 해시(`hash_path(fast=True)`는 트리를 지원)로 온전성 검사.
- 크레딧(SEL-13/HYP3-005)은 새로 제출하는 쌍에만 들므로 `plan`의 `new` 수가 곧 크레딧 견적의 쌍 수다.

### isce2_topsstack (coregister · interferogram)

- **이미 하는 것**(검증): 작업 폴더는 노드 해시와 무관하게 영속(`topsstack_workdir`, ADR-0029). coregister는
  `coreg_secondarys/`에 없는 새 날짜가 있을 때만 `stackSentinel.py`를 업데이트 모드로 다시 돌려 run_files를
  재생성하고, 그 run_files는 새 날짜(및 NESD용 최근 날짜)와 새 쌍에 대해서만 job을 담는다(ADR-0029 확인 사실
  4·5). `_run_window()`는 `runfiles_state.json`의 `completed`에 없는 단계만 실행한다.
- **핵심 관찰**: topsStack의 증분성은 *run_files 재생성*에서 온다. 재생성된 `run_NN_generate_burst_igram` …
  `filter_coherence`의 job 줄은 새 쌍만 포함하므로, interferogram 단계는 `_pairs_done`을 몰라도 새 쌍만
  계산한다. 다만 `merged/interferograms/<pair>/`에 옛 쌍이 남아 있어야 하며(ADR-0027 "절대 삭제 금지" 목록),
  `merged_pairs(workdir)`가 옛 쌍+새 쌍 전부를 산출물 manifest에 넣는다.
- **참여를 위해 남은 것**: (1) `igrams_manifest.json`의 `pairs`를 meta `pairs`로, run_files에서 실제로 실행된
  job의 쌍을 `pairs_computed`, 나머지를 `pairs_reused`로 보고(run file 줄의 쌍 키는 `PAIR_RE`로 추출 가능),
  (2) `_pairs_done`과 `merged/interferograms/<pair>/` 존재를 대조해 **run file 부분집합**을 만들지 판단 — 기본은
  topsStack이 만든 run_files를 신뢰하고, `_pairs_done`에 없는데 run file에도 없는 쌍(예: 사용자가 merged를
  지움)이 있으면 ISCE2 Finding으로 경고. run file 줄을 잘라 실행하는 것(부분집합)은 ADR-0027의 단계 간
  의존(예: `merge_burst_igram`이 `generate_burst_igram` 전체를 전제) 때문에 **단계 안에서 쌍 단위로만** 허용하고
  단계 순서는 바꾸지 않는다, (3) 쌍 식별자 = `hash(looks, filter_strength, esd, 쌍 키)`; 이 값들이 바뀌면 노드
  해시 자체가 바뀌므로(파라미터) 실질적으로는 쌍 키만으로 충분하다.
- **검증 필요**: 업데이트 모드 run_files의 실제 job 목록(ADR-0029 #46)과 `runfiles_state.json`이 재생성 후
  초기화되는 흐름에서 옛 쌍의 merge/filter가 다시 돌지 않는지 — ISCE2 설치 환경(S 사이트)에서 확인
  (open-questions #74).

### 언래핑 백엔드 (snaphu · tophu · spurt)

- `run_unwrap`이 백엔드 공통으로 쌍 단위 재사용을 구현했으므로 백엔드 어댑터는 할 일이 없다. 단, 2-D 백엔드만
  해당한다: spurt(3-D 시공간)는 스택 전체가 한 문제라 쌍 단위 재사용이 정의되지 않으며 `resolve_plan`이
  이미 2-D 계획에서 제외한다(ADR-0025).
- SNAPHU assemble-only(타일 디렉터리 재사용, PERF-03)와는 직교: 쌍 식별자가 같으면 언래핑 자체를 건너뛰고,
  다르면(타일 파라미터 변경) 종전대로 `tile_dirs`를 통해 조립만 다시 한다.

### dolphin (timeseries)

- 시계열 단계는 증분 단계가 아니다(재역산). dolphin의 순차(미니스택) 모드는 플랜 PERF-06 "옵션"으로 남기며
  ADR-0064(R-15 A/B) 결과에 따라 결정한다.

## 결과 (Consequences)

- hyp3/isce2가 계약을 구현하기 전까지 실 경로의 증분 실행은 `PIPELINE-016`을 내고 전량 재계산하되, 언래핑은
  이미 쌍 단위로 재사용된다(가장 비싼 로컬 단계). isce2는 어댑터 자체 업데이트 모드 덕분에 사실상 새 쌍만
  계산하지만 회계(`reused/computed`)가 없어 `plan`에는 드러나지 않는다.
- 구현 순서 제안: (1) isce2 회계 보고(값싸고 검증 가능), (2) hyp3 `_pairs_done` 인정, (3) 실 데이터 측정
  → `bench_result.json` → 플랜 PERF-06 표의 before/after.
