# ADR-0051: 결과 COG 내보내기 설정 (PERF-08, R-12)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: PERF-08, R-12(QGIS), 플랜 §6.2
- 검증 출처(Sources):
  - GDAL COG 드라이버 문서 <https://gdal.org/en/stable/drivers/raster/cog.html> — 생성 옵션
    `BLOCKSIZE`(기본 512, 16의 배수), `COMPRESS`(NONE/LZW/JPEG/DEFLATE/ZSTD/WEBP/LERC/…, 기본 LZW),
    `LEVEL`(DEFLATE 6, ZSTD 9), `PREDICTOR`(YES/NO/STANDARD/FLOATING_POINT, 기본 NO),
    `OVERVIEWS`(AUTO/IGNORE_EXISTING/FORCE_USE_EXISTING/NONE), `OVERVIEW_RESAMPLING`/`RESAMPLING`,
    `BIGTIFF`(IF_NEEDED), `NUM_THREADS`; GDAL ≥ 3.1, `Create()`는 3.13부터(그 전엔 CreateCopy만).
  - rasterio 쓰기 문서 <https://rasterio.readthedocs.io/en/stable/topics/writing.html> —
    CreateCopy 전용 포맷은 `IndirectRasterUpdater`가 임시 인메모리 데이터셋 + `GDALCreateCopy()`로 씀.
  - 이 환경 실측 (rasterio 1.4.4 / GDAL 3.10.3, 2026-09-16): `rasterio.open(path, "w",
    driver="COG", COMPRESS=…, BLOCKSIZE=…, OVERVIEWS="AUTO")`와 `rasterio.shutil.copy(tmp, path,
    driver="COG", …)` 모두 `IMAGE_STRUCTURE.LAYOUT == "COG"`·오버뷰 생성 확인. 40×30처럼 한 블록보다
    작은 래스터는 AUTO 오버뷰가 생기지 않는다(오류 아님).
  - 구현 `src/wintersar/io/cog.py`, 테스트 `tests/unit/io/test_cog.py`.

## 맥락 (Context)

속도 지도·누적 변위·코히어런스·마스크를 QGIS 플러그인과 외부 GIS가 빠르게 읽으려면 타일·오버뷰가
있는 COG가 필요하다(§6.1 PERF-08 "결과는 COG(오버뷰 포함)"). 압축기·예측기·블록 크기·오버뷰
리샘플링·nodata·좌표계 규약을 한곳에서 정해야 파일마다 설정이 달라지지 않는다.

## 선택지 (Options)

1. GTiff 드라이버 + `TILED=YES` + `build_overviews` (모든 GDAL에서 동작, COG 규격 보장은 없음).
2. GDAL COG 드라이버(CreateCopy 경유) + 읽기 검증, COG 드라이버가 없을 때만 1로 폴백.
3. `rio-cogeo` 등 추가 의존성.

## 결정 (Decision)

선택지 2.

| 항목 | 값 | 근거 |
|---|---|---|
| 드라이버 | `COG` (없으면 GTiff tiled + overviews 폴백) | 규격 보장, 문서의 옵션명 그대로 |
| 압축 | 기본 `DEFLATE`, 선택 `ZSTD`/`LZW`/`NONE` | DEFLATE는 모든 GDAL 빌드에 있음; ZSTD는 빌드 옵션(여기선 사용 가능) |
| PREDICTOR | float → `FLOATING_POINT`(3), 정수 → `STANDARD`(2), NONE 압축이면 없음 | 부동소수 필드 압축률 |
| BLOCKSIZE | 512 (16의 배수만 허용) | GDAL 기본값 |
| OVERVIEWS | `AUTO`; 연속 필드 `AVERAGE`, 마스크·connected component `NEAREST` | 범주 값 평균 금지 |
| BIGTIFF | `IF_NEEDED` | L 사이트 스택 |
| nodata | float `NaN`, uint8 마스크 `255` | 정수 배열에 NaN 금지(검증) |
| 좌표계 | `EPSG:4326`; `TimeSeries.lat/lon`은 **픽셀 중심** 격자이므로 변환 원점은 중심 − ½픽셀 | MintPy 픽셀 중심 규약(ADR-0052)과 일관 |
| 방향 | 항상 north-up: 위도가 행과 함께 증가하면 배열을 뒤집어 씀 | GIS 기본 |
| 태그 | `UNIT`, `SIGN`("positive = towards satellite (LOS)"), `START/END/DATE/REF_DATE`, 밴드 설명 | 부호 규약 명시(Phase 4 검수 항목) |
| 검증 | 쓰기 후 `check_cog`: tiled, 래스터가 한 블록보다 크면 오버뷰 존재 | 깨진 파일 조기 발견 |

비정규 격자(레이더 좌표)는 `ValueError`로 거부한다 — 지오코딩이 먼저다.

## 결과 (Consequences)

- `export_timeseries_cogs`가 QGIS 플러그인의 입력 계약이다: `velocity.tif`, `displacement_<YYYYMMDD>.tif`,
  `coherence.tif`, `conncomp.tif`, `mask.tif`, 선택 `displacement_stack.tif`(밴드 = 날짜).
- GDAL 3.13+에서 COG `Create()`가 생기면 rasterio가 직접 쓰게 되지만 옵션은 그대로다.
- `velocity_m_per_yr`가 없으면 픽셀별 최소제곱 기울기(`fit_velocity`)로 채운다. 이는 SBAS
  역산이 아니라 시계열의 선형 적합이며(규칙 11.3 위반 아님), 태그로 구분하지는 않는다 — 엔진
  속도 지도가 있으면 항상 그것을 쓴다.
