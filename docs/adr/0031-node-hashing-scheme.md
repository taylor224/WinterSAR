# ADR-0031: 노드 해시 규칙 (파라미터 정규화·빠른 아티팩트 해시·엔진 버전)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: PERF-03, PERF-06
- 검증 출처(Sources):
  - `src/wintersar/util/hashing.py` (`hash_params`, `hash_file_fast`, `hash_tree`, `hash_path`)
  - `src/wintersar/pipeline/config.py::Config.stage_params` (단계별 설정 절 선택)
  - 플랜 §6.2 PERF-03 "파라미터 정규화(정렬·기본값 채움) 후 해시. 엔진 버전을 해시에 포함"

## 맥락 (Context)

노드 식별자는 `hash(stage, 정규화된 params, 입력 아티팩트 해시, 엔진 버전)`이어야 한다(플랜 §5.3).
세 가지를 정해야 한다: 파라미터를 어떻게 정규화하는가, 수 GB 래스터의 해시를 어떻게 싸게 구하는가,
엔진 버전을 어떻게 얻는가.

## 선택지 (Options)

1. 파라미터: 설정 파일 원문 해시 / **단계 관련 절만 pydantic 기본값 채워 정렬한 JSON** / 전체 설정 해시.
2. 아티팩트: 전체 내용 SHA-256 / **크기+mtime_ns+앞뒤 1 MiB(`hash_path(fast=True)`)** / 경로만.
3. 엔진 버전: 무시 / **`Engine.detect_version()` 문자열을 해시에 포함**.

## 결정 (Decision)

- **파라미터**: `Config.stage_params(stage)`(단계에 영향을 주는 절만, 기본값 채움, 키 정렬, `mode="json"`)에
  `param_overrides[stage]`를 깊은 병합한 뒤 `canonicalise`(재귀 키 정렬, tuple→list, Path→str)한다.
  `_`로 시작하는 키(`_out_dir`, `_cores` …)는 실행 컨텍스트이므로 해시에서 제외한다.
  결과: `unwrap.coherence_threshold` 변경은 `unwrap` 이하만, `engine.looks` 변경은 `coregister` 이하만
  무효화한다(통합 테스트 `test_param_change_reruns_only_changed_stage_and_downstream`).
- **입력 아티팩트**: 상류 manifest의 `extra.artifacts[name].sha256`를 그대로 쓴다. 이 값은 실행 직후
  `hash_path(path, fast=True)`로 계산하며 방법을 `artifact.meta.hash_method="fast"`에 기록한다.
  캐시 적중 시(`find_cached(verify=True)`) 같은 방법으로 재계산해 manifest 값과 다르면 **stale**로 보고
  다시 실행한다(사용자가 산출물을 손댄 경우도 잡힌다: `test_modified_output_invalidates_cache`).
  빠른 해시는 mtime을 포함하므로 같은 내용을 다시 써도 해시가 바뀐다. 이는 "강제 재실행 후 하류 재실행"과
  일치하는 보수적 동작이며, 내용 동일성 기반 재사용이 필요해지면 `fast=False`를 단계별 옵션으로 연다.
- **엔진 버전**: `Dag.engine_version(name)`이 `get_engine(name).detect_version()`을 한 번 호출해 캐시한다.
  미설치(`None`)도 해시에 들어가므로 설치 후 첫 실행은 자동으로 새 노드가 된다
  (`test_engine_version_change_invalidates_cache`).
- **해시 길이**: `hash_params` 기본 16 hex(64비트). 단계당 노드 수가 수천 개를 넘지 않으므로 충돌 확률은
  무시할 수 있고 디렉터리 이름으로 짧아 좋다.
- **미해결 노드**: 상류가 캐시에 없으면 입력 해시를 알 수 없다. 이런 노드는 `node_hash=None`이며 `plan`에는
  `pending-<hash(stage, params, producer 이름, engine, version)>`(`extra.provisional=true`)로 표시한다.
  실행기는 상류가 끝난 직후 실제 해시를 계산해 캐시를 다시 조회한다.

## 결과 (Consequences)

- `search`의 params에 AOI **경로**가 들어가므로 프로젝트 디렉터리를 옮기면 `search` 이하가 무효화된다.
  select 모듈이 AOI 파일 내용 해시를 `candidates` 아티팩트 메타에 넣으면 이후 단계는 영향이 없다.
- 작업 디렉터리를 mtime 보존 없이 복사(`cp` without `-p`)하면 모든 캐시가 stale이 된다. 문서에 `rsync -a`를
  권고한다.
- manifest에는 `hash_method`가 기록되므로 나중에 방법을 바꿔도 이전 manifest를 올바르게 검증할 수 있다.
