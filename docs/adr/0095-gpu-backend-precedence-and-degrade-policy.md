# ADR-0095: GPU 백엔드 선택 우선순위와 CPU 강등(degrade) 정책 (PERF-10)

- 상태(Status): 채택 (ADR-0053의 "WINTERSAR_GPU=1 이면 예외" 조항을 대체)
- 날짜(Date): 2026-09-23
- 관련 ID: PERF-10, 플랜 §6.1 "선택적 CuPy 백엔드(자동 감지, CPU fallback)", §6.2 "PERF-10 (GPU)", ENV-005
- 검증 출처(Sources):
  - 구현 `src/wintersar/compute/xp.py` (`resolve_backend`, `stage_gpu`, `degrade_finding`),
    테스트 `tests/unit/compute/test_xp.py`, `tests/unit/compute/test_gpu_parity.py`
    (`test_ported_function_forced_gpu_without_cupy_degrades_to_cpu`).
  - 설정 필드 `src/wintersar/pipeline/config.py` `ComputeCfg.gpu: Literal["auto"] | bool`; 실행기
    `src/wintersar/pipeline/executor.py` — `params["_gpu"] = bool(self.machine.gpu)`,
    `machine = detect().budget(..., gpu=cfg.compute.gpu)` (`src/wintersar/util/sysinfo.py`
    `MachineSpec.budget`: `auto` → 감지값, `true`/`false` → 강제).
  - ENV-005 메시지 `src/wintersar/i18n/{ko,en}.yaml` (`env.ENV-005.cause/fix`, 기존 키 재사용).
  - CuPy 장치 탐지 `cupy.cuda.runtime.getDeviceCount`
    <https://docs.cupy.dev/en/stable/reference/generated/cupy.cuda.runtime.getDeviceCount.html>.

## 맥락 (Context)

ADR-0053 은 `compute.xp` 추상화를 정의했지만 커널을 부르는 코드가 없었고(리뷰 지적: `compute.{xp,kernels}`
를 import 하는 모듈 없음), 백엔드 선택은 환경변수와 함수 인자만 보았다. 이번에 unwrap 마스크·프린지 밀도,
DEM 기하 마스크, 연구 모듈 Goldstein/멀티룩이 커널로 이식되면서(ADR-0097) 다음을 정해야 한다.

1. `config.compute.gpu`(auto|true|false)가 실행기 안에서는 어떻게 커널까지 전달되는가.
2. 실행기 밖(CLI `select precheck`, `research repr-phase`, 테스트)에서는 무엇을 보는가.
3. GPU 를 요구했는데 CuPy/CUDA 가 없을 때 — ADR-0053 은 `GpuNotAvailableError` 를 던졌다. 그러나 GPU
   워크스테이션에서 쓴 config 를 CPU 머신에서 재실행하면 언래핑 단계 전체가 죽는다. 플랜 §6.1 은
   "CPU fallback" 을 요구한다.

## 선택지 (Options)

1. 환경변수만 사용(현행). 실행기가 `os.environ` 을 바꿔 config 를 전달 — 프로세스 전역이라 병렬 단계/테스트
   에서 새고, 되돌리기 어렵다.
2. 모든 이식 함수에 `gpu=` 키워드를 추가하고 호출 스택 전체에 손으로 전달.
3. `contextvars` 기반 단계 컨텍스트(`stage_gpu(params["_gpu"])`) + 명시 인자 + 환경변수 + 자동 감지를 한
   함수(`resolve_backend`)로 합치고, 강등은 예외 대신 Finding 으로 보고.

## 결정 (Decision)

선택지 3. `wintersar.compute.xp.resolve_backend(gpu=None, *, strict=False) -> Backend` 가 유일한 결정
지점이며, 우선순위는 **높은 것부터**:

| 순위 | 출처 | 값 | 비고 |
|---|---|---|---|
| 1 | 환경변수 `WINTERSAR_GPU` | `0/false/cpu` → numpy 강제, `1/true/cuda` → GPU 요청, `auto`/미설정 → 다음 순위 | 운영자의 1회성 오버라이드(예: 고장난 GPU 를 config 수정 없이 끔). 실행기 안팎 모두 적용 |
| 2 | 함수 인자 `gpu=` | `True`/`False`; `None`/`"auto"` → 다음 순위 | 이식 함수(`combine_masks`, `fringe_density`, `slope_aspect`, `local_incidence`, `range_slope`, `compute_geometry_masks`, `masks_for_both_directions`, `research.goldstein_filter`, `repr_filtered`)와 `representative_phase(**kw)` 가 받음 |
| 3 | 단계 컨텍스트 `stage_gpu(params["_gpu"])` | 실행기 private param `_gpu` = `config.compute.gpu` 를 `MachineSpec.budget` 으로 해석한 bool | 단계 코드가 `with stage_gpu(params.get("_gpu")):` 로 감싸면 그 아래 모든 커널이 상속. 스레드/태스크별(`contextvars`) |
| 4 | 자동 감지 | CuPy import 가능 **그리고** CUDA 장치 ≥ 1 → cupy, 아니면 numpy | `cupy_available()` (`lru_cache`, 테스트는 `reset_backend_cache()`) |

- **실행기 안**: `config.compute.gpu` 는 실행기가 이미 `_gpu` 로 넘기므로(ADR-0034 private params) 단계
  코드는 `_gpu` 를 읽어 인자(2) 또는 컨텍스트(3)로 전달한다. `compute.gpu: auto` 는 감지값(`MachineSpec.gpu`),
  `true`/`false` 는 강제값이 온다. **실행기 밖**에서는 `_gpu` 가 없으므로 환경변수(1)와 자동 감지(4)만 남는다.
- **강등 정책**: 요청이 `True` 인데 CuPy/CUDA 가 없으면 예외 없이 numpy 로 실행하고 `Backend.findings` 에
  `ENV-005`(severity `WARN`, `message_key="env.ENV-005.cause"`, `fix_key="env.ENV-005.fix"`,
  evidence `requested_by ∈ {env, argument, stage}`, `cupy_installed`, `cuda_device`, `backend="numpy"`,
  scope `compute`)를 담는다. 같은 프로세스에서 경고 로그(`wintersar.compute`)는 한 번만 남긴다. 단계 코드는
  `findings.extend(resolve_backend(params.get("_gpu")).findings)` 로 보고서에 붙인다(`check-install` 의
  ENV-005 는 INFO 로 "GPU 없음" 을 알리는 것이고, 여기서는 명시 요청이 무시된 것이므로 WARN).
- `strict=True`(`get_xp(..., strict=True)`)만 예전처럼 `GpuNotAvailableError` 를 던진다 — 벤치/테스트가
  "정말 GPU 에서 돌았는가" 를 확인해야 할 때.
- `get_xp(prefer_gpu)` 는 호환용 얇은 래퍼(`resolve_backend(prefer_gpu).xp`)로 남긴다. 커널 자체는 여전히
  입력 배열의 모듈(`xp_of`)을 따르므로, 백엔드 결정은 이식 함수(경계)에서 한 번만 하고 안에서는 `asarray(a, xp)`
  로 올리고 `to_numpy` 로 내린다. 모든 공개 이식 함수는 **numpy 를 돌려준다**.

## 결과 (Consequences)

- ADR-0053 의 "`WINTERSAR_GPU=1` 이면 `GpuNotAvailableError`" 는 이 ADR 로 대체된다(테스트
  `test_env_gpu_request_degrades_to_cpu_with_env_005`). 나머지(수치 정책, 공통 연산만 사용)는 유지.
- `unwrap/api.py`(다른 담당)는 `stack_fringe_density`/`_unwrap_one` 호출에 `gpu=params.get("_gpu")` 를 넘기고
  `run_unwrap` 의 findings 에 `resolve_backend(params.get("_gpu")).findings` 를 추가해야 config 값이 실제로
  커널까지 닿는다(통합자 항목). 그 전까지는 환경변수·자동 감지로 동작한다. 대안으로 실행기 `_dispatch` 를
  `with stage_gpu(params["_gpu"]):` 로 감싸면 모든 단계가 한 줄로 상속한다.
- 환경변수가 config 보다 우선하므로 "config 가 GPU 인데 왜 CPU 로 도는가" 는 `WINTERSAR_GPU` 부터 확인한다;
  `Backend.source` 가 어느 출처가 이겼는지 알려준다.
