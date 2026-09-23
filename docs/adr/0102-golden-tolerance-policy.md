# ADR-0102: 골든 통계 비교의 허용 오차 정책

- 상태(Status): 채택 (플랫폼 간 첫 야간 실행으로 확인 예정)
- 날짜(Date): 2026-09-23
- 관련 ID: 플랜 §8 "수치 비교는 허용 오차를 명시(`np.testing.assert_allclose`)", ADR-0100, ADR-0101, 규칙 11.4
- 검증 출처(Sources):
  - 구현 `src/wintersar/bench/golden.py::Tolerances`, `compare_golden`; 테스트
    `tests/unit/bench/test_golden.py::test_tolerances_*`, `test_missing_and_extra_keys_are_mismatches`
  - `numpy.isclose` 의 판정식 `|a - b| <= atol + rtol * |b|`(b = 기준값)와 같은 형태를 골든값을 b 로 두고 사용
    <https://numpy.org/doc/stable/reference/generated/numpy.isclose.html>
  - fake engine 산출물 dtype: `float32` 스택(`src/wintersar/engines/fake.py`), 골든 통계는 float64 누적
    (`golden.array_stats`)
  - 같은 머신 두 번 실행 = 바이트 동일(ADR-0100). 플랫폼 간(macOS arm64 → Linux x86_64) 차이는 아직 실측하지
    못함 → `docs/open-questions.md` 행 참조

## 맥락 (Context)

골든은 macOS 에서 만들고 CI(Linux) 에서 비교한다. numpy 의 초월함수·FFT·합산은 플랫폼/SIMD 에 따라 마지막
비트가 다를 수 있고, float32 로 저장되는 스택에서는 일부 픽셀이 1 ulp(상대 약 6e-8) 만큼 다를 수 있다. 반면
잡아야 할 변화(생성기·엔진·지표 정의 변경)는 통계를 그보다 몇 자릿수 크게 움직인다. 정수·문자열·목록은 노이즈가
없으므로 정확히 같아야 한다. 정책은 "무엇이 정확 비교이고 무엇이 근사 비교이며 그 폭은 얼마인가"를 한 곳에 둔다.

## 선택지 (Options)

1. 전부 정확 비교 — 플랫폼 간 마지막 자리 차이로 실패, 골든을 CI 러너에서 만들어야 함.
2. 전부 근사 비교(넓은 rtol) — 쌍 수·Finding 목록의 변화를 놓칠 수 있다.
3. **종류별 정책**: 비-float 정확, float 은 `rtol`/`atol`, 픽셀 비율은 절대 오차만.

## 결정 (Decision)

선택지 3 (`Tolerances(rtol=1e-6, atol=1e-9, fraction_atol=1e-4)`).

| 값의 종류 | 비교 | 근거 |
|---|---|---|
| 정수·불리언·문자열·`None`·목록 길이 (n_pairs, n_dates, shape, dtype, pairs/dates 문자열, status, engine, rule_id 목록, site 블록) | **정확** | 노이즈가 없다; 하나라도 다르면 정의가 바뀐 것 |
| 키 집합 | **정확** (누락·추가 모두 불일치) | 스키마 변경은 재생성으로만 |
| float (min/max/mean/std/백분위, closure_rms, wrapped_abs_mean) | `\|a−b\| ≤ 1e-9 + 1e-6·\|b\|` | float32 1 ulp 노이즈(≈6e-8 상대)가 96×96 통계에 남기는 영향은 1e-8 이하; 알고리즘 변경은 1e-3 이상 |
| `*_fraction` (mask/masked/nan/conncomp_nonzero/unwrap_error_fraction) | `\|a−b\| ≤ 1e-4` (rtol 0) | 임계값(코히어런스 0.3) 경계의 픽셀 하나가 ulp 로 뒤집히면 1/129024 ≈ 7.8e-6 이 움직인다. 1e-4 는 약 13 픽셀까지 흡수하고, 0.0 인 비율(unwrap_error_fraction)은 rtol 로 비교할 수 없으므로 절대 오차만 쓴다 |
| NaN / inf | NaN 은 NaN 과만 같음, inf 는 정확 | 판정식이 NaN 에서 항상 거짓이므로 명시 |
| int ↔ float | float 규칙 | JSON 왕복에서 `0` 과 `0.0` 을 구분하지 않기 위해 |

- 판정식은 골든값을 기준(b)으로 하는 `numpy.isclose` 형태. 골든에는 반올림을 하지 않는다(ADR-0100).
- 스크립트는 `--rtol/--atol/--fraction-atol` 로 폭을 바꿔 볼 수 있지만 테스트와 CI 는 기본값만 쓴다.
- 시간 예산: 회귀 테스트 모듈은 `@pytest.mark.slow` + `timeout(90)`; 실행 자체는 1초 미만이므로 예산은
  "느려짐"이 아니라 "멈춤"을 잡는 상한이다(수치는 적지 않음, 11.8).

## 결과 (Consequences)

- 플랫폼 간 첫 야간 실행에서 float 불일치가 나오면 (a) 값이 1e-6 상대 이내인지 `GOLDEN-001` 의 evidence 로
  확인하고, (b) 노이즈로 판명되면 `rtol` 을 1e-5 로 넓히는 것으로 이 ADR 을 갱신한다. 그 이상 차이는 노이즈가
  아니라 플랫폼 의존 코드(스레드 수·SIMD 에 따른 합산 순서)이므로 생성기 쪽을 고친다.
- 임계값 경계 픽셀에 의한 `*_fraction` 흔들림이 1e-4 를 넘으면 합성 코히어런스 생성기가 경계 근처 값을 너무
  많이 만드는 것이므로 사이트 정의(threshold)를 조정한다.
- 새 통계 키를 추가할 때는 이름으로 정책이 결정된다: 비율은 `_fraction` 접미사, 그 외 float 는 기본 rtol.
