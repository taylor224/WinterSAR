# ADR-0017: 기하 마스크의 각도·부호 규약 (heading, look azimuth, aspect, 국지 입사각)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-04 / SEL-12 / 플랜 §5.1.5, §12.1
- 검증 출처(Sources):
  - MintPy `utils0.py` (heading 정의·예시값): https://raw.githubusercontent.com/insarlab/MintPy/main/src/mintpy/utils/utils0.py
    — "head_angle - the azimuth angle of the SAR platform's orbit (along-track direction) measured
    from the north, with clockwise as positive"; 근극궤도 예시 `head_angle = -12`(상승), `-168`(하강);
    `heading2azimuth_angle`: right-looking이면 `az_angle = (head_angle - 90) * -1`. (소스 열람만, import 금지 — 규칙 11.2)
  - ESA Sentinel-1 Instrument Payload: https://sentinel.esa.int/web/sentinel/missions/sentinel-1/instrument-payload
    — "right-looking active phased array antenna"
  - SentiWiki S1 Mission: https://sentiwiki.copernicus.eu/web/s1-mission — 궤도 경사 98.18°, 고도 693 km, 태양동기, 12일 주기
  - ISCE2 `topozero.f90`: https://raw.githubusercontent.com/isce-framework/isce2/main/components/zerodop/topozero/src/topozero.f90
    — `costheta = (enu(1)*alpha + enu(2)*beta - enu(3)) / sqrt(1 + alpha² + beta²)` (법선·시선 내적으로 국지 입사각)
  - ESA-PhiLab radiometric-slope-correction (Vollrath, Mullissa & Reiche 2020, Remote Sens. 12(11):1867, doi:10.3390/rs12111867) 코드:
    https://raw.githubusercontent.com/ESA-PhiLab/radiometric-slope-correction/master/javascript/slope_correction_module.js
    — `phi_r = phi_i - phi_s`, `alpha_r = atan(tan(alpha_s)·cos(phi_r))`, 유효 조건 `alpha_r < theta_i`(레이오버 아님),
    `alpha_r > -(90° - theta_i)`(셰도우 아님). `phi_i`는 입사각 밴드의 aspect(= near range, 즉 센서 쪽 방위).
  - Ulander 1996, "Radiometric slope correction of synthetic-aperture radar images", IEEE TGRS 34(5):1115–1122,
    doi:10.1109/36.536527 (국지 입사각 고전 공식)
  - Kropatsch & Strobl 1990, "The generation of SAR layover and shadow maps from digital elevation models",
    IEEE TGRS 28(1):98–107, doi:10.1109/36.45752 (레이오버·셰도우 정의)
  - `asf_search` 14.0.0 소스 grep: `heading` 필드 없음 (`.venv/lib/python3.11/site-packages/asf_search/`)

## 맥락

`select/geometry_masks.py`는 DEM과 궤도 기하로 레이오버·셰도우·foreshortening을 계산한다(R-04, SEL-12).
각도 규약을 하나라도 틀리면 상승/하강 마스크가 뒤바뀌므로(플랜 Phase 1 DoD "상승/하강 결과가 지형에
맞게 다르게 나옴") 규약과 부호를 출처와 함께 고정해야 한다. 과제 지시문의 공식
`cos θ_loc = cos s cos θ + sin s sin θ cos(φ − a)`는 φ의 정의에 따라 부호가 달라지므로 검증이 필요했다.

## 선택지

1. φ = look azimuth(센서→지표, heading + 90)로 두고 지시문 공식을 그대로 사용.
2. φ = 지표→센서 방위(heading + 270)로 두고 `+` 공식을 사용, look azimuth는 별도 함수로 제공.
3. 각도 공식을 버리고 ENU 벡터 내적으로만 계산.

## 결정

선택지 2 (내부 구현은 3과 동치인 폐형식).

규약(모두 도 단위, 방위각은 북에서 시계방향):

| 항목 | 정의 | 값·근거 |
|---|---|---|
| `heading_deg` | 플랫폼 진행 방향 방위각 | MintPy 정의와 동일. Sentinel-1 중위도 기본값 상승 −12°, 하강 192°(≡ −168°) |
| `look_azimuth` | 센서→지표 관측 방향(across-track) | right-looking이므로 `heading + 90` (ESA: Sentinel-1은 right-looking) |
| `sensor_azimuth` φ_sen | 지표→센서 방위 | `look_azimuth + 180 = heading + 270` |
| `aspect` a | 사면이 향하는(내리막) 방위 | GIS 규약(gdaldem, ee.Terrain.aspect). 평지는 0 |
| `slope` s | 경사각 | `np.gradient` 중앙차분, 북쪽이 위인 배열(`dy_m > 0`), 남쪽이 위면 `dy_m < 0` |
| `incidence_deg` θ | 평지 입사각 | IW 약 29–46° |

국지 입사각(유도): 상향 법선 `n = (sin s sin a, sin s cos a, cos s)`, 센서 방향 단위벡터
`u = (sin θ sin φ_sen, sin θ cos φ_sen, cos θ)` → `cos θ_loc = n·u = cos s cos θ + sin s sin θ cos(φ_sen − a)`.
이는 Ulander(1996)의 고전 공식이며 ISCE2 `topozero.f90`의 `costheta`(하향 법선 `(∂z/∂E, ∂z/∂N, −1)`과
하향 시선의 내적)와 정확히 같다. **look azimuth로 쓰면 마지막 항의 부호가 `−`로 바뀐다** — 지시문 공식은
φ가 "지표→센서 방위"일 때만 성립하므로 코드는 `sensor_azimuth()`를 명시적으로 사용한다.
검산: s = 10°, θ = 39°에서 센서를 향한 사면 θ_loc = 29°, 반대 사면 49° (`test_local_incidence_facing_away_and_perpendicular`).

레이오버·셰도우는 레인지 방향 경사 성분 `α_r = atan(tan s · cos(φ_sen − a))`(양수 = 센서를 향함)로 판정한다
(Vollrath 2020 코드와 동일, `phi_i`가 near-range 방위이므로 우리 φ_sen과 같음):

- 레이오버: `α_r > θ` (경계 검증: θ = 39°에서 38.9° 미검출, 39.1° 검출)
- 셰도우: `α_r < −(90° − θ)` (경계 검증: 50.9° 미검출, 51.1° 검출, 이때 θ_loc이 90°를 넘음)
- foreshortening 지수: `F = clip(1 − sin θ_loc / sin θ, 0, 1)`, 레이오버 픽셀은 1. 평지 0.

Vollrath 2020의 `θ_lia = acos(cos α_az · cos(θ − α_r))`는 `tan⁴ s` 차수의 근사이므로 채택하지 않고 정확한 내적식을
쓴다(문서화만).

Sentinel-1 heading 기본값(−12°/192°)의 근거: 경사 98.18° 역행 궤도의 적도 지상궤적 방위는 약 ±8°이고
지구 자전과 위도 증가로 중위도에서 약 −12~−15°(상승), 192~195°(하강)가 된다. MintPy 예시값(−12/−168)과
문헌 진술("around −15° ascending / −165° descending")이 이 범위에 있다. 정확한 값은 사이트·위도에 따라
1~3° 다르며 마스크 결과에 미치는 영향은 작지만, 궤도 파일(POEORB) 상태벡터에서 사이트별 heading을 계산하는
함수는 미확정 항목으로 남긴다(`docs/open-questions.md` #13). `asf_search` 메타데이터에는 heading이 없음을 확인했다.

## 결과

- `look_azimuth()`, `sensor_azimuth()`, `slope_aspect()`, `local_incidence()`, `range_slope()`가 위 규약을 구현하고
  docstring에 출처를 적는다. 규약 변경은 골든 파일(`tests/regression/golden/geometry/ridge_masks.npz`) 재생성과
  이 ADR 개정을 동반해야 한다(규칙 11.4, 11.10).
- 상승(look ENE)은 서향 사면, 하강(look WNW)은 동향 사면을 레이오버로 만든다 — 합성 능선 테스트로 고정.
- Phase 4 `validate.los`(MintPy enu2los, open-questions #7)와 LOS 부호를 맞출 때 이 ADR의 heading 정의를 기준으로 한다.
