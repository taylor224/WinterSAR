# ADR-0062: SHP(통계적 동질 픽셀) 검정 선택 — 두 표본 KS 검정(점근 임계값), 창 ≤ 15

- 상태(Status): 채택 (기본값은 연구자 확인 대기, 오픈 항목 #51)
- 날짜(Date): 2026-09-16
- 관련 ID: R-07, PERF-04
- 검증 출처(Sources):
  - Ferretti, Fumagalli, Novali, Prati, Rocca, Rucci 2011, "A New Algorithm for Processing
    Interferometric Data-Stacks: SqueeSAR", IEEE TGRS 49(9):3460–3470, doi:10.1109/TGRS.2011.2124465
    (KS 검정으로 SHP 선택) — Crossref 메타데이터 확인
  - Parizzi, Brcic 2011, "Adaptive InSAR Stack Multilooking Exploiting Amplitude Statistics: A
    Comparison Between Different Techniques and Practical Results", IEEE GRSL 8(3):441–445,
    doi:10.1109/LGRS.2010.2083631 (KS·AD·t 검정 등 비교) — Crossref 확인
  - dolphin `src/dolphin/shp/_ks.py` —
    https://raw.githubusercontent.com/isce-framework/dolphin/main/src/dolphin/shp/_ks.py
    (`estimate_neighbors(amp_stack, halfwin_rowcol, alpha, ...)`, ECDF 최대 차이, 중심 픽셀은
    이웃 마스크에서 제외)
  - scipy 1.17 `scipy/stats/_stats_py.py` `ks_2samp` 점근 분기: `en = m·n/(m+n)`,
    `prob = kstwobign.sf(sqrt(en)·d)`; `kstwobign.isf(0.05) = 1.3581`, `isf(0.01) = 1.6276`
    (`.venv/lib/python3.11/site-packages/scipy/stats/_stats_py.py`, `_continuous_distns.py`)
  - scipy `ttest_ind(a, b, equal_var=False)`(Welch) — 같은 파일
  - 구현: `src/wintersar/research/repr_phase.py`(`ks_statistic`, `ks_critical_value`,
    `t_test_accept`, `shp_neighbors`, `shp_adaptive_multilook`, `repr_shp`), 테스트
    `tests/unit/research/test_repr_phase.py::test_ks_statistic_matches_scipy_*`,
    `::test_shp_selects_more_neighbours_inside_region_than_across_boundary`

## 맥락 (Context)

플랜 §5.7 `shp`: "통계적 동질 픽셀(SHP) 기반 적응 멀티룩(진폭 유사성 검정)". 문헌은 진폭
시계열의 분포 동일성 검정을 쓴다. 창은 15×15 이하로 제한(작업 지시). 벡터화된 numpy 구현이
필요하다(dolphin 미설치).

## 선택지 (Options)

1. **두 표본 Kolmogorov–Smirnov 검정(채택, 기본)** — SqueeSAR 원본, 분포 무가정, 진폭이 연속이라
   동점 문제 없음. 점근 임계값 `D_crit = K_α · sqrt((n+m)/(nm))`.
2. Welch t 검정(옵션 `test="t"`) — Parizzi & Brcic가 실용적이라 평가; 평균만 비교하므로 빠르다.
   `scipy.stats.ttest_ind(equal_var=False)`와 픽셀별 동일 결과.
3. GLRT / Anderson–Darling — 미구현(후속).

## 결정 (Decision)

- `ks_statistic(a, b)`: 두 표본을 합쳐 안정 정렬한 뒤 ±1/n, ∓1/m 계단의 누적합 최댓값 =
  `sup|F_a − F_b|`. 창의 모든 오프셋에 대해 벡터화(창² 회 호출, 각 호출은 (n, NY, NX) 전체).
  scipy `ks_2samp(...).statistic`과 1e-12 이내 일치(테스트).
- 임계값: `K_α = kstwobign.isf(α)`(scipy 점근 분포), `α = 0.05` 기본. 소표본에서는 보수적
  (기각률 < α) — 실데이터 확인 항목(#51). iid 검사에서 채택률 ≈ 0.966(n = 30).
- 이웃 마스크 `(NY, NX, w, w)`: 블록 **중심 픽셀** 기준, 중심은 항상 포함(dolphin은 제외하지만
  가족이 비지 않게 하려는 선택), 영상 밖 오프셋은 제외. `w`는 홀수, 3 ≤ w ≤ 15.
- `repr_shp` 모드: `centre`(기본, 블록당 1가족 — 빠름) / `pixelwise`(전 픽셀 가족 후 블록 평균,
  SqueeSAR의 DS 필터링에 가까움, factor² 배 비용). 기본 창 = factor 이상 최소 홀수(멀티룩과
  비슷한 지지 영역; 넓히는 것은 명시적 선택).
- 검증: 스케일 1:4의 두 영역에서 경계를 걸치는 블록의 가족 크기가 내부 블록보다 작고, 선택된
  이웃의 ≥ 90 %가 중심과 같은 영역(테스트).

## 결과 (Consequences)

- 창이 클수록 위상 기울기가 큰 곳(급경사 변형·고프린지)에서 평균이 편향된다 — 멀티룩과 같은
  트레이드오프이며 실험 표로 판단한다.
- 후속: GLRT(dolphin 기본) 추가, NaN/마스크 픽셀 처리, 소표본 정확 임계값(scipy `kstwo`) 옵션.
- 오픈 항목 #51(기본값 α·창의 실데이터 검증).
