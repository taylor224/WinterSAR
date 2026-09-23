# ADR-0081: 캐시 크기 예산 — `cache gc --max-size`의 퇴거 규칙과 크기 회계 (PERF-03)

- 상태(Status): 채택
- 날짜(Date): 2026-09-23
- 관련 ID: PERF-03, PERF-06, R-11
- 검증 출처(Sources):
  - 플랜 §6.2 PERF-03 "캐시 크기 제한과 `wintersar cache gc`"
  - `src/wintersar/pipeline/cache.py::gc`, `size_summary`, `GcReport`; ADR-0032(작업 디렉터리·GC N 안내)
  - 테스트: `tests/unit/pipeline/test_cache.py`(`test_gc_max_bytes_trims_the_oldest_survivors`,
    `test_gc_budget_evicts_oldest_across_stages_and_spares_failed_newest`, `test_size_summary_…`),
    `tests/unit/pipeline/test_cli.py::test_cache_gc_max_size_budget`

## 맥락 (Context)

`cache gc --keep N`은 단계당 항목 *수*만 제한한다. 간섭도·언래핑 노드는 수십 GB이고 증분 모드(ADR-0080)는
노드 디렉터리에 `pairs/`(쌍 단위 결과)를 더하므로, 바이트 예산이 없으면 작업 디렉터리 크기를 묶을 수 없다.
예산이 있을 때 무엇을 먼저 지우고 무엇은 절대 지우지 않을지 정해야 한다.

## 선택지 (Options)

1. **가장 오래된 `finished_at`부터 단계 구분 없이 퇴거, 단 각 단계의 최신 ok 항목은 보호.**
2. LRU(최근 적중 시각): manifest에 적중 시각을 써야 하고(적중마다 쓰기), `plan`(dry-run)도 갱신할지 애매하다.
3. 크기 큰 것부터: 방금 만든 언래핑 결과가 가장 크므로 다음 실행이 곧바로 재계산으로 떨어진다.
4. 예산 초과 시 실패(퇴거 안 함): 운영자가 손으로 지워야 한다.

## 결정 (Decision)

선택지 1.

- `cache.gc(workdir, keep_latest=3, dry_run=False, stages=None, max_bytes=None)`:
  1. 단계별로 `keep_latest`개(최신순, 고아 제외)만 남기고 나머지를 지운다(종전 규칙, ADR-0032).
  2. `max_bytes`가 있으면 남은 항목의 합이 예산 이하가 될 때까지 **`finished_at`이 오래된 순서로**(단계를
     가리지 않고) 더 지운다. **보호 항목** = 각 단계의 최신 `ok` manifest(고아·failed는 보호하지 않음)는
     예산이 0이어도 지우지 않는다. 다음 `run`이 바로 그 항목에 대해 해석·적중하므로, 이를 지우면 크기 예산이
     "강제 전량 재계산"으로 바뀐다. 실패 항목은 로그 보존 때문에 `keep_latest` 안에는 들지만 예산에는
     밀린다.
  3. 실행 중(잠금) 항목은 언제나 유지. dry-run은 계획만 보고한다.
- `GcReport`에 `max_bytes`, `protected`, `kept_bytes`, `over_budget`(보호 항목만으로 예산 초과)을 더한다.
  `over_budget`이면 gc는 더 할 수 있는 게 없고, 사용자는 예산을 올리거나 작업 디렉터리를 나눠야 한다
  (i18n `pipeline.cli.cache_over_budget`).
- 크기 회계: `cache.size_summary(workdir, entries=None, max_bytes=None)` →
  `total_bytes`, `by_stage`, `n_entries`, `n_orphans`, `orphan_bytes`, `protected_bytes`,
  (`max_bytes`, `over_budget_bytes`). `cache ls --json`의 `data`에 이 값을 붙이는 것은 cli.py 소유자 몫
  (현재 `size_by_stage`만 있음). 항목 크기(`dir_size`)는 `pairs/`를 포함한다.
- 설정 필드(`compute.cache_max_gb`)와 실행 후 자동 gc는 도입하지 않는다(config.py는 공유 계약). 필요해지면
  `cache gc --max-size`를 cron/운영 스크립트에서 부른다.

## 결과 (Consequences)

- `--max-size 0`은 "각 단계의 최신 ok 하나만 남기기"와 같다(테스트로 고정).
- ADR-0032의 안내("N은 동시에 유지할 튠 변형 수")는 유지되고, 크기 예산은 그 위의 상한이다. 예산이 튠 변형을
  먼저 밀어내므로 스윕(ADR-0044) 중에는 예산을 넉넉히 두거나 스윕이 끝난 뒤 gc를 돌린다.
- 증분 모드에서 노드 디렉터리는 재실행 때마다 `finished_at`이 갱신되어 항상 최신이므로, 예산이 증분 사슬을
  먼저 지우는 일은 없다.
