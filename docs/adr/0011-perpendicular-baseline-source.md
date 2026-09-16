# ADR-0011: 수직 기선 출처 — asf_search stack API 우선, 궤도 상태벡터 자체 계산 fallback

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-01, SEL-06, 미확정 사항 #2
- 검증 출처(Sources):
  - `.venv/lib/python3.11/site-packages/asf_search/search/baseline_search.py` (`stack_from_id`, `stack_from_product`)
  - `.venv/lib/python3.11/site-packages/asf_search/baseline/stack.py`, `baseline/calc.py`
    (`calculate_perpendicular_baselines`, `get_up_beam_vector`, `get_paired_granule_baseline`)
  - `.venv/lib/python3.11/site-packages/asf_search/Products/S1Product.py` (`get_state_vectors`, `has_baseline`)
  - 실제 호출 (2026-09-16, 인증 없음): `S1_270859_IW2_20240107T093233_VV_0C4A-BURST`.stack(2023-11-01~2024-03-01)
    → 10개 날짜, `tests/fixtures/asf/burst_products.json["stack"]`
  - EOF 파싱 요소: ISCE2 `Sentinel1.py::extractPreciseOrbit`, s1-reader `s1_reader.py::get_burst_orbit`
  - EOF 파일명 규약: scottstanie/sentineleof `eof/products.py::SentinelOrbit.FILE_REGEX`

## 맥락

플랜 §5.1.2: "수직 기선은 asf_search stack API의 baseline 값을 우선 사용하고, 없으면 정밀궤도(POEORB)로
자체 계산(구현·검증 필요, 단위테스트로 알려진 쌍과 비교)". 미확정 사항 #2는 stack API가 Sentinel-1
(특히 BURST)에서 수직 기선을 실제로 계산하는지였다.

## 확인한 사실

1. **stack API는 BURST에서 동작하며 인증이 필요 없다.** `stack_from_id(reference)`는
   `fullBurstID` + 편파 + `processingLevel=BURST`로 CMR을 다시 검색한 뒤 `properties['perpendicularBaseline']`
   (정수 m, 기준 = 0)과 `properties['temporalBaseline']`(일)을 붙여 준다. 값은 서버가 아니라 **클라이언트가**
   각 그래뉼의 CMR 상태벡터 2개(`SV_POSITION_PRE/POST`, 10 s 간격)와 씬 중심(`CENTER_LAT/LON`)으로 계산한다
   (`baseline/calc.py`). 상태벡터가 없는 그래뉼은 `None`.
2. 실측값(2024-01-07 기준): 2023-11-08 +155, 11-20 −127, 12-02 −75, 12-14 −7, 12-26 +130, 2024-01-19 −15,
   01-31 −37, 02-12 −33, 02-24 −42 m. Sentinel-1 궤도 튜브(±100 m급)와 일관된 범위.
3. **자체 계산 검증.** `baseline.perpendicular_baseline_from_state_vectors`는 (a) 위성별로 표적에 대한
   zero-Doppler 시각을 3차 Hermite 보간 궤도 위에서 구하고, (b) `n = normalize(v_ref × L)`
   (`L` = 표적→기준 위성 단위벡터)로 `B⊥ = (P_sec − P_ref)·n`을 계산한다. 이는 asf_search의
   `get_up_beam_vector`/`get_paired_granule_baseline`과 같은 투영·부호 규약이며
   `|B| sin(기선–시선 각)`에 해당한다. 같은 CMR 상태벡터로 계산한 결과는 10개 날짜 모두 stack API 값과
   **|차이| ≤ 0.4 m** (정수 반올림 포함) — `tests/unit/select_search/test_baseline.py::
   test_orbit_method_matches_stack_api_on_real_fixture`가 1 m 허용으로 고정한다.
4. 해석적 기하 단위테스트: 표적 상공 H=700 km, 횡방향 D=525 km(ρ=875 km)의 직선 궤도에서
   기선 100 m 동쪽 → B⊥ = 80 m, B∥ = −60 m(3-4-5), 기선이 시선 방향이면 0, `n` 방향이면 100 m,
   순수 along-track 이동은 영향 없음. 위도·경도 3곳(0/0, 서울, 시드니)에서 동일하게 통과.
5. 2개 상태벡터만 있을 때 선형 보간은 10 s 구간 중앙에서 ~100 m의 반경 오차(asf_search는 `radius_fix`로
   보정)가 나지만, 위치+속도를 쓰는 Hermite 보간은 원궤도 테스트에서 2 cm 미만이다.

## 선택지

1. stack API만 사용 (상태벡터 없는 그래뉼은 `None`).
2. 자체 계산만 사용 (POEORB 필요, 검색 직후에는 없음).
3. stack API 우선, 값이 없거나 API가 실패하면 상태벡터(POEORB/RESORB EOF → 없으면 CMR 2개 벡터)로
   자체 계산 (`method='auto'`), 리포트에 출처 표시.

## 결정

선택지 3. `compute_pair_baselines(records, pairs, method='asf'|'orbit'|'auto')`:

- 날짜별 B⊥는 기준 날짜(`choose_reference`: 중앙 날짜, 가장 많은 날짜에 존재하는 burst) 기준값이고,
  쌍의 B⊥는 `B⊥(secondary) − B⊥(reference)`(SBAS 표준 근사). 같은 날짜의 여러 burst는 평균한다.
- stack에 없는 이웃 burst(같은 취득·같은 궤도)는 날짜·트랙으로 매칭한다(궤도가 같으므로 차이는 m 단위).
- 기선을 못 구한 날짜는 `SEL-SEARCH-06`(WARN), orbit fallback 사용은 `SEL-SEARCH-08`(INFO),
  API 오류는 `SEL-SEARCH-05`로 보고한다.
- EOF 우선순위 POEORB > RESORB > PREORB, 최신 생성본. `.EOF` 파일명 규약은 sentineleof와 동일.

## 결과

- SEL-06은 `Pair.perp_baseline_m is None`을 "판정 불가"로 다뤄야 한다.
- 자체 계산의 표적은 기준 씬 중심(`extra.center_lat/lon`)이다. AOI 중심으로 바꾸면 값이 m 단위로
  달라질 수 있으므로 stack API와 비교할 때는 같은 표적을 써야 한다.
- 네트워크 테스트 `tests/network/test_asf_search.py`가 위 실측값(−15, +130, +155 m)을 ±1 m로 재확인한다.
