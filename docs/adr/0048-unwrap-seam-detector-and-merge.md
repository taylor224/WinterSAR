# ADR-0048: 타일 경계 단차 검출기와 단순 병합의 정의 (research.stitching과 공용)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-06, R-07, PERF-04
- 검증 출처(Sources):
  - 플랜 §5.4 "타일 경계 단차 검출(오버랩 구간 차이의 2π 정수배 분포)", §6.2 "타일 경계 단차
    검출기는 연구 모듈(stitching) 지표와 공용", §5.7 metrics "타일 경계 단차 수"
  - 구현: `src/wintersar/unwrap/tiling.py` (`tile_grid`, `boundary_jumps`, `merge_tiles`,
    `merge_labels`), 테스트 `tests/unit/unwrap/test_tiling.py`,
    `tests/integration/test_unwrap_scheduler.py::test_executor_tiling_detects_injected_seam_offsets`

## 맥락 (Context)

언래핑 결과의 타일 단차 지표는 스케줄러 통계(`stats.json`), bench, 연구 모듈(coarse_ref vs
overlap_consensus 비교)이 같은 정의를 써야 비교가 성립한다. 또 백엔드가 타일을 직접 지원하지
않을 때 실행기가 쓰는 "단순 병합"이 연구 모듈의 2π 일관 스티칭과 혼동되지 않아야 한다.

## 선택지 (Options)

1. 병합 후 래스터의 경계선에서만 단차를 잰다.
2. 타일별 결과의 오버랩 구간에서만 잰다.
3. 두 입력을 모두 받되 각각의 의미를 고정한다.

## 결정 (Decision)

선택지 3. `boundary_jumps(data, tiles)`:

- **overlap 모드**(입력 = 타일별 배열 목록): 인접 타일 A(위/왼쪽)·B(아래/오른쪽)의 익스텐트 교집합
  에서 `k = round((A − B) / 2π)`를 픽셀마다 계산. 히스토그램 `{k: count}`, 최빈값
  `mode_offset_cycles`(동률이면 |k|가 작은 쪽), `n_jump_pixels = count(k ≠ 0)`.
- **seam 모드**(입력 = 병합된 2-D 래스터): 코어 경계선에서 `k = round((B행 − A행) / 2π)`
  (열 경계도 동일). 올바르게 언래핑된 위상은 이웃 픽셀 차가 π 미만이므로 `k ≠ 0`은 타일 오프셋이다.
- NaN(마스크) 픽셀은 제외. 요약: `n_boundaries`, `n_boundaries_with_jump`(최빈값 ≠ 0인 경계 수),
  `n_jump_pixels`, 집계 히스토그램. 부호 규약: overlap 모드는 "A가 B보다 k 사이클 높다",
  seam 모드는 "B가 A보다 k 사이클 높다".
- **타일 격자** `tile_grid(shape, rows, cols, overlap_px)`: 코어는 서로 소이며 래스터를 덮고,
  인접 타일 익스텐트는 정확히 `overlap_px`를 공유한다(아래쪽 코어에서 `overlap//2`, 위쪽에서
  나머지). 오버랩이 최소 코어 변보다 크면 ValueError.
- **단순 병합** `merge_tiles(method="feather")`: NaN을 무시하는 가중 평균. 가중치는 오버랩 여백의
  2배 길이 선형 램프(이웃 타일과 코어 경계에서 교차)이고 합으로 정규화한다. `method="core"`는
  코어 소유 타일 값을 그대로 쓴다. **어느 쪽도 2π 오프셋을 제거하지 않는다** — 그것은 백엔드의
  타일 조립(SNAPHU 보조 네트워크, tophu 저해상도 기준) 또는 `research.stitching`의 몫이다.
- `merge_labels`: 연결성분 라벨은 코어 배정 + 타일 간 고유 번호 재부여(같은 성분이 두 타일에
  걸치면 두 라벨을 가진다; 병합은 하지 않는다). 65535 초과 시 uint32.

## 결과 (Consequences)

- 연구 모듈은 `wintersar.unwrap.tiling.boundary_jumps`/`tile_grid`를 import해 지표를 내고, 자체
  스티칭 결과를 `merge_tiles`의 입력 형식(타일별 배열, 익스텐트 좌표)으로 만들면 실행기와 호환된다.
- 실행기(`api._unwrap_one`)는 엔진 백엔드(자체 타일링, `backends.supports_native_tiles`)에는
  `boundary_jumps(merged, tiles)`(seam 모드)를, 테스트 백엔드에는 자체 타일링 후 overlap 모드를
  기록한다. 단차가 있으면 UNW-003(WARN).
- 테스트 훅 `params["_tile_offset_cycles"] = {"row,col": k}`는 `truth` 백엔드에만 적용된다.
