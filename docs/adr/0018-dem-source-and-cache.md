# ADR-0018: DEM 소스(Copernicus GLO-30 기본)와 콘텐츠 주소 캐시

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-04 / SEL-12 / PERF-02 / ENV-006 / ADR-0001
- 검증 출처(Sources):
  - sardem README: https://raw.githubusercontent.com/scottstanie/sardem/master/README.md
    — `--data-source {NASA,NASA_WATER,COP,3DEP,NISAR}` (기본 COP), `--bbox left bottom right top`, 기본 출력 `elevation.dem`,
    Copernicus DEM 인용 DOI `10.5270/ESA-c5d3d65` 및 저작권 문구
  - sardem `dem.py` `main()` 시그니처: https://raw.githubusercontent.com/scottstanie/sardem/master/sardem/dem.py
    — `main(output_name=None, bbox=None, geojson=None, wkt_file=None, data_source=None, xrate=1, yrate=1,
    make_isce_xml=False, keep_egm=False, shift_rsc=False, cache_dir=None, output_type="float32",
    output_format="GTiff", vrt_filename=None)`; `bbox (tuple[float]): (left, bot, right, top)`;
    `keep_egm=False`면 지오이드고 → WGS84 타원체고 변환
  - sardem `cop_dem.py`: https://raw.githubusercontent.com/scottstanie/sardem/master/sardem/cop_dem.py
    — 버킷 `https://copernicus-dem-30m.s3.amazonaws.com/{t}/{t}.tif` (GLO-30), EGM2008 → WGS84 변환
  - `pyproject.toml` optional extra `dem = ["sardem>=0.11"]`; ADR-0001 (sardem은 핵심 의존성 아님, PyPI 월 ~300 다운로드)
  - `wintersar.util.hashing.hash_params` (정규화 JSON sha256)

## 맥락

기하 마스크(SEL-12)와 이후 ISCE2/HyP3 경로 모두 AOI를 덮는 DEM이 필요하다. 플랜 PERF-02는 궤도·DEM·대기모델을
실행마다 재다운로드하는 비효율을 지적하고 콘텐츠 주소 캐시를 요구한다. 의존성 정책상 `sardem`은 설치되어 있지
않으며(ADR-0001, open-questions #10) 어댑터는 부재를 감지해 Finding으로 보고해야 한다.

## 선택지

1. DEM 다운로드 로직을 직접 구현(AWS 버킷 타일 스티칭, 지오이드 변환).
2. `sardem`을 지연 import로 호출하고 없으면 ENV-006 Finding — 캐시 계층만 자체 구현.
3. ISCE2/HyP3가 각자 DEM을 받게 두고 wintersar는 관여하지 않음.

## 결정

선택지 2.

- 기본 소스 `copernicus_glo30`(sardem `COP`), 대안 `srtm1`(sardem `NASA`, Earthdata 로그인 필요). 두 소스 모두 30 m급.
  InSAR 처리기는 타원체고를 기대하므로 `keep_egm` 기본값(False, 변환 수행)을 그대로 쓴다.
- 호출 키워드는 위 시그니처를 그대로 사용: `main(output_name=..., bbox=(l,b,r,t), data_source="COP"|"NASA",
  cache_dir=<tiles>, output_format="GTiff", output_type="float32")`. `main()`의 반환값은 문서화되어 있지 않으므로
  반환값에 의존하지 않고 출력 파일 존재·크기로 성공을 판정한다(`DemFetchError`).
- 캐시 레이아웃(PERF-02): `<cache_dir>/dem/<source>_<hash>.tif` + 같은 이름의 `.json` 프로브넌스(bbox, 소스, fetcher,
  생성 시각, 저작권 문구; 경로는 `mask_mapping` 처리) + `<cache_dir>/dem/tiles/`(sardem 타일 캐시).
  `<hash>` = `hash_params({"bbox": 소수 4자리로 반올림한 (l,b,r,t), "source": 이름, "layout": 1})`.
  반올림(~11 m)으로 거의 같은 AOI가 같은 항목을 공유한다. 기본 여유(buffer) 0.05°는 AOI 경계 픽셀의
  중앙차분 경사가 유효하도록 한다.
- 원자성: 임시 파일(`*.part<pid>.tif`)에 받은 뒤 `Path.replace`로 이름을 바꾼다. 실패 시 캐시 항목이 남지 않는다.
- `sardem` 부재: `importlib.util.find_spec("sardem") is None` → `check_install()`은 ENV-006 **WARN**(선택 패키지),
  `get_dem()`은 ENV-006 **FAIL**을 담은 `DemNotInstalledError`. 설치 힌트 `pip install 'wintersar[dem]'`.
- 테스트는 fetcher 주입(`get_dem(..., fetcher=fake)`)과 `sys.modules`에 가짜 `sardem.dem`을 넣어 키워드 이름을 검증한다.
  실제 네트워크 다운로드 테스트는 `-m network`로 Phase 2에서 추가한다.

## 결과

- Copernicus DEM 사용 시 리포트·산출물에 저작권 문구(i18n `select_geometry.dem.attribution.copernicus_glo30`)를 포함해야 한다.
- 캐시 레이아웃 변경은 `DEM_CACHE_LAYOUT_VERSION`을 올려 자동 무효화한다.
- ISCE2 topsStack 어댑터(Phase 2)는 같은 캐시 경로의 DEM을 `make_isce_xml` 대신 자체 변환으로 재사용할지 결정해야 한다
  (open-questions #14).
- 발주자가 sardem 설치를 승인(open-questions #10)하면 `uv sync --extra dem` 후 네트워크 테스트로 실제 호출을 검증한다.
