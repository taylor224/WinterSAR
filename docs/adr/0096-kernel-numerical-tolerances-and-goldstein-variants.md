# ADR-0096: 커널 수치 허용 오차와 Goldstein 필터 변형(variant) 규약 (PERF-10)

- 상태(Status): 채택 (GPU 패리티 허용 오차는 CUDA 머신 실측 전까지 잠정 — open question #72)
- 날짜(Date): 2026-09-23
- 관련 ID: PERF-10, 플랜 §6.2 "커널별 CPU 결과와 수치 일치 테스트(허용 오차 명시)", §8, R-07(연구 `filtered` 방법)
- 검증 출처(Sources):
  - 구현 `src/wintersar/compute/kernels.py`; 테스트 `tests/unit/compute/test_kernels.py`,
    `tests/unit/compute/test_gpu_parity.py`, `tests/unit/research/test_repr_phase_kernels.py`(옛 구현을
    `_legacy_*` 로 동결한 등가 테스트), `tests/unit/unwrap/test_masks_xp.py`,
    `tests/unit/unwrap/test_scheduler_xp.py`, `tests/unit/select_geometry/test_geometry_compute.py`.
  - Goldstein & Werner (1998), GRL 25(21), doi:10.1029/1998GL900033.
  - dolphin `goldstein.py` (Bartlett 가중·step=psize//2·weight_sum 정규화·빈 패치 건너뜀 확인, 2026-09-23)
    <https://raw.githubusercontent.com/isce-framework/dolphin/main/src/dolphin/goldstein.py>.
  - CuPy: `cupy.pad`(mode `constant`/`wrap`) <https://docs.cupy.dev/en/stable/reference/generated/cupy.pad.html>;
    `cupy.extract` <https://docs.cupy.dev/en/stable/reference/generated/cupy.extract.html>;
    `cupy.gradient(axis=)` <https://docs.cupy.dev/en/stable/reference/generated/cupy.gradient.html>;
    `cupy.where` <https://docs.cupy.dev/en/stable/reference/generated/cupy.where.html>; ufunc 인자는
    "cupy.ndarray object or a scalar" <https://docs.cupy.dev/en/stable/reference/generated/cupy.ufunc.html>;
    NumPy 대응표(`hypot/arctan2/degrees/radians/clip/broadcast_to/count_nonzero/concatenate/nan_to_num`
    존재, `errstate` 없음) <https://docs.cupy.dev/en/stable/reference/comparison.html>;
    타이밍 동기화 `cupy.cuda.Device().synchronize()`
    <https://docs.cupy.dev/en/stable/reference/generated/cupy.cuda.Device.html>, 워밍업 필요
    <https://docs.cupy.dev/en/stable/user_guide/performance.html>.
  - scipy `uniform_filter(mode="wrap")` 의 짝수 크기 중심 규약은 테스트
    `test_wrap_smoothing_matches_scipy_periodic_uniform_filter` 로 실측(size 2·3·4·5).

## 맥락 (Context)

연구 모듈 `repr_phase.goldstein_filter` 는 `compute.kernels.goldstein_filter` 와 세부가 달랐다(Bartlett vs Hann
가중, 0 패딩 격자 vs 마지막 패치 클램프, 주기적(`wrap`) vs 0 패딩 스펙트럼 평활, 입력 크기 유지 vs 필터 값
그대로, complex128 vs complex64). "중복 구현 삭제" 를 위해서는 커널이 연구 구현을 **수치적으로 재현**해야
하고(규칙 11.10: 연구자 확인 없이 기본값·결과를 바꾸지 않음), 그 등가를 허용 오차와 함께 테스트로 남겨야 한다
(규칙 11.4).

## 선택지 (Options)

1. 연구 `filtered` 를 커널 기본 변형으로 바꾸고 결과 변화를 수용 — 실험 재현성이 깨지고 Phase 6 설계 검토를
   거치지 않은 변경.
2. 커널에 연구 변형을 옵션으로 추가(`weight`, `pad`, `smooth_mode`, `keep_magnitude`, dtype 유지)하고 연구
   모듈은 얇은 래퍼만 남김.
3. 두 구현을 모두 유지 — 리뷰가 지적한 상태 그대로.

## 결정 (Decision)

선택지 2.

### dtype 규약
- `goldstein_filter`: complex64 입력 → complex64 출력, complex128 → complex128(누적·블렌드 가중치도 같은
  실수 정밀도), 실수 입력은 wrapped phase 로 보고 complex64. 이전에는 항상 complex64 로 캐스트했다.
- `multilook`, `box_sum`: 입력 dtype 유지(누적합만 float64/complex128, ADR-0053).
- `coherence_estimate`, `phase_noise_std`: float32.

### Goldstein 변형 옵션(모두 키워드 전용, 기본값 = 파이프라인 필터 = ADR-0053 그대로)
| 옵션 | 기본(파이프라인) | 연구(`repr_phase`) |
|---|---|---|
| `weight` | `hann`(양의 2-D Hann) | `bartlett`(거울 선형 램프, 짝수 창, 가장자리 `0.5/half` 양수 — dolphin 은 0) |
| `pad` | `clamp`(마지막 패치를 영상 끝에 맞춤, 창보다 작은 영상 거부) | `zero`(`window + k·step` 격자로 0 패딩 후 잘라냄, 작은 영상 허용) |
| `smooth_mode` | `box`(0 패딩 박스 평균) | `wrap`(주기 박스 평균 = `uniform_filter(mode="wrap")`) |
| `keep_magnitude` | `False` | `True`(`|z|·out/|out|`, 0 은 0) |
| `overlap` | 정수 픽셀 | 연구의 비율 `overlap` 은 `step = max(1, round(window·(1-overlap)))`, `overlap_px = window - step` 으로 변환 |

- **빈 패치 규칙(양쪽 공통, 새 규칙)**: 값이 전부 0 인 패치는 데이터도 가중치도 더하지 않는다(dolphin 과 같고
  연구 구현이 하던 것). 호스트 동기화 없이 0-차원 장치 플래그 `xp.any(patch != 0)` 를 곱해 구현 — GPU 에서
  패치마다 `bool()` 을 부르면 동기화가 패치 수만큼 생긴다. 파이프라인 기본 변형에서는 전부-0 패치가 있을 때만
  결과가 달라진다(마스크 영역이 이웃을 감쇠시키지 않게 됨).
- `smooth_mode="wrap"` 은 `xp.pad(mode="wrap")` 뒤 `box_sum` — 창 배치는 `size//2` 앞, `size-1-size//2` 뒤로
  scipy 와 같다(짝수 크기 포함, 실측).

### 허용 오차(테스트에 명시)
| 비교 | 허용 오차 | 근거 |
|---|---|---|
| 연구 `multilook` vs 옛 블록 평균 | **비트 동일**(`assert_array_equal`) | 같은 crop→reshape→`mean` 산술 |
| 연구 `goldstein_filter`/`repr_filtered` vs 동결한 옛 구현(complex128) | `rtol=1e-7, atol=1e-9` | 차이는 반올림뿐: 누적합 박스 평균 vs scipy 러닝 합, `acc/max(wsum,1e-12)` 정규화(크기 복원 시 스케일 상쇄). 6개 파라미터 조합(짝수 평활·창보다 작은 영상 포함)+NaN/실수 입력 |
| `smooth_mode="wrap"` vs `uniform_filter(mode="wrap")` | float64 `atol=1e-12`, float32 `1e-6` | size 2·3·4·5 |
| `combine_masks`, `fringe_density`, `slope_aspect`, `local_incidence`, `range_slope`, `compute_geometry_masks` (CPU) vs 이식 전 numpy | **비트 동일** | 같은 numpy 호출을 같은 순서로; `extract(cond, a)` 는 `a[cond]` 와 C-order 로 동일. 골든 파일(`tests/regression/golden/geometry`) 변경 없음 |
| complex64 vs complex128 Goldstein 위상 | `< 1e-4 rad`(wrapped 차) | float32 누적 오차 |
| **GPU 대 CPU**(`test_gpu_parity.py`, `@pytest.mark.gpu`) — float32 커널 | 값 `rtol=atol=1e-5`, 복소수는 위상 `< 1e-4 rad` + 크기 1e-5 | ADR-0053 유지 |
| GPU 대 CPU — float64 경로(기하 각도, 연구 Goldstein complex128, fringe density) | `rtol=atol=1e-9`(실수), `rtol=1e-6, atol=1e-8`(복소) | CUDA 수학 함수 ~1 ulp 차 예상 |
| GPU 대 CPU — 불리언 마스크(layover/shadow/combine_masks) | 불일치 픽셀 비율 `≤ 1e-3` | 임계값 경계 픽셀만 뒤집힐 수 있음 |
| 강제 GPU(무 CuPy) → CPU 강등 | 결과 **동일**(`array_equal`, NaN 포함) + ENV-005 | ADR-0095 |

CPU 가 항상 기준이다. GPU 패리티 테스트는 이 머신(CuPy 없음)에서는 이유를 명시해 skip 되며, CUDA 머신에서
`pytest tests/unit/compute/test_gpu_parity.py` 로 실행해 위 잠정 오차를 확정한다(#72).

## 결과 (Consequences)

- 연구 모듈의 사설 FFT/블록 평균 코드는 삭제되었고 테스트가 재도입을 막는다
  (`test_no_private_goldstein_or_block_mean_left_in_research`).
- `bench_kernels(shape)` 는 커널별 CPU/GPU wall time 을 **반환만** 한다(워밍업 1회 후 반복의 중앙값, 장치
  동기화 포함). 규칙 11.8: 숫자는 `bench_result.json` 으로만 기록하고 문서에는 쓰지 않는다.
- 새 커널/이식 함수는 같은 표에 행을 추가해야 하며, `scipy` 에 의존하는 경로는 GPU 백엔드에 넣지 않는다.
