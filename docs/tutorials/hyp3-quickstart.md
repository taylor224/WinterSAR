# 튜토리얼 1 — HyP3 빠른 시작 (초안, Phase 8 에서 확정)

목표(G1): SARscape 없이 **ASF HyP3 On-Demand** 로 burst 간섭도를 만들고 MintPy 로 SBAS 시계열까지
산출합니다. 로컬에는 ISCE2·SNAPHU 가 필요 없습니다 — 정합·간섭도·언래핑은 HyP3 클라우드가 수행하고,
wintersar 는 선별·제출·다운로드·MintPy 입력 구성·시계열·검증을 맡습니다.

> 이 문서의 명령은 현재 트리의 `src/wintersar/cli.py` 와 각 모듈 `cli.py` 에 있는 것만 씁니다. 처리 시간·
> 크레딧 소모량 같은 수치는 `bench_result.json` 이 있을 때만 적습니다(규칙 11.8).

## 0. 준비물

| 항목 | 확인 |
|---|---|
| NASA Earthdata Login 계정 | https://urs.earthdata.nasa.gov — HyP3 사용 신청이 승인되어 있어야 합니다(거부되면 `KB-HYP3-004`) |
| 인증 정보 | `~/.netrc` 에 `machine urs.earthdata.nasa.gov login <id> password <pw>`(우선) 또는 환경변수 `EARTHDATA_TOKEN`(ADR-0013) |
| hyp3-sdk | `pip install 'wintersar[hyp3]'` (hyp3-sdk ≥ 7, BSD-3-Clause; 의존성 정책 예외는 open-questions #10) |
| MintPy | `conda install -c conda-forge mintpy` (GPL-3 → subprocess 로만 호출, 절대 import 하지 않음) |
| AOI | GeoJSON 폴리곤(EPSG:4326). QGIS 플러그인의 "캔버스 범위로/활성 레이어로" 버튼으로도 만들 수 있음 |

```bash
wintersar check-install            # hyp3 / mintpy 가 '사용 가능' 인지, ENV-003(인증) 이 없는지 확인
```

`ENV-001` 이 나오면 조치 문구의 설치 명령을 따르세요. `ENV-004`(GDAL/PROJ 불일치)는 같은 환경에서
rasterio·pyproj 를 함께 재설치합니다.

## 1. 설정 파일

```bash
wintersar init config.yaml
```

`config.yaml` 에서 확인할 값(플랜 §4.4):

```yaml
project: { name: site-a-subsidence, workdir: ./work, language: ko }
aoi: aoi.geojson
time_range: { start: 2023-01-01, end: 2025-12-31 }
data:
  source: asf
  product: burst              # burst 우선 (PERF-01); 없으면 SLC fallback
  polarization: VV
  orbit_direction: auto       # asc | desc | auto → 방향별 스택 후보를 모두 만듭니다
  relative_orbit: auto        # 트랙을 알면 번호를 적으세요 (SEL-01)
  credentials: env:EARTHDATA_TOKEN   # 또는 netrc
selection:
  network: sbas
  max_perp_baseline_m: 150
  max_temporal_baseline_days: 48
  min_coverage: 0.95
  budget_credits: 2000        # (선택) 넘으면 SEL-13 WARN
engine:
  interferogram: hyp3
  looks: auto                 # HyP3 가 제공하는 looks 옵션 범위 안에서 스냅 (ADR-0020)
timeseries:
  engine: mintpy
  reference_point: auto_recommend
  troposphere: era5
validate:
  leveling_csv: data/leveling.csv   # 대조군이 없으면 이 절을 지우세요 (PIPELINE-010 으로 건너뜀)
```

## 2. 검색과 사전검증

```bash
wintersar search   --config config.yaml
#  → work/select/candidates.json  (검색은 무인증; 인증이 없으면 다운로드 단계를 위해 WARN 만 냅니다)
wintersar precheck work/select/candidates.json --config config.yaml
#  → work/select/precheck_report.{md,html,json}; FAIL 이 있으면 종료 코드 1 (--no-fail 로 무시)
```

리포트에서 볼 것:

- 후보 스택 표: 트랙(`T052D_VV` 형식)·방향·편파·날짜 수·커버리지·기선 분포, 추천 스택(`*`).
- Finding(원인 → 조치): `SEL-01`(트랙 혼합) `SEL-04`(공통 burst/커버리지) `SEL-06/07`(기선·계절)
  `SEL-09`(픽셀 간격·자동 looks) `SEL-12`(레이오버·셰도우 비율 — 상승/하강 중 유리한 방향 추천)
  `SEL-13`(크레딧 예산). 개념은 [concepts](../concepts/index.md).
- 추천 스택이 마음에 들면 `data.relative_orbit` 과 `data.orbit_direction` 을 그 값으로 고정합니다.

## 3. 계획(견적)과 실행

```bash
wintersar plan --config config.yaml
```

`plan` 은 단계 표(캐시됨/실행 예정), 예상 리소스와 **HyP3 크레딧 견적**을 보여 줍니다. 크레딧 단가는
`src/wintersar/engines/hyp3_costs.yaml`(출처: HyP3 credits 문서, 무료 월 8,000 크레딧)에서만 읽으며
코드에 하드코딩하지 않습니다. 견적이 `selection.budget_credits` 를 넘으면 `SEL-13` WARN 입니다.

```bash
wintersar run --config config.yaml
```

HyP3 경로의 단계(ADR-0020): `interferogram` 단계 하나가 burst 쌍 작업(`INSAR_ISCE_BURST` /
`INSAR_ISCE_MULTI_BURST`)을 제출 → 폴링 → 다운로드 → 산출물 검증 → MintPy `prep_hyp3` 호환 디렉터리 구성까지
수행하며 언래핑 결과(`unw`)도 함께 내므로 로컬 `unwrap` 단계는 건너뜁니다. 그 뒤 `timeseries → corrections →
geocode` 는 MintPy 어댑터가 `smallbaselineApp.py --dostep` 으로 단계별 실행합니다(템플릿은 머신 스펙에 맞춰
`compute.cluster/numWorker/maxMemory` 를 자동 설정, PERF-09, ADR-0021).

실행 중단·실패 시:

```bash
wintersar diagnose work/logs/ --engine hyp3      # KB-HYP3-001(크레딧) / 002(입력 검증) / 003(DEM) / 004(접근 권한)
wintersar run --config config.yaml --from timeseries   # 상류는 캐시에서, 시계열부터 재개
```

`run` 은 실패하면 자동으로 `diagnose` 를 붙여 Finding 을 표에 함께 보여 줍니다(ADR-0033).

## 4. 반복 튠 (PERF-03)

```bash
wintersar run --config config.yaml --set timeseries.coherence_threshold=0.6   # 시계열 이후만 재실행
wintersar run --config config.yaml --force timeseries                          # 강제 재실행
wintersar cache ls --config config.yaml ; wintersar cache gc --config config.yaml --keep 3
```

파라미터를 바꾸면 그 단계와 하류만 다시 돌고, 같은 값으로 되돌리면 캐시가 그대로 적중합니다.

## 5. 결과 확인과 검증

- 산출물 위치: `work/<stage>/<hash>/out/`, 실행 요약 `work/runs/<run_id>.json`. QGIS 플러그인 패널 ④가
  COG/GeoTIFF 를 레이어로 불러옵니다.
- 기준점 추천·확정: `wintersar refpoint --ts work/ts/timeseries.h5 --aoi aoi.geojson --top 5`
  → `timeseries.reference_point: [lat, lon]` 에 기록 후 `--from timeseries` 재실행.
- 대조군 검증: `wintersar validate --ts work/ts/timeseries.h5 --leveling data/leveling.csv [--gnss data/gnss.csv]`
  — 자세한 절차는 [검증·튠 튜토리얼](validate-tune.md).

## 다음

로컬에서 파라미터를 완전히 통제하려면 [ISCE2 로컬 튜토리얼](isce2-local.md). 성능 주장은
`wintersar bench --site benchmarks/sites/S_synthetic.yaml` 결과(`bench_result.json`)로만 합니다.

## English summary

The HyP3 path needs an Earthdata Login (with HyP3 access), `~/.netrc` or `EARTHDATA_TOKEN`,
`pip install 'wintersar[hyp3]'` and MintPy from conda-forge (called only via subprocess). Run
`wintersar check-install`, `wintersar init config.yaml` (set `engine.interferogram: hyp3`,
`timeseries.engine: mintpy`), then `search` -> `precheck` (read the `SEL-xx` findings, pin the
recommended track) -> `plan` (credit estimate from `hyp3_costs.yaml`, never hard-coded) -> `run`.
HyP3 performs coregistration, interferogram and unwrapping in the cloud (local `unwrap` is skipped);
MintPy runs `timeseries -> corrections -> geocode`. On failure use `wintersar diagnose work/logs
--engine hyp3` and resume with `run --from <stage>`; parameter changes re-run only downstream stages.
Finish with `refpoint` and `validate` against levelling/GNSS CSVs.
