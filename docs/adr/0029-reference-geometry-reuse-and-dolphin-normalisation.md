# ADR-0029: topsStack 참조 기하 재사용(PERF-11)과 dolphin 시계열 정규화(PERF-05 A/B)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: PERF-11, PERF-05, PERF-03, 플랜 12.3 표 5행("topsStack 참조 기하 재사용 가능성"), open-questions #5
- 검증 출처(Sources):
  - `stackSentinel.py checkCurrentStatus(inps)` 전문 및 `slcStack()`의 `if not updateStack:` 분기
    (https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/stackSentinel.py)
  - `Stack.py` 각 단계의 읽기/쓰기 디렉터리(ADR-0026 표)
  - dolphin `timeseries.py`(단위·부호·파일명), `_common.py`(`_directory` 기본값), MintPy `timeseries.h5` 규약(ADR-0021, `io/timeseries.py`)

## 맥락 (Context)

플랜 12.3 5행은 "날짜가 추가될 때 topo/geometry를 다시 계산해야 하는가, 무엇을 캐시해야 하는가"를 확인 항목으로 두었다.
또 PERF-05는 MintPy(A)와 dolphin(B) 시계열을 같은 컨테이너로 비교해야 한다.

## 확인한 사실 — topsStack 업데이트 모드

`checkCurrentStatus`:

1. `coreg_secondarys/` 가 존재하고 `[0-9]???[0-9]?[0-9]?` 날짜 폴더가 있으면 "An existing stack with following coregistered SLCs was found".
2. `newAcquisitions = secondaryDates − coregSLC`; 없으면 "No new acquisition found to update the stack." + `sys.exit(1)`.
3. NESD: `numSLCReprocess = 2*num_overlap_connections`(날짜 수로 상한), geometry: `num_connections`. 최근 정합 SLC 그만큼을 새 날짜와
   함께 **다시 처리**하며, 그 날짜들의 원본 SAFE가 없으면 `Exception('The original SAFE files for latest {0} coregistered SLCs is needed')`.
4. `stackUpdate = True` → `slcStack()`에서 `run_NN_unpack_topo_reference`(reference 언팩 + **topo/geometry**)와
   `run_NN_extract_burst_overlaps`를 **생성하지 않는다**. 즉 참조 기하(`reference/`, `geom_reference/`)와 burst overlap 서브셋은 재사용된다.
5. 나머지 단계(unpack_secondary_slc, average_baseline, geo2rdr/resample, misreg, merge, igram…)는 재처리 대상 날짜·쌍에 대해서만 run file이 만들어진다.

→ **결론: 날짜 추가 시 topo/geometry 재계산은 필요 없다(상류가 이미 건너뜀). 캐시(보존)해야 할 것**:
`reference/`, `geom_reference/`, `coreg_secondarys/`(모든 날짜), `misreg/`, `baselines/`, `merged/`, `stack/`, 그리고
**최근 `2*num_overlap_connections`개 날짜의 원본 SAFE**. 반대로 `run_files/`, `configs/`는 재생성해야 하며(존재 시 stackSentinel.py가 종료),
`ESD/`, `coarse_*`, `secondarys/`는 재생성 가능하다(ADR-0027 aggressive 정리와 일치).

## 결정 (Decision) — 어댑터

1. topsStack 작업 폴더는 DAG 노드 해시와 무관하게 **영속** 위치 `<project workdir>/isce2_topsstack/<stack_id>_<geomkey>`에 둔다.
   `geomkey = hash(stack_id, reference_date, bbox, swaths, polarization, esd, dem)` — 기하에 영향을 주는 값만 포함(룩·정리 정책·필터는 제외).
   PERF-03 캐시는 노드 산출물(manifest)만 해시하므로 재실행 시 상류 캐시와 충돌하지 않는다.
2. coregister 단계에서 `coreg_secondarys/`에 없는 새 날짜가 있으면 `run_files/`·`configs/`를 `.bak-<UTC>`로 옮기고(ISCE2-015)
   stackSentinel.py를 다시 실행(업데이트 모드, ISCE2-008 INFO), `runfiles_state.json`을 초기화한다. 새 날짜가 없고 run_files가 있으면
   stackSentinel.py를 부르지 않고 미완료 단계만 이어 실행한다.
3. 정리 정책은 ADR-0027의 "절대 삭제 금지" 목록으로 위 보존 대상을 보호한다.

## 결정 (Decision) — dolphin 산출물 정규화 (PERF-05 A/B)

| 항목 | A: MintPy (`read_timeseries_h5`) | B: dolphin (`read_dolphin_timeseries`) | 정규화 결과(`TimeSeries`) |
|---|---|---|---|
| 변위 | `timeseries` (n_date, ny, nx) 미터, 첫 날짜 0 | `timeseries/<ref>_<date>.tif` (n_date−1개), 참조일 파일 없음 | `displacement_m[0]=0` 삽입, float32 |
| 부호 | 양수 = 위성 방향(MintPy 규약, ADR-0040) | wavelength 있으면 `-λ/4π·φ` → 양수 = 위성 방향 | 동일. wavelength 없던 실행은 wintersar가 같은 식으로 변환(DOL-006, `attrs.units_converted_by`) |
| 속도 | `velocity.h5` m/yr | `timeseries/velocity.tif` (단위/yr, 365.25 d) | `velocity_m_per_yr` |
| 좌표 | `Y_FIRST/X_FIRST` 또는 geometry lat/lon | 래스터 지오레퍼런스(EPSG) 또는 레이더 좌표(`lat.rdr`/`lon.rdr` 지정) | 1-D lat/lon(북-상 지리 격자) 또는 2-D(투영·레이더), 없으면 픽셀 인덱스(`attrs.coords`) |
| 코히어런스 | `temporalCoherence.h5` | `temporal_coherence*.tif`(interferograms/ 또는 linked_phase/) | `coherence` |
| 참조점 | `REF_LAT/REF_LON` | `timeseries_options.reference_point`(row, col) 또는 자동 | dolphin은 lat/lon 미기록 → `reference_latlon=None` (DOL-004로 명시 권고) |
| 아티팩트 | `timeseries`(h5) | `timeseries`(npz: dates, displacement_m, velocity_m_per_yr, lat, lon, coherence) + `dolphin_workdir`, `velocity`, `unw_dolphin` | validate/bench는 `TimeSeries`로 비교 |

## 결과 (Consequences)

- open-questions #5는 본 ADR로 "완료". 실데이터에서 업데이트 모드가 실제로 topo를 건너뛰는지(run_files 목록)는 Phase 5에서 확인한다(#46).
- dolphin 참조점을 lat/lon으로 받는 변환(row/col ↔ lat/lon)은 io 모듈의 래스터 지오레퍼런스 유틸이 생기면 추가한다(#49).
