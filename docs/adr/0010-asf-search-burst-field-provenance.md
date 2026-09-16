# ADR-0010: asf_search SLC-BURST 제품의 필드 출처 (검색 시점 vs 다운로드 후)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-01, SEL-08, SEL-09, PERF-01, 미확정 사항 #1
- 검증 출처(Sources):
  - `.venv/lib/python3.11/site-packages/asf_search/ASFProduct.py` (`_base_properties`, `umm_get`)
  - `.venv/lib/python3.11/site-packages/asf_search/Products/S1Product.py` (`pgeVersion`, `get_state_vectors`)
  - `.venv/lib/python3.11/site-packages/asf_search/Products/S1BurstProduct.py` (`burst` 하위 dict, `additionalUrls`)
  - 실제 CMR 응답 (2026-09-16, provider ASF, `SENTINEL-1_BURSTS` 컬렉션, 서울 AOI):
    `tests/fixtures/asf/burst_products.json`, `slc_products.json`
  - SAFE annotation 요소 경로: ISCE2 `components/isceobj/Sensor/TOPS/Sentinel1.py`,
    isce-framework `s1-reader/src/s1reader/s1_annotation.py`, bopen `xarray-sentinel/xarray_sentinel/sentinel1.py`
  - ASF burst 메타데이터 XML 구조: ASFHyP3 `burst2safe/src/burst2safe/utils.py::get_subxml_from_metadata`
    및 같은 저장소 `tests/test_data/*.xml` (실제 파일 구조 확인, 복사하지 않음)
  - Sentinel-1 IW SLC 명목 픽셀 간격 2.3 m × 14.1 m: Copernicus SentiWiki S1 Products 페이지
    (`https://sentiwiki.copernicus.eu/web/s1-products`, 2026-09-16 조회)

## 맥락

플랜 §5.1.1은 "어떤 필드가 검색 API에서 바로 오고 어떤 필드가 다운로드 후에만 있는지 구현 시 확인"을
요구한다(미확정 사항 #1). SEL-08(IPF 버전 차이)과 SEL-09(픽셀 간격 편차)의 판정 시점이 여기에 달려 있다.

## 확인한 사실

asf_search 14.0.0의 `S1BurstProduct.properties`는 CMR UMM-G 그래뉼 레코드에서 만들어지며, 다음이
**검색 응답만으로** 채워진다 (실제 응답으로 확인):

| BurstRecord 필드 | asf_search 속성 | CMR 원천 | 예시 값 |
|---|---|---|---|
| granule_id | `sceneName` (= `fileID`) | `GranuleUR` | `S1_270859_IW2_20240107T093233_VV_0C4A-BURST` |
| platform | `platform` | `ASF_PLATFORM` | `SENTINEL-1A` (SLC는 `Sentinel-1A`) |
| mode | `beamModeType` | `BEAM_MODE` | `IW` |
| subswath | `burst.subswath` | `SUBSWATH_NAME` | `IW2` |
| full_burst_id | `burst.fullBurstID` | `BURST_ID_FULL` | `127_270859_IW2` (`<track>_<relativeBurstID>_<subswath>`) |
| relative_orbit | `pathNumber` | `PATH_NUMBER` | `127` |
| absolute_orbit | `orbit` | `OrbitCalculatedSpatialDomains` | `51999` |
| flight_direction | `flightDirection` | `ASCENDING_DESCENDING` | `ASCENDING` |
| polarization | `polarization` | `POLARIZATION` | `VV` (SLC는 `VV+VH`) |
| acquisition_time | UMM `TemporalExtent…BeginningDateTime` (μs) / `startTime` (초) | | `2024-01-07T09:32:34.976057Z` |
| **ipf_version** | **`pgeVersion`** | `PGEVersionClass.PGEVersion` (PGEName `Sentinel-1 IPF`) | `003.71` → 정규화 `3.71` |
| footprint_wkt | `geometry` | `SpatialExtent` GPolygon | |
| url | `url` | `RelatedUrls[USE SERVICE API]` | `https://sentinel1-burst.asf.alaska.edu/<SLC>/IW2/VV/2.tiff` |
| extra.metadata_url | `additionalUrls[0]` | 같은 항목의 두 번째 URL | `…/IW2/VV/2.xml` |
| extra.state_vectors | `baseline.stateVectors` | `SV_POSITION_PRE/POST`, `SV_VELOCITY_PRE/POST` | ECEF 위치·속도 2개(10 s 간격) |
| extra.azimuth_time_interval_s, lines_per_burst | (asf_search 미매핑) UMM `AZIMUTH_TIME_INTERVAL`, `LINES_PER_BURST` | | `0.0020556`, `1506` |

**검색 응답에 없는 것** — `range_pixel_spacing_m`, `azimuth_pixel_spacing_m`, `incidence_near/far_deg`.
이 값들은 다운로드 후 SAFE annotation(`imageAnnotation/imageInformation/rangePixelSpacing`,
`azimuthPixelSpacing`, `incidenceAngleMidSwath`, `geolocationGrid/…/incidenceAngle`) 또는 ASF burst
XML(`burst/metadata/product[swath,polarisation]/content` 안에 동일한 annotation 포함, IPF 버전은
`burst/manifest//safe:software[@name="Sentinel-1 IPF"]/@version`)에서 읽는다. 실측 IW1 값:
2.329562 m × 13.97051 m, 입사각 30.69°(근) ~ 36.77°(원).

burst XML 다운로드는 Earthdata 인증(asf-urs 쿠키)이 필요하므로 검색 단계에서는 읽을 수 없다.

## 선택지

1. 검색 단계에서 burst XML까지 내려받아 픽셀 간격을 채운다 (인증 필수, 느림).
2. 검색 단계는 CMR 값만 사용하고, 픽셀 간격·입사각은 `metadata.enrich_record`로 다운로드 후 채운다.
   SEL-09는 값이 없으면 IW 명목값(2.3 × 14.1 m)으로 looks를 계산하고 "확인 필요"로 표시한다.
3. UMM `AZIMUTH_TIME_INTERVAL`에 지상 속도를 곱해 애지머스 간격을 근사한다.

## 결정

선택지 2. `metadata.burst_record_from_asf`는 검색 시점 필드만 채우고 `FIELD_PROVENANCE` 표로 출처를
기계 판독 가능하게 남긴다. IPF 버전은 `pgeVersion`으로 **검색 시점에** 얻을 수 있으므로 SEL-08은
다운로드 없이 판정 가능하다. 픽셀 간격·입사각은 `parse_safe_annotation` /
`parse_burst_xml_metadata` + `enrich_record`로 채운다(요소 경로 위 출처로 검증, 단위테스트 고정).
선택지 3의 근사값은 `extra.azimuth_time_interval_s`로만 보존하고 필드에는 넣지 않는다.

## 결과

- SEL-09는 annotation이 없는 스택에서 "값 없음"을 구분해야 한다(`None`). looks 자동 산출은 IW 명목값을
  기본으로 쓰되 리포트에 출처를 표시한다(looks.py 담당).
- `acquisition_time`은 naive UTC(공유 `make_burst` 팩토리와 동일 규약).
- SLC fallback 레코드는 `full_burst_id = 씬 이름`, `subswath = "IW1+IW2+IW3"`, 편파는 요청 채널로 정규화
  (`extra.polarizations`에 원본 유지).
