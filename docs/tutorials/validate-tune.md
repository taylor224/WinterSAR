# 검증·튠 튜토리얼 (초안) — `wintersar validate | refpoint | closure | sweep`

> 상태: 초안(Phase 4). 국내 사이트 실데이터 리포트가 나오면 예시 수치·그림을 채운다. 성능 수치는 `bench_result.json`이
> 있을 때만 적는다(규칙 11.8).

SBAS 결과가 "맞는지"는 세 가지로 확인한다: (1) 위상이 스스로 모순되지 않는가(폐합, `closure`), (2) 기준점이 안정한가
(`refpoint`), (3) 독립 관측(수준측량·GNSS)과 맞는가(`validate`). 그 다음 `sweep`으로 파라미터를 4~5회 돌려 고른다(R-11).

## 1. 대조군 CSV 준비 (R-10)

플랜 §5.6 스키마, UTF-8, 쉼표 구분, 헤더 필수:

```csv
site_id,lat,lon,elev_m,date,up_m,east_m,north_m,method,sigma_mm
BM-101,37.5942,126.9073,82.0,2024-01-03,0.0133,,,leveling,1.0
BM-101,37.5942,126.9073,82.0,2024-01-15,0.0097,,,leveling,1.0
CORS-A,37.5928,126.9054,78.0,2024-01-01,0.0512,0.0003,-0.0002,gnss,2.0
```

- 단위는 **미터**(10 m를 넘는 값은 mm로 간주해 `VAL-007`로 거부). `sigma_mm`만 mm.
- 수준측량은 `up_m`만, GNSS는 `east_m, north_m, up_m` 전부. 값은 누적 변위(임의 시점 기준). 비교 시 InSAR와 대조군
  모두 **첫 공통 시점**을 0으로 다시 맞추므로 상수 오프셋은 상관없다.
- `date`는 `YYYY-MM-DD`(또는 `YYYYMMDD`).

### 국토지리정보원 자료를 CSV로 바꾸기 (ADR-0043)

- 수준점 성과: 국토정보플랫폼(map.ngii.go.kr) 기준점 조회에서 점 번호·경위도·표고를 얻어 `site_id, lat, lon, elev_m`에 넣고,
  재측량 성과 차이를 `up_m`(m)로 적는다. 시계열 성과의 공개 배포 경로는 확인되지 않았다(기관 문의).
- GNSS: 공공데이터포털 "상시관측소 RINEX 좌표 분석 데이터"(연간 GAMIT 좌표) 또는 RINEX를 직접 후처리한 일 단위 좌표를
  ENU 변위(첫 날 기준)로 바꿔 `east_m, north_m, up_m`에 넣는다. 열 이름은 반드시 위 스키마로.

## 2. 검증 실행

```bash
wintersar validate --ts work/timeseries/<hash>/timeseries.npz --leveling data/leveling.csv \
    --gnss data/gnss.csv --out work/validate --radius 100
wintersar --lang en --json validate --ts ... --leveling ...   # QGIS/스크립트용 JSON
```

옵션: `--radius`(지점 주변 평균 반경, 기본 100 m), `--method mean|median`, `--align nearest|interp`
(`--max-gap-days 6`), `--heading/--incidence`(시계열에 기하가 없을 때). 결과: `validation_report.{md,html,json}`,
`plots/site_*.png`.

리포트 읽는 법:
- **bias는 크고 RMSE는 작다** → 기준점 오프셋(§3에서 기준점 재선정 후 재기준화).
- **RMSE가 크고 특정 시점에 계단** → 언래핑 오류(§4 폐합에서 의심 간섭도 확인).
- **속도 차이(dv)만 크다** → 대기 보정·deramp 문제(§5 sweep에서 `timeseries.troposphere` 비교).
- LOS 부호 규약: 양수 = 위성 방향(융기가 양수). 수준측량은 `up × cos(입사각)`으로 LOS에 투영(ADR-0040).

### GNSS 비교에는 heading이 필요하다

수준측량은 `up × cos(입사각)`이라 heading이 없어도 되지만, GNSS는 동서·남북 성분까지 쓰므로 heading을 모르면 부호가
뒤집힌다(상승 -12° ↔ 하강 192°에서 동쪽 성분이 −0.616 ↔ +0.616으로 반전). 그래서 heading을 모르는 시계열은
**추측하지 않고** `VAL-008`로 멈춘다. 다음 중 하나로 알려준다:

- `wintersar validate --heading <도>`(시계열 파일 값보다 우선),
- 시계열 파일 자체(MintPy `timeseries.h5`의 `HEADING`, `io.formats.write_timeseries_npz`의 `heading_deg`),
- `config.yaml`의 `data.orbit_direction: asc|desc` — 파이프라인 `validate` 단계가 중위도 근사값(−12°/192°)을
  **보조값**으로만 채우고(파일 값이 있으면 그쪽이 우선) `VAL-017` 경고를 남긴다. `auto`면 채우지 않는다.

## 3. 기준점 추천 (R-09)

```bash
wintersar refpoint --ts work/timeseries/<hash>/timeseries.npz --aoi aoi.geojson --top 5 \
    --coherence work/avgSpatialCoh.npy --dem work/dem.npy --out work/refpoint.json
```

점수 = 코히어런스·연결성분·표고 유사성·AOI 거리·속도 안정성의 가중합(ADR-0041, 가중치는 `--weights coherence=0.4,...`로
조정). 표의 1위를 `config.yaml`에 기록하고 시계열 단계부터 다시 실행한다:

```yaml
timeseries:
  reference_point: [37.59321, 126.90544]
```

MintPy 자동 규칙(코히어런스 ≥ 0.85인 픽셀 중 **무작위**)과의 비교가 같이 출력된다.

## 4. 위상 폐합 (§12.1)

```bash
wintersar closure --igrams work/multilook/<hash>/igrams.npz --unw work/unwrap/<hash>/unw.npz --out work/closure
```

unw 모드에서 정수 2π 배수 ≠ 0인 픽셀 비율이 큰 간섭도가 의심 대상(ADR-0042). `closure_dashboard.json`은 QGIS 플러그인이
읽는다. 의심 간섭도는 네트워크에서 제외하거나 `timeseries.unwrap_error_correction: phase_closure`로 보정한다.

## 5. 파라미터 스윕 (R-08, R-11)

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
```

8개 조합 중 첫 조합만 전체 파이프라인을 돌리고, 나머지는 바뀐 섹션의 단계부터만 재실행된다(DAG 캐시, PERF-03).
`sweep.md` 표에서 `*`가 Pareto 전선, `sweep_pareto.png`가 산점도. 지표 정의는 ADR-0044.

대조군(`--leveling/--gnss` 또는 `validate.leveling_csv`) 없이 돌리면 기본 목표에 들어 있는 `gt_rmse`를 잴 수 없다.
이때는 그 목표를 순위 계산에서 빼고 `VAL-018`(정보)로 알린 뒤 남은 목표(`closure_rms`, `wall_time_s`)로 전선을
고른다 — 표가 통째로 비지 않는다.

권장 반복(4~5회): ① 폐합으로 언래핑 임계 결정 → ② 기준점 확정 → ③ 대기 보정 비교 → ④ 필터/looks 미세 조정 → ⑤ 최종 검증 리포트.

## 6. 파이프라인 안에서 자동 실행

`config.yaml`에 `validate.leveling_csv`(및 `validate.gnss.path`)가 있으면 `wintersar run`의 마지막 `validate` 단계가
자동으로 리포트를 만들고 `VAL-013` 요약을 findings에 남긴다. 없으면 단계는 건너뛴다(`PIPELINE-010`).

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
| VAL-017 | GNSS를 실측이 아닌 기본 heading으로 투영(경고) |
| VAL-018 | 잴 수 없는 목표를 스윕 순위에서 제외(정보) |
