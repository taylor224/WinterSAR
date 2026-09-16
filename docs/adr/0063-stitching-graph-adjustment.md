# ADR-0063: 타일 2π 오프셋 결정 — coarse_ref(tophu 방식)와 overlap_consensus(오버랩 합의 + 그래프 최소제곱)

- 상태(Status): 채택 (기본값 변경 없음; 스케줄러 옵션 승격은 ADR-0060 판정 후)
- 날짜(Date): 2026-09-16
- 관련 ID: R-06, R-07, PERF-04
- 검증 출처(Sources):
  - tophu v0.2.1 `src/tophu/_multiscale.py` —
    https://raw.githubusercontent.com/isce-framework/tophu/v0.2.1/src/tophu/_multiscale.py
    (`adjust_conncomp_offset_cycles`: `avg_offset = mean(unwrapped_hires[valid] − unwrapped_lores[valid])`,
    `avg_offset_cycles = round(avg_offset / (2π))`, `hires[conncomp] −= 2π·avg_offset_cycles`;
    `upsample_unwrapped_phase`: 저해상도 사이클 차를 `upsample_nearest`로 올려 `wrapped_hires + 2π·k`)
  - ADR-0048(경계 단차 검출기·단순 병합 정의, `unwrap.tiling.tile_grid/boundary_jumps/merge_tiles`)
  - 플랜 §5.7 stitching, §12.1, §6.2 PERF-04("타일 경계 단차 검출기는 연구 모듈 지표와 공용")
  - 구현: `src/wintersar/research/stitching.py`, 테스트 `tests/unit/research/test_stitching.py`

## 맥락 (Context)

독립 언래핑된 타일은 각각 미지의 `2π·k_i` 모호성을 가진다. tophu는 저해상도 언래핑 해를
기준으로 (연결성분별) 정수 오프셋을 정한다. 연구자 요구(R-06 "오버랩 구간 단차 처리, 가중치")는
오버랩에서 직접 이웃 타일 간 오프셋을 재고 코히어런스로 가중하는 방식이다. 두 방식을 같은
입력·출력 규약으로 비교할 수 있어야 한다.

## 선택지 (Options)

1. `coarse_ref`만 — 기준 위상 오류가 그대로 타일 오프셋 오류가 된다.
2. 인접 타일 쌍을 순차적으로 이어 붙이기(스패닝 트리) — 루프 불일치를 감지·완화하지 못한다.
3. **둘 다 제공, `overlap_consensus`는 그래프 최소제곱(채택)**.

## 결정 (Decision)

- 입력 `tiles = [(unw_tile, slice_y, slice_x)]`(익스텐트 좌표, `tile_grid` 배치), 출력
  `(merged, offsets_cycles, report)`; `offsets_cycles[i]`는 타일 `i`에서 **뺀** 사이클 수.
- **coarse_ref**: `k_i = round(stat(unw_tile − upsample_nearest(lowres_ref)) / 2π)`, `stat` = mean
  (tophu) 또는 median. 타일 단위(tophu는 연결성분 단위 — 오픈 항목 #64).
- **overlap_consensus**: 인접 쌍(`unwrap.tiling.adjacent_pairs`)마다 익스텐트 교집합에서
  `d = round((A − B)/2π)`; 가중 최빈값(동률이면 |k| 작은 쪽) 또는 가중 중앙값, 가중치 = 코히어런스
  (`coh`) 또는 임의 품질 맵(`weights`). 간선 가중 `W_ab = Σw · 합의 비율`. 미지수 `o_i`,
  방정식 `o_a − o_b = k_ab`, 게이지 행 `o_0 = 0`(큰 가중), `np.linalg.lstsq(√W·A, √W·k)` → 반올림.
  잔차 `A·o − k`가 0이 아닌 간선 = 루프 불일치(`n_inconsistent_edges`, `residual_max_cycles`;
  CLI는 `RES-005` 경고). 연결되지 않은 타일은 0.
- 병합: 오프셋 제거 후 `unwrap.tiling.merge_tiles`(feather/core, ADR-0048); 기하는
  `tiles_from_slices`가 익스텐트에서 코어를 복원(`tile_grid` 규칙: 오버랩의 `overlap//2`는
  아래/오른쪽 타일 코어). 보고서에 `boundary_jumps`(seam 모드)를 넣어 단차 지표를 공용화한다.
- 검증: 3×3 타일(오버랩 12, 잡음 0.2 rad, 대기 0.8 rad)에서 두 방법 모두 주입 오프셋을 정확히
  복원, 단차 0; 기준 위상 잡음 0.8 rad에서도 `coarse_ref(median)` 정확; 코히어런스 0 가중으로
  손상된 오버랩을 무시.

## 결과 (Consequences)

- 실행기(`unwrap.api`)와의 연결은 `merge_tiles` 입력 형식(타일별 배열·익스텐트)이 같으므로
  옵션 승격 시 어댑터만 필요하다(ADR-0060 판정 후).
- 연결성분 단위 오프셋(#64)은 백엔드 conncomp 라스터를 받아야 한다.
- 오버랩이 없는 타일(오버랩 0)은 `overlap_consensus`로 결정할 수 없다 — `coarse_ref` 사용.
