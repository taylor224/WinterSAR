# burst 와 Full Burst ID — 왜 씬(프레임) 단위가 아니라 burst 단위인가 (SEL-03, SEL-04, SEL-10, PERF-01)

Sentinel-1 IW(Interferometric Wide swath) 모드는 **sub-swath 3개**(IW1, IW2, IW3)를 번갈아 조명하며,
각 sub-swath 는 애지머스 방향으로 짧은 **burst** 단위로 촬영됩니다(TOPS 방식,
[TOPS 정합](tops-coregistration.md)). burst 의 지리적 위치는 궤도 주기마다 거의 같아서 같은 **Full Burst
ID**(예: `052_109903_IW2` = 트랙 052, burst 번호, sub-swath)는 같은 지역을 뜻합니다.

반면 배포되는 IW SLC 씬(프레임)의 경계는 날짜마다 조금씩 다릅니다. 그래서 씬 단위로 "AOI 가 프레임 안에
있다"고 골라도 다른 날짜에는 AOI 의 일부가 프레임 밖이거나 두 프레임에 걸칠 수 있고, 정합·간섭도
단계에서 겹침이 부족해 실패하거나 결과가 잘립니다. 연구자 경험("다 받아도 맞는 게 몇 개 없다")의 큰 원인입니다.

## wintersar 에서

- **검색은 burst 제품 우선**: `data.product: burst`(기본). ASF 의 Sentinel-1 burst 제품을 AOI × 기간으로
  조회해 `BurstRecord` 로 정규화하고, burst 제품이 없는 지역/기간에만 SLC 로 fallback 합니다(ADR-0012).
  필요한 burst 몇 개만 받으므로 씬당 수 GB 를 받는 SLC 경로보다 전송량이 줄어드는 것이 `PERF-01` 의
  **가설**입니다(수치는 bench 결과로만).
- **스택의 burst 집합** = AOI 와 교차하면서 **모든 날짜에 공통**으로 존재하는 burst. 어떤 날짜에 burst 가
  빠지면 (a) 그 날짜를 제외한 경우와 (b) 그 burst 를 제외한 경우의 커버리지를 둘 다 계산해 선택지를 줍니다
  (`SEL-04` 의 조치 문구, `StackCandidate.n_dates_dropped / n_bursts_dropped`).
- `SEL-03` 은 모드(IW)와 sub-swath 구성이 모든 날짜에서 같은지, `SEL-04` 는 공통 burst 가 AOI 를
  `selection.min_coverage`(기본 0.95) 이상 덮는지(공통 burst 없음 → FAIL, 커버리지 부족 → WARN),
  `SEL-10` 은 날짜별 burst 수·라인 결손을 검사합니다.
- ISCE2 로컬 경로에서는 burst GeoTIFF 를 `burst2safe` 로 SAFE 형태로 재구성해 `topsStack` 에 넣습니다
  ([ISCE2 로컬 튜토리얼](../tutorials/isce2-local.md)). HyP3 경로는 burst InSAR 작업을 직접 제출합니다.

관련: [relative orbit](relative-orbit.md), ADR-0010(burst 메타데이터 필드 출처), ADR-0012(검색 쿼리).

## English summary

Sentinel-1 IW images are made of three sub-swaths, each acquired as short azimuth bursts (TOPS).
A burst's ground footprint is nearly identical every cycle, so the same Full Burst ID (e.g.
`052_109903_IW2`) means the same area, whereas SLC frame boundaries shift from date to date and do
not guarantee overlap. wintersar therefore searches ASF burst products first (`data.product: burst`),
builds each stack from the bursts that intersect the AOI **on every date**, and reports coverage
alternatives when a date is missing a burst (`SEL-04`). `SEL-03` checks mode/sub-swath consistency,
`SEL-10` burst counts and missing lines.
