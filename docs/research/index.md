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

모든 명령은 `--json`(QGIS 파싱용 봉투)과 `--lang ko|en`을 따른다. 오류는 `RES-00x`
진단(원인 → 조치, `i18n/{ko,en}/research.yaml`)으로 나온다.

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
| `S_synth_repr_phase` | 대표위상 5종 (합성 SLC 스택, 3 시드) | 실행 가능 | [`results/S_synth_repr_phase.md`](results/S_synth_repr_phase.md) |
| `S_synth_stitching` | 스티칭 2종 (3×3 타일, 5 시드) | 실행 가능 | [`results/S_synth_stitching.md`](results/S_synth_stitching.md) |
| `S_synth_seq_estimator_ab` | R-15 dolphin vs MintPy A/B 프로토콜 | `requires: [dolphin, mintpy]` 미설치 → 건너뜀 | [`results/S_synth_seq_estimator_ab.md`](results/S_synth_seq_estimator_ab.md) |

결과 파일은 표만 담는다(규칙 11.8: 수치는 `<name>.json`이 근거). 해석과 기본값 변경 제안은
[experiment-design.md](experiment-design.md)의 판정 규칙을 따라 ADR에 쓴다. 실데이터 2 사이트
(Phase 6 DoD)는 `data.kind: site`로 같은 YAML 스키마를 쓰되 아직 미착수다.

## 미확정 사항

`docs/open-questions.md` #60(Okada 변형원), #61(SHP 기본값), #62(tophu 저역통과 기준선),
#63(dolphin·MintPy 설치 정책), #64(연결성분 단위 coarse_ref).
