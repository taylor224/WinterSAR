# TOPS 정합(coregistration) — 기하 정합 + ESD, 그리고 abort 의 진짜 원인 (SEL-11, KB-ISCE2-00x)

Sentinel-1 IW 는 TOPS(Terrain Observation by Progressive Scans) 방식으로 안테나를 애지머스 방향으로
스위핑하며 burst 를 찍습니다. 그 결과 burst 안에서 도플러 중심이 선형으로 크게 변하고, 정합 오차가
**애지머스 방향 위상 램프**로 곧바로 나타납니다. 그래서 TOPS 간섭도는 약 **1/1000 픽셀** 수준의 애지머스
정합 정확도가 필요합니다 — 일반 stripmap 영상 정합보다 훨씬 엄격합니다.

## 표준 절차

1. **기하 정합**: 정밀궤도(POEORB) + DEM 으로 보조 영상의 각 픽셀이 기준 영상의 어디에 대응하는지 계산해
   리샘플링합니다. "픽셀을 회전시켜 맞추는" 방식이 아니라 궤도·지형 기하로 푸는 것입니다.
2. **ESD(Enhanced Spectral Diversity)**: 인접 burst 의 오버랩 구간에서 두 burst 의 위상차를 비교해 남은
   애지머스 오프셋을 미세 보정합니다. 오버랩 구간의 코히어런스가 낮으면 ESD 추정이 실패합니다
   (`KB-ISCE2-002`: "Coherence threshold too strict. No points left for reliable ESD estimate").

## 정합이 abort 되는 주원인

| 원인 | 사전검증 / 진단 |
|---|---|
| 다른 트랙(relative orbit)의 씬을 섞음 | `SEL-01` FAIL ([relative orbit](relative-orbit.md)) |
| 공통 burst 가 없음 / 프레임 경계 불일치 | `SEL-04` FAIL, `KB-ISCE2-001` ("No common bursts found …") |
| 궤도 파일 누락·기간 불일치 | `SEL-11` INFO(POEORB 없으면 RESORB), `KB-ISCE2-003` |
| DEM 이 처리 영역을 덮지 못함 | `KB-ISCE2-004` |
| 편파 불일치(VV 와 VH 를 섞음) | `SEL-05` FAIL |

정밀궤도(POEORB)는 취득 후 며칠 뒤에 배포되므로 최신 날짜는 복원궤도(RESORB)로 처리될 수 있습니다.
궤도 정확도가 낮으면 잔여 궤도 램프가 커지는데, 이는 `timeseries.deramp` 로 일부 흡수됩니다
([기준점과 재평탄화](reference-point-deramp.md)).

## wintersar 에서

- `engine.esd: true`(기본) — ISCE2 topsStack 경로에서 ESD 를 켭니다. HyP3 는 자체 절차를 따릅니다.
- 보조 데이터(궤도·DEM)는 콘텐츠 주소 캐시에 둡니다(`compute.cache_dir`, `PERF-02`, ADR-0022).
- 실패하면 `wintersar diagnose <workdir>/logs --engine isce2` 가 위 KB 항목으로 원인 → 조치를 보여 줍니다.

관련: [burst](burst.md), [ISCE2 로컬 튜토리얼](../tutorials/isce2-local.md), ADR-0021/0023 (엔진 어댑터).

## English summary

TOPS bursts sweep the antenna in azimuth, so coregistration errors turn into azimuth phase ramps
and about 1/1000-pixel azimuth accuracy is required. The standard recipe is geometric
coregistration (precise orbits + DEM) followed by ESD refinement using burst-overlap phase
differences; pixels are not "rotated into place". Coregistration aborts are almost always caused by
mixed tracks (`SEL-01`), no common bursts (`SEL-04`, `KB-ISCE2-001`), missing orbit files
(`SEL-11`, `KB-ISCE2-003`) or a DEM that does not cover the area (`KB-ISCE2-004`);
`wintersar diagnose --engine isce2` maps the log to these causes and fixes.
