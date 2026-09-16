# ADR-0061: phase linking(EVD/EMI, 순차 추정기) numpy 구현과 참고문헌

- 상태(Status): 채택 (연구 코드; 기본 경로 아님)
- 날짜(Date): 2026-09-16
- 관련 ID: R-07, R-15, PERF-05, PERF-06, 규칙 11.2·11.3
- 검증 출처(Sources):
  - dolphin `src/dolphin/phase_link/_core.py` —
    https://raw.githubusercontent.com/isce-framework/dolphin/main/src/dolphin/phase_link/_core.py
    (`evd: eigh_largest_stack(C_arrays * abs(C_arrays))`, `emi: eigh_smallest_stack(Gamma_inv * C_arrays)`,
    `Gamma = (1 - beta) * Gamma + beta * Id`, 참조 날짜 `estimate *= exp(-1j * angle(ref))`)
  - dolphin `src/dolphin/phase_link/metrics.py` —
    https://raw.githubusercontent.com/isce-framework/dolphin/main/src/dolphin/phase_link/metrics.py
    (시간 코히어런스 `|Σ_{i<j} exp(j(arg C_ij − (φ_i − φ_j)))| / N_pairs`)
  - Ansari, De Zan, Bamler 2018, "Efficient Phase Estimation for Interferogram Stacks",
    IEEE TGRS 56(7):4109–4125, doi:10.1109/TGRS.2018.2826045 (EMI) — Crossref 메타데이터 확인
  - Ansari, De Zan, Bamler 2017, "Sequential Estimator: Toward Efficient InSAR Time Series Analysis",
    IEEE TGRS 55(10):5637–5652, doi:10.1109/TGRS.2017.2711037 (순차 추정기·compressed SLC) — Crossref 확인
  - 구현: `src/wintersar/research/repr_phase.py`(`sample_coherence_matrix`, `link_phases`,
    `phase_link_stack`, `temporal_coherence`, `repr_phase_link`), 테스트
    `tests/unit/research/test_repr_phase.py::test_phase_linking_*`, `::test_sequential_estimator_*`

## 맥락 (Context)

플랜 §5.7의 `phase_link`는 "미니스택 phase linking(dolphin EMI/EVD) 결과를 저해상도 기준 위상으로
사용"이다. dolphin은 설치되어 있지 않고(CLAUDE.md), 대표위상 연구에는 블록 단위 추정만 필요하므로
numpy로 구현한다(연구 코드; 규칙 11.3의 "SBAS 역산 코어 재구현 금지"와는 다른 단계).

## 선택지 (Options)

1. dolphin을 선택 의존성으로 두고 `run_phase_linking`을 호출 — 미설치, JAX 의존, 블록 단위
   대표위상에는 과함.
2. **numpy 구현(채택)** — dolphin과 같은 행렬 정의를 따르고 합성 공분산으로 검증.
3. EVD만 구현 — EMI가 R-15 논의의 핵심(순차 추정기)이라 제외 불가.

## 결정 (Decision)

- **표본 코히어런스 행렬**: `factor × factor` 블록(`L = factor²` looks)에서
  `C_ij = <s_i s_j*> / sqrt(<|s_i|²><|s_j|²>)`, 위상은 `φ_i − φ_j`.
- **EVD**: `C ∘ |C|`의 최대 고윳값 고유벡터(dolphin과 동일). **EMI**: `Γ = (1−β)|C| + βI`,
  `Γ⁻¹ ∘ C`의 최소 고윳값 고유벡터(`β = 0.01`, dolphin 기본 정규화 형태). 참조 날짜(기본 0)의
  위상을 0으로 맞춘다. `np.linalg.eigh` 배치 호출(에르미트 행렬).
- **시간 코히어런스**: dolphin `estimate_temp_coh`와 같은 식(상삼각 평균의 절댓값).
- **순차 추정기**(`ministack_size`): 미니스택별 링크(참조 = 첫 날짜) → compressed SLC
  `c_k = v_kᴴ s_k / sqrt(m)`(픽셀별, `v_k`는 블록 추정치를 블록 픽셀로 확장) → compressed SLC들을
  다시 링크해 미니스택 간 위상 `θ_k` → `φ_i = φ_i^(k) + θ_k`, 마지막에 참조 날짜 0. Ansari 2017의
  datum connection과 같은 구조(단, 잔차 위상 보정 등 후처리는 생략).
- **쌍 대표위상**(`repr_phase_link(pair=(i, j))`): `tc · exp(j(φ_j − φ_i))` — 부(j) − 참조(i)
  규약(`synth.make_stack`, `SynthSlcStack.pair_phase_true`)과 일치. 인자 이름은 `link_method`
  (디스패처의 `method`와 충돌 방지).
- **검증**: 정확한 공분산에서는 두 방법 모두 오차 1e-8 이하; 표본 스택(10 날짜, 64 looks,
  τ = 6, floor 0.3)에서 RMSE < 0.2 rad, 순차(m = 4) 결과가 전체 스택 결과와 0.25 rad 이내.

## 결과 (Consequences)

- `L < N`(looks가 날짜 수보다 적음)이면 표본 행렬이 특이하여 EMI의 `Γ⁻¹`이 불안정하다 —
  `S_synth_repr_phase`(factor 3 → 9 looks, 12 날짜)에서 그 조건이 재현된다. 실험에서는 `factor`를
  `ceil(sqrt(N))` 이상으로 두거나 `beta`를 키워야 하며, 이는 표에서만 읽는다(규칙 11.8).
- 실데이터 A/B(R-15)는 이 구현이 아니라 dolphin 본체로 수행한다(ADR-0064). 이 구현은 합성
  실험과 대표위상 비교용이다.
- 후속: 마스크/NaN 픽셀 가중, 미니스택 간 잔차 보정, GPU(`compute.xp`) 경로(PERF-10).
