# ADR-0014: 사전검증 규칙(SEL-01~13)의 임계값과 심각도

- 상태(Status): 제안 — **연구자 확인 필요(researcher confirmation required)**. 규칙 11.10에 따라 확인 전에는 기본값을 바꾸지 않는다.
- 날짜(Date): 2026-09-16
- 관련 ID: R-01, R-02, R-03, R-04, SEL-01 ~ SEL-13, PERF-13
- 검증 출처(Sources):
  - Sentinel-1 궤도: 12일 반복, 주기당 175 궤도 — https://sentiwiki.copernicus.eu/web/s1-mission ("12 day repeat cycle and 175 orbits per cycle")
  - 정밀궤도(POEORB)는 "computed after a number of days", MOEORB 1일 — 같은 페이지 (POD products)
  - IW SLC 픽셀 간격 2.3 × 14.1 m, 1×1 looks — https://sentiwiki.copernicus.eu/web/s1-products (Level-1 SLC Product Characteristics, Table 6)
  - ASF `fullBurstID` = `<relative orbit>_<burst id>_<subswath>` (예 `017_034465_IW2`) — https://docs.asf.alaska.edu/api/keywords/
  - HyP3 burst InSAR 쌍 조건(같은 burst·relative orbit, 같은 편파 HH/VV, 같은 궤도 방향), 크레딧은 burst 작업 단위 — https://hyp3-docs.asf.alaska.edu/guides/burst_insar_product_guide/
  - 임계값 기본값: `src/wintersar/pipeline/config.py::SelectionCfg`, `EngineCfg` (플랜 §4.4)

## 맥락

플랜 §5.1.3 규칙표는 판정(FAIL/WARN/INFO)만 정하고 임계값은 설정값(`selection.*`)에 둔다.
구현하면서 각 규칙의 판정 근거·기본값·해석을 한곳에 고정해야 연구자가 검수할 수 있다.
구현: `src/wintersar/select/rules.py` (규칙마다 `sel_xx(ctx) -> list[Finding]`, 레지스트리 `RULES`,
문서용 `rule_table()`), 메시지 `src/wintersar/i18n/{ko,en}/select.yaml`.

## 결정 (규칙별 판정·임계값)

| ID | 검사 | 판정 | 기본값 / 근거 | 검수 포인트 |
|---|---|---|---|---|
| SEL-01 | 스택 내 relative orbit 동일. 후보의 `relative_orbit`, 레코드의 트랙, `fullBurstID` 접두(3자리 트랙)를 모두 비교 | 불일치 → FAIL. 같은 방향에 트랙이 2개 이상이면 전역 INFO(트랙 간 간섭 불가 안내) | 도메인 사실(같은 트랙만 간섭 가능) | — |
| SEL-02 | 비행 방향 동일 | 불일치 → FAIL | 도메인 사실 | — |
| SEL-03 | 모드 동일 + 날짜별 sub-swath 집합 동일 | 불일치 → FAIL | 도메인 사실 | 비-IW 모드 자체를 경고할지 |
| SEL-04 | 모든 날짜 공통 burst의 AOI 커버리지 | 공통 없음/0 → FAIL; `< min_coverage` → WARN (대안 두 가지 커버리지 첨부) | `min_coverage=0.95` | 0.95가 적정한가 |
| SEL-05 | 동일편파(VV 또는 HH) 존재. `VV+VH` 같은 이중편파는 통과 | 없음 → FAIL | HyP3 조건(HH/VV) | — |
| SEL-06 | 쌍별 \|B⊥\| | `> max_perp_baseline_m` → WARN(쌍 단위); 기선 미계산 → INFO | `150 m` (플랜 §4.4) | Sentinel-1 궤도관은 좁아 150 m 초과가 드묾; 100 m로 낮출지 |
| SEL-07 | 쌍별 시간 기선; 계절 교차 | `> max_temporal_baseline_days` → WARN; 식생기↔적설·낙엽기 교차 → INFO(스택당 1건, 쌍 수·예시) | `48일`. 계절 모델: 북반구 11~3월 적설·낙엽, 5~9월 식생, 4·10월 환절기(판정 제외); 남반구는 6개월 이동 | **휴리스틱**. 한국 사이트에서 적설 월 범위·환절기 처리 확인 필요 |
| SEL-08 | IPF 메이저 버전 | 2개 이상 → INFO | 알려진 이슈는 diagnose KB에서 관리 | KB 항목 연결 |
| SEL-09 | 픽셀 간격 편차 `(max-min)/median` | `> pixel_spacing_tolerance` → WARN; 항상 INFO(자동 looks) | `0.01`(1%), `target_pixel_m=40` | 1%가 실제 메타데이터 잡음보다 큰지 |
| SEL-10 | 날짜별 burst 수 = 공통 burst 수; `extra.missing_lines` | 불일치 → WARN | 내부 규약(`extra["missing_lines"]`은 metadata.py가 채움) | — |
| SEL-11 | POEORB 가용성 | 훅 `poeorb_available(date) -> bool|None`; False → INFO, None/훅 없음 → 생략 | 네트워크 접근은 규칙 밖(훅 주입) | 훅 구현 위치(fetch 단계) |
| SEL-12 | 레이오버+셰도우 비율 | `> max_layover_shadow_fraction` → WARN, 이하 → INFO(값 보고) | `0.10`. 입력은 `geometry_masks.GeometryMaskResult.stats` 또는 `{방향|stack_id: stats}` | 10%가 적정한가(산지 사면 사이트) |
| SEL-13 | 크레딧·리소스 | `credits > budget_credits` → WARN; 예산 내 → INFO; credits 미상 → INFO(`info_unknown`) | `budget_credits=None`. **크레딧 수치는 추정하지 않음**(HyP3 크레딧 표는 open-questions #3) | — |

공통: `Finding.scope`는 `stack_id` 또는 `"<stack_id>:<pair.key>"`; 리포트의 추천(`recommend_stack`)은
스택 범위의 FAIL만 배제 기준으로 삼는다. 메시지는 원인 → 조치 순서(`select.SEL-xx.{fail|warn|info}` → `select.SEL-xx.fix`).

## 결과

- 연구자 검수 전에는 위 기본값을 변경하지 않는다(규칙 11.10). 변경 시 `tests/unit/select/test_rules.py`의 양성·음성 테스트를 갱신한다.
- 계절 모델(SEL-07)과 SEL-12 임계값은 `docs/open-questions.md`에 검수 항목으로 등록했다.
- Phase 1 DoD의 "실제 AOI 3곳"에서 FAIL/WARN 분포를 보고 임계값 재조정 여부를 결정한다.
