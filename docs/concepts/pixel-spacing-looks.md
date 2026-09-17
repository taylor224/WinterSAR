# 픽셀 간격과 looks — "애지머스가 짜부됐다"는 결함이 아니다 (SEL-09, R-03)

Sentinel-1 IW SLC 의 픽셀은 **slant range 방향 약 2.3 m × 애지머스 방향 약 14 m** 로 비대칭입니다
(공간 해상도로는 약 5 × 20 m). 이는 TOPS 촬영 방식의 설계 결과이며 **항상** 그렇습니다 — 어떤 씬에서만
가끔 생기는 문제가 아닙니다. 멀티룩 전의 SLC 를 그대로 띄우면 애지머스 방향이 길게 눌린(1:6 정도)
영상이 보이는 것이 정상입니다.

## looks 로 정사각 근사 픽셀 만들기

멀티룩(multilook)은 레인지·애지머스 방향으로 각각 `rg_looks × az_looks` 픽셀을 평균해 잡음을 줄이고
픽셀을 정사각에 가깝게 만드는 과정입니다. 레인지 방향은 지상 거리로 환산해야 합니다:

```
ground range spacing = slant range spacing / sin(입사각 θ)
```

예를 들어 θ ≈ 39° 이면 2.3 m / sin 39° ≈ 3.7 m 이므로, 목표 픽셀 40 m 를 위해 레인지 looks 를 애지머스보다
훨씬 크게(예: 10 × 3 → 37 m × 42 m) 줘야 합니다. 레인지 looks 를 4:1, 5:1 처럼 더 주는 것이 표준이며
"애지머스만 늘리는" 방향이 아닙니다.

## wintersar 에서

- `engine.looks: auto`(기본) 이면 `select.looks` 가 `range_pixel_spacing_m`, `azimuth_pixel_spacing_m`,
  중앙 입사각, `engine.target_pixel_m`(기본 40 m)로부터 **종횡비 ≤ 1.2** 인 `(rg_looks, az_looks)` 를
  고릅니다(ADR-0015). 결과와 실제 종횡비는 precheck 리포트의 `SEL-09` INFO 문구에 나옵니다.
- `SEL-09` WARN 은 스택 안에서 픽셀 간격이 날짜마다 다를 때(허용 편차 `selection.pixel_spacing_tolerance`,
  기본 1 %) — 서로 다른 처리 설정의 씬이 섞였다는 신호입니다.
- 픽셀 간격·IPF 버전은 검색 시점에 없을 수 있어(ADR-0010) 명목값으로 대체될 수 있습니다; 리포트에 출처를
  표시합니다(open-questions #27).
- 직접 지정하려면 `engine.looks: [10, 3]` 처럼 `[rg, az]` 를 씁니다. HyP3 는 제공하는 looks 옵션 범위
  안에서만 고를 수 있습니다(ADR-0020).

관련: ADR-0015(알고리즘·고정 테스트값), [언래핑 타일](unwrap-tiling-multiresolution.md)(멀티룩 후 픽셀 수가
메모리 모델의 입력).

## English summary

IW SLC pixels are about 2.3 m in slant range by 14 m in azimuth (resolution roughly 5 x 20 m) by
design, always, so an azimuth-squashed SLC is normal, not a defect. Multilooking averages
`rg_looks x az_looks` pixels; because ground range spacing is slant spacing / sin(incidence),
range looks must be several times the azimuth looks (e.g. 10 x 3 for a 40 m target at ~39
degrees). `engine.looks: auto` picks the pair closest to `engine.target_pixel_m` with aspect ratio
<= 1.2 (ADR-0015); `SEL-09` warns when pixel spacing varies within a stack.
