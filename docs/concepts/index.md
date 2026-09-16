# 개념 정리 (Concepts) — InSAR 온보딩

플랜 §12.1 의 온보딩 초안을 한 개념당 한 페이지로 풀었습니다. 순서대로 읽으면 "왜 사전검증이 그렇게
판정하는지", "왜 파이프라인이 그 파라미터를 요구하는지"가 코드와 연결됩니다. 각 페이지 끝에 English
summary 가 있습니다([ADR-0072](../adr/0072-docs-structure-and-i18n.md)).

| # | 페이지 | 한 줄 요약 | 연결되는 코드/규칙 |
|---|---|---|---|
| 1 | [relative orbit(트랙)](relative-orbit.md) | 간섭은 **같은 트랙 번호** 사이에서만 성립. "같은 하강궤도"로는 부족 | `SEL-01/02`, `data.relative_orbit` |
| 2 | [burst / Full Burst ID](burst.md) | 같은 burst ID = 같은 지역. IW SLC 프레임 경계는 날짜마다 달라 겹침이 보장되지 않음 | `SEL-03/04/10`, `data.product`, `PERF-01` |
| 3 | [픽셀 간격과 looks](pixel-spacing-looks.md) | IW SLC 는 레인지 ~2.3 m × 애지머스 ~14 m 로 **원래** 비대칭. looks 로 정사각 근사 | `SEL-09`, `engine.looks`, `engine.target_pixel_m` |
| 4 | [레이오버·foreshortening·셰도우](geometry.md) | 측면 관측 레이더의 거리 기하 왜곡. 궤도 방향에 따라 다른 사면이 왜곡됨 | `SEL-12`, `unwrap.mask.layover` |
| 5 | [TOPS 정합](tops-coregistration.md) | 기하 정합 + ESD 미세 보정. abort 의 주원인은 트랙·burst·궤도 파일 문제 | `SEL-11`, `engine.esd`, `KB-ISCE2-00x` |
| 6 | [언래핑 타일과 다중해상도](unwrap-tiling-multiresolution.md) | 큰 간섭도는 타일로 나눠 언래핑 후 재조립. 다중해상도는 저해상도 "대표위상"으로 2π 단차 제거 | `unwrap.*`, `PERF-04`, `R-06/07` |
| 7 | [기준점과 재평탄화](reference-point-deramp.md) | 언래핑 위상은 상대값. 안정·고코히어런스·유사 표고 기준점 + deramp = SARscape refinement 에 대응 | `timeseries.reference_point`, `timeseries.deramp`, `KB-MINTPY-001/004` |
| 8 | [loop closure(위상 폐합)](loop-closure.md) | 세 날짜 간섭도의 순환 합은 0 이어야 함. 잔차가 큰 간섭도 = 언래핑 오류 의심 | `timeseries.unwrap_error_correction`, `validate.closure` |
| 9 | [순차 추정기와 compressed SLC](sequential-estimator-compressed-slc.md) | 미니스택 phase linking 으로 언래핑 횟수를 M → N−1 로. 정확도는 A/B(R-15)로 판단 | `timeseries.engine: dolphin`, `PERF-05/06` |

읽는 순서 제안: 1 → 2 → 3 → 4 (선별) · 5 → 6 (처리) · 7 → 8 (검증) · 9 (연구).

## 용어 표기

용어는 처음에 "한국어(영어)" 로 병기하고 이후에는 영어 원어를 그대로 씁니다. 엔진 로그·논문·설정 키가
영어이기 때문입니다: relative orbit, burst, looks, layover/shadow, ESD, coherence, reference point,
loop closure, phase linking.

## English summary

One page per onboarding concept from plan §12.1, each tied to the rule IDs and config keys that
implement it: relative orbit (`SEL-01/02`), burst IDs (`SEL-03/04/10`), pixel spacing and looks
(`SEL-09`), radar geometry distortions (`SEL-12`), TOPS coregistration (`SEL-11`, ESD), tiled and
multi-resolution unwrapping (`unwrap.*`), reference point and deramp (`timeseries.*`), loop closure
and the sequential estimator / compressed SLC A/B (`R-15`). Suggested order: 1-4 (selection),
5-6 (processing), 7-8 (validation), 9 (research).
