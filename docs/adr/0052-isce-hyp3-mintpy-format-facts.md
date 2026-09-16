# ADR-0052: ISCE2·HyP3·MintPy 포맷 확인 결과와 `wintersar.io.formats` 규약

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: 플랜 §3.3(포맷 변환은 `wintersar.io` 한곳), §5.6, PERF-05/06(출력 스키마 통일), 규칙 11.2(MintPy import 금지)
- 검증 출처(Sources) — 모두 2026-09-16 조회:
  - ISCE2 `components/isceobj/Image/Image.py`
    <https://github.com/isce-framework/isce2/blob/main/components/isceobj/Image/Image.py>:
    `parameter_list` = BYTE_ORDER, SCHEME, CASTER, NUMBER_BANDS, WIDTH, LENGTH, DATA_TYPE, IMAGE_TYPE,
    FILE_NAME, EXTRA_FILE_NAME, ACCESS_MODE, DESCRIPTION, XMIN, XMAX, ISCE_VERSION; 좌표 facility
    `Coordinate1/2`(startingValue, delta, size); `TO_NUMPY = {'BYTE':'i1','SHORT':'i2','INT':'i4',
    'LONG':…, 'FLOAT':'f4','DOUBLE':'f8','CFLOAT':'c8','CDOUBLE':'c16'}`; `renderVRT` typeMap
    BYTE→Byte, SHORT→Int16, INT→Int32, FLOAT→Float32, DOUBLE→Float64, CFLOAT→CFloat32, CDOUBLE→CFloat64;
    SCHEME ∈ {BIL, BIP, BSQ}; ENDIAN 'l'/'b'.
  - MintPy `mintpy/utils/readfile.py`
    <https://github.com/insarlab/MintPy/blob/main/src/mintpy/utils/readfile.py>: `read_isce_xml`은
    `root.findall('property')`의 `name`을 소문자로, `./component[@name='coordinate1'|'coordinate2']`의
    `delta`/`startingvalue`를 X/Y_STEP·X/Y_FIRST로 읽고 `1e-7 < |step| < 1`이면 degrees;
    `DATA_TYPE_ISCE2NUMPY = {'byte':'uint8','short':'int16','int':'int32','float':'float32',
    'double':'float64','cfloat':'complex64'}`; `read_binary`의 BIL/BIP/BSQ 배치.
  - HyP3 Burst InSAR 제품 가이드 <https://hyp3-docs.asf.alaska.edu/guides/burst_insar_product_guide/>:
    파일 접미사·자료형·단위 표(`_unw_phase.tif` float32 rad, **음수 = 센서 방향 이동**;
    `_wrapped_phase.tif`; `_corr.tif` 0–1; `_conncomp.tif` uint8; `_lv_theta.tif` rad(look-vector
    elevation, −π/2..π/2); `_lv_phi.tif` rad(East 기준, 북쪽으로 증가); `_dem.tif` m(지오이드 보정);
    `_water_mask.tif` uint8 **1 = 육지, 0 = 물**; `_amp.tif`(multi-burst); `.txt` 처리 파라미터),
    UTM 투영, 20x4 = 80 m / 10x2 = 40 m / 5x1 = 20 m.
  - MintPy `prep_hyp3.py` <https://github.com/insarlab/MintPy/blob/main/src/mintpy/prep_hyp3.py>:
    `.txt`는 `key, value = line.strip().replace(' ','').split(':')[:2]`로 파싱; 키 UTCtime,
    Azimuthlooks, Rangelooks, Earthradiusatnadir, Spacecraftheight, Slantrangenear, Heading, Baseline,
    Unwrappingtype; `HEADING = float(Heading) % 360 - 360`, `ORBIT_DIRECTION = ASCENDING if |HEADING| < 90`;
    제품명 정규식(단일/다중 burst)과 날짜 필드; `lv_theta/lv_phi`는 UNIT 'radian'.
  - MintPy `objects/stackDict.py` <https://github.com/insarlab/MintPy/blob/main/src/mintpy/objects/stackDict.py>
    `geometryDict.write2hdf5`: HyP3/Gamma 각도는 `data[data == 0] = nan` 후
    `incidenceAngle = 90 − theta·180/π`, `azimuthAngle = wrap(phi·180/π − 90, [−180, 180])`
    ("theta는 수평면 기준, phi는 East 기준 반시계 양").
  - MintPy `objects/stack.py` <https://github.com/insarlab/MintPy/blob/main/src/mintpy/objects/stack.py>:
    `timeseries.write2hdf5` 데이터셋 `timeseries` float32 (numDate, length, width) [m], `date` bytes
    'YYYYMMDD', `bperp` float32; 속성 `FILE_TYPE`; `GEOMETRY_DSET_NAMES` = height, latitude, longitude,
    rangeCoord, azimuthCoord, incidenceAngle, azimuthAngle, slantRangeDistance, shadowMask, waterMask,
    commonMask, bperp; `DSET_UNIT_DICT` timeseries 'm', velocity 'm/year', temporalCoherence '1',
    incidenceAngle/azimuthAngle 'degree', height 'm'.
  - MintPy 속성 문서 <https://mintpy.readthedocs.io/en/latest/api/attributes/>: X_FIRST/Y_FIRST
    ("첫 픽셀의 **왼쪽 위 모서리**"), X_STEP/Y_STEP, X_UNIT/Y_UNIT(degrees|meters), LENGTH(행), WIDTH(열),
    REF_LAT/REF_LON, REF_X/REF_Y, REF_DATE, **HEADING("북 기준 시계 방향 양")**, ORBIT_DIRECTION,
    WAVELENGTH, EPSG(지오코딩 파일만), UTM_ZONE, FILE_TYPE, UNIT, NO_DATA_VALUE.
  - MintPy `utils/utils0.py` <https://github.com/insarlab/MintPy/blob/main/src/mintpy/utils/utils0.py>:
    `get_lat_lon` 픽셀 중심 = `Y_FIRST + Y_STEP·(row + 0.5)`; `azimuth2heading_angle` 문서 —
    azimuthAngle은 "북 기준 반시계 양"(상승 ≈ 102°, 하강 ≈ −102°), heading(우측 관측) = −(az − 90)
    (상승 ≈ −12°, 하강 ≈ −168°); `enu2los`는 "위성 방향 이동이 양".
  - 구현 `src/wintersar/io/formats.py`, 테스트 `tests/unit/io/test_formats.py`.

## 맥락 (Context)

엔진 출력을 `IgramStack`/`TimeSeries`로 바꾸는 코드는 `wintersar.io`에만 둔다(§3.3). 각 포맷의
필드 이름·부호·단위·nodata를 추측하지 않고 확인해야 Phase 4(부호 규약 검수)와 A/B(PERF-05/06)가
성립한다.

## 결정 (Decision)

### ISCE2 (`read_isce_raster`, `parse_isce_xml`)

- 사이드카 `<file>.xml`의 `property`/`component` 이름을 **대소문자 무시**로 읽는다(ISCE는 소문자로
  렌더링하고 MintPy도 소문자화한다; 파라미터 목록은 대문자 이름이므로 둘 다 받는다).
- 자료형은 **VRT/MintPy 해석**(BYTE = uint8)을 따른다. ISCE `TO_NUMPY`의 `'i1'`과 어긋나지만 마스크
  값 0–3·255 처리에 unsigned가 맞고 GDAL(`Byte`)도 unsigned다. `LONG`은 플랫폼 의존이라 int64로 둔다.
- 스킴: BSQ (band, line, pixel), BIL (line, band, pixel), BIP (line, pixel, band). 파일 크기가 사이드카
  계산치보다 작으면 `IsceFormatError`.
- `.xml`이 없으면 ISCE가 함께 쓰는 `.vrt`(VRTRawRasterBand)를 rasterio로 연다. 둘 다 없으면
  `FileNotFoundError`.
- 지오코딩 파일(`.geo`)의 coordinate1/2 → `X_FIRST/X_STEP/Y_FIRST/Y_STEP`, MintPy 규칙으로 `*_UNIT = degrees`.

### HyP3 (`read_hyp3_product_dir`)

- 접미사 표는 제품 가이드 그대로(`HYP3_SUFFIXES`); `_clip` 파일이 있으면 우선(MintPy 문서의 공통 범위
  클립 관행). 필수는 `_unw_phase`·`_corr`.
- **부호를 뒤집지 않는다.** HyP3 unwrapped phase는 "음수 = 위성 방향"이고, 합성기
  `synth.PHASE_PER_M_LOS = −4π/λ`(위성 방향 변위 양 → 위상 음)와 같은 규약이다. `IgramStack.unw`는
  따라서 라디안, 음수 = 위성 방향.
- nodata: `unw`의 래스터 nodata(보통 0)와 `conncomp == 0` 픽셀은 NaN; `mask = 물(water_mask == 0) |
  ~isfinite(unw)`.
- `.txt`는 prep_hyp3와 같은 방식으로 파싱해 `attrs["hyp3_meta"][pair]`에 둔다. `Heading`은 (−180, 180]로
  정규화해 `attrs["heading_deg"]`(MintPy HEADING 규약과 동일 부호; 상승 ≈ −12°, 하강 ≈ −168°).
- 기하: stackDict와 동일하게 0 → NaN, `incidence_deg = 90 − deg(lv_theta)`,
  `azimuth_deg = wrap(deg(lv_phi) − 90)`(북 기준 반시계, MintPy azimuthAngle), `dem_m`.
- 모든 쌍의 래스터 크기가 같아야 한다. 다르면 쌍 키를 담아 `Hyp3FormatError`(HyP3 어댑터 HYP3-009와 연결).

### MintPy HDF5 (`read_timeseries_h5`, `write_timeseries_h5`)

- `h5py`만 사용. 데이터셋 `timeseries`/`date`(/`bperp`), 루트 속성은 문자열.
- 지오코딩 파일은 `X_FIRST…`로 **픽셀 중심** 격자를 만든다(`+0.5`); `X_UNIT = meters` + `EPSG`면 pyproj로
  EPSG:4326 변환. 레이더 좌표 파일은 `geometryRadar.h5`의 `latitude/longitude`가 필요하다.
- 사이드카 기본 탐색: `geometryGeo.h5`/`inputs/geometryGeo.h5`(incidenceAngle[degree], height[m]),
  `velocity.h5`(velocity[m/year]), `temporalCoherence.h5`, `maskTempCoh.h5`; `geo_` 접두 파일은 `geo_`
  사이드카를 본다.
- `HEADING` → `TimeSeries.heading_deg`(북 기준 시계 방향, `io/timeseries.py` 규약과 동일), `REF_LAT/REF_LON`
  → `reference_latlon`, `REF_DATE` 없으면 첫 날짜.
- `write_timeseries_h5`는 픽스처·fake 결과용이며 `Y_FIRST = lat[0] − dy/2`(중심 → 모서리)로 쓴다.

### `.npz` (`read_timeseries_npz`, `write_timeseries_npz`)

- fake engine 형식(`dates`, `displacement_m`, `velocity_m_per_yr`)을 읽고, 좌표가 없으면 서울 AOI 중심
  (37.55, 126.95)·80 m(HyP3 기본 20x4 looks) 정규 격자를 만들어 `attrs["latlon_synthetic"] = True`로
  표시한다. 실데이터 파일에는 항상 `lat`/`lon`이 있어야 한다.

## 결과 (Consequences)

- `engines/mintpy.read_timeseries_h5`와 중복이다. `io.formats`가 정본이므로 엔진 어댑터가 이를 호출하도록
  바꾸는 것을 통합자에게 제안한다(needs_from_others). 테스트 `test_h5_reader_matches_engines_reader_when_available`가
  두 구현의 일치를 확인한다.
- ISCE BYTE 부호(`i1` vs `uint8`)는 값 ≥ 128을 쓰는 사용자 정의 파일에서만 차이가 난다 — open-questions에 기록.
- HyP3 `lv_phi` 기준(East)과 MintPy(North)의 90° 차이는 이 모듈 안에서만 변환하고, 다른 모듈은
  `attrs["azimuth_deg"]`(MintPy 규약)만 본다.
