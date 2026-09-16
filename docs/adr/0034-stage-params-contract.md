# ADR-0034: 단계 파라미터 계약 (중첩 설정 절 + 오버라이드 + `_` 비공개 키)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-05, PERF-03
- 검증 출처(Sources):
  - `src/wintersar/pipeline/config.py::Config.stage_params` (단계 → 설정 절 매핑)
  - `src/wintersar/engines/base.py::Engine.run` docstring (`_out_dir`, `_workdir`, `_cache_dir`, `_cores`,
    `_memory_gb`, `_gpu`)
  - `src/wintersar/engines/fake.py` (top-level 키 `n_dates`, `shape`, `looks: int` … 를 읽음)

## 맥락 (Context)

엔진 `run(stage, inputs, params, log_dir)`의 `params`가 어떤 모양인지 어댑터 작성자들이 합의해야 한다.
`Config.stage_params`는 설정 **절 단위**로 중첩된 사전(`{"unwrap": {...}}`)을 돌려주고, fake 엔진은
top-level 키(`n_dates`, `shape`)를 읽는다. 절을 평탄화하면 `engine.looks="auto"`가 fake 엔진의
`int(params["looks"])`와 충돌하고, `precheck`처럼 절이 여러 개인 단계는 키 충돌 위험이 있다.

## 선택지 (Options)

1. 절 평탄화(`params["coherence_threshold"]`).
2. **중첩 유지**(`params["unwrap"]["coherence_threshold"]`) + 오버라이드는 top-level 또는 중첩 깊은 병합.
3. 둘 다 넣기(중복).

## 결정 (Decision)

선택지 2.

- `params` = `canonicalise(deep_merge(Config.stage_params(stage), param_overrides[stage]))`.
  - 설정 값은 절 이름 아래 중첩: `params["engine"]["looks"]`, `params["unwrap"]["tiles"]`,
    `params["timeseries"]["troposphere"]`.
  - 오버라이드(`api.run(param_overrides=...)`, CLI `--set stage.key=value`)는 top-level에 놓이며
    (`params["n_dates"]`), 점 표기로 절 안을 겨냥할 수 있다(`--set unwrap.unwrap.cost=smooth` →
    `params["unwrap"]["cost"]`). 값은 YAML 스칼라로 파싱된다(`0.4` → float, `[24,24]` → list).
- 실행기가 추가하는 비공개 키(해시 제외): `_out_dir`, `_log_dir`, `_workdir`, `_cache_dir`, `_cores`,
  `_memory_gb`(이번 단계에 허용된 예약량), `_gpu`. 경로는 `str`.
- 엔진에 전달되는 `inputs`는 `StageSpec.inputs + optional_inputs`에 선언된 아티팩트만 담는다
  (선언되지 않은 상류 산출물에 의존하면 해시가 건전하지 않으므로 금지).

## 결과 (Consequences)

- 어댑터 작성 규칙: 설정 값은 `params[<section>][<key>]`로 읽고, 실험용 top-level 오버라이드는
  `params.get(<key>)`로 읽는다. fake 엔진은 후자만 사용하므로 `unwrap.coherence_threshold` 설정값은 fake
  경로에서 해시에만 영향을 준다(테스트에서 `--set unwrap.coherence_threshold=…`로 명시 전달).
- `unwrap` 스케줄러(`wintersar.unwrap.api.run_unwrap(igrams, params, out_dir, log_dir, machine)`)도 같은
  `params`를 받는다. 다른 모양을 원하면 unwrap 모듈이 ADR로 갱신한다.
