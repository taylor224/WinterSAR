# ADR-0046: 타일 오버랩 기본값 (25 %, 최소 200 px)과 bench 결정 절차

- 상태(Status): 채택 (잠정 기본값, Phase 6 bench로 확정)
- 날짜(Date): 2026-09-16
- 관련 ID: R-06, PERF-04
- 검증 출처(Sources):
  - SNAPHU man page `--tile` 설명 (오버랩은 픽셀 단위 `rowovrlp`/`colovrlp`),
    <https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/snaphu_man1.html>
  - snaphu-py `tile_overlap` docstring: "Overlap, in pixels, between neighboring tiles. Increasing
    overlap may help to avoid phase discontinuities between tiles."
    <https://github.com/isce-framework/snaphu-py/blob/main/src/snaphu/_unwrap.py>
  - tophu는 오버랩 인자가 없다(타일 비중첩, 저해상도 기준으로 2π 보정)
    <https://github.com/isce-framework/tophu/blob/main/src/tophu/_multiscale.py>
  - 플랜 §5.4: "오버랩 기본 25%(최소 200 px, 설정 가능 — 연구자 경험치 30%와 문헌·포럼 예시
    200 px 사이에서 벤치마크로 결정)"; `docs/open-questions.md` #9
  - 구현: `TilesCfg.overlap = 0.25`, `TilesCfg.min_overlap_px = 200`
    (`src/wintersar/pipeline/config.py`), `scheduler.overlap_pixels()`,
    `tests/unit/unwrap/test_scheduler.py::test_constants_match_config_defaults`

## 맥락 (Context)

타일 언래핑의 경계 단차는 오버랩이 클수록 줄지만 메모리와 시간은 오버랩 제곱에 가깝게 늘어난다
(타일 한 변이 25 % 늘면 픽셀 수는 약 56 % 증가). 연구자 경험치(30 %)와 SNAPHU 사용자 예시(수백 px)
가 서로 다른 단위를 쓰므로 하나의 규칙으로 통일해야 한다.

## 선택지 (Options)

1. 픽셀 고정(예: 200 px).
2. 비율 고정(예: 30 %).
3. `max(비율 × 타일 변, 최소 px)` — 작은 타일에서는 최소 픽셀이, 큰 타일에서는 비율이 지배.

## 결정 (Decision)

선택지 3. `overlap_px = max(round(overlap · min(core_ny, core_nx)), min_overlap_px)`,
단 타일 코어 변 길이를 넘지 않게 자른다(인접하지 않은 타일끼리 겹치면 접합·단차 검출 가정이
깨진다; `tile_grid`가 ValueError로 막는다). 기본값 `overlap = 0.25`, `min_overlap_px = 200`.

- 오버랩 픽셀은 SNAPHU/snaphu-py와 같은 의미(인접 타일이 공유하는 픽셀 수)로 정의하고
  `tile_grid`는 그 절반씩을 양쪽 코어에서 가져온다(홀수면 위쪽 타일이 1 px 적게).
- tophu는 오버랩을 쓰지 않으므로 `tile_overlap`을 무시한다(어댑터 책임).
- **bench 결정 절차(Phase 6)**: 합성 3종(가우시안 침하 · 급경사 램프 · 난류 대기 강함)과 실데이터
  2 사이트에서 오버랩 {10 %, 25 %, 30 %, 200 px, 400 px} × 타일 {2×2, 3×3}를 돌려
  `boundary_jumps`(경계 단차 수·픽셀 수), `unwrap_error_fraction`(합성), 폐합 잔차 RMS(실데이터),
  wall time·peak RSS를 `bench_result.json`에 기록한다. 단차 0에 도달하는 최소 오버랩이 사이트 간
  일관되면 그 값을 기본값으로 올리고 이 ADR을 대체한다. 단차 검출기 정의는 ADR-0048.

## 결과 (Consequences)

- `wintersar unwrap plan`의 `overlap_px`는 위 식으로 재현 가능하며 테스트가 고정한다.
- `docs/open-questions.md` #9는 bench 결과가 나올 때까지 열어 둔다. 기본값 변경은 연구자 확인 후
  ADR로만 한다(규칙 11.10).
- 오버랩이 커서 타일이 예산을 넘으면 스케줄러가 타일 수를 늘리거나 워커 수를 줄인다(ADR-0045).
