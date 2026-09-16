# Phase 6 실험 설계 (R-07 대표위상 · R-06 스티칭 · R-15 순차 추정기 A/B)

상태: **연구자 확인 대기** (규칙 11.10 도메인 검수 체크포인트, ADR-0060). 아래 설계가
확인되기 전에는 `unwrap` 스케줄러 기본값(복소 멀티룩 + coarse_ref, tophu 기본)을 바꾸지 않는다.

## 1. 질문과 가설

| ID | 질문 | 가설(검증 대상) | 판정 지표 |
|---|---|---|---|
| Q1 (R-07) | 저해상도 대표위상을 산술 복소 멀티룩(`ml`) 대신 "지역을 가장 잘 설명하는" 위상으로 바꾸면 다중해상도 언래핑의 타일 정합이 좋아지는가 | 코히어런스 가중·SHP·phase linking·필터링 중 적어도 하나가 저코히어런스/이질 지역에서 `ml`보다 진 위상에 가깝다 | `phase_rmse_rad`, `phase_mae_rad`(저해상도 진 위상 대비), 이후 스티칭 오류율 |
| Q2 (R-06) | 타일 2π 오프셋을 저해상도 기준(`coarse_ref`) 대신 오버랩 합의 + 그래프 조정(`overlap_consensus`)으로 정하면 단차가 줄어드는가 | 기준 위상에 잡음·오류가 있을 때 `overlap_consensus`가 더 안정적이며, 기준이 정확하면 둘은 같다 | `offsets_exact`, `n_wrong_offsets`, `seam_boundaries_with_jump`, `unwrap_error_fraction` |
| Q3 (R-15) | 순차 추정기(compressed SLC) 경로가 전통 SBAS보다 실성능이 떨어지는가 | 문헌(CRLB 근접) vs 연구자 경험(성능 저하) — 실데이터 A/B로만 판단 | 폐합 RMS, 대조군 RMSE, 언래핑 작업 수, wall time, peak RSS |

## 2. 합성 데이터 (진값 제공, `research.synth`)

| 요소 | 구현 | 파라미터 |
|---|---|---|
| 변형 | 가우시안 침하 그릇, 선형 램프, 여러 변형원의 합(`deformation_sources`); Okada는 훅만(#60) | `amplitude_m`, `sigma_px`, `ramp` |
| 대기 | 파워법칙 스펙트럼(β = 8/3) 난류 위상(`turbulent_atmosphere`) | `std_rad` |
| DEM 오차 | 높이 오차 `dh` × 높이-위상 계수 `-4π/λ · B⊥/(R sin θ)` → **B⊥에 비례**(`dem_error_phase`) | `std_m`, `bperp_m` |
| 잡음 | 코히어런스 맵 기반 원형 가우시안 위상 잡음(`_phase_noise`, CRLB형 표준편차) | `coherence_base`, `looks` |
| SHP 영역 | 영역별 Rayleigh 진폭(스케일 상이)의 진폭 스택(`shp_amplitude_stack`) | `region_kind`, `region_scales` |
| 마스크 | 수역(왼쪽 띠), 능선 레이오버(센서를 향한 사면 경사 > 입사각, ADR-0017 규약) | `water_fraction`, `height_m`, `sigma_px` |
| SLC 스택 | 알려진 공분산 `A²·Γ∘e^{j(φ_i−φ_j)}`의 표본 `s = D·chol(Γ)·w`(`make_slc_stack`) | `tau_dates`, `coherence_floor` |
| 타일 진값 | `tile_grid` 익스텐트 + 타일별 정수 2π 오프셋 주입(+잡음)(`make_tiled_truth`) | `rows`, `cols`, `overlap`, `max_offset_cycles` |

합성 3종(Phase 6 DoD): (a) 가우시안 침하 + 약한 대기, (b) 급경사 램프(고프린지),
(c) 강한 대기 + 저코히어런스 얼룩. 실험 YAML의 `data` 블록으로 지정한다.

## 3. 방법

- 대표위상(`repr_phase.METHODS`): `ml`(tophu 기준선, 저역통과 없음 — #62), `coh_weighted`(p),
  `shp`(KS 또는 t 검정, 창 ≤ 15, `centre`/`pixelwise` 모드), `phase_link`(EVD/EMI, 순차 미니스택),
  `filtered`(Goldstein α, 창, 오버랩). 세부와 참고문헌: ADR-0061, ADR-0062.
- 스티칭(`stitching.METHODS`): `coarse_ref`(tophu `round(mean(Δ)/2π)`), `overlap_consensus`
  (가중 최빈/중앙 + 가중 최소제곱, 게이지 타일 0). 세부: ADR-0063.

## 4. 지표 (`research.metrics`)

| 지표 | 정의 | 출처 |
|---|---|---|
| `phase_rmse_rad`, `phase_mae_rad` | `wrap(angle(est) − 진 저해상도 위상)`의 RMS / 평균 절댓값. 진 저해상도 위상 = 언래핑 진값의 블록 평균 | 이 모듈 |
| `unwrap_error_fraction` | 상수 오프셋 제거 후 `|err| ≥ π` 픽셀 비율 | `synth.unwrap_error_fraction` |
| `seam_boundaries_with_jump`, `seam_jump_pixels` | 병합 래스터의 코어 경계에서 `round(Δφ/2π) ≠ 0` | `unwrap.tiling.boundary_jumps`(ADR-0048) |
| `offsets_exact`, `n_wrong_offsets` | 주입 오프셋과의 일치(타일 0 기준 상대) | 이 모듈 |
| `closure_rms_rad` | `φ_ij + φ_jk − φ_ik`(MintPy 부호) RMS | `validate.closure`(ADR-0042) |
| 대조군 RMSE | `validate.metrics.compare` 훅 | R-10 |
| `wall_s`, `cpu_s`, `peak_rss_mb` | `ResourceTimer`(psutil RSS 표본 최대) | 규칙 11.8: JSON에만 |

## 5. 프로토콜

1. 시드 ≥ 3(합성) / 3회 반복 중앙값(실데이터, 플랜 §6.3). 표에는 평균 ± 표준편차와 n.
2. 방법 간 비교는 같은 데이터·같은 `factor`·같은 마스크에서만.
3. 결과는 `<name>.json`(모든 행 + 요약 + 환경) → `<name>.md`(표만). 산문 해석 금지.
4. 실데이터 사이트(S, 국내 1곳 대조군 포함)는 `data.kind: site`로 추가하되 벤치 프로토콜
   (`bench_result.json`)을 거친다.

## 6. 판정 규칙(제안 — 연구자 확인 필요)

- Q1: 합성 3종 **모두**에서 어떤 방법의 `phase_rmse_rad`가 `ml`보다 낮고, 실데이터 2 사이트에서
  스티칭 후 `seam_boundaries_with_jump`가 늘지 않으며, wall time이 `ml`의 10배 이내이면
  `unwrap` 스케줄러 옵션(`unwrap.repr_phase`)으로 승격. 기본값 변경은 별도 ADR.
- Q2: `overlap_consensus`가 `coarse_ref`와 같거나 나은 `n_wrong_offsets`를 모든 시드에서 보이고
  기준 위상 잡음(`ref_noise_std_rad`)에 대해 더 안정적이면 스케줄러의 재조립 옵션으로 승격.
- Q3: ADR-0064의 규칙(대조군 RMSE·폐합 RMS가 나빠지지 않을 때만 옵션 채택).

## 7. 연구자 확인 체크리스트

- [ ] 합성 3종의 파라미터 범위(변형 진폭, 대기 std, 코히어런스)가 현장 조건을 대표하는가
- [ ] 대표위상 품질 지표로 저해상도 진 위상 RMSE가 적절한가(대안: 언래핑 후 오류율만)
- [ ] SHP 검정(KS, α = 0.05)과 창 기본값(#61)
- [ ] 스티칭 판정 규칙의 허용 오차(단차 0개 요구 vs 비율)
- [ ] R-15 A/B의 미니스택 크기(10)·EMI 선택·동일 언래퍼 조건(ADR-0064)
