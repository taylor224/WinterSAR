# ADR-0040: LOS 투영 부호·헤딩 규약 (MintPy `enu2los` 동일)

- 상태(Status): 채택 (도메인 체크포인트 — 연구자 확인 대기, 규칙 11.10)
- 날짜(Date): 2026-09-16
- 관련 ID: R-10 / 플랜 §5.6 `los.py`, §12.1 기준점·재평탄화 / open-questions #7
- 검증 출처(Sources):
  - MintPy `src/mintpy/utils/utils0.py` (2026-09-16 WebFetch, raw.githubusercontent.com/insarlab/MintPy/main):
    `heading2azimuth_angle`, `azimuth2heading_angle`, `enu2los`, `get_unit_vector4component_of_interest`
    — 소스 열람만, `import mintpy` 금지(규칙 11.2, GPL-3)
  - ADR-0017 (heading 정의: 북에서 시계방향, Sentinel-1 중위도 기본값 상승 −12°, 하강 192° ≡ −168°)
  - `src/wintersar/io/timeseries.py` docstring: `displacement_m`는 "positive = towards the satellite"

## 맥락

대조군(수준측량·GNSS)은 ENU로 주어지고 InSAR 시계열은 LOS다. 부호 하나만 틀려도 융기와 침하가 뒤바뀌므로
플랜 §5.6은 "MintPy `enu2los`와 동일하게 맞추고 단위테스트로 고정(상승/하강 각각 알려진 케이스)"을 요구한다.
open-questions #7("MintPy enu2los 부호·헤딩 규약")을 여기서 닫는다.

## 검증한 사실 (MintPy 소스 인용)

`heading2azimuth_angle(head_angle, look_direction='right')`:

```python
if look_direction == 'right':
    az_angle = (head_angle - 90) * -1
else:
    az_angle = (head_angle + 90) * -1
az_angle -= np.round(az_angle / 360.) * 360.
```

`enu2los(v_e, v_n, v_u, inc_angle, head_angle=None, az_angle=None)` docstring:

- `head_angle` — "azimuth angle of the SAR platform along track direction measured from the north with
  clockwise direction as positive"
- `az_angle` — "azimuth angle of the LOS vector from the ground to the SAR platform measured from the north
  with anti-clockwise direction as positive"
- 반환 `v_los` — "displacement in LOS direction, **motion toward satellite as positive**"

```python
v_los = (  v_e * np.sin(np.deg2rad(inc_angle)) * np.sin(np.deg2rad(az_angle)) * -1
         + v_n * np.sin(np.deg2rad(inc_angle)) * np.cos(np.deg2rad(az_angle))
         + v_u * np.cos(np.deg2rad(inc_angle)))
```

`get_unit_vector4component_of_interest(..., comp='enu2los')` → `[-sin(inc)·sin(az), sin(inc)·cos(az), cos(inc)]`;
`comp='u2los'` → `[0, 0, cos(inc)]`.

`azimuth2heading_angle` docstring의 예시값: "ascending orbit: heading angle of -12 and azimuth angle of 102;
descending orbit: heading angle of -168 and azimuth angle of -102".

## 선택지

1. MintPy 공식을 그대로 복사(heading → az_angle 변환 포함). MintPy 산출물(`asc_desc2horz_vert`, `view.py`)과 값이 일치.
2. ADR-0017의 `sensor_azimuth = heading + 270`(시계방향)으로 자체 유도. 수학적으로 동치이지만 각도 정의가 달라 혼동 위험.
3. "positive = away from satellite"(range 증가 = 침하 양수) 규약. SARscape 등 일부 도구 관행이나 MintPy·TimeSeries 컨테이너와 충돌.

## 결정

선택지 1. `wintersar.validate.los`는 위 공식을 그대로 구현한다(`heading_to_azimuth`, `los_unit_vector`,
`enu_to_los`, `vertical_to_los`, `los_to_vertical`). LOS 양수 = 위성 방향(range 감소).

유도되는 부호(단위테스트 `tests/unit/validate/test_los.py`로 고정, 입사각 39°):

| 지반 운동 | 상승(heading −12°, az 102°) | 하강(heading 192° ≡ −168°, az −102°) |
|---|---|---|
| 순수 융기 1 m | **+cos 39° = +0.777** | **+0.777** |
| 순수 동향 1 m | −sin 39°·sin 102° = **−0.616** | −sin 39°·sin(−102°) = **+0.616** |
| 순수 북향 1 m | sin 39°·cos 102° = **−0.131** | **−0.131** |

해석: 오른쪽 관측(right-looking) 위성은 상승 궤도에서 동쪽을, 하강 궤도에서 서쪽을 본다. 동향 운동은
상승 궤도에서는 위성에서 멀어지므로 LOS 음수, 하강 궤도에서는 다가오므로 양수. 융기는 어느 궤도든 위성에
가까워지므로 양수.

`los_to_vertical(los, inc) = los / cos(inc)`는 **순수 수직 운동 가정**(MintPy `u2los`의 역)이며 수평 성분이
있으면 상승+하강 분해를 써야 한다(문서화, 미구현).

## 결과

- 도메인 체크포인트(규칙 11.10): 위 표는 MintPy 소스에서 기계적으로 유도한 것이며 **연구자가 실데이터
  (알려진 GNSS 지점의 상승/하강 LOS 시계열)로 부호를 확인하기 전까지 기본값을 바꾸지 않는다**.
  open-questions #7을 "진행 중 → ADR-0040(연구자 확인 대기)"로 둔다.
- `TimeSeries.displacement_m`, `research.synth`(`PHASE_PER_M_LOS = −4π/λ`, 위상 = −(4π/λ)·LOS 변위), fake 엔진,
  `validate.metrics`는 모두 같은 규약(양수 = 위성 방향)을 쓴다. 규약을 바꾸려면 이 ADR 개정 + `test_los.py` 갱신.
- heading 사이트별 값(±1~3°)은 open-questions #13(ADR-0017)의 후속. LOS 투영 오차는 sin(inc)·Δaz ≈ 3% 이하.
