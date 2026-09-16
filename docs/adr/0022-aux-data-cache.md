# ADR-0022: 보조 데이터(궤도·기상 모델·DEM) 콘텐츠 주소 캐시 설계 (PERF-02)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: PERF-02, PERF-09, 플랜 §3.1(궤도·DEM), §6.1 PERF-02, 규칙 11.11
- 검증 출처(Sources):
  - sentineleof 0.13.0 (PyPI 2026-06-25, MIT): https://pypi.org/pypi/sentineleof/json
  - `eof/download.py`: https://github.com/scottstanie/sentineleof/blob/master/eof/download.py
    (`download_eofs(orbit_dts=None, missions=None, sentinel_file=None, save_dir=".",
    orbit_type="precise"|"restituted", force_asf=False, …) -> list[Path]`, "missions … Must be same
    length as orbit_dts")
  - `eof/cli.py`: https://github.com/scottstanie/sentineleof/blob/master/eof/cli.py
    (`eof --date YYYY-MM-DD --mission S1A|S1B|S1C|S1D --save-dir DIR --orbit-type precise|restituted`)
  - MintPy 기상 모델 캐시 동작: smallbaselineApp.cfg 234–237행 "MintPy application will look for the
    GAM files in the directory before downloading a new one"

## 맥락 (Context)

PERF-02: 궤도·DEM·ERA5를 실행마다 다시 받고, 튠 반복 시 대기 시간이 반복된다. 재실행 시 네트워크
바이트 0을 지표로 삼는다. `sentineleof`는 의존성 정책상 미설치(ADR-0001)이므로 있으면 쓰고 없으면
ENV-006으로 알려야 한다.

## 선택지 (Options)

1. 엔진별 임의 폴더에 다운로드 후 존재 여부만 확인 — 부분 다운로드를 적중으로 오인, 키 충돌.
2. 키(JSON) 해시 → `<cache>/<namespace>/<sha256[:32]>/` 콘텐츠 주소 디렉터리 + 완료 마커 + 원자적
   rename + 파일 락 — 채택.
3. 외부 캐시 라이브러리 도입 — 의존성 추가 대비 이득 없음.

## 결정 (Decision)

- `wintersar.engines.aux_cache.cached_fetch(cache_dir, key, fetcher, namespace, offline, lock_timeout_s)`:
  `.complete` 마커가 있으면 fetcher를 호출하지 않는다. 미스일 때 `entry.tmp-<uuid>`에 받아
  `key.json`(마스킹된 키·파일 목록·바이트)과 마커를 쓴 뒤 rename. fetcher 예외나 빈 결과는 엔트리를
  남기지 않는다. `O_EXCL` 락 파일로 동시 fetch를 막고, 락 나이가 2×timeout이면 stale로 제거.
- `WINTERSAR_OFFLINE=1`(또는 `offline=True`)이면 미스는 `CacheMissOfflineError`(AUX-002) —
  "오프라인 재실행 시 네트워크 호출 0"을 테스트가 카운팅 fetcher로 보장한다.
- 궤도: 키 `{kind: orbit, mission, date, orbit_type}`(namespace `orbits`), fetcher는 `eof.download.
  download_eofs(orbit_dts=[acq_time], missions=[mission], save_dir=tmp, orbit_type=…)`; 모듈이 없고
  `eof` CLI만 있으면 위 CLI 플래그로 호출; 둘 다 없으면 ENV-006 WARN(HyP3 경로는 궤도가 필요 없음).
  실패는 AUX-001 WARN으로 보고하고 진행을 막지 않는다.
- 기상 모델: MintPy/PyAPS가 스스로 내려받고 폴더 내 기존 파일을 재사용하므로 캐시는 안정된 공유 폴더
  `<cache>/weather/<MODEL>`만 제공하고 템플릿의 `mintpy.troposphericDelay.weatherDir`에 넣는다(ADR-0021).
- 기본 `cache_dir`는 `Config.cache_dir`(= `compute.cache_dir` | `$WINTERSAR_CACHE` | `~/.cache/wintersar`).
  `cache_stats`/`prune(max_bytes)`(완료 시각 기준 LRU)로 `wintersar cache gc`를 뒷받침한다.

## 결과 (Consequences)

- DEM(sardem)·GACOS도 같은 `cached_fetch`로 붙이면 된다(키에 종류·타일·해상도 포함).
- 캐시 디렉터리는 사용자 홈 아래이므로 로그·JSON에는 `mask_text`로 `~`로 치환된다.
- 실제 `sentineleof` 호출 검증은 설치 승인 후 `-m network` 테스트로 한다(open-questions #10).
