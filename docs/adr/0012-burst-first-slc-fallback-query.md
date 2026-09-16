# ADR-0012: 검색 쿼리 구성 — BURST 우선, SLC fallback, candidates.json 형식

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-01, PERF-01, SEL-04, SEL-05
- 검증 출처(Sources):
  - `.venv/lib/python3.11/site-packages/asf_search/search/search.py` (키워드 목록), `ASFSearchOptions/validator_map.py`
  - `.venv/lib/python3.11/site-packages/asf_search/constants/{PRODUCT_TYPE,PLATFORM,BEAMMODE,POLARIZATION,FLIGHT_DIRECTION}.py`
  - `.venv/lib/python3.11/site-packages/asf_search/WKT/validate_wkt.py` (`_simplify_geometry`: 병합 → convex hull → CCW)
  - `.venv/lib/python3.11/site-packages/asf_search/Products/S1Product.py::get_stack_opts` (SLC 편파 필터 `VV`,`VV+VH`)
  - 실제 응답: BURST 검색은 `polarization='VV'` 단일값, SLC는 `'VV+VH'` (2026-09-16)

## 맥락

플랜 §5.1.1: AOI × 기간 × Sentinel-1 × BURST 조회, burst가 없는 지역/기간은 SLC fallback. 다른 모듈
(network, rules, cli)이 같은 `SearchResult`/`candidates.json`을 읽으므로 형식을 고정해야 한다.

## 결정

1. **AOI 정규화** (`aoi_to_wkt`): GeoJSON Feature/FeatureCollection/Geometry 또는 WKT 파일 → 단일 유효
   POLYGON, 구멍 제거, CCW, 소수점 6자리. 다중 도형은 union 후 여전히 다중이면 convex hull —
   asf_search `validate_wkt`가 어차피 같은 처리를 하므로 결과가 예측 가능하다.
2. **쿼리 키워드** (`build_query`): `intersectsWith`, `start`/`end`(`YYYY-MM-DDT00:00:00Z` ~ `23:59:59Z`),
   `platform`(`Sentinel-1A..D`), `processingLevel`(`BURST` | `SLC`), `beamMode='IW'`,
   `polarization`(BURST: `[VV]`; SLC: `[VV, VV+VH]` 등 asf_search의 stack 옵션과 같은 이중편파 포함 규칙),
   `flightDirection`(asc/desc일 때만), `relativeOrbit`(auto가 아닐 때만). 모든 키가 `ASFSearchOptions`
   검증기를 통과함을 테스트로 고정.
3. **BURST → SLC fallback**: `data.product=burst`일 때 BURST 결과가 0건이면 SLC로 재검색하고
   `SEL-SEARCH-02`(INFO)를 남긴다. 둘 다 0건이면 `SEL-SEARCH-03`(WARN). `data.product=slc`면 바로 SLC.
   `searchComplete=False`는 `SEL-SEARCH-04`(WARN), 예외는 `SEL-SEARCH-05`(FAIL)로 바꾸고 절대 raise하지 않는다.
   CMR bbox 근사로 들어온 레코드는 shapely로 AOI 교차를 재확인해 제외한다.
4. **인증**: 검색은 인증 없이 동작한다. `search_from_config`는 오프라인으로 자격증명 존재만 확인해
   없으면 `KB-AUTH-001`(WARN)을 붙인다(다운로드 단계에서 실패할 것을 미리 알림).
5. **candidates.json**: `{"schema_version": 1, "product_type": "BURST"|"SLC", "query": {...},
   "records": [BurstRecord JSON...], "findings": [Finding JSON...]}`; 저장 전에 `mask_mapping`(규칙 11.11).

## 결과

- `SearchResult.summary()`가 CLI/리포트용 요약(날짜·트랙·burst 수)을 제공한다.
- SLC fallback 레코드의 그룹핑·커버리지는 씬 단위이므로 network.py가 `product_type`으로 분기해야 한다.
