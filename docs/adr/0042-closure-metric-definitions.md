# ADR-0042: 위상 폐합(loop closure) 지표 정의와 의심 간섭도 판정

- 상태(Status): 채택 (임계값은 초기값)
- 날짜(Date): 2026-09-16
- 관련 ID: R-09 / R-11 / 플랜 §5.6 `closure.py`, §12.1 loop closure
- 검증 출처(Sources):
  - MintPy `src/mintpy/objects/stack.py::ifgramStack.get_design_matrix4triplet` (2026-09-16 WebFetch)
  - MintPy `src/mintpy/unwrap_error_phase_closure.py::calc_num_triplet_with_nonzero_integer_ambiguity` (동일)
  - Yunjun, Fattahi, Amelung (2019) CAGEO, eq. 8–9 (T_int) — MintPy docstring 인용
  - Zheng, Fattahi, Agram, Simons, Rosen (2022) IEEE TGRS "On Closure Phase and Systematic Bias in Multilooked SAR Interferometry" (wrapped closure bias 해석)

## 맥락

플랜 §12.1: "세 날짜의 간섭도를 순환 합산하면 이론상 0이어야 한다. 잔차가 큰 간섭도는 언래핑 오류가 의심되므로
자동 제외·보정한다." §5.6: 픽셀별·간섭도별 통계, 의심 간섭도 순위, MintPy phase-closure 보정 결과와 대조, 대시보드 JSON.

## 검증한 사실

MintPy 설계 행렬(부호 규약):

```python
# for 3 SAR acquisition in t1, t2 and t3 in time order,
# ifg1 for (t1, t2) with 1 / ifg2 for (t1, t3) with -1 / ifg3 for (t2, t3) with 1
closure_list = itertools.combinations(date_list, 3)   # sorted unique dates
row[idx12] = 1; row[idx23] = 1; row[idx13] = -1
```

정수 모호성과 T_int:

```python
closure_pha = np.dot(C, unw)
closure_int = np.round((closure_pha - ut.wrap(closure_pha)) / (2.*np.pi))
num_nonzero_closure = np.sum(closure_int != 0, axis=0)   # "T_int ... Yunjun et al. (2019, CAGEO)"
```

## 결정

정의(`wintersar.validate.closure`):

| 지표 | 정의 |
|---|---|
| `closure(i,j,k)` | φ_ij + φ_jk − φ_ik (MintPy와 같은 부호·삼각형 열거 순서) |
| `per_triplet_rms[t]` | 삼각형 t의 유효 픽셀에 대한 폐합 RMS (rad) |
| `per_pixel_rms[y,x]` | 픽셀의 모든 유효 삼각형에 대한 폐합 RMS (rad); 유효 삼각형이 없으면 NaN |
| `integer_multiples` | `round((closure − wrap(closure)) / 2π)` (unw 모드) — MintPy `closure_int` |
| `per_pixel_nonzero` | `Σ_t [integer_multiples ≠ 0]` — MintPy T_int (`numTriNonzeroIntAmbiguity`) |
| `per_triplet_nonzero_fraction[t]` | 삼각형 t에서 정수 모호성 ≠ 0인 유효 픽셀 비율 |
| `per_igram_score[p]` | unw 모드: 간섭도 p가 참여한 삼각형의 `nonzero_fraction` 평균; wrapped 모드: `per_triplet_rms` 평균 |
| `closure_coherence` | 픽셀별 \|mean_t exp(j·wrap(closure_t))\| — 시간적 코히어런스 대용(ADR-0044) |

모드:
- **unw**(기본, `stack.unw` 있을 때): 정수 2π 배수가 언래핑 오류의 증거. 대기·노이즈는 0 근처의 실수로 남는다.
- **wrapped**: 폐합을 (−π, π]로 감싼다. 0이 아닌 값은 멀티룩·필터링의 closure phase bias(Zheng 2022)나 노이즈이며
  언래핑 오류가 아니다. 합성 스택(노이즈 없음)은 두 모드 모두 폐합 0.

의심 간섭도(`suspicious`) — **탐욕적 박리(greedy peeling)**: 점수가 가장 높은 간섭도를 표시하고 그 간섭도가 속한
삼각형을 비활성화한 뒤 재계산을 반복한다. 한 간섭도의 오류가 같은 삼각형의 이웃 두 간섭도 점수를 함께 올리는
문제를 피한다(테스트: 한 간섭도에 2π 주입 → 그 간섭도만 표시).
- unw 임계: `nonzero_fraction > 0.05`(유효 픽셀의 5%, 초기값 `DEFAULT_SUSPICIOUS_FRACTION`).
- wrapped 임계: 점수 > 0.3 rad **그리고** 중앙값 + 3·MAD(1.4826 배) 초과, 간섭도 3개 이상일 때.

대시보드 JSON(`to_dashboard`): 모드·개수·임계값·요약(픽셀 RMS 평균/중앙값/p95, T_int 평균, 비영 픽셀 비율)·
픽셀 RMS 히스토그램·삼각형 표·간섭도 순위표·의심 목록. `write_closure_maps`는 `.npz`로 지도들을 저장한다.

## 결과

- MintPy phase-closure 보정 결과(`numTriNonzeroIntAmbiguity.h5`)와의 대조는 `io.formats`가 MintPy HDF5를 읽게 되면
  `per_pixel_nonzero`와 직접 비교한다(정의가 동일). 현재는 정의 일치만 문서화.
- 임계값 0.05 / 0.3 rad / 3·MAD는 초기값이며 실데이터 분포로 재조정한다(open-questions 신규 행). 변경 시 `test_closure.py`.
- 폐합 지표는 sweep(ADR-0044)의 `closure_rms`·`temporal_coherence`로 재사용된다.
