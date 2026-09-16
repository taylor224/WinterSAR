# ADR 색인 (Architecture Decision Records)

규칙 11.7: 설계 결정과 "확인 필요" 항목의 검증 결과는 `docs/adr/NNNN-slug.md` 로 남긴다
(템플릿 [`0000-template.md`](0000-template.md)). 번호 대역은 모듈별로 고정해 병렬 작업 시 충돌을 피한다.

| 대역 | 모듈 | 현재 문서 |
|---|---|---|
| 0001–0009 | 공통(라이선스·경계) | [0001](0001-license-and-engine-boundaries.md) 프로젝트 라이선스와 엔진 경계 |
| 0010–0019 | select (검색·선별·사전검증·기하) | [0010](0010-asf-search-burst-field-provenance.md) asf_search 필드 출처 · [0011](0011-perpendicular-baseline-source.md) 수직 기선 출처 · [0012](0012-burst-first-slc-fallback-query.md) BURST 우선/SLC fallback · [0013](0013-earthdata-auth-policy.md) Earthdata 인증 · [0014](0014-precheck-rule-thresholds.md) SEL 임계값 · [0015](0015-looks-selection-algorithm.md) looks 산출 · [0016](0016-network-and-reference-recommendation.md) 네트워크·참조일 · [0017](0017-geometry-angle-conventions.md) 각도 규약 · [0018](0018-dem-source-and-cache.md) DEM 소스·캐시 · [0019](0019-isce2-shadow-layover-comparison.md) ISCE2 shadow/layover 비교 |
| 0020–0029 | engines (어댑터) | [0020](0020-hyp3-api-and-credits.md) HyP3 어댑터 · [0021](0021-mintpy-template-and-perf09.md) MintPy 어댑터 · [0022](0022-aux-data-cache.md) 보조 데이터(궤도·기상 모델·DEM) 콘텐츠 주소 캐시 설계 · [0023](0023-snaphu-py-api-and-subprocess-config.md) snaphu-py API 사실 확인과 SNAPHU 설정 파일 subprocess 경로 · [0024](0024-tophu-api-facts.md) tophu 다중해상도 언래핑 API 사실 확인과 어댑터 설계 · [0025](0025-spurt-status-and-license.md) spurt(3D 시공간 언래핑) 존재·라이선스 확인과 어댑터 범위 · [0026](0026-isce2-topsstack-flags-and-run-files.md) ISCE2 topsStack `stackSentinel.py` 플래그·run_files 사실과 어댑터 매핑 · [0027](0027-topsstack-parallel-safety-and-cleanup.md) run_files 단계별 병렬 안전성 표와 디스크 정리 정책 · [0028](0028-dolphin-cli-config-and-license.md) dolphin CLI·설정 YAML 키·라이선스 확인과 어댑터 범위 · [0029](0029-reference-geometry-reuse-and-dolphin-normalisation.md) topsStack 참조 기하 재사용(PERF-11)과 dolphin 시계열 정규화(PERF-05 A/B) |
| 0030–0034 | pipeline (DAG·캐시·실행) | [0030](0030-lightweight-dag-vs-workflow-engines.md) 경량 DAG · [0031](0031-node-hashing-scheme.md) 노드 해시 · [0032](0032-workdir-layout-and-manifest.md) 작업 디렉터리 · [0033](0033-failure-and-retry-semantics.md) 실패·재시도 · [0034](0034-stage-params-contract.md) 단계 파라미터 |
| 0035–0039 | diagnose (로그 파서·KB·리소스) | [0035](0035-diagnose-kb-schema-and-pattern-verification.md) KB 스키마 · [0036](0036-resource-model-initial-coefficients.md) 리소스 모델 · [0037](0037-generic-log-parser-scope.md) 범용 파서 |
| 0040–0044 | validate (기준점·폐합·대조군·LOS) | [0040](0040-los-sign-convention.md) LOS 투영 부호·헤딩 규약 · [0041](0041-refpoint-scoring-weights.md) 기준점 추천 점수의 가중치 초기값과 MintPy 자동 선택 비교 · [0042](0042-closure-metric-definitions.md) 위상 폐합(loop closure) 지표 정의와 의심 간섭도 판정 · [0043](0043-ngii-ground-truth-access.md) 국토지리정보원(NGII) 수준점·GNSS 상시관측소 데이터 접근 방식 조사 결과 · [0044](0044-sweep-design.md) 파라미터 스윕 설계 |
| 0045–0049 | unwrap (스케줄러·타일) | [0045](0045-unwrap-scheduler-strategy-rules.md) 전략 규칙 · [0046](0046-unwrap-tile-overlap-default.md) 오버랩 기본값 · [0047](0047-unwrap-parallelisation-order.md) 병렬화 순서 · [0048](0048-unwrap-seam-detector-and-merge.md) 단차 검출 |
| 0050–0054 | io / compute / bench | [0050](0050-zarr-v3-igram-store-and-chunk-presets.md) Zarr v3 간섭도 스택 저장소와 청크 프리셋 · [0051](0051-cog-export-settings.md) 결과 COG 내보내기 설정 · [0052](0052-isce-hyp3-mintpy-format-facts.md) ISCE2·HyP3·MintPy 포맷 확인 결과와 `wintersar.io.formats` 규약 · [0053](0053-gpu-backend-policy-and-tolerances.md) GPU(CuPy) 백엔드 정책과 커널 수치 허용 오차 · [0054](0054-bench-protocol-implementation.md) 벤치마크 프로토콜 구현 |
| 0060–0064 | research (대표위상·스티칭·실험) | [0060](0060-research-experiment-design.md) Phase 6 연구 실험 설계 · [0061](0061-phase-linking-implementation.md) phase linking(EVD/EMI, 순차 추정기) numpy 구현과 참고문헌 · [0062](0062-shp-test-choice.md) SHP(통계적 동질 픽셀) 검정 선택 · [0063](0063-stitching-graph-adjustment.md) 타일 2π 오프셋 결정 · [0064](0064-r15-sequential-estimator-ab-protocol.md) R-15 순차 추정기(dolphin) vs 전통 SBAS(MintPy) A/B 프로토콜 |
| 0070–0072 | docs / QGIS 플러그인 | [0070](0070-qgis-plugin-thin-client-architecture.md) 플러그인 아키텍처 · [0071](0071-qgis-plugin-environment-discovery.md) 환경 탐색 · [0072](0072-docs-structure-and-i18n.md) 문서 구조·언어 |

새 ADR 을 쓸 때: 자기 대역의 다음 번호를 쓰고, 상태(제안/채택/기각/대체됨)·날짜·관련 ID·검증 출처를
머리말에 적는다. 출처 없는 수치·API 인자명은 쓰지 않는다(규칙 11.3).

## English summary

Architecture decision records live here, one file per decision, numbered in per-module ranges so
parallel work never collides: 0001 common, 0010–0019 select, 0020–0029 engines, 0030–0034 pipeline,
0035–0039 diagnose, 0040–0044 validate, 0045–0049 unwrap, 0050–0054 io/compute/bench,
0060–0064 research, 0070–0072 docs/QGIS. Every ADR states status, date, related IDs and the sources
that were actually verified (no guessed numbers or API names, rule 11.3).
