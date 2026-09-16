# 미확정 사항 (Open questions)

규칙 11.9: 미확정 사항은 여기 남기고 진행을 막지 않는다. 확인되면 ADR 번호를 적고 행을 "완료"로 옮긴다.

| # | 항목 | 확인 방법 | 영향 | 담당 | 기한 | 상태 / ADR |
|---|---|---|---|---|---|---|
| 1 | asf_search burst 제품에서 픽셀 간격·IPF 버전·burst ID 필드 이름과 가용 시점 | `asf_search` 14.0.0 소스(`S1BurstProduct`) + 실제 응답 | SEL-08/09 | select | Phase 1 | 진행 중 → ADR-0010 |
| 2 | asf_search stack API의 Sentinel-1 수직 기선 계산 가능 여부 | 실제 호출 + 알려진 값 비교 (`-m network`) | SEL-06 | select | Phase 1 | 진행 중 → ADR-0011 |
| 3 | HyP3 크레딧 정책·작업별 비용 | HyP3 문서 | plan 견적 | engines/hyp3 | Phase 2 | 진행 중 → ADR-0020 |
| 4 | SNAPHU 라이선스 조건, snaphu-py/dolphin/spurt/sardem 저장소 LICENSE 원문 | LICENSE 파일 | §9 표 | Taylor | Phase 8 | 미착수 |
| 5 | topsStack 참조 기하 재사용 가능성 | ISCE2 소스 | PERF-11 | engines/isce2 | Phase 5 | 미착수 |
| 6 | 국토지리정보원 수준점·GNSS 데이터 접근 방식과 이용 조건 | 기관 안내 | Phase 4 어댑터 | 연구자 | Phase 4 | 미착수 |
| 7 | MintPy enu2los 부호·헤딩 규약 | MintPy 소스(`mintpy/utils/utils0.py`) | validate.los | validate | Phase 4 | 진행 중 → ADR-0040 |
| 8 | SNAPHU 메모리 상수(100 MB/백만 픽셀) 실측치 | bench S | unwrap 스케줄러 | bench | Phase 2 | 미착수 (설정값 `unwrap.memory_mb_per_mpixel`) |
| 9 | 타일 오버랩 기본값(25% vs 30% vs 200 px) | bench + 연구 모듈 지표 | unwrap 기본값 | research | Phase 6 | 미착수 (기본 25%, 최소 200 px) |
| 10 | 의존성 정책 예외 승인: `hyp3-sdk`(월 ~7.5k 다운로드), `sentineleof`, `sardem`, `snaphu`, `burst2safe`를 개발 환경에 설치할지 | Taylor 확인 | HyP3 경로·궤도·DEM 실행 테스트 | Taylor | Phase 2 | 대기 (ADR-0001) |
| 11 | zarr 3.x API 채택(설치된 3.1.6) vs 2.x 호환 | io 모듈 구현 시 확인 | PERF-08 | io | Phase 2 | 진행 중 → ADR-0050 |
