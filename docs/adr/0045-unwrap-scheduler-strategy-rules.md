# ADR-0045: 언래핑 스케줄러 전략 규칙과 상수 (메모리 모델 c, 타일 결정, 프린지 임계)

- 상태(Status): 채택 (상수는 bench로 재보정 예정)
- 날짜(Date): 2026-09-16
- 관련 ID: R-06, PERF-04, PERF-03
- 검증 출처(Sources):
  - SNAPHU 홈페이지 <https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/> —
    "In single-tile mode the required memory is on the order of 100 MB per 1,000,000 pixels
    in the input interferogram." (2026-09-16 WebFetch로 확인)
  - SNAPHU man page <https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/snaphu_man1.html> —
    `--tile`: "The interferogram is partitioned into ntilerow by ntilecol tiles, each of which is
    unwrapped independently. Tiles overlap by rowovrlp and colovrlp pixels"; `--nproc`: "The
    program forks a new process for each tile ... at most n processes will run concurrently";
    `--assemble`: "Assemble the tile-mode temporary files from a previous tile-mode run".
    **man page 본문에는 메모리 수치가 없다** (플랜 §5.4의 "SNAPHU 문서상 c ≈ 100 MB"는 홈페이지 문구다).
  - snaphu-py `unwrap()` 시그니처
    <https://github.com/isce-framework/snaphu-py/blob/main/src/snaphu/_unwrap.py>:
    `ntiles: tuple[int, int] = (1, 1)`, `tile_overlap: int | tuple[int, int] = 0`, `nproc: int = 1`,
    `scratchdir`, `delete_scratch` — "Increasing the number of tiles may improve runtime and reduce
    peak memory utilization, but may also introduce tile boundary artifacts".
  - tophu `multiscale_unwrap()` <https://github.com/isce-framework/tophu/blob/main/src/tophu/_multiscale.py>:
    `downsample_factor`, `ntiles`, `nlooks`, `unwrap_func`; 오버랩 인자 없음. 타일 접합은
    "Add or subtract multiples of 2π to each connected component to minimize the mean discrepancy
    between the high-res and low-res unwrapped phase".
  - 구현: `src/wintersar/unwrap/scheduler.py`, 테스트 `tests/unit/unwrap/test_scheduler.py`.

## 맥락 (Context)

플랜 §5.4는 간섭도 M개를 언래핑할 때 (1) 단일 타일 메모리 추정 → (2) 예산 초과 시 타일 분할
→ (3) 프린지 밀도/크기에 따라 tophu 우선 → (4) 간섭도 단위 병렬을 규정한다. 규칙에 필요한
상수(메모리 상수 c, 프린지 임계값, 타일 격자 결정 방식)와 "per-process 예산"의 정의를 확정해야
`wintersar unwrap plan`이 결정론적으로 동작하고 bench가 무엇을 재보정하는지 분명해진다.

## 선택지 (Options)

1. 메모리 상수를 코드에 고정하고 실측치는 문서에만 기록.
2. 상수를 설정(`unwrap.memory_mb_per_mpixel`, 기본 100)에 두고 스케줄러는 값을 읽기만 함; bench가
   3개 이상 픽셀 수로 회귀선을 맞춘 뒤 기본값을 ADR로 갱신.
3. 프린지 밀도를 계산하지 않고 크기만으로 tophu 선택.

## 결정 (Decision)

선택지 2 + 프린지 밀도 계산(플랜 §5.4 3항 그대로).

- **메모리 모델** `estimate_memory_mb(shape, c) = c · ny · nx / 1e6` (MB). `c` 기본 100 MB/Mpixel은
  SNAPHU 홈페이지 문구가 근거이며 "on the order of" 수준의 초기값이다. 실측 전까지 성능 수치로
  인용하지 않는다(규칙 11.8). 예산 단위는 `MachineSpec.memory_gb × 1024` MB.
- **예산의 정의**: `choose_strategy`가 받는 `MachineSpec`은 이미 `budget()`이 적용된 값이다
  (`compute.memory_gb: auto` → 탐지값 × 0.8). 스케줄러는 추가 여유를 두지 않는다.
- **타일 결정**: `m ≤ 예산`이면 단일 타일. 아니면 per-process 예산 = `예산 / nproc_target`
  (nproc_target은 ADR-0047의 타일 워커 수)으로 `ntiles = ceil(m · nproc_target / 예산)`을
  근사 정사각 격자(`near_square_grid`: `rows = round(sqrt(ntiles · ny / nx))`)로 만들고,
  오버랩(ADR-0046)을 더한 타일 크기로 메모리를 다시 계산해 `nproc · tile_mb ≤ 예산`이 되도록
  nproc을 줄이거나(우선) 타일 수를 늘린다. 최대 4096 타일까지도 한 타일이 예산을 넘으면
  `memory_insufficient` 사유를 남기고 UNW-002(WARN)를 낸다.
- **명시 타일**: `unwrap.tiles: {rows, cols, overlap, min_overlap_px}`가 있으면 자동 결정을
  건너뛰고 그대로 쓴다(`tiles_explicit`). `nproc_per_igram`도 설정값을 유지한다.
- **프린지 밀도** `fringe_density(wrapped, mask)`: 복소 이웃 차 `angle(z[i+1]·conj(z[i]))`의
  절댓값 평균을 π로 나눈 값(0~1). 1.0 = 픽셀당 π(에일리어싱 한계), 순수 잡음 ≈ 0.5, 매끄러운 위상
  ≪ 0.1. 마스크(수역·저코히어런스)를 제외하고 계산해 잡음이 "고프린지"로 오인되는 것을 줄인다.
  임계 `FRINGE_HIGH = 0.25`(평균 기울기 π/4 rad/px, 약 8 px마다 한 프린지)는 설계 상수이며
  bench 합성 3종에서 tophu/snaphu 오류율 교차점으로 재조정한다.
- **방법 선택**(`unwrap.method: auto`): 타일 분할이 필요하거나 프린지 밀도 ≥ 임계이고 tophu가
  설치되어 있으면 tophu, 아니면 snaphu, 둘 다 없으면 첫 가용 백엔드, 아무것도 없으면 snaphu로
  계획하고 실행 시 ENV-001/UNW-001로 보고. `spurt`는 스택 입력 전용이라 자동 선택 대상이 아니다.
- 모든 결정 사유는 i18n 키(`unwrap.plan.reason.*`)로 `UnwrapPlan.reason_keys`에 남기고
  `explain()`이 ko/en 문장으로 렌더링한다.

## 결과 (Consequences)

- `docs/open-questions.md` #8(메모리 상수 실측)은 bench 모듈이 `bench_result.json`과 함께 닫는다.
  회귀선 절편이 유의하면 모델을 `a + c·P`로 확장한다(설정 키 추가는 config 소유자에게 요청).
- `FRINGE_HIGH`와 정사각 격자 규칙은 연구 모듈(R-07) 실험 결과에 따라 바뀔 수 있다; 기본값 변경은
  ADR로만 한다(규칙 11.10).
- 엔진 어댑터는 `params["ntiles"]`, `params["tile_overlap"]`, `params["nproc"]`, `params["cost"]`,
  `params["init"]`, `params["_tile_dir"]`를 읽고, 타일 임시 디렉터리를 보존했으면
  `stats["tile_dir"]`로 돌려준다(PERF-03 assemble-only 재사용). 엔진 어댑터는 `ntiles`를 직접
  처리하는 것으로 간주하며(`supports_tiles = False`를 선언하면 예외), 테스트 백엔드는 실행기가
  직접 타일링·병합한다(ADR-0048). 어댑터의 반환형은 `wintersar.engines._unwrap_common.UnwrapResult`
  여도 되고(`unw`/`conncomp`/`stats` 속성으로 덕타이핑) 3-튜플이어도 된다.
