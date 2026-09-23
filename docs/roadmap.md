# 로드맵 (v0.1 이후)

이 문서는 두 부분입니다. (1) 플랜 §7 Phase 8 의 "이후 백로그" 네 항목과 그 현재 상태, (2) `docs/open-questions.md`
의 미확정 행을 **담당자별**로 묶은 표. 상태의 원본은 언제나 open-questions 표이며 여기서는 번호로만 가리킵니다
(표는 추가만 하므로 번호는 바뀌지 않습니다). 여기 적힌 담당 열이 open-questions 의 담당 열과 같은지는
`tests/unit/qgis/test_docs.py` 가 검사합니다([ADR-0112](adr/0112-docs-test-policy.md)).

## 1. 백로그 (플랜 §7 Phase 8)

| 항목 | 지금 있는 것 | 필요한 것 | 선행 조건 |
|---|---|---|---|
| **KOMPSAT-5 리더 경로** | 없음. 플랜 §1.3 은 "KOMPSAT 이외 상용 X-band 전용 리더" 만 비목표로 두었고 KOMPSAT-5 는 백로그 | 산출물 포맷·메타데이터 확인, `io.formats` 리더, `BurstRecord` 대응 필드(스트립맵이라 burst 개념 없음), 사전검증 규칙 중 적용 가능한 것 선별 | 샘플 자료·이용 조건 확인(연구자), ADR |
| **GACOS 대기 보정** | `timeseries.troposphere: gacos` 가 MintPy 템플릿의 `mintpy.troposphericDelay.method = gacos` + `gacosDir` 로 매핑됨(`engines/mintpy_template.py`). 자료는 사용자가 직접 받아 둬야 함 | GACOS 요청·다운로드·콘텐츠 주소 캐시(PERF-02, ADR-0022) 어댑터, `KB-MINTPY-003` 조치 문구 갱신 | GACOS 서비스 이용 조건·API 확인(추측 금지), ADR |
| **spurt 3D 언래핑 기본 옵션화** | 어댑터 `engines/spurt.py` 와 `unwrap.method: spurt` 는 있음. 스케줄러는 스택 전용 백엔드로만 취급하고 기본은 `snaphu` (ADR-0025, ADR-0045) | 설치 경로 확정 후 합성·실데이터 A/B, 기본값 변경 ADR | 정책 예외 #22 (conda-forge 부재·`ortools`) |
| **CDSE 데이터 소스** | `data.source: cdse` 가 설정 스키마에 예약만 됨(`pipeline/config.py`). 검색·다운로드는 ASF 만 구현 | CDSE OData/STAC 검색 어댑터, burst 메타데이터 대응(ADR-0010 의 필드 출처 표 확장), 인증(CDSE 토큰) 정책 ADR | API 문서 확인, 네트워크 테스트(`-m network`) |

릴리스 노트의 "알려진 제한" 을 없애는 순서 제안: ① 의존성 정책 예외 승인(#10) → HyP3 경로 실데이터 1회
실행(Phase 2 DoD) → ② 국내 대조군 CSV 확보(#6/#42) → Phase 4 DoD 리포트 → ③ `pixi.lock` 생성(#70)과 ISCE2
환경 실행(#46/#69) → Phase 2 ISCE2 재현 → ④ 벤치 기준선 정책(#58)·골든 허용 오차 실측(#68) → PERF 표 →
⑤ QGIS LTR 검수(#60).

## 2. 미확정 사항 — 담당자별

행 번호는 [open-questions](open-questions.md) 의 `#`, "담당" 열은 그 표의 담당 열을 그대로 옮긴 것입니다.
"완료" 로 표시된 행은 참고로만 남겼습니다.

### 발주자·통합자 (승인·환경)

| # | 요지 | 상태 | 담당 |
|---|---|---|---|
| #4 | SNAPHU 라이선스 조건, snaphu-py/dolphin/spurt/sardem LICENSE 원문 | 미착수 | Taylor |
| #10 | 의존성 정책 예외 승인(hyp3-sdk, sentineleof, sardem, snaphu, burst2safe) | 대기 | Taylor |
| #22 | spurt 설치 정책 예외(conda-forge 부재, ortools) | 대기 | Taylor |
| #66 | §9 라이선스 표의 COMPASS·isce3·LiCSBAS·tophu | 완료 → ADR-0001 | Taylor |
| #67 | 락파일 부재(`pixi.toml` 은 생김, 구현은 #70) | 미착수 | Taylor |
| #70 | `pixi.lock` 생성·커밋(pixi 있는 머신에서) | 미착수 | Taylor |
| #53 | R-15 A/B 에 필요한 dolphin·MintPy 설치 정책 예외 | 대기 | Taylor / research |
| #69 | pixi isce2 feature 의 `PATH` 활성화 시점 확인 | 미착수 | Taylor / engines/isce2 |
| #71 | Docker engines 이미지의 CI 빌드 여부 | 미착수 | Taylor / 통합자 |
| #59 | `zarr>=3.0` 상향, MintPy 리더 위임 | 대기 | 통합자 |
| #65 | mkdocs nav 대상 파일 존재 | 완료 | 통합자 |

### 연구자 (도메인 검수, 규칙 11.10)

| # | 요지 | 상태 | 담당 |
|---|---|---|---|
| #6 | 국토지리정보원 수준점·GNSS 접근 방식·이용 조건 | 미착수 | 연구자 |
| #24 | looks 종횡비 상한 1.2 vs HyP3 고정 옵션 | 대기 | 연구자 |
| #25 | SEL-07 계절 모델·SEL-12 임계의 한국 사이트 적합성 | 대기 | 연구자 |
| #36 | `timeseries.coherence_threshold` → MintPy 네트워크 가지치기 매핑 | 대기 | 연구자 |
| #39 | LOS 부호 규약의 실데이터 확인 | 대기 | 연구자 |
| #40 | 기준점 점수 가중치 초기값 | 대기 | 연구자 |
| #42 | 수준점 시계열 입수 경로, GNSS 일 단위 좌표, 이용약관 | 미착수 | 연구자 |

### select · geometry · network

| # | 요지 | 상태 | 담당 |
|---|---|---|---|
| #1 | asf_search burst 필드 이름·가용 시점 | 진행 중 → ADR-0010 | select |
| #2 | ASF stack API 수직 기선 | 진행 중 → ADR-0011 | select |
| #26 | 토큰만으로 burst 추출기 다운로드 가능 여부 | 미착수 | select |
| #27 | IW 명목 픽셀 간격 출처 표시 | 미착수 | select |
| #35 | `candidates.json` granule 계약 | 미착수 | select |
| #12 | HyP3 RTC `_ls_map.tif` 비트 정의 | 미착수 | select/geometry |
| #14 | 수동 레이오버·셰도우 자체 구현, sardem DEM 재사용 | 미착수 | select/geometry |
| #13 | heading 사이트별 값(`heading_from_orbit`) | 미착수 | select/geometry + select/network |
| #17 | 파이썬 단계 진입점 이름(select/validate) | 대기 | select, validate |

### engines (hyp3 · isce2 · snaphu · dolphin)

| # | 요지 | 상태 | 담당 |
|---|---|---|---|
| #3 | HyP3 크레딧 정책·비용 | 진행 중 → ADR-0020 | engines/hyp3 |
| #33 | HyP3 FAILED 작업의 실패 사유 얻기 | 미착수 | engines/hyp3 |
| #34 | HyP3 작업 이름 제한 | 미착수 | engines/hyp3 |
| #37 | burst 산출물 지리 범위 편차·클립 실데이터 검증 | 미착수 | engines/hyp3 |
| #38 | `_conncomp.tif` 와 `prep_hyp3` 호환성 | 미착수 | engines/hyp3 |
| #5 | topsStack 참조 기하 재사용 | 완료 → #45 | engines/isce2 |
| #45 | (완료) 참조 기하 재사용 조사 결과 | 완료 → ADR-0029 | engines/isce2 |
| #46 | 실제 ISCE2 환경에서 플래그·run_files 재확인 | 미착수 | engines/isce2 |
| #47 | 이온층 단계 순차 실행 권고 원문 | 미착수 | engines/isce2 |
| #20 | SNAPHU 1.x `ASSEMBLEONLY` 구 문법 | 미착수 | engines/snaphu |
| #21 | snaphu-py 타일 임시 파일 보존 | 미착수 | engines/snaphu |
| #48 | dolphin 설정 키 버전 호환 | 미착수 | engines/dolphin |
| #49 | dolphin 참조점 변환, ISCE 평면 바이너리 읽기 | 미착수 | io + engines/dolphin |

### pipeline · unwrap · diagnose

| # | 요지 | 상태 | 담당 |
|---|---|---|---|
| #18 | macOS `ru_maxrss` 단위 | 대기 | pipeline |
| #28 | 트리 내 시그니처 확인(estimate/diagnose_logs/run_unwrap) | 진행 중 | pipeline |
| #63 | 단계별 진행 이벤트(`--progress`) | 미착수 | pipeline / docs/qgis |
| #16 | `run_unwrap` 반환형·retry_hint 전달 | 대기 | unwrap |
| #15 | `resources.estimate` 시그니처 | 대기 | diagnose |
| #29 | 구버전 SNAPHU secondary-node 문구 | 진행 중 → ADR-0035 | diagnose |
| #30 | HyP3 무료 크레딧 리셋 시점 | 미착수 | diagnose |
| #31 | topsStack DEM 범위 부족 시 오류 문자열 | 미착수 | diagnose |

### validate · io · compute

| # | 요지 | 상태 | 담당 |
|---|---|---|---|
| #7 | MintPy enu2los 부호·헤딩 규약 | 진행 중 → ADR-0040 | validate |
| #41 | 폐합 의심 임계값과 MintPy 산출물 대조 | 미착수 | validate + io |
| #43 | sweep 대용 지표를 MintPy 실제 값으로 교체 | 미착수 | validate + io |
| #11 | zarr 3.x API 채택 | 진행 중 → ADR-0050 | io |
| #44 | `load_timeseries` 시그니처 | 대기 | io |
| #56 | Zarr timeseries 청크 프리셋 실측 | 미착수 | io |
| #57 | ISCE2 BYTE 자료형 부호 | 미착수 | io |
| #55 | Goldstein 필터 상수 원 논문 대조 | 미착수 | compute |
| #72 | PERF-10 GPU 패리티·커널 벤치를 CUDA 머신에서 실행 | 미착수 | compute |

### research · bench

| # | 요지 | 상태 | 담당 |
|---|---|---|---|
| #9 | 타일 오버랩 기본값 | 미착수 | research |
| #19 | tophu `downsample_factor` 기본값 | 미착수 | research |
| #50 | Okada 변형원 구현 방식 | 미착수 | research |
| #51 | SHP 검정 기본값의 소표본 보수성 | 미착수 | research |
| #52 | `ml_lowpass` 기준선 추가 여부 | 미착수 | research |
| #54 | 연결성분 단위 스티칭 | 미착수 | research + unwrap |
| #23 | 프린지 밀도 임계·타일 워커 규칙 실측 | 미착수 | bench / research |
| #8 | SNAPHU 메모리 상수 실측 | 미착수 | bench |
| #32 | 리소스 모델 계수 실측 | 미착수 | bench |
| #58 | 벤치 회귀 게이트 기준선 머신 | 미착수 | bench |
| #68 | 골든 통계의 플랫폼 간 허용 오차 실측 | 미착수 | bench |

### docs / qgis

| # | 요지 | 상태 | 담당 |
|---|---|---|---|
| #60 | QGIS LTR 2종 수동 검수 | 미착수 | docs/qgis |
| #61 | conda 활성화 훅 환경 변수(`conda run` 폴백) | 미착수 | docs/qgis |
| #62 | QGIS 4.x 프로필 폴더 이름 | 미착수 | docs/qgis |
| #64 | pixi 환경 디렉터리 레이아웃 | 미착수 | docs/qgis |
| #73 | `mkdocs build` 미실행(docs extra 미설치) — nav·링크·명령만 테스트 | 미착수 | docs/qgis |

## English summary

Two lists. First, the plan's post-v0.1 backlog with its current state: a KOMPSAT-5 reader path (nothing
exists yet; needs sample data, a format reader and an ADR), GACOS tropospheric correction (only the MintPy
template mapping `timeseries.troposphere: gacos` exists, no downloader/cache), making spurt 3-D unwrapping a
default option (adapter exists, blocked by the install-policy row #22 and an A/B), and a CDSE data source
(`data.source: cdse` is reserved in the config schema but search/download are ASF-only). Second, every row of
`docs/open-questions.md` grouped by owner (Taylor, the researcher, the integrator, select, engines, pipeline,
unwrap, diagnose, validate, io, compute, research, bench, docs/qgis) with its status; the numbers are stable
because the table is append-only, and a test checks that the owner column here matches the source table.
Suggested order to clear the release-note limitations: dependency exceptions (#10) -> one real HyP3 run ->
Korean ground truth (#6/#42) -> `pixi.lock` and an ISCE2 environment (#70, #46/#69) -> bench baseline policy
(#58) and golden tolerances (#68) -> QGIS review (#60).
