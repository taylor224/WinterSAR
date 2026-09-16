# ADR-0041: 기준점 추천 점수의 가중치 초기값과 MintPy 자동 선택 비교

- 상태(Status): 제안 (도메인 체크포인트 — 연구자 확인 전 기본값 변경 금지, 규칙 11.10)
- 날짜(Date): 2026-09-16
- 관련 ID: R-09 / 플랜 §5.6 `refpoint.py`, §12.1 기준점·재평탄화
- 검증 출처(Sources):
  - MintPy `src/mintpy/reference_point.py` (2026-09-16 WebFetch): `select_max_coherence_yx`, `random_select_reference_yx`,
    `reference_point_attribute`
  - MintPy `src/mintpy/defaults/smallbaselineApp.cfg` §3 reference_point (2026-09-16 WebFetch)
  - Yunjun, Fattahi, Amelung (2019) CAGEO §4.3 — cfg 주석이 인용하는 기준점 선택 지침

## 맥락

플랜 §5.6: 후보 픽셀 점수 = w1·평균 코히어런스 + w2·(연결성분이 AOI와 동일) + w3·(1 − |표고 − AOI 대표 표고|/범위)
+ w4·(1 − 거리/최대거리) + w5·(선형 속도 사전 추정치의 절대값이 작음). 상위 k개를 표로 제시하고 "MintPy 자동 선택
(코히어런스만)과 결과 비교 테스트". 가중치 값은 플랜에 없다.

## 검증한 사실

MintPy의 자동 규칙은 이름(`maxCoherence`)과 달리 **무작위**다:

```python
def select_max_coherence_yx(coh_file, mask=None, min_coh=0.85):
    """Select pixel with coherence > min_coh in random"""
    ...
    coh_mask = coh >= min_coh
    ...
    y, x = random_select_reference_yx(coh_mask, print_msg=False)
    #y, x = np.unravel_index(np.argmax(coh), coh.shape)
```

`smallbaselineApp.cfg`:

```
## auto - randomly select a pixel with coherence > minCoherence
## however, manually specify using prior knowledge of the study area is highly recommended
##   with the following guideline (section 4.3 in Yunjun et al., 2019):
## 1) located in a coherence area, to minimize the decorrelation effect.
## 2) not affected by strong atmospheric turbulence, i.e. ionospheric streaks
## 3) close to and with similar elevation as the AOI, to minimize the impact of spatially correlated atmospheric delay
mintpy.reference.maskFile      = auto   #[filename / no], auto for maskConnComp.h5
mintpy.reference.coherenceFile = auto   #[filename], auto for avgSpatialCoh.h5
mintpy.reference.minCoherence  = auto   #[0.0-1.0], auto for 0.85, minimum coherence for auto method
```

`reference_point_attribute`는 `REF_Y`, `REF_X`(지오코딩 시 `REF_LAT`, `REF_LON`)를 기록한다.

## 선택지

1. 균등 가중치(0.2 × 5).
2. 코히어런스·연결성분 우선(0.35 / 0.25), 표고·속도 보조(0.15 / 0.15), 거리 최소(0.10).
3. 코히어런스만(MintPy와 동일) + 연결성분 마스크.

## 결정

선택지 2를 **초기값**으로 채택한다(`DEFAULT_WEIGHTS`, 합 1):

| 성분 | w | 근거 |
|---|---|---|
| coherence | 0.35 | MintPy·Yunjun 2019 지침 1) — 가장 확실한 기준 |
| conncomp | 0.25 | 다른 연결성분의 기준점은 나머지 항이 아무리 좋아도 쓸 수 없음(MintPy maskConnComp 기본) |
| elevation | 0.15 | 지침 3) 층상 대기 지연(표고 상관) 완화 |
| velocity | 0.15 | 변형 중인 픽셀을 기준으로 잡으면 전체가 오프셋됨(§12.1 "안정적인 기준점") |
| distance | 0.10 | 지침 3) 난류 대기의 공간 상관 — 약한 타이브레이커 |

구현 규칙(`refpoint.py`):
- 각 성분은 [0, 1]로 정규화(표고: `1 − |h − median_AOI| / max_dev`, 거리: `1 − d / d_max`, 속도: `1 − |v| / v95`).
- 사용 가능한 성분만으로 가중치를 재정규화(DEM 없으면 표고 항 제외)해 점수가 항상 [0, 1].
- 후보는 체비셰프 거리 5 px 이상 떨어진 픽셀만 나열(한 덩어리의 이웃 픽셀로 목록이 채워지는 것 방지).
- `compare_with_mintpy_auto`는 MintPy 규칙이 무작위이므로 **후보 집합**(코히어런스 ≥ 0.85, mask==0 제외)의 크기·
  장면 대비 비율, 주석 처리된 `argmax` 변형, 우리 상위 후보가 그 집합에 속하는지를 보고한다.
- `apply_reference`는 기준 픽셀 시계열을 빼고 `REF_Y/REF_X/REF_LAT/REF_LON` 속성을 MintPy와 같은 이름으로 기록한다.

## 결과

- 가중치는 **도메인 체크포인트**: 연구자가 국내 사이트에서 후보 표를 검토·확정하기 전에는 바꾸지 않는다
  (open-questions 신규 행). 확정 후 이 ADR을 "채택"으로 갱신하고 `test_refpoint.py`의 순위 테스트를 갱신한다.
- 선택된 기준점은 `config.yaml`의 `timeseries.reference_point: [lat, lon]`으로 기록(CLI가 힌트 출력).
- MintPy 산출물(`avgSpatialCoh.h5`, `maskConnComp.h5`, `geometryGeo.h5`)을 읽는 로더는 `io.formats`(io 모듈) 담당.
