# 레이오버 · foreshortening · 셰도우 — 측면 관측 레이더의 거리 기하 왜곡 (R-04, SEL-12)

SAR는 위성 진행 방향의 옆(오른쪽)을 비스듬히 내려다보며 **되돌아오는 시간(거리)** 순서로 지표를 배열한다.
그래서 지형의 경사가 "센서를 향하느냐, 등지느냐"에 따라 같은 산이 전혀 다르게 찍힌다. 도플러 효과로
설명하는 것은 부정확하다 — 이것은 순수하게 slant range(경사거리) 기하의 문제다.

```
   센서 ⟶ 관측 방향 (look azimuth = heading + 90°, Sentinel-1은 right-looking)
    \
     \ θ (입사각)
      \
       \        ▲ 산 정상
        \      / \
         \    /   \
          \  /     \          ← 센서를 향한 사면(앞 사면)       ← 등진 사면(뒤 사면)
   ─────────┴───────┴─────────
        foreshortening ↔ layover                  shadow
```

| 현상 | 조건 (경사 s, 입사각 θ, 레인지 방향 경사 성분 α_r) | 영상에서 | 위상 사용 |
|---|---|---|---|
| foreshortening | 앞 사면, `0 < α_r < θ` | 사면이 실제보다 짧게 압축됨 (지수 `F = 1 − sin θ_loc / sin θ`, 0~1) | 가능하나 픽셀당 지형 변화가 커서 언래핑·DEM 잔차 위험 |
| **layover(레이오버)** | 앞 사면, `α_r > θ` | 정상이 산기슭보다 센서에 가까워 **먼저** 되돌아옴 → 순서 뒤집힘, 여러 지점이 한 픽셀에 겹침 | **불가** |
| **shadow(셰도우)** | 뒤 사면, `α_r < −(90° − θ)` | 신호가 닿지 않음 → 잡음만 | **불가** |

`θ_loc`(국지 입사각)은 지형 법선과 시선 사이의 각도로, 평지에서 θ, 앞 사면에서 작아지고(0°에 가까우면
레이오버), 뒤 사면에서 커진다(90°를 넘으면 조명되지 않음).

## 왜 상승/하강 궤도에서 다르게 나오나

Sentinel-1은 오른쪽을 본다. 상승 궤도(NNW로 진행, heading ≈ −12°)는 **동쪽**을 관측하므로 센서는 지표의
서남서쪽에 있고, **서향 사면**이 앞 사면이 된다. 하강 궤도(SSW로 진행, heading ≈ 192°)는 **서쪽**을 관측하므로
**동향 사면**이 앞 사면이다. 따라서 남북으로 뻗은 능선은 상승에서는 서쪽 비탈이, 하강에서는 동쪽 비탈이
레이오버가 된다(합성 능선 테스트 `tests/unit/select_geometry/test_geometry_masks.py::test_ridge_layover_flank_flips_between_directions`).

```
상승(look ENE)                        하강(look WNW)
센서 ↘                                        ↙ 센서
   layover ▲ shadow                  shadow ▲ layover
  ────────/ \────────               ────────/ \────────
```

실무 지침:

1. `wintersar`의 사전검증(SEL-12)은 AOI DEM으로 두 방향의 마스크를 모두 계산해 레이오버+셰도우 비율이 낮은
   궤도 방향을 추천한다. 급경사 사이트에서는 이 비율 차이가 10 %p 이상 나기도 한다.
2. 두 방향을 모두 쓸 수 있으면 각각 처리하라. 마스크는 서로 보완적이고, LOS 분해(동서·수직 성분)에도 필요하다.
3. 레이오버·셰도우 픽셀은 언래핑 전에 마스크로 제외한다(`unwrap` 스케줄러의 마스크 입력, PERF-04). foreshortening
   지수가 높은 픽셀은 제외하지 않되 기준점으로 쓰지 않는다.
4. ISCE2 경로에서는 `geom_reference/IW*/shadowMask_NN.rdr`(1 = 레이오버, 2 = 셰도우, 3 = 둘 다)가 있으면 그것을
   우선 사용한다. ISCE2는 앞 사면에 **가려지는** 뒤쪽 지역(수동 레이오버·셰도우)까지 표시하므로 자체 마스크보다
   넓을 수 있다(ADR-0019).

## 각도 규약 요약 (ADR-0017)

- 방위각은 북에서 시계방향. `heading` = 진행 방향, `look azimuth = heading + 90`, 지표→센서 방위 = `heading + 270`.
- `aspect` = 사면이 향하는(내리막) 방위. `slope` = 경사각.
- 국지 입사각: `cos θ_loc = cos s cos θ + sin s sin θ cos(φ_sen − a)`, φ_sen = 지표→센서 방위.
  (look azimuth로 쓰면 마지막 항의 부호가 바뀐다 — 흔한 실수.)
- 레인지 방향 경사: `α_r = atan(tan s · cos(φ_sen − a))`, 양수 = 센서를 향함.

## 코드

```python
from wintersar.select.geometry_masks import (
    masks_for_both_directions, recommend_direction, write_mask_geotiff, compute_from_dem_file,
)
res = masks_for_both_directions(dem, dx=30, dy=30, incidence_deg=39.0)   # 기본 heading −12 / 192
print(recommend_direction(res), res["ASCENDING"].stats)
r = compute_from_dem_file("dem.tif", aoi_wkt, heading_deg=-12.0, incidence_deg=39.0, flight_direction="ASCENDING")
write_mask_geotiff(r, "masks_asc.tif")   # 4밴드 float32 + masks_asc_ls_map.tif (uint8, ISCE2 인코딩)
```

DEM은 `wintersar.select.dem.get_dem(aoi_wkt, cache_dir)`로 받는다(Copernicus GLO-30 기본, `sardem` 필요,
캐시 `<cache_dir>/dem/`; ADR-0018).

## 참고

- Kropatsch & Strobl 1990, IEEE TGRS 28(1):98–107, doi:10.1109/36.45752 — 레이오버·셰도우 맵 생성
- Ulander 1996, IEEE TGRS 34(5):1115–1122, doi:10.1109/36.536527 — 국지 입사각·경사 보정
- Vollrath, Mullissa & Reiche 2020, Remote Sens. 12(11):1867, doi:10.3390/rs12111867 — 레인지/애지머스 경사 분해, GEE 구현
- ESA Sentinel-1 instrument payload (right-looking): https://sentinel.esa.int/web/sentinel/missions/sentinel-1/instrument-payload
- HyP3 RTC 제품 가이드(레이오버·셰도우 정의): https://hyp3-docs.asf.alaska.edu/guides/rtc_product_guide/
