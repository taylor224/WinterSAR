# ADR-0028: dolphin CLI·설정 YAML 키·라이선스 확인과 어댑터 범위

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-06, PERF-05(A/B 시계열 경로), 플랜 5.2 `dolphin.py`, 9장 라이선스 표
- 검증 출처(Sources):
  - `src/dolphin/cli.py`: `tyro.extras.subcommand_cli_from_dict({"run": run_cli, "config": ConfigCli, "unwrap", "timeseries", "filter", "geocode"})`,
    `run_cli(config_file, /, debug=False)` → `DisplacementWorkflow.from_yaml(config_file)`; `dolphin --version`은 `__version__` 출력
  - `src/dolphin/workflows/_cli_config.py`: `ConfigCli(DisplacementWorkflow)` + `print_empty`, `outfile=Path("dolphin_config.yaml")`
  - `src/dolphin/workflows/config/_common.py`, `_displacement.py`, `_unwrap_options.py`, `_enums.py`, `_yaml_model.py`
  - `src/dolphin/unwrap/_constants.py` (`UNW_SUFFIX=".unw.tif"`, `CONNCOMP_SUFFIX=".unw.conncomp.tif"`)
  - `src/dolphin/timeseries.py` (출력 `format_dates(ref, date).tif`, `velocity.tif`, 단위·부호), `src/dolphin/constants.py`
  - README (`mamba install -c conda-forge dolphin`, `dolphin config --slc-files …`, `dolphin run dolphin_config.yaml`, "BSD-3-Clause OR Apache-2.0")
  - LICENSE: "licensed under either the BSD-3-Clause or Apache-2.0 license", Copyright (c) 2022 Caltech
  - PyPI `dolphin` 0.42.7 (2026-06-15), Python ≥3.10 (https://pypi.org/pypi/dolphin/json)
  - 조회일 2026-09-16, main 브랜치. 설치된 dolphin 없음.

## 확인한 사실

- **라이선스**: BSD-3-Clause OR Apache-2.0 → 9장 표의 "확인 필요" 해소. import도 가능하지만 어댑터는 `dolphin run` 서브프로세스만 사용.
- **CLI**: `dolphin run <config.yaml> [--debug]`; `dolphin config` 는 tyro가 `DisplacementWorkflow` 필드에서 플래그를 만든다
  (`cslc_file_list` 별칭 `--cslc`, `--slc-files`; `Strides` `--sx/--sy`; `HalfWindow` `--hwx/--hwy`). 어댑터는 플래그 대신
  **YAML을 직접 생성**한다(필드명이 곧 YAML 키, `to_yaml(by_alias=True)`, `from_yaml → cls(**data)`).
- **YAML 키(검증)**: `cslc_file_list`, `input_options.{subdataset, cslc_date_fmt='%Y%m%d', wavelength, azimuth_blocks, halo_rows}`,
  `work_directory`, `keep_paths_relative`, `mask_file`, `log_file`,
  `worker_settings.{gpu_enabled=False, threads_per_worker=1, n_parallel_bursts=1, block_shape=(512,512)}` (**`n_workers` 필드는 main에 없음**),
  `phase_linking.{ministack_size=15, max_num_compressed=10, output_reference_idx, half_window{x=14,y=7}, use_evd, beta,
  zero_correlation_threshold, shp_method('glrt'|'ks'|'rect'), shp_alpha, mask_input_ps, baseline_lag, compressed_slc_plan, write_crlb, write_closure_phase}`,
  `interferogram_network.{reference_idx, max_bandwidth, max_temporal_baseline, indexes}` (아무것도 없으면 nearest-3),
  `unwrap_options.{run_unwrap, run_goldstein, run_interpolation, unwrap_method, n_parallel_jobs=-1, zero_where_masked,
  preprocess_options, snaphu_options{ntiles, tile_overlap, n_parallel_tiles, init_method('mcf'|'mst'), cost('defo'|'smooth'), single_tile_reoptimize},
  tophu_options{ntiles, downsample_factor, init_method, cost}, spurt_options{…}, whirlwind_options{…}}`,
  `timeseries_options.{run_inversion, method('L1'|'L2'), reference_point(row,col), run_velocity, apply_mask_to_timeseries,
  correlation_threshold=0.2, block_shape, num_parallel_blocks}`, `output_options.{strides{x,y}, epsg, bounds, bounds_epsg=4326, bounds_wkt, …}`,
  `ps_options.amp_dispersion_threshold=0.25`.
- **UnwrapMethod 값**: `snaphu`, `icu`, `phass`, `spurt`, `whirlwind` — `tophu` 값은 없다(`tophu_options`만 존재).
- **출력**: `PS/`, `linked_phase/`, `interferograms/`, `unwrapped/` (`*.unw.tif`, `*.unw.conncomp.tif`), `timeseries/<ref>_<date>.tif`,
  `timeseries/velocity.tif`; 시간 코히어런스는 `temporal_coherence*.tif`(마지막 파일 사용).
- **단위·부호**: `input_options.wavelength`가 있으면 `-1 * (λ / 4π)`를 곱해 **미터, 양수 = 레이더 방향(toward)**; 없으면 라디안.
  `SENTINEL_1_WAVELENGTH = c / 5.405e9`.

## 결정 (Decision)

1. `DolphinEngine`(stages `("timeseries",)`)은 위 키만으로 `dolphin_config.yaml`을 생성(`build_config`)하고 `dolphin run`을 서브프로세스로 실행한다.
   기본값은 쓰지 않고(dolphin 기본 유지) 사용자가 준 값·wintersar가 정하는 값만 기록한다.
2. 입력은 정합 SLC 스택: `dolphin.cslc_files` / `cslc_glob` / `cslc_dir`, 없으면 `igrams`/`coreg_manifest` 아티팩트의 `coreg_slc_dir`
   (topsStack `merged/SLC/*/*.slc.full.vrt`)을 자동 탐색. 없으면 DOL-002(FAIL).
3. `wavelength_m` 기본 = Sentinel-1 파장(미터 출력 강제). `timeseries.coherence_threshold`는 dolphin `correlation_threshold`에 **매핑하지 않는다**
   (의미가 다름: MintPy는 네트워크 가지치기, dolphin은 시계열 픽셀 마스크) — 필요하면 `dolphin.correlation_threshold` 명시.
4. `unwrap_method=tophu` 요청은 DOL-005(WARN)로 알리고 `snaphu`+`snaphu_options.ntiles`로 대체.
5. 버전 핀 `>=0.40,<1`: 키는 main(≈0.42.7)에서만 확인했다.

## 결과 (Consequences)

- `n_workers`(작업 설명의 이름)는 존재하지 않아 `threads_per_worker`(=`_cores`)로 대체; open-questions에 기록.
- 구버전(0.3x 이하)의 YAML 키 차이는 미확인 → 실행 시 dolphin의 pydantic 검증 오류가 DOL-001 로그로 남는다.
- 산출물 정규화는 ADR-0029 참조.
