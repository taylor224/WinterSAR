# ADR-0016: 스택 그룹핑·네트워크·참조일 추천

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-01, R-02, SEL-04, SEL-06, SEL-07
- 검증 출처(Sources):
  - 플랜 §5.1.2 (그룹 키, 공통 burst, 대안 커버리지, 네트워크 종류, 참조일 조건)
  - `Pair` 스키마 제약(`temporal_baseline_days > 0`, 즉 reference < secondary): `src/wintersar/io/schemas.py`
  - 기선 계산 계약: `src/wintersar/select/baseline.py::compute_pair_baselines(records, pairs, method="asf"|"orbit"|"auto", *, findings=...)`
  - 구현: `src/wintersar/select/network.py`, CLI `src/wintersar/select/cli.py`

## 맥락

검색 결과(`BurstRecord` 목록)를 "함께 간섭할 수 있는" 스택 후보로 묶고, 사용자가 burst/날짜 중 무엇을 포기할지 고를 수 있게 대안을 제시하며, 네트워크와 참조일을 추천해야 한다.

## 결정

**그룹 키.** `(relative_orbit, flight_direction, polarization, mode)`. sub-swath는 분할 키가 아니라 후보의 파생 속성(`subswaths`)이다: 한 트랙에서 AOI가 IW1+IW2에 걸치면 하나의 스택이어야 하므로 sub-swath로 나누면 안 된다. 플랜의 "subswath 집합"은 이 파생 속성으로 해석한다. 사전 필터: `data.orbit_direction`, `data.relative_orbit`, `data.polarization`(매칭이 전혀 없으면 전체 유지 → SEL-05가 설명).

**공통 burst와 대안.** AOI와 교차하는 burst만 대상으로
- A `drop_bursts`: 모든 날짜 유지, `common = ∩(날짜별 burst)`;
- B `drop_dates`: `union = ∪(날짜별 burst)` 유지, union을 모두 가진 날짜만.
둘의 커버리지(`area(∪footprint ∩ AOI) / area(AOI)`, 경위도 면적비)를 계산해 `notes["alternatives"]`에 기록한다. 주 후보는 A의 커버리지가 `min_coverage` 이상이면 A, 아니면 커버리지가 큰 쪽(동률이면 날짜가 많은 쪽). 두 경우 모두 "`burst_ids`는 `dates` 전체에 공통"이라는 불변식을 만족한다. `n_dates_dropped` / `n_bursts_dropped`는 선택된 대안의 값.

**네트워크.** `build_network(dates, method, max_temporal_days, max_perp_m, perp_by_date, connections)`:
- `sbas`: `dt ≤ max_temporal_days` 이고, 두 날짜의 B⊥를 알 때만 `|ΔB⊥| ≤ max_perp_m` 적용(모르면 유지 → SEL-06 INFO);
- `sequential`: 각 날짜를 다음 `connections`개(기본 3)와 연결, 임계 미적용(SEL-06/07이 경고);
- `single_reference`: 추천 참조일과 나머지 전부.
`Pair.reference`는 스키마 제약상 항상 이른 날짜이고, 네트워크의 마스터 날짜는 `StackCandidate.reference_date`다.

**참조일.** `recommend_reference(dates, perp_by_date)`: 점수 = `|t − t_mid| / (span/2)` + `Σ_k |B_d − B_k| / max_d Σ_k |B_d − B_k|` (각 항 0~1), 최소 점수, 동률이면 이른 날짜. 기선이 없으면 시간 항만 사용. 쌍별 B⊥는 `perp_by_date_from_pairs`로 첫 날짜 기준 누적(BFS)해 날짜별 값으로 바꾼다(B⊥의 1차 가법성 가정).

**기선 적용 순서(CLI).** `precheck --baseline auto|asf|orbit|none`(기본 `auto`: ASF stack API → 정밀궤도 폴백, 플랜 §5.1.2). 기선이 채워지면 `with_baselines`가 sbas 임계를 다시 적용하고 참조일·네트워크 요약을 갱신하며, baseline 모듈의 Finding(SEL-SEARCH-xx)은 스택 범위로 리포트에 합쳐진다. `none`은 오프라인·테스트용.

## 결과

- `wintersar precheck`는 네트워크 없이도(`--baseline none`) 동작하며, 이때 SEL-06은 "기선 미계산" INFO를 낸다.
- 커버리지 면적비는 경위도 평면 근사이므로 고위도·대형 AOI에서는 소폭 왜곡될 수 있다(커버리지는 판정용 비율이므로 허용).
- 대안을 별도 후보로 분리하지 않고 `notes`에 두므로 `stack_id`가 유일하다. QGIS 플러그인은 `notes.alternatives`를 읽어 선택지를 보여준다.
