# 기준점(reference point)과 재평탄화(deramp) — 언래핑 위상은 상대값이다 (R-09, KB-MINTPY-001/004)

언래핑된 간섭 위상은 **어떤 픽셀을 0 으로 두느냐**에 따라 전체가 오르내리는 상대값입니다. 시계열
역산(SBAS)도 마찬가지라, 모든 변위는 기준점(reference point) 대비 값입니다. 기준점이 스스로 움직이거나
잡음이 크면 그 오차가 전체 지도에 그대로 더해집니다.

좋은 기준점의 조건:

1. **안정**할 것 — 침하·융기가 없다고 믿을 수 있는 곳(암반, 오래된 구조물). 선형 속도 사전 추정치가 작을수록 좋습니다.
2. **코히어런스가 높을** 것 — 잡음이 작아야 기준 오차가 작습니다.
3. 관심 영역(AOI)과 **같은 연결성분** 안에 있을 것 — 언래핑이 끊긴 다른 섬에 기준점을 두면 AOI 값이 무의미합니다.
4. AOI 와 **표고가 비슷**할 것 — 대류권 지연은 고도에 따라 달라 표고 차이가 크면 기준점 자체가 대기 신호를 품습니다.
5. AOI 에서 너무 멀지 않을 것 — 긴 파장 오차(궤도·대기)가 거리에 비례해 커집니다.

## SARscape 의 refinement/re-flattening 에 대응하는 것

SARscape 는 GCP(지상 기준점)로 오프셋·위상 램프를 제거하는 refinement / re-flattening 단계를 둡니다.
MintPy 에서는 이것이 두 단계로 나뉩니다:

- **reference point**(`mintpy.reference.yx/lalo`) — 오프셋 제거. MintPy 의 자동 선택은 평균 공간
  코히어런스가 `minCoherence`(기본 0.85) 이상인 픽셀 중 **무작위** 하나를 고릅니다(ADR-0041 에서 소스로 확인).
  그 이상의 픽셀이 없으면 실패합니다(`KB-MINTPY-001`); 수동 지정 픽셀이 마스크 밖이면 `KB-MINTPY-004`.
- **deramp**(`mintpy.deramp = linear|quadratic|no`) — 잔여 궤도/긴 파장 대기 램프 제거. 이는 "재평탄화"에
  해당하지만, 실제 넓은 범위의 변형(광역 침하)도 함께 제거될 수 있으므로 AOI 크기와 변형 파장을 보고 결정합니다.

## wintersar 에서

- `timeseries.reference_point: auto_recommend`(기본) — `validate.refpoint` 가 위 다섯 조건을 점수화합니다:
  `s = w1·코히어런스 + w2·[AOI 와 같은 연결성분] + w3·(1 − |표고 차|/범위) + w4·(1 − 거리/최대 거리) + w5·(1 − |속도 사전치|)`.
  가중치 초기값은 도메인 검수 항목이며 연구자 확인 전에는 바꾸지 않습니다(규칙 11.10, ADR-0041).
  상위 k 개를 표·지도로 제시하고 사용자가 고른 값을 `timeseries.reference_point: [lat, lon]` 에 기록합니다
  (QGIS 패널 ⑤: 지도 클릭 선택). MintPy 자동 선택(코히어런스만)과의 비교 테스트를 둡니다.
- `timeseries.reference_point: auto` — MintPy 자동 선택에 맡깁니다.
- `timeseries.deramp: linear|quadratic|no` 는 MintPy 템플릿으로 전달됩니다(ADR-0021).
- 기준점 오류는 대조군 검증에서 **일정한 편향(bias)** 으로 드러납니다 — [검증·튠 튜토리얼](../tutorials/validate-tune.md).

관련: [loop closure](loop-closure.md)(연결성분·언래핑 오류), [기하](geometry.md)(레이오버 픽셀은 기준점 금지).

## English summary

Unwrapped phase and SBAS displacements are relative to a reference pixel, so that pixel must be
stable, coherent, inside the same connected component as the AOI, at a similar elevation (tropospheric
delay is height-dependent) and not too far away. MintPy's "maxCoherence" auto-selection actually picks
a random pixel above `minCoherence` 0.85 (verified in source, ADR-0041) and fails when none exists
(`KB-MINTPY-001`). SARscape's refinement/re-flattening corresponds to MintPy's reference point plus
`deramp`. wintersar scores candidate pixels with a weighted sum of coherence, connectivity, elevation
similarity, distance and a small prior velocity (`timeseries.reference_point: auto_recommend`), shows
the top-k for the user to confirm (QGIS map click) and records the choice as `[lat, lon]`.
