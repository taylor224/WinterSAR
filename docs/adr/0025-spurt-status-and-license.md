# ADR-0025: spurt(3D 시공간 언래핑) 존재·라이선스 확인과 어댑터 범위

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-06, 플랜 5.2("spurt는 스택 입력(위상 연결 결과) 전용"), 9장, 12.2("존재·라이선스 확인")
- 검증 출처(Sources):
  - 저장소: https://github.com/isce-framework/spurt (루트에 `LICENSE-Apache-2.0`,
    `LICENSE-BSD-3-Clause`, `CITATION.cff`, `pyproject.toml`, `src/spurt/{graph,io,links,mcf,utils,workflows}`)
  - 릴리스: https://api.github.com/repos/isce-framework/spurt/releases — v0.1.0 (2024-12-16),
    **v0.1.1 (2025-03-24)**
  - PyPI `spurt` 0.1.1 (https://pypi.org/pypi/spurt/json): requires h5py ≥3.6, numpy ≥1.23,
    **ortools ≥9.8.3296**, rasterio ≥1.2, scipy ≥1.12; Python ≥3.9
  - `pyproject.toml` v0.1.1: `[project.scripts] spurt-emcf = "spurt.workflows.emcf:__main__"`;
    `src/spurt/workflows/emcf/__main__.py` 존재(`python -m spurt.workflows.emcf` 가능)
  - `src/spurt/workflows/emcf/_cli.py` (argparse 인자), `_settings.py` (GeneralSettings,
    TilerSettings, SolverSettings, MergerSettings), `_unwrap.py` (`unwrap_tiles(stack:
    spurt.io.SLCStackReader, g_time, gen_settings, solv_settings)`)
  - `src/spurt/io/_slc_stack.py`: `SLCStackReader.from_phase_linked_directory(folder,
    temp_coh_threshold=0.6)` — `*.int.tif` + `temporal_coherence.tif`
  - README v0.1.1: "Spatial and Temporal phase Unwrapping for InSAR time-series";
    "licensed under your choice of BSD-3-Clause or Apache-2.0 licenses"; Copyright (c) 2024 Caltech
  - conda-forge `spurt-feedstock`: **404(없음)** (2026-09-16 조회)

## 맥락

플랜 12.2는 spurt를 "직접 확인하지 못한 저장소"로 두었고 9장 표는 라이선스를 "확인 필요"로 남겼다.
플랜 5.2는 spurt 어댑터가 스택 입력 전용임을 명시한다. 3D 언래핑 기본 옵션화는 Phase 8 이후
백로그다.

## 확인한 사실

- 존재·유지보수: isce-framework 소속, 171+ 커밋, 2025-03 릴리스. API는 0.x(불안정 가능).
- 라이선스: **BSD-3-Clause OR Apache-2.0** (Caltech, 미 정부 후원 고지). import·subprocess 모두
  가능 → 9장 표의 "확인 필요" 해소.
- 진입점: `spurt-emcf` 콘솔 스크립트(EMCF = extended minimum cost flow 워크플로).
  인자(v0.1.1 `_cli.py`):

  | 플래그 | 기본 | 의미 |
  |---|---|---|
  | `-i/--inputdir` (필수) | – | phase-linked SLC 스택 폴더 |
  | `-o/--outputdir` | `./emcf` | 최종 unwrapped 래스터 |
  | `--tempdir` | `./emcf_tmp` | 중간 산출물 |
  | `-w/--t-workers` | 0 (≤0 → ncpus−1) | 시간 언래핑 워커 |
  | `--s-workers` | 1 | 공간 언래핑 워커 |
  | `-b/--batchsize` | 150000 | 시간 언래핑 배치당 링크 수 |
  | `-c/--coh` | 0.6 | **시간적(temporal) 코히어런스** 임계 |
  | `--t-cost-type` | constant (constant/distance/centroid) | 시간 비용 |
  | `--t-cost-scale` | 100 | |
  | `--pts-per-tile` | 800000 | 타일당 점 수 |
  | `--max-tiles` | 49 | |
  | `--merge-parallel-ifgs` | 1 | |
  | `--unwrap-parallel-tiles` | 1 | |
  | `--singletile` | False | |
  | `--log-file` | None | stderr 외 로그 파일 |

- 입력 형식: `SLCStackReader.from_phase_linked_directory(folder, temp_coh_threshold)` —
  날짜별 `*.int.tif`(파일명 앞 8자리 날짜)와 `temporal_coherence.tif`. 즉 dolphin 등 phase
  linking 결과가 필요하며 **간섭도 쌍 단위 2D 입력이 아니다**.
- 타일 출력: HDF5(`uw_data`, `points`, `tile`, `phase_offset`) → 병합 후 unwrapped 래스터.

## 선택지

1. 미구현 스텁(check_install만).
2. **얇은 subprocess 어댑터**: `run("unwrap")`이 `phase_linked_stack` 디렉터리 아티팩트를 받아
   `spurt-emcf`를 실행; 2D `unwrap()`은 명시적 예외.
3. `spurt` 파이썬 API(`unwrap_tiles`, `merge_tiles`)를 직접 호출.

## 결정

선택지 2 (`wintersar/engines/spurt.py`). 0.x API 변동 위험이 있어 파이썬 API 대신 CLI를
subprocess로 호출한다(규칙 11.2).

- `SpurtEngine(name="spurt", stages=("unwrap",), version_constraint=">=0.1,<1",
  stack_only=True, install_hint="pip install spurt")`. 감지: `python_module_version("spurt")`
  → 실행 명령(`spurt_executable`/`$WINTERSAR_SPURT_EXE` → PATH `spurt-emcf` → `python -m
  spurt.workflows.emcf`)의 `--version`.
- `unwrap(igram, coh, mask, params)`는 `StackOnlyEngineError`(NotImplementedError 계열,
  Finding `UNW-009`)를 던진다. 스케줄러는 2D 작업을 snaphu/tophu로 보내야 한다.
- `run()`은 `inputs["phase_linked_stack"]`(kind=dir; `*.int.tif`+`temporal_coherence.tif` 검증,
  아니면 `UNW-010`)를 받아 `build_spurt_command()`로 위 표의 플래그를 만든다. 사용자가 지정한
  `params["spurt"][...]`만 전달하고 나머지는 spurt 기본값에 맡긴다. `_cores`는 `--t-workers`.
  **`unwrap.coherence_threshold`(공간 코히어런스)는 `--coh`(시간 코히어런스)에 매핑하지 않는다.**
- stdout/stderr는 `log_dir/spurt.log`, `--log-file`은 `log_dir/spurt-emcf.log`. 실패 시
  `EngineRunError`(`UNW-003`). 산출물은 3개다(`engines/spurt.py` `run()` 확인): `unw`(dir,
  snaphu/tophu 와 공유하는 `StageSpec("unwrap")` 출력 이름)·`unw_stack`(dir, 같은 `<out>/emcf`
  디렉터리를 가리키는 spurt 전용 별칭)·`unw_stats`(json, `<out>/stats.json`). 앞의 두 dir 아티팩트는
  `meta["layout"] = "spurt_stack"`(쌍별 `*.unw.tif`, snaphu/tophu 의 `unw.npz` 아님)를 갖는다.
- 검증: 가짜 `spurt-emcf` 스크립트(동일 argparse)로 명령 생성·실행·로그·실패 경로 테스트
  (`tests/unit/engines_unwrap/test_spurt.py`).

## 결과

- spurt 경로는 phase-linking(dolphin) 단계가 파이프라인에 들어온 뒤에야 쓸 수 있다(백로그).
  `diagnose` KB에 spurt 항목이 없어 `parse_log`는 엔진 무관 매칭으로 위임한다.
- conda-forge 패키지가 없으므로 `pip install spurt`(ortools 휠 포함)로 설치한다; 전역 의존성
  정책(1만 사용자 미만)상 optional extra로만 둔다(ADR-0001 예외 목록에 추가 필요 →
  needs_from_others).
