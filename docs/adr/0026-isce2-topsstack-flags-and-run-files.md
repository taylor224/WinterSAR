# ADR-0026: ISCE2 topsStack `stackSentinel.py` 플래그·run_files 사실과 어댑터 매핑

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-04(로컬 ISCE2 경로), PERF-01(burst→SAFE), PERF-07(run_files 병렬), 플랜 5.2 `isce2_topsstack.py`, 12.3 표 5행
- 검증 출처(Sources):
  - `stackSentinel.py` (createParser, get_dates, checkCurrentStatus, slcStack/interferogramStack, main):
    https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/stackSentinel.py
  - `Stack.py` (run 클래스: write_wrapper_config2run_file, 각 단계 config 파일 내용, sentinelSLC.get_orbit):
    https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/Stack.py
  - `mergeBursts.py` (`.full` 접미사·VRT 규칙): https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/mergeBursts.py
  - topsStack README: https://github.com/isce-framework/isce2/blob/main/contrib/stack/topsStack/README.md
  - contrib/stack README(PATH 설정·단일 스택 프로세서 경고): https://github.com/isce-framework/isce2/blob/main/contrib/stack/README.md
  - ISCE2 LICENSE(Apache-2.0, Caltech): https://github.com/isce-framework/isce2/blob/main/LICENSE
  - `isce2/__init__.py` (`__version__ = release_version`, ISCE_HOME 미설정 시 "Using default ISCE Path" 출력)
  - `applications/gdal2isce_xml.py` (`-i/--input`, `gdal2isce_xml(fname)`)
  - MintPy `src/mintpy/cli/prep_isce.py` EXAMPLE, `docs/dir_structure.md` topsStack 절, `utils/isce_utils.py` `read_tops_baseline`
  - burst2safe README·`src/burst2safe/burst2safe.py` argparse, PyPI 2.0.3 (2026-08-21, BSD-2-Clause)
  - 조회일 2026-09-16 (main 브랜치; 설치된 ISCE2 없음 → 실행 검증은 Phase 5 S 사이트에서)

## 맥락 (Context)

플랜 5.2의 `isce2_topsstack.py`는 "플래그를 추측하지 말 것"(규칙 11.3)이 가장 큰 위험이었다.
stackSentinel.py의 argparse 전체와 run_files 생성 규칙, 산출물 경로, MintPy `prep_isce`가 기대하는
디렉터리 구조를 소스에서 확인해 어댑터·테스트에 고정한다.

## 확인한 사실

### `stackSentinel.py` 인자 (createParser)

| 플래그 | dest | 기본값 | 어댑터 매핑 (`TopsStackArgs`) |
|---|---|---|---|
| `-s/--slc_directory` (필수) | slc_dirname | – | `slc_dir` (fetch 단계 산출 `SLC/`) |
| `-o/--orbit_directory` (필수) | orbit_dirname | – | `orbit_dir` (없으면 `<workdir>/orbits`) |
| `-a/--aux_directory` (필수) | aux_dirname | – | `aux_dir` (빈 폴더 허용, `<workdir>/aux_cal`) |
| `-w/--working_directory` | work_dir | './' | 영속 작업 폴더(ADR-0029) |
| `-d/--dem` (필수) | dem | – | `dem` (+ `.xml` 사이드카 필수, 없으면 `gdal2isce_xml.py -i`) |
| `-p/--polarization` | polarization | 'vv' | stack.polarization 소문자 |
| `-W/--workflow` | workflow | interferogram (slc/correlation/interferogram/offset) | `workflow` |
| `-n/--swath_num` | swath_num | '1 2 3' | stack.subswaths → "2" 등 |
| `-b/--bbox` | bbox | None ("Lat/Lon Bounding SNWE", 예 '19 20 -99.5 -98.5') | AOI WKT bounds → `S N W E` |
| `-x/--exclude_dates`, `-i/--include_dates` | – | None ('20141007,20141031') | 선택 스택 날짜를 `-i`로 고정 |
| `--start_date`/`--stop_date` | – | None (YYYY-MM-DD) | 선택적 |
| `-C/--coregistration` | coregistration | NESD (geometry/NESD) | `engine.esd: true → NESD`, false → geometry |
| `-m/--reference_date` | reference_date | None ("첫 날짜를 참조로 사용") | stack.reference_date |
| `--snr_misreg_threshold` | snrThreshold | '10' | 선택적 |
| `-e/--esd_coherence_threshold` | esdCoherenceThreshold | '0.85' | NESD일 때만 |
| `-O/--num_overlap_connections` | num_overlap_connections | '3' | NESD일 때만 |
| `-c/--num_connections` | num_connections | '1' | 선택 쌍을 덮는 최소 nearest-N(`derive_num_connections`) |
| `-z/--azimuth_looks`, `-r/--range_looks` | azimuthLooks/rangeLooks | '3'/'9' | `engine.looks` 또는 `select.looks.compute_looks` |
| `-f/--filter_strength` | filtStrength | '0.5' | `engine.filter.alpha` (type none → 0) |
| `-u/--unw_method` | unwMethod | snaphu (icu/snaphu) | 기본은 wintersar.unwrap가 담당, `unwrap_in_isce`일 때만 |
| `-rmFilter/--rmFilter` | rmFilter | False | `rm_filter` |
| `--param_ion`, `--num_connections_ion` | – | None/'3' | 전달만 (이온층 워크플로 미지원) |
| `-useGPU/--useGPU` | useGPU | False | `_gpu` |
| `--num_proc` | numProcess | 1 | **항상 1** — 병렬은 wintersar runfiles가 수행 |
| `--num_proc4topo` | numProcess4topo | 1 | `_cores` (topo.py 내부 병렬) |
| `-t/--text_cmd` | text_cmd | '' | 선택적 |
| `-V/--virtual_merge` | virtualMerge | None ("interferogram/correlation는 True, slc/offset은 False") | 선택적 |

### run_files

- 이름 `'run_{:02d}_<step>'`; 한 줄 = `text_cmd + 'SentinelWrapper.py -c ' + config` (numProcess>1이면 ` &` + `wait`).
- interferogram(NESD) 순서: unpack_topo_reference → unpack_secondary_slc → average_baseline → extract_burst_overlaps →
  overlap_geo2rdr → overlap_resample → pairs_misreg → timeseries_misreg → fullBurst_geo2rdr → fullBurst_resample →
  extract_stack_valid_region → merge_reference_secondary_slc → generate_burst_igram → merge_burst_igram →
  filter_coherence → unwrap. geometry 정합은 overlap/misreg 4단계가 빠진다. slc 워크플로는 merge 단계가 `mergeSLC` 조건부.
- `run_files/`가 있으면 stackSentinel.py는 `sys.exit(1)` ("Please remove or rename this folder") → 어댑터는
  `run_files`·`configs`를 `.bak-<UTC>`로 옮긴 뒤 재생성(ISCE2-015).
- SAFE 탐색은 `S1*_IW_SLC*zip` 우선, 없으면 `S1*_IW_SLC*SAFE`; bbox와 교차하지 않는 SAFE는 제외.
- 궤도: `sentinelSLC.get_orbit`가 `-o` 폴더의 `*.EOF`를 찾고 없으면 `fetchOrbit.py -i <safe> -o <work>/orbits` 실행.
  → 어댑터는 EOF가 없을 때 ISCE2-014(INFO)만 내고 topsStack에 맡긴다(오프라인은 사전 채움).

### 산출물 (Stack.py config)

- `merged/interferograms/<ref>_<sec>/fine.int`(-r/-z 룩 적용, `.full` 은 VRT), `filt_fine.int`, `filt_fine.cor`
  (FilterAndCoherence), `filt_fine.unw`(+`.conncomp`)
- `merged/geom_reference/{lat,lon,los,hgt,shadowMask,incLocal}.rdr`, `merged/SLC/<date>/<date>.slc[.full][.vrt]`
- `baselines/<ref>_<sec>/<ref>_<sec>.txt` — MintPy는 `"Bperp (average):"` 줄을 읽음
- MintPy: `prep_isce.py -f "./merged/interferograms/*/filt_*.unw" -m ./reference/IW1.xml -b ./baselines/ -g ./merged/geom_reference/`,
  `mintpy.load.{metaFile=reference/IW*.xml, baselineDir, unwFile, corFile, connCompFile, demFile=hgt.rdr, lookupY/X=lat/lon.rdr,
  incAngleFile=azAngleFile=los.rdr, shadowMaskFile}` → 어댑터가 `prep_isce.json`으로 기록.

### burst2safe

CLI `burst2safe <granules…> [--orbit N] [--extent …] [--pols VV VH] [--swaths IW1…] [--mode IW] [--min-bursts 1]
[--all-anns] [--output-dir DIR] [--keep-files] [-v]`; 인증 `EARTHDATA_TOKEN` > `~/.netrc` > `EARTHDATA_USERNAME/PASSWORD`;
ISCE2/topsStack 호환 "Yes | 2.6.3 | None". 미설치 → ENV-006(WARN) + fetch 단계에서는 ISCE2-007(FAIL, slc_dir 대안 안내).

## 결정 (Decision)

1. 플래그는 `TopsStackArgs.to_argv()` 한 곳에서만 생성하고 위 표를 테스트(`test_stacksentinel_argv_exact_flags`)로 고정한다.
2. 단계 매핑: fetch(SAFE/궤도/aux/DEM → `slc_manifest`) · coregister(run_files 생성 + `merge_reference_secondary_slc`까지 →
   `coreg_manifest`) · interferogram(`generate_burst_igram`…`filter_coherence` → `igrams`=`merged/interferograms`, ISCE 평면 바이너리
   목록 `igrams_manifest.json` + `prep_isce.json`) · multilook(통과; 룩은 merge_burst_igram이 적용).
3. 언래핑은 기본적으로 wintersar.unwrap(스케줄러·타일) 담당; `isce2.unwrap_in_isce=true`일 때만 `run_NN_unwrap`을 실행하고 `unw`도 낸다.
4. 버전 탐지는 `shutil.which('stackSentinel.py')` + `python3 -c "import isce; print('ISCE_VERSION='+isce.__version__)"` 서브프로세스
   (마커 기반 파싱; in-process import 금지). PATH에는 있으나 import 실패 → ISCE2-016(WARN).
5. 네트워크: topsStack은 쌍 목록을 받지 못하므로 선택 쌍을 모두 덮는 최소 nearest-N을 `-c`로 주고(ISCE2-013), 초과 쌍은
   시계열 단계 임계값으로 가지치기한다.

## 결과 (Consequences)

- `-c` 유도로 선택 네트워크보다 많은 쌍이 생성될 수 있다(디스크·시간 증가). 정확한 쌍 제어가 필요하면 후속으로 config 파일
  편집 방식(run_files 생성 후 불필요 쌍 제거)을 검토한다.
- 실제 ISCE2 설치 환경에서의 실행 검증(플래그 파싱·경로)은 Phase 5 S 사이트 벤치에서 수행한다(open-questions #46).
