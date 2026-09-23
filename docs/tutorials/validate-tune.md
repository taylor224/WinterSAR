# 튜토리얼 3 — 검증·튠 (`wintersar validate | refpoint | closure | sweep`)

SBAS 결과가 "맞는지"는 세 가지로 확인합니다: (1) 위상이 스스로 모순되지 않는가(폐합, `closure`), (2) 기준점이
안정한가(`refpoint`), (3) 독립 관측(수준측량·GNSS)과 맞는가(`validate`). 그 다음 `sweep` 으로 파라미터를 4~5회
돌려 고릅니다(R-11). 국내 사이트 실데이터 리포트는 아직 없으므로(Phase 4 DoD 미충족,
[릴리스 노트](../release-notes.md)) 이 문서의 출력 예시는 전부 **합성** 시계열에서 실제로 나온 모양입니다.
성능 수치는 `bench_result.json` 이 있을 때만 적습니다(규칙 11.8).

> 앞의 두 튜토리얼([HyP3](hyp3-quickstart.md), [ISCE2](isce2-local.md))이 `work/` 를 만들어 두었다고 가정합니다.
> 없으면 아래 0 절로 지금 바로 만들 수 있습니다.

## 0. 지금 바로 해보기 (합성)

```bash
wintersar init config.yaml                     # engine.interferogram / timeseries.engine 을 fake 로, validate: 절 삭제
printf '{"type":"FeatureCollection","features":[]}' > aoi.geojson
wintersar run --config config.yaml             # work/<stage>/<hash>/out/ 에 igrams.npz · unw.npz · timeseries.npz
mkdir -p data
cp <repo>/tests/fixtures/ground_truth/leveling_synth.csv data/leveling.csv
cp <repo>/tests/fixtures/ground_truth/gnss_synth.csv     data/gnss.csv
```

`<hash>` 는 `wintersar cache ls --config config.yaml` 표의 노드 해시입니다. fake 시계열은 픽스처 CSV 와 같은
좌표계(위도 37.59 부근, 경도 126.90 부근)에 놓여 있어 그대로 맞물립니다.

## 1. 대조군 CSV 준비 (R-10)

플랜 §5.6 스키마, UTF-8, 쉼표 구분, 헤더 필수:

```csv
site_id,lat,lon,elev_m,date,up_m,east_m,north_m,method,sigma_mm
BM-101,37.5942,126.9073,82.0,2024-01-03,0.0133,,,leveling,1.0
BM-101,37.5942,126.9073,82.0,2024-01-15,0.0097,,,leveling,1.0
CORS-A,37.5928,126.9054,78.0,2024-01-01,0.0512,0.0003,-0.0002,gnss,2.0
```

- 단위는 **미터**(10 m 를 넘는 값은 mm 로 간주해 `VAL-007` 로 거부). `sigma_mm` 만 mm.
- 수준측량은 `up_m` 만, GNSS 는 `east_m, north_m, up_m` 전부. 값은 누적 변위(임의 시점 기준). 비교 시 InSAR 와
  대조군 모두 **첫 공통 시점**을 0 으로 다시 맞추므로 상수 오프셋은 상관없습니다.
- `date` 는 `YYYY-MM-DD`(또는 `YYYYMMDD`).

### 국토지리정보원 자료를 CSV 로 바꾸기 ([ADR-0043](../adr/0043-ngii-ground-truth-access.md))

- 수준점 성과: 국토정보플랫폼(map.ngii.go.kr) 기준점 조회에서 점 번호·경위도·표고를 얻어 `site_id, lat, lon,
  elev_m` 에 넣고, 재측량 성과 차이를 `up_m`(m)로 적습니다. 시계열 성과의 공개 배포 경로는 확인되지
  않았습니다(기관 문의, open-questions #42).
- GNSS: 공공데이터포털 "상시관측소 RINEX 좌표 분석 데이터"(연간 GAMIT 좌표) 또는 RINEX 를 직접 후처리한 일 단위
  좌표를 ENU 변위(첫 날 기준)로 바꿔 `east_m, north_m, up_m` 에 넣습니다. 열 이름은 반드시 위 스키마로.

## 2. 검증 실행 (validate)

```bash
wintersar validate --ts work/timeseries/<hash>/out/timeseries.npz --leveling data/leveling.csv --out work/validate --radius 100
wintersar validate --ts work/timeseries/<hash>/out/timeseries.npz --leveling data/leveling.csv --gnss data/gnss.csv --heading -12 --incidence 39 --out work/validate-gnss
wintersar --lang en --json validate --ts work/timeseries/<hash>/out/timeseries.npz --leveling data/leveling.csv   # QGIS/스크립트용 JSON
```

옵션: `--radius`(지점 주변 평균 반경 m), `--method mean|median`, `--align nearest|interp`
(`--max-gap-days 6`), `--heading/--incidence`(시계열에 기하가 없을 때), `--no-plots`. 결과:
`validation_report.{md,html,json}`, `plots/site_*.png`. MintPy 산출물은 `--ts …/timeseries.h5` 로 줍니다.

예상 출력(모양):

```text
대조군 검증 — work/timeseries/<hash>/out/timeseries.npz
┃ 지점 ┃ 방법 ┃ n ┃ RMSE (mm) ┃ bias (mm) ┃ 상관 ┃ dv (mm/yr) ┃ 거리 (m) ┃
│ L01-center │ leveling │ … │ … │ … │ … │ … │ … │
│ L03-slope  │ leveling │ … │ … │ … │ … │ … │ … │
│ G01        │ gnss     │ … │ … │ … │ … │ … │ … │
RMSE … mm · bias … mm · 지점 <n>개 · 시점 <n>개 · 반경 <r> m
│ 경고 │ VAL-009 [L99-outside] │ 지점 … 는 가장 가까운 InSAR 픽셀에서 … 떨어져 있어 … │ 지점 좌표(lat/lon 순서, 기준계)를 확인하거나 AOI 를 넓히세요 … │
저장: work/validate/validation_report.md · .html · .json · plots
```

리포트 읽는 법:

- **bias 는 크고 RMSE 는 작다** → 기준점 오프셋(§3 에서 기준점 재선정 후 재기준화).
- **RMSE 가 크고 특정 시점에 계단** → 언래핑 오류(§4 폐합에서 의심 간섭도 확인).
- **속도 차이(dv)만 크다** → 대기 보정·deramp 문제(§5 sweep 에서 `timeseries.troposphere` 비교).
- LOS 부호 규약: 양수 = 위성 방향(융기가 양수). 수준측량은 `up × cos(입사각)` 으로 LOS 에 투영
  ([ADR-0040](../adr/0040-los-sign-convention.md)).

### GNSS 비교에는 heading 이 필요합니다

수준측량은 `up × cos(입사각)` 이라 heading 이 없어도 되지만, GNSS 는 동서·남북 성분까지 쓰므로 heading 을 모르면
부호가 뒤집힙니다(상승 −12° ↔ 하강 192° 에서 동쪽 성분의 부호가 반전). 그래서 heading 을 모르는 시계열은
**추측하지 않고** `VAL-008` 로 멈춥니다(리포트를 만들지 못했으므로 종료 코드 1, 봉투 `ok:false`):

```text
│ 실패 │ VAL-008 │ 지점 G01 를 LOS 로 투영할 수 없습니다: 시계열에 heading_deg 이(가) 없고 인자로도 주어지지 않았습니다. │ --heading/--incidence(도)를 주거나 기하 레이어가 있는 시계열(MintPy geometryGeo.h5 의 incidenceAngle, HEADING 속성)을 읽으세요. │
```

다음 중 하나로 알려 줍니다:

- `wintersar validate --heading <도>` (시계열 파일 값보다 우선),
- 시계열 파일 자체(MintPy `timeseries.h5` 의 `HEADING`, `io.formats.write_timeseries_npz` 의 `heading_deg`),
- `config.yaml` 의 `data.orbit_direction: asc|desc` — 파이프라인 `validate` 단계가 중위도 근사값(−12°/192°)을
  **보조값**으로만 채우고(파일 값이 있으면 그쪽이 우선) `VAL-017` 경고를 남깁니다. `auto` 면 채우지 않습니다.

## 3. 기준점 추천 (refpoint, R-09)

```bash
wintersar refpoint --ts work/timeseries/<hash>/out/timeseries.npz --aoi aoi.geojson --top 5 --out work/refpoint.json
wintersar refpoint --ts work/timeseries/<hash>/out/timeseries.npz --aoi aoi.geojson --top 5 --coherence work/avgSpatialCoh.npy --dem work/dem.npy --weights coherence=0.4,conncomp=0.3
```

점수 = 코히어런스·연결성분·표고 유사성·AOI 거리·속도 안정성의 가중합([ADR-0041](../adr/0041-refpoint-scoring-weights.md),
초기 가중치는 연구자 검수 대기 open-questions #40). 예상 출력(모양):

```text
기준점 추천 — work/timeseries/<hash>/out/timeseries.npz
시계열: 날짜 <n>개, <rows> x <cols> 픽셀
┃ 순위 ┃ 행 ┃ 열 ┃ 위도 ┃ 경도 ┃ 점수 ┃ 코히어런스 ┃ 성분 ┃
│ 1 │ … │ … │ … │ … │ … │ - │ dist=… velo=… │
적용: config.yaml의 timeseries.reference_point: [<lat>, <lon>]
저장: work/refpoint.json
```

코히어런스 파일을 주지 않으면 "코히어런스" 열은 `-` 이고 점수는 거리·속도 성분만으로 계산됩니다. 표의 1위를
`config.yaml` 에 기록하고 시계열 단계부터 다시 실행합니다:

```yaml
timeseries:
  reference_point: [37.59321, 126.90544]
```

```bash
wintersar run --config config.yaml --from timeseries
```

MintPy 자동 규칙(코히어런스 임계 이상 픽셀 중 **무작위**, `--mintpy-threshold`)과의 비교가 같이 출력됩니다.

## 4. 위상 폐합 (closure, §12.1)

```bash
wintersar closure --igrams work/multilook/<hash>/out/igrams.npz --unw work/unwrap/<hash>/out/unw.npz --out work/closure
wintersar closure --igrams work/multilook/<hash>/out/igrams.npz --wrapped --top 10 --out work/closure-wrapped
```

예상 출력(모양):

```text
위상 폐합 통계 — work/multilook/<hash>/out/igrams.npz
unw 폐합: 간섭도 <n>개, 삼각형 <n>개, 픽셀별 RMS 평균 … rad, 의심 간섭도: <없음|목록>
┃ 순위 ┃ 간섭도 ┃ 점수 ┃ 삼각형 수 ┃ 의심 ┃
│ 1 │ 20240101_20240113 │ … │ … │ 아니오 │
저장: work/closure/closure_dashboard.json
저장: work/closure/closure_maps.npz
```

unw 모드에서 정수 2π 배수 ≠ 0 인 픽셀 비율이 큰 간섭도가 의심 대상([ADR-0042](../adr/0042-closure-metric-definitions.md)).
`closure_dashboard.json` 은 QGIS 플러그인이 읽습니다. 의심 간섭도는 네트워크에서 제외하거나
`timeseries.unwrap_error_correction: phase_closure` 로 보정합니다. fake 엔진의 합성 위상은 폐합이 정확히 0 이므로
의심 간섭도가 없는 것이 정상입니다.

## 5. 파라미터 스윕 (sweep, R-08, R-11)

`sweep.yaml`:

```yaml
grid:
  unwrap.coherence_threshold: [0.3, 0.4]
  engine.filter.alpha: [0.4, 0.6]
  timeseries.troposphere: [era5, none]
objectives: [gt_rmse, closure_rms, wall_time_s]
```

```bash
wintersar sweep --config config.yaml --grid sweep.yaml --leveling data/leveling.csv --out work/sweep
wintersar sweep --config config.yaml --grid sweep.yaml --out work/sweep-noref     # 대조군 없이: gt_rmse 는 VAL-018 로 제외
```

8개 조합 중 첫 조합만 전체 파이프라인을 돌리고, 나머지는 바뀐 섹션의 단계부터만 재실행됩니다(DAG 캐시, PERF-03).
`sweep.md` 표에서 `*` 가 Pareto 전선, `sweep_pareto.png` 가 산점도. 지표 정의는 [ADR-0044](../adr/0044-sweep-design.md).

예상 출력(모양):

```text
파라미터 스윕 — 파라미터 조합 8개
[1/8] {"unwrap.coherence_threshold": 0.3, "engine.filter.alpha": 0.4, "timeseries.troposphere": "era5"}
…
| # | 파라미터 | 폐합 RMS (rad) | 시간적 코히어런스 | 잔차 RMS (mm) | 대조군 RMSE (mm) | 실행 시간 (s) | 캐시 적중 | Pareto | 상태 |
| 0 | … | … | … | … | … | … | <n> | * | 성공 |
gt_rmse (min), closure_rms (min), wall_time_s (min) 기준 Pareto 전선(max 표시가 없으면 작을수록 좋음).
스윕 완료: 8개 중 8개 성공, Pareto 전선 <n>개
저장: work/sweep/sweep.json · sweep.md · sweep_pareto.png
```

대조군(`--leveling/--gnss` 또는 `validate.leveling_csv`) 없이 돌리면 기본 목표에 들어 있는 `gt_rmse` 를 잴 수
없습니다. 이때는 그 목표를 순위 계산에서 빼고 `VAL-018`(정보)로 알린 뒤 남은 목표(`closure_rms`, `wall_time_s`)로
전선을 고릅니다 — 표가 통째로 비지 않습니다.

권장 반복(4~5회): ① 폐합으로 언래핑 임계 결정 → ② 기준점 확정 → ③ 대기 보정 비교 → ④ 필터/looks 미세 조정 →
⑤ 최종 검증 리포트.

## 6. 파이프라인 안에서 자동 실행

`config.yaml` 에 `validate.leveling_csv`(및 `validate.gnss.path`)가 있으면 `wintersar run` 의 마지막 `validate`
단계가 자동으로 리포트를 만들고 `VAL-013` 요약을 findings 에 남깁니다. 없으면 단계는 건너뜁니다(`PIPELINE-010`).

```text
│ validate │ - │ - │ 실행됨 │ <hash> │ … │
│ 정보 │ VAL-013 │ 검증 요약: <n>개 지점, 정렬된 시점 <n>개(반경 … m)에서 RMSE … mm, bias … mm. │ 리포트의 지점별 표를 보세요. RMSE 는 작은데 bias 가 크면 기준점 오프셋('wintersar refpoint' 로 재기준화), RMSE 가 크면 언래핑·대기 오차(폐합 확인 후 sweep 시도)입니다. │
```

## 진단 코드

| 코드 | 뜻 |
|---|---|
| VAL-001~007 | CSV 스키마·값·단위 오류 |
| VAL-008 | 시계열에 입사각/헤딩 없음 |
| VAL-009 | 지점이 격자 밖 |
| VAL-010 | 정렬된 시점 부족 |
| VAL-011/012 | 시계열 형식/산출물 없음 |
| VAL-013 | 검증 요약(정보) |
| VAL-014~016 | 대조군 미지정, 스윕 그리드 오류, 스윕 조합 실패 |
| VAL-017 | GNSS 를 실측이 아닌 기본 heading 으로 투영(경고) |
| VAL-018 | 잴 수 없는 목표를 스윕 순위에서 제외(정보) |

## English summary

Three checks tell whether an SBAS result is right: internal consistency (`wintersar closure`, loop-closure
RMS per triplet and suspicious interferograms, ADR-0042), reference-point stability (`wintersar refpoint`,
weighted score of coherence, connected component, elevation, AOI distance and velocity stability, ADR-0041;
paste the printed "적용" line into `timeseries.reference_point` and `run --from timeseries`) and agreement
with independent observations (`wintersar validate --ts … --leveling … [--gnss …]`, CSV schema of plan §5.6 in
metres, LOS projection with the sign convention of ADR-0040; GNSS needs a heading, otherwise `VAL-008`
stops instead of guessing). Then `wintersar sweep --config … --grid sweep.yaml` runs a parameter grid
through the cached DAG (only the stages after a changed section re-run, PERF-03) and reports a Pareto front
over `gt_rmse`, `closure_rms`, `wall_time_s`; without ground truth `gt_rmse` is dropped with `VAL-018`.
Everything in this page runs today on the synthetic fake-engine work directory plus the fixture CSVs under
`tests/fixtures/ground_truth`; no real Korean site report exists yet (Phase 4 DoD not met, see the release
notes) and console excerpts show shapes, not numbers.
