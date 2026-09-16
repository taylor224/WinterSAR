# ADR-0060: Phase 6 연구 실험 설계 — 합성 진값, 지표, 판정 규칙 (도메인 검수 체크포인트)

- 상태(Status): 제안 (연구자 확인 대기 — 규칙 11.10)
- 날짜(Date): 2026-09-16
- 관련 ID: R-07, R-06, R-15, PERF-04, PERF-05
- 검증 출처(Sources):
  - 플랜 §5.7(research 모듈), §7 Phase 6 DoD("합성 3종 + 실데이터 2 사이트에서 방법별 지표 표.
    기준선(복소 멀티룩 + coarse_ref)보다 나은 조합이 있으면 unwrap 스케줄러 옵션으로 승격"),
    §6.3 벤치 프로토콜, §12.1 "언래핑 타일·다중해상도"
  - tophu v0.2.1 `src/tophu/_multiscale.py`(coarse_unwrap: `multilook(igram, downsample_factor)`,
    `adjust_conncomp_offset_cycles`) — https://raw.githubusercontent.com/isce-framework/tophu/v0.2.1/src/tophu/_multiscale.py
  - tophu v0.2.1 `src/tophu/_multilook.py`(잘라낸 뒤 `da.coarsen(np.mean, ...)`) —
    https://raw.githubusercontent.com/isce-framework/tophu/v0.2.1/src/tophu/_multilook.py
  - 구현: `src/wintersar/research/{synth,repr_phase,stitching,metrics,experiments}.py`,
    `src/wintersar/research/experiments/*.yaml`, 설계 문서 `docs/research/experiment-design.md`,
    테스트 `tests/unit/research/`
  - 높이-위상 계수: Hanssen 2001 *Radar Interferometry* 평탄지구 근사 `φ_topo = -4π/λ · B⊥/(R sin θ) · h`
    (부호는 `synth.PHASE_PER_M_LOS`와 동일 규약)

## 맥락 (Context)

다중해상도 언래핑은 저해상도 "대표위상"의 품질과 타일 2π 오프셋 결정 규칙이 결과를 좌우한다
(플랜 §12.1). 연구자의 문제의식(R-07: "산술평균이 아닌 지역을 가장 잘 설명하는 위상")을
검증하려면 (1) 진값이 있는 합성 데이터, (2) 방법을 바꿔 끼울 수 있는 플러그인, (3) 공통 지표,
(4) 재현 가능한 실험 정의가 필요하다. 기본값을 바꾸는 결정은 연구자 검수 후로 미룬다.

## 선택지 (Options)

1. 실데이터에서 바로 tophu/snaphu 옵션을 바꿔가며 비교 — 진값이 없어 오류를 정의할 수 없다.
2. **합성 진값 + 플러그인 + YAML 실험(채택)** — 오류율을 정확히 정의하고 시드 반복이 가능하다.
   실데이터는 폐합 잔차·경계 단차·대조군 RMSE로 같은 표에 합류한다.
3. 외부 프레임워크(dolphin 워크플로 설정 스윕) 사용 — dolphin은 미설치이며 tophu 대표위상
   단계를 바꿀 수 없다.

## 결정 (Decision)

선택지 2. 구성 요소와 정의:

- **합성 진값**(`synth.py`, Phase 0 API 유지): 변형원 합(`deformation_sources`; Okada는
  `NotImplementedError` 훅, 오픈 항목 #60), DEM 오차 `dh` × `-4π/λ · B⊥/(R sin θ)`(**B⊥에 비례**,
  `R = H/cos θ`, `H = 693 km`, `θ = 39°` IW 명목값), 영역별 Rayleigh 진폭 스택(`shp_amplitude_stack`),
  능선 레이오버(`layover_mask_from_ridge`: ADR-0017 규약 "센서를 향한 사면 = 내리막이 센서 쪽",
  좌측 센서에서 `dz/dx > 0`, 경사 > 입사각), 타일 진값(`make_tiled_truth`: `tile_grid` 익스텐트 +
  정수 오프셋 주입, 타일 0 = 게이지 0), 공분산이 알려진 SLC 스택(`make_slc_stack`:
  `s = diag(e^{jφ})·chol(Γ)·w`, `w ~ CN(0, I)`; Ansari 2018 §II의 분산 산란체 모델).
- **기준선**: 대표위상 `ml` = tophu `do_lowpass_filter=False` 경로의 블록 평균(잘라낸 뒤 비중첩
  평균, `multilook` 검증 완료). tophu 기본값의 equiripple 저역통과는 재현하지 않았다(#62).
  스티칭 기준선 `coarse_ref` = tophu `round(mean(hires − lores)/2π)`(ADR-0063).
- **지표**(`metrics.py`): 저해상도 진 위상(언래핑 진값의 블록 평균) 대비 `wrap` 차이의 RMS/MAE,
  언래핑 오류 픽셀 비율(`synth.unwrap_error_fraction`), 경계 단차(`unwrap.tiling.boundary_jumps`
  seam 모드, ADR-0048), 오프셋 일치(타일 0 상대), 폐합 RMS(`validate.closure`, MintPy 부호),
  대조군 RMSE 훅(`validate.metrics.compare`), `ResourceTimer`(psutil RSS 표본 최대·wall·CPU).
- **실험 정의**(`experiments.py`): YAML `name/kind/requires/factor/seeds/data/methods/metrics/protocol`
  → `<name>.json`(행·요약·환경) + `<name>.md`(표만, 규칙 11.8). `requires` 미설치 → 상태 `skipped`
  와 `RES-002`. 내장 3종: `S_synth_repr_phase`, `S_synth_stitching`, `S_synth_seq_estimator_ab`.
- **판정 규칙(제안)**: `docs/research/experiment-design.md` §6 — 합성 3종 모두에서 기준선보다
  낮은 RMSE + 실데이터 2 사이트에서 단차 비증가 + wall time 10배 이내 → `unwrap` 옵션 승격,
  기본값 변경은 별도 ADR.

## 결과 (Consequences)

- 이 ADR이 "채택"으로 바뀌기 전에는 `unwrap` 스케줄러 기본값을 바꾸지 않는다. 연구자 확인
  항목은 experiment-design.md §7 체크리스트.
- 실데이터 사이트(`data.kind: site`)와 합성 (b)(c) 유형 YAML은 후속 작업이다(Phase 6 DoD).
- 수치는 `docs/research/results/<name>.json`에만 근거를 두고, 문서 산문에는 쓰지 않는다.
- 오픈 항목 #60(Okada), #62(tophu 저역통과 기준선).
