# ADR-0027: run_files 단계별 병렬 안전성 표와 디스크 정리 정책 (PERF-07)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: PERF-07, 플랜 6.1/6.2, R-04
- 검증 출처(Sources):
  - `Stack.py` `run.write_wrapper_config2run_file(configName, line_cnt, numProcess=1)` 와 각 단계 메서드가 `self.numProcess`를
    넘기는지 여부 (https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/Stack.py)
  - `stackSentinel.py` `--num_proc` help: "number of tasks running in parallel in each run file (default: 1)",
    `--num_proc4topo`: "number of parallel processes (for topo only)"
  - topsStack README: "User needs to execute each run file in order. The order is specified by the index number of the run file name."
  - `mergeBursts.py`: `suffix='.full'`(1x1 룩이면 ''), `useVirtualFiles`가 False면 `gdal.Translate`로 실제 파일 기록, multilook 시
    `outfile + suffix` → `outfile`
  - `stackSentinel.py checkCurrentStatus`: 업데이트 모드는 `coreg_secondarys/`를 기준으로 판단하고 최근 정합 SLC의 **원본 SAFE**를 요구

## 맥락 (Context)

topsStack은 run file 안의 작업(한 줄 = SentinelWrapper 1회)만 `--num_proc`으로 병렬화하고, run file 사이 의존성은 순서로만
표현한다. wintersar는 `--num_proc 1`로 run file을 만들고 자체 실행기(`engines/runfiles.py`)가 단계 순서를 지키며 단계 안의
작업을 동시에 실행한다. 어떤 단계가 병렬 안전한지, 어떤 디렉터리를 언제 지워도 되는지를 상류 소스 기준으로 고정해야 한다.

## 결정 (Decision)

### 병렬 상한 표 (`DEFAULT_STEP_PARALLEL`) — 상류가 `numProcess`를 넘기는 단계만 코어 수까지 허용

| 단계 | 상류 numProcess | 기본 상한 | 근거 |
|---|---|---|---|
| unpack_topo_reference | 없음(단일 작업) | 1 | topo.py 내부 병렬(`numProcess4topo` = `_cores`) |
| unpack_secondary_slc, average_baseline | 있음 | cores | 날짜별 독립 |
| extract_burst_overlaps, timeseries_misreg, extract_stack_valid_region | 단일 셸 명령 | 1 | |
| overlap_geo2rdr, overlap_resample, pairs_misreg | 있음 | cores | 날짜/쌍별 독립 |
| fullBurst_geo2rdr, fullBurst_resample | 있음 | cores | |
| merge_reference_secondary_slc, grid_baseline, dense_offsets | 없음(line_cnt 미사용) | 1 | 상류가 직렬로 기록 |
| generate_burst_igram, merge_burst_igram, filter_coherence, unwrap | 있음 | cores | 쌍별 독립(unwrap은 메모리 제한 시 `max_parallel_per_step`으로 낮출 것) |
| 이온층 단계 전부 | subband/generateIgram_ion만 있음 | 1 (보수적) | README의 순차 실행 권고 원문 미재확인 → open-questions |
| 알 수 없는 단계 | – | 1 | 보수적 기본 |

`parallel_cap(step, cores, overrides)` = `min(cores, cap)`; `None` = cores. 실패 시: 작업 1회 재시도(`retries=1`), 여전히 실패면
새 작업 제출 중단 → 실행 중 작업 대기 → 단계 `failed` 반환(다음 단계 미실행). 실패 작업·로그 경로는 `first_failure()`와
`runfiles_summary.json`에 기록되어 `wintersar diagnose`가 읽는다. 각 작업 stdout/stderr는 `log_dir/<run_file>/job_NNN.log`
(시도별 헤더 + `# exit=`). 완료 단계는 `<workdir>/runfiles_state.json`에 기록되어 재실행 시 건너뛴다.

### 디스크 정리 정책 (`cleanup_targets`)

| 정책 | 시점 | 삭제 대상 | 안전 근거 |
|---|---|---|---|
| none | – | 없음 | |
| stage (기본) | `merge_burst_igram` 완료 후 | `interferograms/<pair>/` (burst 단위 간섭도) — 단 `merged/interferograms/<pair>/fine.int`가 **실제 파일**(비-VRT)일 때만 | 이후 단계(filter_coherence, unwrap)는 `merged/`만 읽음(Stack.py). 1x1 룩·useVirtualFiles=True면 merged가 VRT라 삭제 금지 |
| aggressive | 워크플로 마지막 단계(`unwrap` 또는 correlation의 `filter_coherence`) 완료 후 | stage + `ESD/`, `coarse_offsets/`, `coarse_interferograms/`(NESD 임시), `secondarys/`(원본 SAFE에서 재생성 가능) | 어떤 후속 단계도 읽지 않음; 업데이트 모드는 `coreg_secondarys/`와 원본 SAFE만 필요 |
| 절대 삭제 금지 | – | `reference/`, `geom_reference/`, `coreg_secondarys/`, `misreg/`, `baselines/`, `merged/`, `stack/`, `configs/`, `run_files/` | MintPy prep_isce 입력·업데이트 모드 입력 |

## 결과 (Consequences)

- `unwrap` 단계의 동시 실행 수는 메모리 모델(open-questions #8)이 확정될 때까지 코어 수 상한이며, 기본 경로에서는 wintersar.unwrap
  스케줄러가 대신 실행한다.
- 이온층 워크플로는 실행되더라도 직렬(1)로 돈다; 필요하면 `isce2.max_parallel_per_step`으로 완화한다.
- 정리 정책 검증(실데이터에서 삭제 후 재실행 가능성)은 Phase 5 S 사이트에서 수행한다.
