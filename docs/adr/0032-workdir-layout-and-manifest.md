# ADR-0032: 작업 디렉터리 레이아웃과 manifest

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-05, R-11, PERF-03, PERF-06
- 검증 출처(Sources):
  - 플랜 §5.3 "출력은 `work/<stage>/<hash>/`, `manifest.json`(입출력 목록·해시·엔진 버전·시간·peak RSS·디스크)"
  - `src/wintersar/io/schemas.py::StageRecord`
  - `src/wintersar/pipeline/cache.py`

## 맥락 (Context)

캐시 디렉터리 구조와 manifest 내용은 실행기·`cache ls/gc`·diagnose·QGIS 플러그인·SNAPHU assemble-only
(`unwrap` manifest의 tile dir) 모두가 읽는다. 한 번 정하면 바꾸기 어렵다.

## 선택지 (Options)

1. `work/<stage>/<hash>/{manifest.json, out/, logs/}` — 단계별 디렉터리, 해시별 하위 디렉터리.
2. `work/<hash>/` 평면 구조 + 인덱스 파일.
3. SQLite 인덱스 + 임의 경로.

## 결정 (Decision)

선택지 1.

```
work/
  <stage>/<node_hash>/manifest.json   # StageRecord (원자적 쓰기: tmp + Path.replace)
  <stage>/<node_hash>/out/            # 엔진 산출물 (_out_dir)
  <stage>/<node_hash>/logs/           # 엔진 stdout/stderr (log_dir; diagnose 입력)
  runs/<run_id>.json                  # RunResult 요약(마스킹됨) — 리포트/QGIS용
```

- `manifest.json` = `StageRecord`. 추가 필드는 `extra`에 둔다:
  - `extra.artifacts[name]` = `Artifact` 덤프(`path`, `kind`, `sha256`, `meta.hash_method`, 엔진 메타).
    하류 노드 해시 해석은 이 값만 읽고 데이터를 열지 않는다.
  - `extra.cache_hit`(적중 시 true), `extra.fallback`(`--from` 최신 결과 재사용), `extra.forced`,
    `extra.run_id`, `extra.error`(실패 메시지, 마스킹), `extra.retry_hint`(ADR-0033),
    `extra.diagnose_error`(diagnose 자체 예외).
  - SNAPHU assemble-only용 tile dir는 unwrap 스케줄러가 `extra`(예: `extra.tile_dir`)에 기록한다(계약은
    unwrap 모듈 소유).
- `resources`: `wall_time_s`(perf_counter), `peak_rss_gb`(`ru_maxrss`, 프로세스 수명 최대치 — 단계별 값이
  아님을 `notes.rss_method`에 명시), `disk_gb`(`out/` 크기).
- 같은 해시로 다시 실행(`--force`)하면 `out/`와 `logs/`를 비우고 다시 쓴다(오래된 파일 혼입 방지).
- `status`가 `ok`이고 `extra.artifacts`의 모든 경로가 존재하며 빠른 해시가 일치할 때만 적중이다.
  `failed` manifest는 남겨 두되(로그·진단 보존) 적중 대상이 아니다.
- **GC**: `cache.gc(keep_latest=N)`은 단계별로 `finished_at` 기준 최신 N개만 남긴다. 상태(ok/failed)를
  구분하지 않으므로 실패 로그도 N개 안에서 보존된다. 하류 manifest가 참조하는 상류를 지워도 하류 적중
  자체는 깨지지 않지만(입력 해시는 하류 manifest에 있음), 상류 노드 해시를 다시 계산할 수 없어 다음
  실행에서 상류부터 다시 돈다. 따라서 N은 "동시에 유지할 튠 변형 수"로 안내한다(기본 3).
- 크기 회계: `cache ls`가 항목별·단계별 바이트를 보여 준다(`dir_size`).

## 결과 (Consequences)

- `work/select/`(search CLI가 직접 쓰는 위치, 플랜 §4.5)는 STAGE_ORDER 밖 디렉터리이므로 `cache ls/gc`가
  건드리지 않는다.
- manifest 스키마 변경은 `StageRecord`(공유 계약)를 통해서만 하며, 구버전 manifest는 `record_artifacts`가
  `outputs`(경로만)로 폴백한다.
