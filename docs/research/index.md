# research 모듈 — 대표위상 · 타일 스티칭 · A/B 실험 (R-07, R-15, PERF-05)

플랜 §5.7 / Phase 6. 다중해상도 언래핑(tophu, SARscape decomposition level)은 저해상도
"대표위상"을 먼저 언래핑하고 타일마다 2π 사이클을 가감한다. 이 모듈은 그 두 단계를
플러그인으로 만들어 합성 진(true) 데이터에서 비교한다. 결론(기본값 변경)은 연구자 검수
후 ADR로만 바꾼다(규칙 11.10, ADR-0060).

| 파일 | 역할 | ADR |
|---|---|---|
| `research/synth.py` | 합성 간섭도·스택·SLC 스택·타일 진값(변형원 합, DEM 오차 ∝ Bperp, SHP 진폭 영역, 능선 레이오버) | ADR-0060 |
| `research/repr_phase.py` | 대표위상 5종: `ml`, `coh_weighted`, `shp`, `phase_link`, `filtered` — `representative_phase(method, igram, coh, factor, stack=None, **kw)` | ADR-0061, ADR-0062 |
| `research/stitching.py` | 타일 2π 오프셋: `coarse_ref`(tophu 방식), `overlap_consensus`(오버랩 합의 + 타일 그래프 최소제곱) — `stitch(tiles, shape, method, lowres_ref, coh, weights)` | ADR-0063 |
| `research/metrics.py` | 언래핑 오류 픽셀 비율, 타일 경계 단차 수, 폐합 잔차 RMS, 대조군 RMSE 훅, 실행 시간·메모리(`ResourceTimer`) | ADR-0060 |
| `research/experiments.py` + `experiments/*.yaml` | YAML 실험 → `<name>.json` + `<name>.md`(표만) | ADR-0060, ADR-0064 |
| `research/cli.py` | `wintersar research repr-phase / stitch / synth / experiment / experiments` | — |

## 사용법

```bash
# 합성 데이터
wintersar research synth --kind igrams --out igrams.npz --n-dates 8 --shape 128 128
# --noise-model crlb(기본, 크라메르-라오 하한) | exact(실제 위상 분포; looks=1에서 ~2배 잡음)
wintersar research synth --kind slc    --out slc.npz    --n-dates 12 --shape 128 128
wintersar research synth --kind tiles  --out tiles.npz  --rows 3 --cols 3 --overlap 16

# 대표위상 (저해상도 복소 배열 저장)
wintersar research repr-phase --igram igrams.npz --method ml --factor 3 --out repr.npz
wintersar research repr-phase --igram igrams.npz --stack slc.npz --method phase_link \
    --factor 3 --index 1 -p link_method=emi --out repr.npz

# 타일 스티칭
wintersar research stitch --tiles tiles.npz --method overlap_consensus --out merged.npz
wintersar research stitch --tiles tiles.npz --method coarse_ref --out merged.npz   # tiles.npz 안의 lowres_ref 사용

# 실험 (내장 이름 또는 YAML 경로)
wintersar research experiments
wintersar research experiment S_synth_repr_phase --out results/ --docs-dir docs/research/results
```

모든 명령은 `--json`(QGIS 파싱용 봉투)과 `--lang ko|en`을 따른다. 오류는 `RES-0xx`
진단(원인 → 조치, `i18n/{ko,en}/research.yaml`)으로 나온다. `shp`/`phase_link`의 `--stack`은
간섭도와 같은 원해상도 격자여야 하고(다르면 `RES-009`), 없는 실험 이름은 내장 목록과 함께
`RES-010`으로 나온다.

## 데이터 규약

- 대표위상 결과: `complex128 (ny // factor, nx // factor)`. 각도 = 대표위상, 크기 = 품질
  (`coh_weighted`는 가중 페이저 코히어런스, `phase_link`는 시간 코히어런스, `ml`/`filtered`는
  단순 멀티룩 크기). 입력은 끝에서부터 `factor` 배수로 잘린다(tophu `multilook`과 동일).
- 쌍 `(i, j)`의 위상은 **부(j) − 참조(i)** (`synth.make_stack`의 `disp[j] − disp[i]`,
  `SynthSlcStack.pair_phase_true`). 마스크 픽셀은 호출자가 0으로 만든다(tophu/dolphin 규약).
- 타일: `(unw_tile, slice_y, slice_x)` 목록(익스텐트 좌표). `tiles.npz` 형식은
  `stitching.save_tiles_npz` 참조. 오프셋 부호: `offsets_cycles[i]`는 타일 `i`에서 **뺀** 사이클 수.
- 경계 단차 지표는 `wintersar.unwrap.tiling.boundary_jumps`(ADR-0048)와 공용이다.

## 실험과 결과

| 실험 | 종류 | 상태 | 결과 |
|---|---|---|---|
| `S_synth_repr_phase` | 합성 (a) — 대표위상 7종 (합성 SLC 스택, 3 시드) | 실행 가능 | [`results/S_synth_repr_phase.md`](results/S_synth_repr_phase.md) |
| `S_synth_steep_ramp` | 합성 (b) — 급경사 램프(3.8 px당 1 프린지, 16 룩) 대표위상 5종 | 실행 가능 | [`results/S_synth_steep_ramp.md`](results/S_synth_steep_ramp.md) |
| `S_synth_strong_atmosphere` | 합성 (c) — 강한 대류권 지연(3 rad) + 단일 룩 정확 잡음 대표위상 4종 | 실행 가능 | [`results/S_synth_strong_atmosphere.md`](results/S_synth_strong_atmosphere.md) |
| `S_synth_stitching` | 스티칭 2종 (3×3 타일, 5 시드) | 실행 가능 | [`results/S_synth_stitching.md`](results/S_synth_stitching.md) |
| `S_synth_seq_estimator_ab` | R-15 dolphin vs MintPy A/B 프로토콜 | `requires: [dolphin, mintpy]` 미설치 → 건너뜀 | [`results/S_synth_seq_estimator_ab.md`](results/S_synth_seq_estimator_ab.md) |

결과 파일은 표만 담는다(규칙 11.8: 수치는 `<name>.json`이 근거). 해석과 기본값 변경 제안은
[experiment-design.md](experiment-design.md)의 판정 규칙을 따라 ADR에 쓴다.

### Phase 6 DoD 진행 상황

- **합성 3종** — (a) `S_synth_repr_phase`, (b) `S_synth_steep_ramp`, (c)
  `S_synth_strong_atmosphere` 로 충족. (b)는 `deformation_kind: linear` +
  `deformation_ramp`, (c)는 `noise_model: exact`(단일 룩 정확 위상 분포)를 쓴다.
- **실데이터 2 사이트** — 미착수. `data.kind: site`(같은 YAML 스키마)와
  `experiments.py`의 site 러너가 아직 없고, ADR-0060이 후속 작업으로 남겨 둔 항목이다.
  ADR-0060의 승격 규칙(합성 3종 + 실데이터 2 사이트)은 그때까지 평가할 수 없다.
- **실행 시간·메모리** — 실험 JSON에는 `wall_s`/`cpu_s`/`peak_rss_mb`가 들어가지만 렌더된
  표에서는 제외한다(`experiments.PERF_METRICS`). 규칙 11.8이 요구하는 `bench_result.json`
  스키마(`stages`/`total`/`machine`/`git_sha`)를 실험 러너가 만들지 않기 때문이다.
- **GPU(PERF-10)** — `wintersar.compute.{xp,kernels}`의 numpy/cupy 스위치와 커널별 CPU 기준
  허용오차 테스트는 있으나(ADR-0053), 적용 계층은 CUDA 장비에서의 Phase 5 전후 측정
  (§6.3, 규칙 11.8)이 나오기 전까지 보류다. 파이프라인의 멀티룩·Goldstein 필터는 ISCE2
  내부(`engine.interferogram` 스테이지, `--useGPU`)에서 돌아가므로 research 모듈의 numpy
  커널이 `compute.kernels`의 중복이 아니며, 둘을 바꿔 끼우면 결과가 달라진다
  (`repr_filtered`가 의존하는 크기 보존 계약, ADR-0061의 후속 항목).

## 미확정 사항

`docs/open-questions.md` #50(Okada 변형원), #51(SHP 기본값), #52(tophu 저역통과 기준선),
#53(dolphin·MintPy 설치 정책), #54(연결성분 단위 coarse_ref).
