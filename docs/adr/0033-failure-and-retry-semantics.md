# ADR-0033: 실패·재시도 의미론 (`--force`, `--from`, diagnose 연동, retry_hint)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-02, R-05, R-14, PERF-03, PERF-04
- 검증 출처(Sources):
  - 플랜 §4.5 "`run`은 실패 시 자동으로 `diagnose`를 호출해 Finding을 리포트에 첨부한다"
  - 플랜 §5.3 "실패 처리: stage 로그를 diagnose에 넘겨 Finding을 리포트에 첨부하고 재시도 가능 여부 표시"
  - `src/wintersar/pipeline/executor.py`, `src/wintersar/pipeline/api.py`

## 맥락 (Context)

연구자의 반복 튠은 "실패 → 원인 파악 → 파라미터 조정 → 변경 단계부터 재실행"의 순환이다. 실패가 어떤
기록을 남기고, 어떤 플래그가 무엇을 다시 돌리는지 명확해야 한다.

## 결정 (Decision)

### 실행 순서와 중단
- 단계는 `STAGE_ORDER`대로 순차 실행. 한 단계가 실패하면 **즉시 중단**하고 이후 단계는 기록하지 않는다.
- `plan` 단계에서 이미 `FAIL` Finding이 있으면(엔진 미등록 `PIPELINE-005`, 미설치 `ENV-001`, 입력 없음
  `PIPELINE-002`) **아무것도 실행하지 않고** `ok=False`로 반환한다(fail fast; 상류만 돌리고 싶으면 `--until`).

### 실패 기록
1. manifest를 `status: failed`, `finished_at`, `extra.error`(마스킹)로 쓴다. 로그는 `logs/`에 남는다.
2. `wintersar.diagnose.api.diagnose_logs(log_dir, engine)`를 지연 import로 호출한다. 모듈이 없으면 `[]`,
   diagnose 자체가 예외를 내면 `extra.diagnose_error`에 적고 원래 실패를 가리지 않는다.
3. Finding 순서: `PIPELINE-001`(항상, FAIL, `evidence.log_excerpt`에 각 `*.log`의 마지막 40줄·8 KB 한도·
   마스킹) → 예외가 들고 온 `findings` 속성(예: 엔진 미설치 `ENV-001`, 산출물 누락 `PIPELINE-009`) →
   diagnose Finding → `PIPELINE-008`(retry_hint가 있을 때, INFO).
4. **retry_hint**: 예외 객체의 `retry_hint` 속성, 또는 Finding의 `evidence['retry_hint']`/`params['retry_hint']`
   중 첫 값을 `extra.retry_hint`에 복사한다. unwrap 스케줄러가 타일 파라미터 재조정을 제안하는 통로다.
5. `Executor.run`은 `PipelineError(records, findings, artifacts, failed)`를 던지고, `api.run`은 이를 받아
   `RunResult(ok=False, failed_stage=…)`로 바꾼다(라이브러리 호출자는 예외를 다루지 않아도 된다).
   CLI는 Finding을 원인→조치 순으로 출력하고 종료 코드 1.

### 재시도 플래그
- **재실행(기본)**: 실패 manifest는 캐시 적중이 아니므로 같은 설정으로 다시 `run`하면 실패 단계부터 다시
  시도한다(상류는 캐시).
- **`--force STAGE`**: 해당 단계와 **producer 링크로 도달 가능한 모든 하류**를 강제 재실행한다
  (`Dag._apply_force`). 해시가 같아도 재실행하며 같은 디렉터리를 비우고 덮어쓴다. 하류를 명시적으로
  포함하는 이유: 빠른 해시가 mtime을 포함해 어차피 하류가 무효화되지만, 파일 시스템 mtime 해상도에
  의존하지 않기 위해서다.
- **`--until STAGE`**: 해당 단계까지만 그래프에 포함한다(그 이후는 기록조차 없다).
- **`--from STAGE`**: 그 앞 단계는 실행하지 않는다. 정확한 해시가 캐시에 있으면 그것을, 없으면 그 단계의
  **가장 최근 ok manifest**를 재사용하고 `PIPELINE-003`(WARN)을 붙인다. 아무것도 없으면 `PIPELINE-002`
  (FAIL)로 실행 전에 중단한다. `--from`은 "상류 설정을 바꿨지만 재계산하지 않겠다"는 사용자의 명시적
  선택이므로 WARN으로 충분하다고 본다.
- `--force`와 `--from`이 겹치면 `--from` 앞 단계의 force는 무시된다.

### 메모리 예산
- `ResourceBudget.reserve(stage, need)`로 단계 실행 전에 예약(sum ≤ budget), 종료 후 해제. 예상치가 예산을
  넘으면 실행은 하되 `PIPELINE-006`(WARN)을 기록한다 — 타일링·분할은 엔진(unwrap 스케줄러, PERF-04)의
  책임이며 `_memory_gb`로 허용량을 전달한다.

## 결과 (Consequences)

- 실패 로그 픽스처(`tests/fixtures/logs/`)는 diagnose 모듈이 관리한다. 파이프라인 테스트는 diagnose 훅을
  monkeypatch로 대체해 순서·retry_hint 전달만 검증한다.
- `PIPELINE-001`의 `fix`는 `wintersar diagnose <log_dir> --engine <engine>`과 `run --from <stage>`를 안내한다.
- 부분 실패(단계 안 일부 job만 실패) 재개는 엔진 어댑터의 `logs/`·`out/` 재사용 정책에 달려 있으며,
  이 ADR은 단계 단위만 정의한다. ISCE2 run_files 병렬 실행기(PERF-07)가 구현될 때 보강한다.
