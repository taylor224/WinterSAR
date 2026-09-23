# ADR-0080: 증분 업데이트 모드 — 쌍 단위 서브캐시와 노드 해시 규칙 (PERF-06)

- 상태(Status): 채택
- 날짜(Date): 2026-09-23
- 관련 ID: PERF-06, PERF-03, PERF-11, R-05, R-11
- 검증 출처(Sources):
  - 플랜 §6.1 PERF-06 "정합 SLC·간섭도 캐시 재사용, 새 쌍만 생성·언래핑, 시계열만 재역산", §5.3, §7 Phase 5
  - `src/wintersar/pipeline/incremental.py`(해시 규칙·`PairCache`), `src/wintersar/pipeline/dag.py`(`Dag.resolve`/`fresh`/`partial_cache`),
    `src/wintersar/pipeline/executor.py`(`_pairs_dir`/`_pairs_done` 전달·회계), `src/wintersar/engines/fake.py`,
    `src/wintersar/unwrap/api.py::run_unwrap`
  - ADR-0031(노드 해시), ADR-0032(작업 디렉터리·manifest), ADR-0034(단계 파라미터 계약)
  - 테스트: `tests/integration/test_incremental.py`, `tests/unit/pipeline/test_incremental.py`,
    `tests/unit/unwrap/test_api_incremental.py` (읽을 수 없는 매니페스트·읽히지 않는 쌍 파일·쌍 단계 한정은
    `test_unreadable_pair_manifest_is_discarded_but_named`, `test_plan_and_run_report_an_unreadable_pair_manifest`,
    `test_an_unloadable_cached_pair_is_recomputed_not_a_stage_failure`,
    `test_unloadable_cached_pair_is_unwrapped_again_not_fatal`, `test_only_pair_stages_take_part_in_the_sub_cache_contract`)

## 맥락 (Context)

모니터링 운영에서는 12일마다 새 영상이 한 장씩 추가된다. 현재 단계 산출물은 스택 전체가 한 파일
(`igrams.npz`, `unw.npz`)이고 노드 해시(ADR-0031)는 `n_dates`와 상류 산출물의 내용 해시를 포함하므로, 날짜를
하나 더하면 interferogram 이하 전 단계가 새 노드가 되어 모든 쌍을 다시 만들고 다시 언래핑한다. PERF-06의
목표는 "새 날짜가 필요로 하는 것만" — 새 날짜에 닿는 쌍의 간섭도 생성·언래핑 — 다시 하고 시계열만 다시
역산하는 것이다. 동시에 PERF-03의 의미(파라미터 하나 변경 → 변경 단계와 하류만 재실행)는 그대로여야 한다.

결정할 것은 세 가지다. (1) 날짜가 추가돼도 노드 해시가 움직이지 않게 하는 규칙, (2) 노드 디렉터리 안에서
쌍 단위 결과를 어떻게 보관·재사용하는가, (3) `plan`/`run`이 무엇을 보여 주는가.

## 선택지 (Options)

1. **노드 해시 규칙**
   1. 그대로 두고 엔진 내부에서만 재사용(topsStack 업데이트 모드처럼 엔진 작업 폴더를 영속화, ADR-0029).
      DAG는 매번 새 노드 → 상류 캐시가 무의미하고 언래핑은 전량 재실행.
   2. **쌍/날짜 집합을 파라미터가 아닌 데이터로 취급**: 증분 단계에서는 `n_dates`류 키와 `stack` 입력을 해시에서
      빼고, 증분 단계가 만든 입력은 내용 해시 대신 *생산 노드의 해시*로 식별한다. 신선도(fresh)는 별도 검사.
   3. 쌍마다 DAG 노드를 만든다(쌍 단위 노드 그래프). 수백~수천 노드, 리소스 예약·manifest·CLI 표 전부 재설계.
2. **쌍 단위 보관** — `out/` 안에 두기 / **노드 디렉터리의 `pairs/` 형제 디렉터리** / 별도 전역 콘텐츠 주소 저장소.
3. **쌍 식별자** — 파일 mtime / **`hash(쌍에 영향을 주는 파라미터, 쌍별 입력 식별자)`**(fake 간섭도: 쌍 키 자체,
   언래핑: 쌍 배열의 내용 해시).

## 결정 (Decision)

### 1. 해시 규칙(`incremental.py`, `dag.py`)

- 증분 단계 `INCREMENTAL_STAGES = (fetch, coregister, interferogram, multilook, unwrap)`. 시계열 이후는
  내용 해시 그대로(전량 재역산이 맞다). 이 중 **쌍 단계** `PAIR_STAGES = (interferogram, multilook, unwrap)`만
  §2의 쌍 단위 서브캐시 계약에 참여한다. fetch/coregister는 해시 규칙(날짜 집합은 데이터)만 따른다: 작업이 쌍이
  아니라 날짜 단위이고 엔진 작업 폴더(topsStack `coreg_secondarys/`, ADR-0082) 안에서 이미 재사용되므로 어떤
  엔진(fake 포함)도 `pairs/`를 만들지 않는다. 따라서 실행기는 이 두 단계에 `_pairs_dir`/`_pairs_done`을 넘기지
  않고 `extra["incremental"]`도 `PIPELINE-016`도 기록하지 않는다(`Node.pair_stage`).
- `node_hash(stage, params, inputs, engine, version)`에서 증분 단계는 **`DATA_KEYS = {n_dates, dates, pairs}`**
  (최상위 키)를 params에서 빼고, **`DATA_INPUTS = {stack}`**(precheck 산출물)을 inputs에서 뺀다.
- 증분 단계의 입력이 증분 단계에서 왔으면 그 입력의 식별자는 **생산 노드의 `node_hash`**다(`Dag._identity`).
  비증분 생산자(search/precheck)의 입력은 종전대로 내용 해시. 따라서 fetch→…→unwrap 사슬은 상류가 실행되기
  *전에* 해시가 정해진다(`plan`이 사슬 전체의 상태를 보여 줄 수 있음; `provisional`은 timeseries 이하만).
- **신선도(`Dag.fresh`)**: 해시가 같은 ok manifest가 있어도, 증분 노드는 (a) manifest에 기록된 입력 *내용* 해시
  (`record.inputs`)가 지금 사용 가능한 입력의 내용 해시와 같고 (b) `DATA_KEYS` 값이 같을 때만 적중이다. 아니면
  `stale`. manifest의 `inputs`는 종전처럼 내용 해시를 기록하므로 구버전 manifest도 그대로 검사된다.
- 파라미터 변경(`unwrap.coherence_threshold`, `seed`, 엔진 버전 …)은 여전히 새 해시 → PERF-03 하류 무효화.
  `--force`는 강제 폐쇄(closure)로 하류까지 전량 재실행(부분 캐시 사용 안 함).
- 기본 모드(`incremental=False`)에서도 규칙은 같다: 날짜가 늘면 같은 노드 디렉터리에서 **전량 재계산**한다
  (`prepare_node_dir(clean=True)`가 `pairs/`도 지움). 6일치와 7일치 결과가 서로 다른 디렉터리에 공존하던
  이전 동작은 사라진다 — 날짜 집합은 "튠 변형"이 아니라 데이터이므로 최신 것 하나면 된다.

### 2. 쌍 단위 서브캐시(`PairCache`)

```
work/<stage>/<hash>/
  manifest.json            # StageRecord (+ extra.pairs, extra.incremental)
  out/                     # 조립된 스택 산출물 (igrams.npz / unw.npz / stats.json)
  logs/
  pairs/manifest.json      # {"version":1,"stage":…,"pairs":{<key>:{hash,path,file_hash,meta,computed_at}}}
  pairs/<key>.npz          # 쌍 하나의 결과
```

- 실행기는 `incremental=True`이고 노드가 증분 단계이며 `--force`가 아닐 때 `out/`·`logs/`만 비우고 `pairs/`는
  남긴 뒤 `params["_pairs_dir"]`, `params["_pairs_done"]`(파일이 존재하는 항목만, 절대 경로)을 넘긴다.
- 엔진/언래핑 실행기는 쌍마다 식별자 `pair_identity(params_hash, input_hash)`를 계산해 `_pairs_done`의 항목과
  **식별자가 같고 파일이 온전(빠른 해시 `file_hash` 일치)**할 때만 재사용한다. `file_hash`가 없는 항목은 검증할 수
  없으므로 재사용하지 않는다(`PairEntry.intact()`). 빠른 해시는 크기·mtime·앞뒤 1 MiB만 보므로 파일 중간 손상은
  놓칠 수 있다: 온전해 보이는 적중이 **읽히지 않으면**(`np.load` 실패) 그 쌍 하나만 캐시 미스로 돌린다 —
  `PairCache.discard(key)` 후 다시 계산·`store`(언래핑은 2차 라운드, fake는 즉시 합성). 단계 실패(`PIPELINE-001`)로
  번지지 않는다. 새로 계산한 쌍은 `pairs/`에 쓰고 manifest에 등록한다. `save()`는 참조되지 않는 결과 파일을
  정리한다.
- `pairs/manifest.json`이 **있는데 읽을 수 없으면**(잘린 JSON, `pairs`가 매핑이 아님 …) 없는 것처럼 버리고 전량
  계산하되, 원인을 `PairCache.manifest_error`(예외 클래스명 또는 `InvalidManifest`)에 남긴다. `plan`/`run`은
  `extra["incremental"]["manifest"] = "unreadable"`과 `PIPELINE-017`(INFO, "매니페스트를 읽을 수 없어 N개 쌍을
  모두 다시 계산")으로 이를 첫 증분 실행(매니페스트 없음, 조용함)과 구분해 보여 준다. 그 실행이 매니페스트를 다시
  만든다. wintersar 자신의 `save()`는 원자적(tmp → replace)이라 이 상태를 만들지 않는다.
- 산출물 meta에 `pairs`(스택 순서), `pairs_reused`, `pairs_computed`를 보고하면 실행기가
  `record.extra["pairs"]`, `record.extra["incremental"] = {requested, supported, reused, computed, computed_pairs}`로
  기록한다. 요청했는데 보고가 없으면 `supported: false` + `PIPELINE-016`(INFO) — 실제로는 전량 재계산했음을
  숨기지 않는다. `PIPELINE-016`/`PIPELINE-017`은 "그 실행에서 일어난 일"이라 캐시 적중으로 manifest를 재생할 때는
  빼고 보여 준다(`executor.cache_hit_record`, `RUN_EVENT_RULES`); 결과 자체에 대한 finding(UNW-003 등)은 재생된다.
- 쌍 식별자의 "입력" 부분: fake 간섭도는 **쌍 키**(합성이 `(params, key)`의 순수 함수, 아래), multilook/unwrap은
  **쌍 배열의 내용 해시**(`pair_content_hash`: dtype·shape·bytes). 언래핑의 "파라미터" 부분은 `UnwrapCfg` 덤프 +
  *해석된* 계획(method, 타일 행·열·오버랩) + 백엔드로 가는 사용자 키 + `_tile_offset_cycles`이고 병렬도는 제외
  (`unwrap.api.pair_param_hash`). `auto`가 다른 머신에서 다른 타일링을 고르면 다른 결과이므로 식별자에 넣는다.

### 3. fake 엔진의 쌍 단위 순수성

`synth.make_stack`은 RNG 스트림 하나를 날짜·쌍 순서로 소비하므로 날짜를 더하면 옛 쌍의 값이 바뀌었다. fake
엔진은 이제 날짜별 대기를 `rng(seed, date)`, 쌍별 코히어런스·잡음을 `rng(seed, pair)`에서 뽑는다
(`FakeEngine.synth_pair`). 변형·대기는 날짜 차이, 잡음은 쌍 고유이므로 폐합 일관성은 유지되고 옛 쌍은 비트
단위로 같다. 그 결과 "6→7 증분 == 7 처음부터"가 테스트로 성립한다
(`test_incremental_result_equals_a_full_recompute`). 날짜는 `2024-01-01 + 12일·i`로 고정(`n_dates` N→N+1은 정확히
한 날짜와 그 날짜에 닿는 쌍만 추가). `timeseries`는 항상 재계산(재역산).

### 4. `plan` / `run` API

- `api.plan(cfg, …, incremental=True)`, `api.run(cfg, …, incremental=True)`. `Dag(cfg, incremental=…)`.
- 노드 상태에 **`incremental`**(부분 캐시)가 추가된다: 증분 모드 + 해시 일치 디렉터리에 `pairs/` 항목이 있을 때.
  `StageRecord.extra["incremental"] = {"status":"partial","expected","done","cached","new"}`, 그리고
  `PIPELINE-015`(INFO) "캐시된 쌍 N개 재사용, 새 쌍 M개 계산". 기대 쌍 집합은 엔진 훅
  `expected_pairs(stage, params, available)`(fake) → 입력 meta `pairs` → precheck `stack.json`의 `pairs` 순으로
  구하고 `igrams` 사슬을 따라 상속한다. 모르면 `new: null`, 텍스트는 `?`.
- 추정치: 증분 노드의 `n_pairs`는 새 쌍 수로 잡는다(`plan.stage_size`).
- `RunResult.incremental`, `RunResult.partial`, `RunResult.pair_summary()`; `to_dict()`에 `incremental`, `pairs`.
  `pair_summary()`는 `{"reused", "computed", "stage", "by_stage"}` — 단계별 수를 `by_stage`에 두고 상위 수치는
  **한 단계**(가장 많이 계산한 단계, 동률이면 상류)의 값이다. 단계에 걸쳐 **합산하지 않는다**: 세 쌍 단계가 같은
  쌍 집합을 다루므로 합산은 한 쌍을 세 번 세어 `PIPELINE-015`의 단계별 수와 어긋난다.
- CLI 플래그(`--incremental`)와 표의 "부분 캐시" 행은 cli.py 소유자가 붙인다(i18n 키 `pipeline.cli.status_incremental`,
  `incremental_pairs`, `incremental_mode`는 준비됨).

## 결과 (Consequences)

- 첫 실행이 증분 모드가 아니면 `pairs/`가 없으므로 다음 증분 실행은 전량 계산하며(`stale`, 상태는 `to_run`)
  그 실행이 캐시를 심는다. 운영 사이트는 처음부터 `incremental=True`로 돌린다.
- 언래핑의 쌍 단위 보관은 실 데이터에서 `unw.npz`(압축) 외에 비압축 쌍 파일을 추가로 둔다(디스크 약 2배).
  그래서 `_pairs_dir`가 있을 때만 쓴다(단독 `wintersar unwrap run`은 종전과 동일). `cache gc`의 크기 예산
  (ADR-0081)이 노드 디렉터리 단위로 이를 회계한다.
- 같은 노드 디렉터리를 덮어쓰므로 옛 날짜 집합의 `timeseries` 노드는 입력이 사라져 다시는 적중하지 않는다
  (의도: 최신 스택만 유효).
- 실 엔진의 참여 조건은 ADR-0082(실 엔진 검증 항목은 open-questions #74). 측정(증분 1장 vs 전체)은
  `bench_result.json`이 생기기 전에는 수치를 쓰지 않는다(규칙 11.8, open-questions #75).
