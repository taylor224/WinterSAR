# ADR-0021: MintPy 어댑터 — 템플릿 키 확인, 단계 순서, PERF-09 자동 설정, HDF5 읽기 규약

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-05, PERF-02, PERF-09, 플랜 §5.2 mintpy.py, §9(GPL 경계), 규칙 11.2
- 검증 출처(Sources):
  - MintPy 1.6.4 (PyPI 2026-07-25): https://pypi.org/pypi/mintpy/json
  - 템플릿: https://github.com/insarlab/MintPy/blob/main/src/mintpy/defaults/smallbaselineApp.cfg
    (키 목록 스냅샷 `tests/fixtures/mintpy/smallbaselineApp_keys.txt`)
  - 단계 목록: https://github.com/insarlab/MintPy/blob/main/src/mintpy/defaults/template.py (`STEP_LIST`)
  - CLI: https://github.com/insarlab/MintPy/blob/main/src/mintpy/cli/smallbaselineApp.py
    (`customTemplateFile` 위치 인자, `--dir/--work-dir`, `--dostep`, `-v/--version`)
  - 버전 문자열: https://github.com/insarlab/MintPy/blob/main/src/mintpy/version.py
    (`"MintPy version {v}, date {d}"`)
  - 파일명 체인: https://github.com/insarlab/MintPy/blob/main/src/mintpy/smallbaselineApp.py
    (`get_timeseries_filename`, `run_geocode`)
  - HDF5 레이아웃: https://github.com/insarlab/MintPy/blob/main/src/mintpy/objects/stack.py
    (`timeseries.write2hdf5`), 속성: https://mintpy.readthedocs.io/en/latest/api/attributes/
  - 픽셀 중심 규약: https://github.com/insarlab/MintPy/blob/main/src/mintpy/utils/utils0.py (`get_lat_lon`)
  - 디렉터리·패턴 예시: https://github.com/insarlab/MintPy/blob/main/docs/dir_structure.md
  - hyp3 로드 시 `prep_hyp3` 자동 호출: https://github.com/insarlab/MintPy/blob/main/src/mintpy/load_data.py (`prepare_metadata`)

## 맥락 (Context)

MintPy는 GPL-3이므로 import 없이 `smallbaselineApp.py`를 subprocess로 단계별 실행하고 결과 HDF5는
h5py로 읽어야 한다(ADR-0001). PERF-09는 `mintpy.compute.*` 기본값(maxMemory 4 GB, numWorker 4)이
머신에 맞지 않아 역산·DEM 오차 단계가 느리거나 OOM이 나는 문제를 머신 스펙 자동 설정으로 없앤다.

## 확인한 사실 (Verified facts)

- **단계 순서(1.6.4 STEP_LIST)**: load_data, modify_network, reference_point, quick_overview,
  correct_unwrap_error, invert_network, correct_LOD, correct_SET, correct_ionosphere,
  correct_troposphere, deramp, correct_topography, residual_RMS, reference_date, velocity, geocode,
  google_earth, hdfeos5.
- **CLI**: `smallbaselineApp.py <template> --dostep <step> --dir <workdir>`; 템플릿 파일명이
  `smallbaselineApp.cfg`이면 사용자 템플릿으로 인식되지 않으므로 `wintersar_mintpy.cfg`를 쓴다.
  `--version` 출력 `MintPy version 1.6.4, date 2026-07-25`를 정규식으로 파싱한다.
- **템플릿 키**(값 범위는 cfg 주석): `mintpy.load.processor=[isce, aria, hyp3, …]`, `mintpy.load.{unwFile,
  corFile, connCompFile, demFile, incAngleFile, azAngleFile, waterMaskFile, metaFile, baselineDir,
  lookupYFile, lookupXFile, shadowMaskFile}`, `mintpy.load.updateMode=[yes/no]`,
  `mintpy.network.{tempBaseMax, perpBaseMax, coherenceBased, minCoherence, keepMinSpanTree}`,
  `mintpy.reference.lalo=[31.8,130.8 / auto]`, `mintpy.unwrapError.method=[bridging / phase_closure /
  bridging+phase_closure / no]`, `mintpy.troposphericDelay.method=[pyaps / height_correlation / gacos /
  opera / no]`, `.weatherModel=[ERA5 / MERRA / NARR]`, `.weatherDir`, `.gacosDir`, `mintpy.deramp=[no /
  linear / quadratic]`, `mintpy.topographicResidual=[yes/no]`, `mintpy.compute.cluster=[local / slurm /
  pbs / lsf / none]`, `mintpy.compute.numWorker=[int > 1 / all / num%]`, `mintpy.compute.maxMemory=[float,
  GB]`, `mintpy.geocode`, `mintpy.save.kmz`, `mintpy.save.hdfEos5`, `mintpy.plot`.
  `TEMPLATE_KEYS` 튜플은 스냅샷 파일과 테스트로 대조된다(`test_every_emitted_key_exists_in_official_template`).
- **HyP3 패턴**: `hyp3/*/*unw_phase_clip.tif` 형태(문서 예시). wintersar 레이아웃은
  `<data_dir>/<pair>/<product>/…`이므로 `*/*/*_unw_phase[_clip].tif` 등을 절대경로로 기록한다.
  processor=hyp3이면 load_data 단계가 `prep_hyp3.py`를 자동 실행하므로 별도 호출이 없다.
- **topsStack 패턴**: `reference/IW*.xml`, `baselines`, `merged/interferograms/*/filt_*.{unw,cor,unw.conncomp}`,
  `merged/geom_reference/{hgt,lat,lon,los,shadowMask}.rdr`.
- **출력 파일명 체인**: `timeseries.h5` → pyaps `_ERA5` / height_correlation `_tropHgt` / gacos `_GACOS`
  → deramp `_ramp` → topographicResidual(auto yes) `_demErr`; `velocity.h5`, `temporalCoherence.h5`,
  `maskTempCoh.h5`, `inputs/ifgramStack.h5`, `inputs/geometryGeo.h5|geometryRadar.h5`. 입력에
  `Y_FIRST`가 있으면(HyP3 등 지오코딩 산출물) geocode 단계는 건너뛰고, 아니면 `geo/geo_*.h5`.
- **timeseries.h5**: 루트 데이터셋 `timeseries`(float32, (n_date, length, width), m), `date`(bytes
  YYYYMMDD), `bperp`(float32, 선택); 모든 속성은 루트 attrs에 문자열. 좌표: `X_FIRST/Y_FIRST`는 첫
  픽셀 좌상단 모서리이고 MintPy는 픽셀 중심을 `Y_FIRST + Y_STEP*(row+0.5)`로 계산한다(get_lat_lon).
  UTM이면 `X_UNIT=meters` + `EPSG`. 참조점 `REF_LAT/REF_LON/REF_X/REF_Y/REF_DATE`, `HEADING`(북 기준
  시계방향, HyP3는 음수로 정규화), `UNIT='m'`, `FILE_TYPE='timeseries'`.

## 선택지 (Options)

1. MintPy 파이썬 API import — GPL 전염, 기각(규칙 11.2).
2. 전체 `smallbaselineApp.py` 1회 실행 — 단계별 캐시·진단 불가.
3. `--dostep` 단계별 subprocess + 단계별 로그 + h5py 리더 — 채택.

## 결정 (Decision)

- `MintPyEngine`: `stages=('timeseries','corrections','geocode')`, 단계 매핑은 STEP_LIST[0:6] /
  [6:15] / [15:18] (hdfeos5는 `mintpy.save.hdfEos5=yes`일 때만). 각 단계는 `log_dir/mintpy_<step>.log`,
  실패 시 MP-001(+로그 패턴에 따라 MP-002 OOM, MP-003 입력 없음) Finding을 `<stage>.findings.json`에
  쓰고 `MintPyStepError`를 던진다. corrections/geocode는 `mintpy_workdir` 아티팩트로 작업 폴더를 물려받는다.
- **PERF-09 자동값**(`mintpy_template.compute_settings`): `cluster=local`, `numWorker = cores-1`
  (2 미만이면 `cluster=none`, cfg가 int>1을 요구), `maxMemory = 0.8 × RAM`(GB, 소수 1자리; 실행기가
  `_cores/_memory_gb` 예산을 주면 그 값). `mintpy.load.updateMode=yes`로 재실행을 건너뛰게 한다.
- **설정 매핑**: `timeseries.reference_point=[lat,lon]` → `mintpy.reference.lalo`(auto면 MP-006 WARN),
  `troposphere: era5→pyaps+ERA5+weatherDir=<cache>/weather/ERA5`(PERF-02), `gacos→gacos+gacosDir`,
  `height_correlation`, `none→no`; `deramp`, `unwrap_error_correction` 1:1;
  `selection.max_temporal_baseline_days/max_perp_baseline_m` → `mintpy.network.tempBaseMax/perpBaseMax`;
  `timeseries.coherence_threshold` → `mintpy.network.coherenceBased=yes` + `minCoherence`
  (MST 유지) — **도메인 검토 항목**(open-questions #36; 연구자 확인 전까지 기본값 0.7 유지).
- `read_timeseries_h5(path, geometry_path=…)`는 `wintersar.io.timeseries.TimeSeries`를 반환하며
  lat/lon은 픽셀 중심, UTM은 pyproj로 변환, 레이더 좌표는 geometry의 `latitude/longitude` 필요.

## 결과 (Consequences)

- MintPy가 실제로 설치된 환경의 스모크 테스트(`@pytest.mark.engine`)는 Phase 2 DoD에서 별도 실행.
- 템플릿 키 변경(MintPy 2.x)은 스냅샷 테스트가 먼저 깨지도록 되어 있다.
- 부호 규약(LOS 양수 방향)은 ADR-0040(validate)에서 확정하며, 리더는 값을 변환하지 않는다.
