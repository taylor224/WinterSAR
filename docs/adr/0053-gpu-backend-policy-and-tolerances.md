# ADR-0053: GPU(CuPy) 백엔드 정책과 커널 수치 허용 오차 (PERF-10)

- 상태(Status): 채택 (Goldstein 세부 상수는 문헌 확인 후 재검토)
- 날짜(Date): 2026-09-16
- 관련 ID: PERF-10, 플랜 §6.2 "PERF-10 (GPU)", §8(GPU 커널은 CPU 결과와 일치 테스트)
- 검증 출처(Sources):
  - CuPy 참조 (2026-09-16): FFT `cupy.fft.fft2/ifft2/fftshift/ifftshift`
    <https://docs.cupy.dev/en/stable/reference/fft.html>; `cupy.pad(array, pad_width, mode='constant',
    …)` 모드 constant/edge/reflect/symmetric/wrap 등
    <https://docs.cupy.dev/en/stable/reference/generated/cupy.pad.html>; NumPy 대응표에서
    `cupy.cumsum, angle, exp, abs, conj, isfinite, hanning` 확인
    <https://docs.cupy.dev/en/stable/reference/comparison.html>;
    `cupy.asnumpy` <https://docs.cupy.dev/en/stable/reference/generated/cupy.asnumpy.html>;
    `cupy.cuda.runtime.getDeviceCount`
    <https://docs.cupy.dev/en/stable/reference/generated/cupy.cuda.runtime.getDeviceCount.html>.
  - Goldstein & Werner (1998), Radar interferogram filtering for geophysical applications,
    GRL 25(21), doi:10.1029/1998GL900033 — 필터 `H(u,v) = S{|Z(u,v)|}^α · Z(u,v)`.
  - 구현 `src/wintersar/compute/xp.py`, `src/wintersar/compute/kernels.py`; 테스트
    `tests/unit/compute/`.

## 맥락 (Context)

멀티룩·Goldstein 필터·코히어런스 추정은 CPU numpy 후처리 단계이며, GPU가 있으면 CuPy로 가속하되
없으면 그대로 동작해야 한다(§6.1 PERF-10, "ISCE2 내부는 건드리지 않음"). 커널마다 CPU 결과와의
수치 일치를 허용 오차와 함께 테스트해야 한다(§8).

## 선택지 (Options)

1. 커널을 numpy와 cupy로 두 번 작성.
2. `xp` 배열 모듈 추상화 하나로 작성하고 두 라이브러리에 공통인 연산만 사용(array API 방식).
3. 외부 프레임워크(JAX/PyTorch) 도입.

## 결정 (Decision)

선택지 2.

- **백엔드 선택** `get_xp(prefer_gpu)`: 환경변수 `WINTERSAR_GPU`(0/false → numpy 강제, 1/true → CuPy
  필수, 없으면 `GpuNotAvailableError`) > 인자 `prefer_gpu`(True/False/"auto") > auto(CuPy import 가능
  **그리고** CUDA 장치 ≥ 1). 모듈 import는 CUDA를 건드리지 않는다(lazy). 결과는 `lru_cache`,
  테스트는 `reset_backend_cache()`.
- **커널 규약**: 입력 배열의 모듈(`xp_of`)을 따르므로 numpy를 넣으면 CPU, cupy를 넣으면 GPU에서 돈다.
  호스트↔장치 이동은 호출자가 `asarray(a, xp=cupy)` / `to_numpy`로 명시한다.
- **공통 연산만 사용**: fft2/ifft2, pad(constant/reflect), cumsum(dtype 인자), abs/angle/exp/conj,
  maximum/minimum/where/sqrt, reshape/mean. `scipy.ndimage`는 쓰지 않고 박스 합은 누적합(summed-area
  table)으로 구현해 두 백엔드가 같은 산술을 수행하게 한다. 누적합은 **float64/complex128**로 계산하고
  입력 dtype으로 되돌린다(float32 누적합은 수 메가픽셀에서 작은 창 합을 잃는다 — 실측 오차 2000×2000
  에서 4e-6 유지). 0이어야 할 값이 음수로 나오는 취소 오차는 `maximum(…, 0)`으로 막는다.
- **Goldstein 구현 선택값**(문헌 확인 전 wintersar 결정): 패치 `window`(기본 64 = `config.engine.filter.window`
  기본값), `alpha` 기본 0.6(= `config.engine.filter.alpha`), 오버랩 기본 `window // 2`, 스펙트럼 크기
  평활 3×3 박스, 패치 합성 가중치 2-D Hann(모든 픽셀 양의 가중), 마지막 패치를 영상 끝에 맞춰 **패딩
  없이** 전체를 덮는다. 원 논문의 패치·오버랩·평활 상수는 `docs/open-questions.md`에 확인 항목으로 남긴다.
- **허용 오차(테스트에 명시)**: 박스 합 vs `scipy.ndimage.uniform_filter` 1e-4(절대, float32);
  상수의 멀티룩은 정확히 일치; Goldstein — 패치당 정수 주기의 순수 평면 위상 변화 `< 1e-3 rad`,
  `alpha = 0` 항등 `< 1e-4 rad`, 잡음 위상 표준편차 절반 이하로 감소; 코히어런스 — 동일 영상 `> 1 − 1e-4`,
  독립 잡음 평균 `< 0.4`(창 7×7, 49 looks); **GPU 대 CPU**: 값 `rtol = atol = 1e-5`, 위상 `1e-4 rad`
  (`@pytest.mark.gpu`, CuPy/CUDA 없으면 skip). CPU가 항상 기준이며 GPU 결과를 기준으로 삼지 않는다.

## 결과 (Consequences)

- CuPy는 선택 의존성(`wintersar[gpu]`)이고 이 개발 머신에는 없다. GPU 패리티 테스트는 CUDA 머신에서
  `pytest -m gpu`로 돌린다; CI는 CPU 경로만 검증한다.
- `cupy.hanning`이 있지만 창 함수는 numpy로 만들어 업로드한다(백엔드 간 공식 차이 배제).
- 새 커널은 같은 규칙(공통 연산, CPU 기준, 허용 오차 명시)을 따라야 하며 `scipy` 의존 커널은 GPU 경로에
  넣지 않는다.
