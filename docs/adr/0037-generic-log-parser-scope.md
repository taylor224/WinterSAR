# ADR-0037: 범용 로그 파서가 잡는 것 (What the generic log parser catches)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-02, R-14, Phase 3 DoD("매칭 실패 시 미분류 Finding 과 로그 발췌"), 규칙 11.11
- 검증 출처(Sources):
  - CPython `Lib/traceback.py` (`Traceback (most recent call last):`), `Lib/subprocess.py`
    `CalledProcessError.__str__` (`Command '%s' returned non-zero exit status %d.` / `died with %r.`) — 로컬 `inspect.getsource` 확인
  - `signal.strsignal(SIGKILL)` == `Killed`(macOS 는 `Killed: 9`), `os.strerror(errno.ENOMEM)` == `Cannot allocate memory` — 로컬 실행 확인
  - linux `mm/oom_kill.c`: `"%s: Killed process %d (%s) ..."`, message `"Out of memory"`
  - SNAPHU `src/snaphu_util.c` MAlloc `Out of memory\n`, 여러 곳의 `...\nAbort\n`
  - glibc `elf/dl-load.c` `cannot open shared object file` (KB-ENV-001 용)

## 맥락 (Context)

KB 패턴이 없는 실패도 사용자가 무엇이 잘못됐는지 볼 수 있어야 한다. 그러려면 (1) 로그에서
"실패로 보이는 지점"을 찾아 발췌하고, (2) 그 발췌를 개인정보·토큰 마스킹 후 `KB-UNKNOWN`
Finding 에 담아야 한다. 너무 느슨하면 정상 로그(경고만 있는 로그)에도 미분류 경고가 붙고,
너무 엄격하면 새 실패 유형이 보이지 않는다.

## 선택지 (Options)

1. 마지막 N 줄만 발췌 → 실패 지점이 중간에 있으면 놓친다.
2. `error` 라는 단어가 있는 모든 줄 → `error_reporting`, `ERROR_CODE=0` 같은 오탐이 많다.
3. **신뢰도 순으로 좁게 정의한 이벤트 집합 + 엔진별 추가 마커, 경고는 제외, 아무것도 없고
   호출자가 실패를 알 때(`assume_failed=True`)만 꼬리 발췌.**

## 결정 (Decision)

선택지 3 (`wintersar/diagnose/parsers/generic.py`). 이벤트 종류와 규칙:

| kind | 규칙 | 근거 |
|---|---|---|
| `traceback` | `^Traceback (most recent call last):` 이후 들여쓰기된 프레임 줄을 건너뛰고 첫 비들여쓰기 줄(예외 줄)을 앵커로 | CPython traceback 형식 |
| `exception` | 줄 시작의 `pkg.mod.SomeError: msg` / `SomeException` (들여쓰기된 줄은 프레임으로 보고 제외) | 예외 클래스명 관례(`Error|Exception|Exit|Interrupt|Fault`) |
| `exit` | `Command '...' returned non-zero exit status N.` / `died with <Signals.X: N>.` | `subprocess.CalledProcessError.__str__` |
| `oom` | `Out of memory`, 줄 시작 `Killed`(`Killed: 9` 포함), `MemoryError`, `Cannot allocate memory` | SNAPHU MAlloc, linux oom_kill.c, strsignal, strerror(ENOMEM) |
| `error` | 줄의 앞 80자 안에 단어 `ERROR` / `FATAL` / `CRITICAL`(대문자, `\b` 경계 → `ERROR_CODE` 제외) | logging 관례(`ERROR: ...`, `[ERROR]`, GDAL `ERROR 1:`) |
| `abort` | 단독 `Abort` 줄 | SNAPHU `exit(ABNORMAL_EXIT)` 직전 출력 |

- `WARNING` 은 이벤트가 아니다(경고만 있는 로그 → Finding 없음).
- 엔진별 추가 마커(`parsers/<engine>.py` `event_patterns`): snaphu `Abort`, isce2 `raise Exception(` /
  `*****\nERROR:`, mintpy `^ERROR: `, hyp3 `<Response [4xx|5xx]>` / `FAILED`, asf `ASF*Error` / `CMR*Error`.
- 엔진 탐지: 파일명 힌트(`snaphu`, `unwrap`, `isce`, `run_`, `mintpy`, `hyp3`, `asf` …) → 내용 마커
  점수(마커당 최대 5회) → 유일 최고점만 채택, 동점이면 None(범용). 탐지 결과는 KB 항목 우선순위와
  발췌 규칙에만 영향을 주며, 매칭이 없으면 전체 KB 를 다시 시도하므로 오탐이 결과를 숨기지 않는다.
- `KB-UNKNOWN`: 첫 이벤트의 앞뒤 3줄(트레이스백은 앞 6줄)을 `evidence.excerpt` 와 `params.excerpt` 에
  넣고(최대 2000자), 이벤트 최대 8개를 `evidence.events` 로 남긴다. 모든 텍스트는
  `util.masking.mask_text` 를 거친다(홈 경로 → `~`, Bearer/JWT/`token=` → `***`).
- 대용량 로그는 마지막 20 MB 만 읽고, NUL 바이트가 있는 파일과 로그 확장자가 아닌 파일은 건너뛴다.

## 결과 (Consequences)

- 새 엔진을 추가할 때는 `parsers/<engine>.py` 에 `ParserSpec` 만 더하면 된다(파일명 힌트·마커·추가 이벤트).
- 오탐이 보고되면 이 표를 좁히는 방향으로만 수정하고, 픽스처 `tests/fixtures/logs/generic/` 에 회귀 케이스를 추가한다.
- 범용 파서는 결코 KB 매칭을 대체하지 않는다: 이벤트가 있어도 KB 가 일치하면 `KB-UNKNOWN` 은 생성되지 않는다.
