# ADR-0020: HyP3 어댑터 — SDK API 확인 결과, 산출물 규약, 크레딧 정책 (R-05, R-08, PERF-02/06)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-05, R-08, SEL-13, PERF-02, PERF-06, 플랜 §5.2 hyp3.py, §12.3 행 3
- 검증 출처(Sources):
  - hyp3-sdk 7.7.8 (PyPI 2026-09-02, BSD-3-Clause, python>=3.10): https://pypi.org/pypi/hyp3-sdk/json
  - `src/hyp3_sdk/hyp3.py`, `jobs.py`, `util.py` (main, 2026-09-16 조회):
    https://github.com/ASFHyP3/hyp3-sdk/tree/main/src/hyp3_sdk
  - Burst InSAR 제품 가이드: https://hyp3-docs.asf.alaska.edu/guides/burst_insar_product_guide/
  - 크레딧 표: https://hyp3-docs.asf.alaska.edu/using/credits/ (2026-09-16 조회, `hyp3_costs.yaml`에 사본)
  - MintPy `prep_hyp3.py`: https://github.com/insarlab/MintPy/blob/main/src/mintpy/prep_hyp3.py
  - MintPy HyP3 디렉터리 예시: https://github.com/insarlab/MintPy/blob/main/docs/dir_structure.md

## 맥락 (Context)

Phase 2 ①은 HyP3 경로로 "설치 후 1시간 내 속도 지도"를 목표로 한다. 플랜 §5.2는 `hyp3_sdk`로
`INSAR_ISCE_BURST`/`INSAR_ISCE_MULTI_BURST` 작업을 제출·폴링·다운로드·검증하고 MintPy `prep_hyp3`
호환 레이아웃을 만들 것과, 크레딧 수치를 하드코딩하지 말 것을 요구한다. `hyp3-sdk`는 의존성 정책
(ADR-0001, 월 다운로드 임계치 미달)으로 개발 환경에 설치되지 않으므로 API는 GitHub 소스로 확인했고,
모든 네트워크 호출은 `Hyp3Client` 프로토콜 뒤에 두어 테스트는 가짜 클라이언트로 돈다.

## 확인한 사실 (Verified facts)

### hyp3-sdk 7.7.8 API (소스 라인 기준)

| 항목 | 시그니처 / 값 |
|---|---|
| 생성자 | `HyP3(api_url='https://hyp3-api.asf.alaska.edu', username=None, password=None, token=None, prompt: 'password'\|'token'\|bool\|None=None)` — 인증 정보가 없으면 `~/.netrc` 사용, `token`은 `Authorization: Bearer` 헤더 |
| 단일 burst | `submit_insar_isce_burst_job(granule1, granule2, name=None, apply_water_mask=False, looks='20x4'\|'10x2'\|'5x1')` → `prepare_…` 딕셔너리 `{'job_parameters': {'granules': [g1, g2], 'apply_water_mask', 'looks'}, 'job_type': 'INSAR_ISCE_BURST', 'name'?}` |
| 다중 burst | `submit_insar_isce_multi_burst_job(reference: list[str], secondary: list[str], name=None, apply_water_mask=False, looks=…)` → `{'job_parameters': {'reference', 'secondary', 'apply_water_mask', 'looks'}, 'job_type': 'INSAR_ISCE_MULTI_BURST'}` |
| 일괄 제출 | `submit_prepared_jobs(prepared_jobs: dict \| list[dict]) -> Batch` (POST `/jobs`) — 어댑터는 이 경로만 사용 |
| 상태 | `get_job_by_id(job_id) -> Job`, `find_jobs(start, end, status_code, name, job_type, user_id)`, `refresh(batch)`, `watch(batch, timeout=10800, interval=60)` |
| Job | 속성 `job_id, job_type, request_time, status_code, user_id, name, bucket, bucket_prefix, job_parameters, files=[{filename,url,s3,size}], logs, browse_images, thumbnail_images, expiration_time, processing_times, credit_cost, priority`; 메서드 `succeeded()/failed()/complete()/pending()/running()/expired()`, `download_files(location, create=True) -> list[Path]` (성공·미만료 작업만) |
| 크레딧 | `check_credits() -> float\|int\|None` (`/user`의 `remaining_credits`), `costs() -> dict` (`/costs`), `Batch.total_credit_cost()` |
| 기타 | `util.chunk(itr, n=200)`, `util.extract_zipped_product(zip)`(부모 폴더에 풀고 `parent/stem` 반환), `PROD_API`/`TEST_API` 상수 |

어댑터는 `watch()` 대신 자체 폴링(초기 30 s, ×1.5 백오프, 최대 300 s, 기본 타임아웃 10800 s =
SDK 기본값)을 구현해 `jobs.json`에 작업 ID를 즉시 기록하고, 타임아웃·중단 후 재실행 시 제출된
작업 ID를 재사용한다(PERF-06).

### Burst InSAR 산출물 (제품 가이드)

- 이름: 단일 `S1_bbbbbb_IWs_yyyymmdd_yyyymmdd_pp_INTzz_uuuu`, 다중
  `S1_rrr_bbbbbbs1ntt-bbbbbbs2ntt-bbbbbbs3ntt_IW_yyyymmdd_yyyymmdd_pp_INTzz_uuuu` (MintPy `prep_hyp3`의
  정규식과 동일하게 `PRODUCT_NAME_RE_*`로 검증).
- looks → 픽셀: `20x4`=80 m(기본), `10x2`=40 m, `5x1`=20 m. 다중 burst는 1–15개, 참조·보조 개수 동일.
- 파일 접미사(필수/권장 분류는 wintersar 판단): 필수 `_unw_phase.tif`(rad, 음수=위성 방향),
  `_corr.tif`, `_dem.tif`, `_lv_theta.tif`, `_lv_phi.tif`(rad), `<name>.txt`(처리 파라미터);
  권장 `_conncomp.tif`, `_water_mask.tif`(1=육지), `_wrapped_phase.tif`, `_amp.tif`;
  기타 `_lat_rdr/_lon_rdr/_los_rdr/_wrapped_phase_rdr.tif`(단일 burst만), `.README.md.txt`, 브라우즈 PNG/KMZ.
- `prep_hyp3`가 `<name>.txt`에서 읽는 키(공백 제거): `UTCtime, Azimuthlooks, Rangelooks,
  Earthradiusatnadir, Spacecraftheight, Slantrangenear, Heading, Baseline, Unwrappingtype` →
  이 키가 없으면 HYP3-002 FAIL.

### 크레딧 (2026-09-16 표)

HyP3 Basic 월 8,000 크레딧 무료, HyP3+ 1 크레딧 = $0.05. Burst InSAR 비용은 **looks × 작업당 burst
쌍 수**의 계단 함수이며 `src/wintersar/engines/hyp3_costs.yaml`에 표 전체와 출처·조회일을 기록했다
(80 m: 1–4쌍 1, 5–12쌍 5, 13–15쌍 10; 40 m: 1–3쌍 1, 4–9쌍 5, 10–15쌍 10; 20 m: 1쌍 1 … 15쌍 110).
코드는 `job_credits(looks, n_bursts)`로만 조회하고 숫자를 갖지 않는다.

## 선택지 (Options)

1. `HyP3.watch()`와 `Batch.download_files()`를 그대로 사용 — 진행 상태를 파일에 남기지 못해 재개 불가.
2. 얇은 `Hyp3Client` 프로토콜(submit/refresh/download/check_credits/costs) + 자체 상태 파일·폴링 — 채택.
3. REST API 직접 호출 — SDK의 인증(EDL 리다이렉트) 처리를 재구현해야 하므로 기각.

## 결정 (Decision)

- `wintersar.engines.hyp3.Hyp3Engine`: `name='hyp3'`, `stages=('interferogram',)`,
  `produces=('igrams','unw')`. **HyP3가 정합·간섭도·멀티룩·SNAPHU 언래핑을 한 작업으로 수행하므로
  interferogram 단계가 `igrams`와 `unw` 아티팩트를 동시에 내고, 파이프라인은
  `engine.interferogram == hyp3`일 때 로컬 unwrap 단계를 건너뛴다.** 두 아티팩트는 같은 GeoTIFF
  디렉터리를 가리키며 `meta.patterns`에 MintPy `mintpy.load.*` 글롭을 담는다.
- 레이아웃: `<out_dir>/hyp3/<YYYYMMDD_YYYYMMDD>/<product>/<product>_<suffix>.tif` + `<product>.txt`
  (`prep_hyp3`가 같은 폴더의 `<product>.txt`를 찾음). 산출물 범위가 서로 다르면 공통 교집합으로 자른
  `*_clip.tif`를 추가하고 패턴을 `_clip`으로 바꾼다(MintPy는 동일 크기 스택 필요; HYP3-009 INFO).
- 상태: `<out_dir>/hyp3/jobs.json`(쌍→job_id/status/product_dir/credit_cost). 재실행 시 검증된 산출물은
  건너뛰고, 진행 중·성공(미만료) 작업은 재제출 없이 폴링·재다운로드하며, 실패·만료 작업만 재제출.
- 크레딧: 제출 전 `job_credits` 합 > `check_credits()` 이면 HYP3-005 FAIL로 중단
  (`hyp3.ignore_credits=true`로 강행). `estimate(plan)`은 `StageRecord.params`의
  `n_pairs/n_bursts/looks(engine.looks, engine.target_pixel_m)`로 견적한다.
- looks 매핑: `engine.looks=[20,4]|[10,2]|[5,1]`은 그대로, `auto`는 `target_pixel_m`에 가장 가까운
  80/40/20 m, 그 외는 가장 가까운 값 + HYP3-003 WARN.
- 인증: `EARTHDATA_TOKEN`(Bearer) 또는 `~/.netrc`(urs.earthdata.nasa.gov); 없으면 ENV-003 WARN.
- 입력: `candidates.json`(`StackCandidate`)의 `notes.granules = {date_iso: {burst_id: granule}}`
  또는 `params.jobs`(명시 granule 목록). select 모듈이 이 계약을 채워야 한다(open-questions #13).
- 진단 ID: HYP3-001(작업 실패) … HYP3-013(공통 범위 없음), 텍스트는 `i18n/{ko,en}/engines_hyp3_mintpy.yaml`.

## 결과 (Consequences)

- `hyp3-sdk` 설치 승인(open-questions #10) 전까지 실제 제출은 `-m network` 수동 테스트로만 확인한다.
  SDK 7.x 메이저 변경 시 `version_constraint='>=7,<8'`이 먼저 경고한다.
- 작업 이름 길이 제한, 다중 burst 산출물의 실제 지리 범위 편차는 실데이터로 확인해야 한다(open-questions #12, #15).
- `mintpy.load.connCompFile = */*/*_conncomp.tif`는 MintPy 문서의 HyP3 예시에 없는 선택 키다(cfg에서 optional).
  실데이터 load_data에서 문제가 나면 패턴에서 제외한다(open-questions #27).
- 2026-09-16 재검증: PyPI JSON `releases['7.7.8'][0].upload_time = 2026-09-02T23:41:28`, 크레딧 표·제품 접미사·
  `prep_hyp3` 정규식/키·`util.chunk(n=200)`·`Job.expired()` 모두 위 표와 일치.
- 크레딧 표는 문서 갱신 시 `hyp3_costs.yaml`의 `fetched` 날짜와 함께 갱신한다.
