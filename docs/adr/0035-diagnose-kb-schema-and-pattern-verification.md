# ADR-0035: 진단 KB 스키마와 패턴 검증 정책 (Diagnosis KB schema & pattern verification policy)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-02, R-14, PERF-13, KB-SNAPHU-001~003, KB-ISCE2-001~005, KB-MINTPY-001~004, KB-HYP3-001~004, KB-ASF-001, KB-AUTH-001, KB-ENV-001~002
- 검증 출처(Sources): 아래 "검증한 문자열" 표 (모두 2026-09-16 에 WebFetch 또는 설치된 패키지 소스에서 확인)

## 맥락 (Context)

플랜 §5.5 는 KB 항목을 YAML(`id, engine, pattern, cause{ko,en}, fix{ko,en}, refs, severity`)로 두고
"구현 시 실제 로그 문자열로 패턴 확정"을 요구한다. 규칙 11.3(추측 금지)에 따라 정규식은
업스트림 소스 코드의 실제 메시지에서만 만들어야 하고, 확인하지 못한 항목은 `pattern_verified: false`
로 표시하고 `docs/open-questions.md` 에 남겨야 한다. 또한 규칙 11.6 은 cause/fix 문장이 i18n
카탈로그(`diagnose.<ID>.cause/fix`)에도 있어야 한다고 정한다.

## 선택지 (Options)

1. KB YAML 에만 문장을 두고 i18n 은 참조만 한다 → 규칙 11.6 위반(카탈로그에 키 없음).
2. i18n 에만 문장을 두고 KB 는 키만 가진다 → KB 파일이 단독으로 읽히지 않아 `docs/kb/` 렌더링·검토가 어렵다.
3. **KB YAML 을 단일 원본으로 두고 스크립트가 i18n `diagnose.KB-*.cause/fix` 와 `docs/kb/*.md` 를 생성, 테스트가 동일성을 검사한다.**

패턴 검증 방식:

1. 기억에 의존해 정규식 작성 → 금지(규칙 11.3).
2. **소스 파일을 직접 가져와(raw GitHub / site-packages) 메시지 문자열을 인용하고, 그 파일 경로·함수명을 `pattern_source` 에 기록한다.**

## 결정 (Decision)

선택지 3 + 소스 인용 방식.

스키마(`wintersar/diagnose/kb_loader.py`, pydantic `extra="forbid"`):

| 필드 | 의미 |
|---|---|
| `id` | `KB-<ENGINE>-<NNN>` |
| `engine` | `snaphu` / `isce2` / `mintpy` / `hyp3` / `asf` / `any` (엔진 독립) |
| `stage` | 실패가 속한 파이프라인 단계(선택) |
| `pattern` | Python 정규식, 항상 `re.MULTILINE` 로 컴파일. 인라인 전역 플래그 `(?m)`·`(?i)` 금지(3.11 에서 위치 제한), 대소문자 무시는 `ignore_case: true` |
| `extract` | `Finding.params` 로 복사할 named group 목록(생략 시 모든 named group). 여러 번 일치하면 그룹별 첫 비어있지 않은 값을 사용 |
| `cause`/`fix` | `{ko, en}` — "원인 → 조치" 순서로 렌더링 |
| `refs` | 출처 URL/경로(필수, 비어 있으면 테스트 실패) |
| `severity` | FAIL / WARN / INFO |
| `retry_hint` | `{stage, action, params, note}` — `attach_retry_hint()` 가 pipeline 에 넘기는 재시도 제안. `params` 의 키는 검증된 인자명(snaphu-py kwargs, MintPy 템플릿 키, wintersar config 필드)만 사용하고, 크기(예: 500→250)는 **추정치**임을 `note` 에 명시 |
| `supersedes` | 이 항목이 일치하면 결과에서 제거할 KB id (예: KB-SNAPHU-002 가 일치하면 범용 OOM 항목 KB-ENV-002 제거) |
| `pattern_verified` | 업스트림 문자열로 도출했는지. `true` 이면 `pattern_source` 필수 |

매칭 정책(`matcher.py`, `api.py`): 파일명·내용 마커로 엔진을 추정 → 해당 엔진 + `any` 항목 우선 →
없으면 전체 항목 → 그래도 없고 범용 파서(ADR-0037)가 오류 흔적을 찾으면 `KB-UNKNOWN`(WARN)
+ 마스킹된 발췌. 로그당 KB 항목당 Finding 1개(`evidence.count` 에 횟수).

### 검증한 문자열 (요약; 전체는 각 KB 의 `pattern_source`)

| KB | 원본 문자열 | 출처 |
|---|---|---|
| KB-SNAPHU-001 | `Exceeded maximum number of secondary nodes\nDecrease TILECOSTTHRESH and/or increase MINREGIONSIZE\n` | gmgunter/snaphu `src/snaphu_tile.c` TraceRegions (snaphu-py `ext/snaphu` 서브모듈). **플랜의 "secondary arcs" 문구는 소스에 없음**(변수명에만 존재) → 패턴은 "nodes" 만 사용, 구버전 문구는 open question #29 |
| KB-SNAPHU-002 | `Out of memory\n` (MAlloc/CAlloc/ReAlloc), `Unexpected or abnormal exit of child process %ld\nAbort\n` | `src/snaphu_util.c`, `src/snaphu.c` Unwrap |
| KB-SNAPHU-003 | `tiles too small or overlap too large for given input\n`, `minimum region size too large for given tile parameters\n`, `Minimum region size cannot exceed tile size\nAbort\n` | `src/snaphu_io.c` CheckParams, `src/snaphu_tile.c` SetupTile |
| (SNAPHU 조치 문구) | `--assemble`, `--tiledir`, `-S`, `--tile <nrow> <ncol> <rowovrlp> <colovrlp>`, `--nproc`; `DEF_TILECOSTTHRESH 500`, `DEF_MINREGIONSIZE 100` | `src/snaphu.h` OPTIONSHELPFULL. **플랜의 "-A(assemble-only)" 는 실제 옵션명이 아님** → `--assemble` 로 기재. snaphu-py 인자명 `tile_cost_thresh`, `min_region_size`, `ntiles`, `tile_overlap`, `nproc`, `single_tile_reoptimize` 는 `src/snaphu/_unwrap.py` |
| KB-ISCE2-001 | `No common bursts found for swath {0}`, `No swaths contain any burst overlaps ...`, `NODATA: No bursts to extract`, `There is no imagery to extract...`, `There is no common region between the two dates to process` | isce2 `components/isceobj/TopsProc/runComputeBaseline.py`, `Sensor/TOPS/Sentinel1.py`, `TopsProc/runTopo.py`. topsStack 자체 스크립트에는 해당 문구가 없고 TopsProc 공용 코드에 있음 |
| KB-ISCE2-002 | `Coherence threshold too strict. No points left for reliable ESD estimate` | `TopsProc/runESD.py`, `contrib/stack/topsStack/estimateAzimuthMisreg.py`. 조치의 옵션명 `-e/--esd_coherence_threshold`(기본 '0.85'), `-C/--coregistration {geometry,NESD}` 는 `stackSentinel.py` createParser |
| KB-ISCE2-003 | `No suitable orbit file found. If you want to process anyway - unset the orbitdir parameter`, `Failed to download orbit ID:`, `Failed to find {1} orbits for tref {0}` | `Sentinel1.py` s1_findOrbitFile, `topsStack/fetchOrbit.py` |
| KB-ISCE2-004 | `Could not create a stitched DEM. Some tiles are missing`, `The full region of interested is not available. A DEM with all null values will be created.`, `Unknown reference system for DEM: {0}`, `WGS84 version of dem found by reference set to EGM96` | `contrib/demUtils/demstitcher/DemStitcher.py`, `TopsProc/runVerifyDEM.py`. topsStack 에 "DEM does not cover" 류 문자열은 없음 → open question #31 |
| KB-ISCE2-005 | `No acquisition fulfills the temporal range and bbox requirement.` | `stackSentinel.py` get_dates |
| KB-MINTPY-001 | `No pixel with average spatial coherence > {} are found for automatic reference point selection!` | MintPy `src/mintpy/reference_point.py` select_max_coherence_yx |
| KB-MINTPY-002 | `Not enough reliable pixels (minimum of {int(min_num_pixel)}). ` (+ `print(f'ERROR: {msg}')`, `raise RuntimeError(msg)`) | `src/mintpy/smallbaselineApp.py` generate_temporal_coherence_mask |
| KB-MINTPY-003 | `WARNING: downloading failed for 3 times, stop trying and continue.`, `CDS account is NOT setup to the latest format! ...`, `PYAPS: No account info found for ...` | `src/mintpy/tropo_pyaps3.py` |
| KB-MINTPY-004 | `input reference point is OUT of data coverage!`, `input reference point is in masked OUT area ...` | `reference_point.py` read_reference_input |
| (MintPy 기본값) | `minCoherence` auto=0.85, `minTempCoh` auto=0.7, `minNumPixel` auto=100 | `src/mintpy/defaults/smallbaselineApp.cfg` |
| KB-HYP3-001 | `These jobs would cost {total_cost} credits, but you have only {remaining_credits} remaining.` | ASFHyP3/hyp3 `lib/dynamo/dynamo/jobs.py` put_jobs; SDK 가 `HyP3Error(f'{response} {response.json()["detail"]}')` 로 감쌈(`hyp3_sdk/exceptions.py`) |
| KB-HYP3-002 | `Burst IDs do not match for {g1} and {g2}.`, `The requested scenes need to have the same polarization, got: ...`, `Only VV and HH polarizations are currently supported, got: ...` | `apps/api/src/hyp3_api/validation.py` |
| KB-HYP3-003 | `Some requested scenes do not have DEM coverage: {bad_granules}` | 같은 파일 check_dem_coverage |
| KB-HYP3-004 | `must request access before submitting jobs`, `request for access is pending review`, `request for access has been rejected` | `lib/dynamo/dynamo/exceptions.py` |
| KB-ASF-001 | `HTTP {status_code}: {errors}` → `ASFSearch4xxError`/`ASFSearch5xxError`, `Connection Error (Timeout): CMR took too long to respond...` | 설치된 asf_search 14.0.0 `search/search_generator.py`, `exceptions.py` |
| KB-AUTH-001 | `Invalid/Expired token passed`, `Failed to log in with provided credentials`, `HTTP {status}: {text}`(ASFAuthenticationError), `Was not able to authenticate with username and password provided` / `... .netrc file and no credentials provided`, `Please create a .netrc file in your home directory`, requests `{status} Client Error: {reason} for url:` | asf_search `ASFSession.py`, `download/download.py`; hyp3_sdk `util.py`; isce2 `DemStitcher.py`; requests `models.py` |
| KB-ENV-001 | `... lacks DATABASE.LAYOUT.VERSION.MAJOR / DATABASE.LAYOUT.VERSION.MINOR metadata. It comes from another PROJ installation.`, `Cannot find proj.db`; `cannot open shared object file` | OSGeo/PROJ `src/iso19111/factory.cpp`; glibc `elf/dl-load.c` (bminor 미러) |
| KB-ENV-002 | `"%s: Killed process %d (%s) ..."` with `"Out of memory"`; `signal.strsignal(SIGKILL)=="Killed"`; `os.strerror(ENOMEM)=="Cannot allocate memory"`; `Command '%s' died with %r.` | linux `mm/oom_kill.c`; CPython 로컬 확인 |

## 결과 (Consequences)

- 새 KB 항목 추가 절차: YAML 작성(출처 인용) → `.venv/bin/python scripts/render_kb_docs.py --write-i18n`
  → `tests/unit/diagnose` 통과(`--check` 가 문서·i18n 최신 여부를 검사).
- 현재 20개 항목 모두 `pattern_verified: true`; 검증 불가 항목이 생기면 `false` + open question 행.
- 플랜 문구와 다른 점: SNAPHU "secondary arcs"→"secondary nodes", "-A"→`--assemble`,
  "no common bursts" 는 topsStack 이 아니라 TopsProc 코드에 있음. 플랜 문서는 수정하지 않고 여기 기록.
- 되돌리는 조건: 업스트림이 메시지를 바꾸면 해당 항목의 `pattern_source` 를 갱신하고 픽스처를 교체한다.
