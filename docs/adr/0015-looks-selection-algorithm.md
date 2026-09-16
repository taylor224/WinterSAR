# ADR-0015: looks 자동 산출 알고리즘 (R-03, SEL-09)

- 상태(Status): 채택 (종횡비 상한 1.2는 연구자 확인 후 확정)
- 날짜(Date): 2026-09-16
- 관련 ID: R-03, SEL-09
- 검증 출처(Sources):
  - IW SLC 픽셀 간격 2.3 × 14.1 m, 해상도 2.7×22 ~ 3.5×22 m, 1×1 looks — https://sentiwiki.copernicus.eu/web/s1-products (Table 6)
  - HyP3 burst InSAR looks 옵션 20×4 → 80 m, 10×2 → 40 m, 5×1 → 20 m — https://hyp3-docs.asf.alaska.edu/guides/burst_insar_product_guide/
  - 구현·손계산: `src/wintersar/select/looks.py`, `tests/unit/select/test_looks.py`

## 맥락

IW SLC 픽셀은 레인지 2.3 m(슬랜트) × 애지머스 14.1 m로 항상 비대칭이다("애지머스가 짜부"는 결함이 아니라 설계).
레인지 looks를 애지머스보다 크게 주어 정사각 근사 픽셀을 만들어야 하며, 목표 픽셀 크기(`engine.target_pixel_m`, 기본 40 m)에 맞는 정수 looks를 자동으로 골라야 한다.

## 선택지

1. HyP3 고정 옵션(5×1 / 10×2 / 20×4)만 제공.
2. 목표 크기와 종횡비 상한을 만족하는 정수 쌍을 탐색(엔진 무관), HyP3 어댑터는 가장 가까운 옵션으로 스냅.
3. 실수 looks(리샘플링)로 정확히 정사각 픽셀.

## 결정

선택지 2. `compute_looks(range_pixel_spacing_m, azimuth_pixel_spacing_m, incidence_deg, target_pixel_m=40, max_aspect=1.2)`:

1. 지상 레인지 간격 `g = slant / sin(θ)` (θ는 near/far 입사각 평균).
2. `rg ∈ 1..N`, `az ∈ 1..M` 열거(각 축 목표의 약 2배까지).
3. 종횡비 `max(rg·g, az·a) / min(...) ≤ max_aspect` 인 조합만 남김.
4. 픽셀 크기 척도 = `sqrt(rg·g × az·a)`(등면적 정사각형 한 변). `|크기 − 목표|` 최소화, 동률이면 총 looks가 적은 쪽(고해상도), 그다음 종횡비가 작은 쪽.
5. 만족 조합이 없으면(비정상 입력) 종횡비 최소 조합으로 폴백.

IW 대표값(2.33 m, 14.1 m, θ=39°, g=3.7024 m)에서의 결과(테스트 고정값):

| 목표 | looks(rg×az) | 픽셀(m) | 종횡비 |
|---|---|---|---|
| 20 m | 4×1 | 14.81 × 14.1 | 1.05 |
| 40 m | 10×3 | 37.02 × 42.3 | 1.14 |
| 80 m | 20×6 | 74.05 × 84.6 | 1.14 |

HyP3의 5×1(≈18.5×14.1 m, 종횡비 1.31)과 10×2(37×28 m, 1.31)는 상한 1.2를 넘어 선택되지 않는다. `max_aspect=1.35`면 20 m 목표에서 5×1이 선택됨을 테스트로 고정했다.

`spacing_stats()`는 스택 레코드의 중앙값 간격과 평균 입사각을 쓰고, 메타데이터가 전혀 없으면 위 ESA 공칭값을 쓰며 `nominal=True`로 표시한다(SEL-09 evidence에 기록).

## 결과

- HyP3 어댑터(Phase 2)는 자동 looks를 HyP3 옵션 중 픽셀 크기가 가장 가까운 것으로 스냅해야 하고, 실제 적용값을 매니페스트에 기록한다.
- 종횡비 상한 1.2 vs HyP3 관행(~1.3)은 연구자 확인 항목(`docs/open-questions.md`). 확인 결과에 따라 `DEFAULT_MAX_ASPECT`만 바꾸고 테스트 고정값을 갱신한다.
- 리포트(`precheck_report.*`)는 스택별 looks·픽셀·종횡비를 표로 보여준다.
