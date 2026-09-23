# 튜토리얼 1 — HyP3 빠른 시작 (search → precheck → plan → run → diagnose → validate → refpoint → sweep)

목표(G1): SARscape 없이 **ASF HyP3 On-Demand** 로 burst 간섭도를 만들고 MintPy 로 SBAS 시계열까지
산출합니다. 로컬에는 ISCE2·SNAPHU 가 필요 없습니다 — 정합·간섭도·언래핑은 HyP3 클라우드가 수행하고,
wintersar 는 선별·제출·다운로드·MintPy 입력 구성·시계열·검증을 맡습니다.

> **읽는 법.** 각 단계는 두 줄로 되어 있습니다. **실데이터** 줄은 Earthdata 계정·HyP3 크레딧·MintPy 가 있을 때
> 실제로 치는 명령이고, **지금 바로(합성)** 줄은 내장 fake 엔진과 저장소 픽스처만으로 지금 이 자리에서
> 돌려 볼 수 있는 같은 명령입니다. 이 문서의 모든 명령은 `wintersar <명령> --help` 에 실제로 있는 옵션만 쓰며
> 테스트가 이를 검사합니다([ADR-0112](../adr/0112-docs-test-policy.md)). 처리 시간·크레딧 소모량 같은 수치는
> `bench_result.json` 이 있을 때만 적습니다(규칙 11.8) — 아래의 "예상 출력" 은 **모양**이지 값이 아닙니다.
> 설치 전반은 [설치 안내](../install.md)를 보세요.

## 0. 준비물

| 항목 | 실데이터 경로에 필요한 것 | 합성 경로 |
|---|---|---|
| NASA Earthdata Login | <https://urs.earthdata.nasa.gov> 계정. HyP3 는 별도 **사용 신청 승인**이 필요하며 승인 전 제출은 `KB-HYP3-004` 로 거부됩니다 | 불필요 |
| 인증 정보 | `~/.netrc` 의 `machine urs.earthdata.nasa.gov login <id> password <pw>` (우선) 또는 환경변수 `EARTHDATA_TOKEN` ([ADR-0013](../adr/0013-earthdata-auth-policy.md): 둘 다 있으면 `.netrc` 우선, 검색은 무인증) | 불필요 |
| HyP3 크레딧 | 잔여 크레딧과 작업별 비용은 <https://hyp3-docs.asf.alaska.edu/using/credits/> (표의 사본과 조회일은 `src/wintersar/engines/hyp3_costs.yaml`, [ADR-0020](../adr/0020-hyp3-api-and-credits.md)) | 불필요 |
| hyp3-sdk | `uv sync --extra hyp3` 또는 `pip install 'wintersar[hyp3]'` — 의존성 정책 예외라 개발 환경에는 기본 설치되지 않습니다(open-questions #10) | 불필요 |
| MintPy | conda-forge `mintpy` (GPL-3 → subprocess 로만 호출, 절대 import 하지 않음). 설치는 [설치 안내](../install.md) | 불필요 (`timeseries.engine: fake`) |
| AOI | GeoJSON 폴리곤(EPSG:4326). QGIS 플러그인의 "캔버스 범위로/활성 레이어로" 로도 만들 수 있음 | 빈 FeatureCollection 으로 충분 |
| 대조군(선택) | 수준측량·GNSS CSV (스키마는 [검증·튠 튜토리얼](validate-tune.md)) | `tests/fixtures/ground_truth/*.csv` |

## 1. 설치 상태 확인

```bash
wintersar check-install                     # 전체 엔진·인증·GDAL/PROJ·GPU
wintersar check-install --engine hyp3 --engine mintpy   # 이 경로에 필요한 둘만
```

예상 출력(모양):

```text
설치 상태 점검 — wintersar <version>
Python <x.y.z> · <os>
CPU <n>코어 · 메모리 <n> GB · GPU <예|아니오>
┃ 엔진 ┃ 버전 ┃ constraint ┃ 상태 ┃ stages ┃
│ fake │ …    │ *          │ 사용 가능 │ fetch, coregister, interferogram, multilook, unwrap, timeseries, corrections, geocode │
│ hyp3 │ -    │ >=7.0,<8   │ 미설치    │ interferogram │
│ mintpy │ -  │ >=1.5,<2   │ 미설치    │ timeseries, corrections, geocode │
…
┃ ┃ ID ┃ 원인 ┃ 조치 ┃
│ 실패 │ ENV-001 │ 엔진 'hyp3'을(를) 찾을 수 없습니다 … │ 설치 방법: pip install 'wintersar' (hyp3-sdk>=7, BSD-3-Clause) + Earthdata Login │
│ 경고 │ ENV-003 │ Earthdata 자격증명 없음 … │ … │
실패 <n> · 경고 <n> · 정보 <n>
```

`check-install` 은 *보고* 명령이라 미설치 엔진이 있어도 종료 코드 0 입니다(`--strict` 로 1). `ENV-001` 이
나오면 조치 문구의 설치 명령을 따르고, `ENV-003` 은 인증 정보가 없다는 뜻입니다. `ENV-004`(GDAL/PROJ 불일치)는
같은 환경에서 rasterio·pyproj 를 함께 재설치합니다.

## 2. 설정 파일

```bash
mkdir site-a && cd site-a
wintersar init config.yaml          # 예시 설정 (플랜 §4.4)
```

실데이터 — `config.yaml` 에서 확인할 값:

```yaml
project: { name: site-a-subsidence, workdir: ./work, language: ko }
aoi: aoi.geojson
time_range: { start: 2023-01-01, end: 2025-12-31 }
data:
  source: asf
  product: burst              # burst 우선 (PERF-01); 없으면 SLC fallback (ADR-0012)
  polarization: VV
  orbit_direction: auto       # asc | desc | auto → 방향별 스택 후보를 모두 만듭니다
  relative_orbit: auto        # 트랙을 알면 번호를 적으세요 (SEL-01)
  credentials: env:EARTHDATA_TOKEN   # 또는 netrc
selection:
  network: sbas
  max_perp_baseline_m: 150
  max_temporal_baseline_days: 48
  min_coverage: 0.95
  budget_credits: 2000        # (선택) 견적이 넘으면 SEL-13 WARN
engine:
  interferogram: hyp3
  looks: auto                 # HyP3 가 제공하는 looks 옵션(20x4 | 10x2 | 5x1) 안에서 스냅 (ADR-0020)
timeseries:
  engine: mintpy
  reference_point: auto_recommend
  troposphere: era5
validate:
  leveling_csv: data/leveling.csv   # 대조군이 없으면 이 절을 지우세요 (PIPELINE-010 INFO 로 건너뜀)
```

지금 바로(합성) — 두 엔진을 `fake` 로 바꾸고 `validate:` 절을 지웁니다. AOI 는 빈 FeatureCollection 이면 됩니다:

```bash
printf '{"type":"FeatureCollection","features":[]}' > aoi.geojson
```

```yaml
engine:
  interferogram: fake
timeseries:
  engine: fake
# validate: 절 삭제
```

## 3. 검색 (search)

실데이터:

```bash
wintersar search --config config.yaml
```

- 결과: `work/select/candidates.json` (burst 레코드 목록, `BurstRecord` 스키마).
- 검색 자체는 무인증입니다. 인증 정보가 없으면 다운로드 단계를 위해 WARN 만 냅니다(ADR-0013).
- ASF/CMR 호출이 실패하면 `KB-ASF-001`(재시도 힌트 `retry_later`) 이 나옵니다.

지금 바로(합성): `search` 는 네트워크가 필요하므로 합성 대체가 없습니다. 개발자는
`tests/unit/select/conftest.py` 의 `full_stack()` 으로 만든 레코드를 `{"product_type": "BURST", "query": {},
"records": [...]}` 형식으로 저장해 다음 단계를 시험할 수 있습니다(단위 테스트 `tests/unit/select/test_cli.py` 가
같은 방법을 씁니다).

## 4. 사전검증 (precheck)

```bash
wintersar precheck work/select/candidates.json --config config.yaml
#   --no-fail          FAIL 이 있어도 종료 코드 0
#   --baseline none    ASF stack API 를 부르지 않음 (오프라인; SEL-06 은 "미정" 으로 남음)
#   --geometry work/select/geometry.json   레이오버·셰도우 통계 (SEL-12)
```

- 결과: `work/select/precheck_report.{md,html,json}` (`--out` 으로 위치 변경).
- `precheck` 는 *동작* 명령이라 FAIL 이 있으면 종료 코드 1 입니다(`--no-fail` 로 끕니다).

예상 출력(모양) — 합성 후보 10 레코드·AOI 교차 시 실제로 나온 형태:

```text
┃ ┃ ID ┃ 원인 ┃ 조치 ┃
│ 정보 │ SEL-06 [T<track><A|D>_VV] │ 수직 기선을 아직 계산하지 못했습니다 … │ … 정밀궤도(POEORB)를 받아 precheck 를 다시 … │
│ 정보 │ SEL-09 [T…] │ IW SLC 픽셀은 레인지 … × 애지머스 … 로 비대칭입니다. … 자동 looks = … │ engine.looks 를 auto 로 두면 … │
│ 정보 │ SEL-13 [T…] │ 예상 작업 수 <n>개(쌍 × burst). 크레딧 견적은 아직 없습니다 … │ … wintersar plan 으로 확인 … │
실패 0 · 경고 0 · 정보 3
사전검증 완료: 후보 1개, 실패 0 · 경고 0 · 정보 3
추천 스택: T<track><A|D>_VV (커버리지 100%, 날짜 <n>개, 쌍 <n>개)
리포트: work/select/precheck_report.json, work/select/precheck_report.md, work/select/precheck_report.html
```

리포트에서 볼 것:

- 후보 스택 표: 트랙(`T052D_VV` 형식)·방향·편파·sub-swath·날짜 수·burst 수·AOI 커버리지·쌍 수·기선 분포, 추천 스택.
- Finding(원인 → 조치): `SEL-01`(트랙 혼합) `SEL-04`(공통 burst/커버리지) `SEL-06/07`(기선·계절)
  `SEL-09`(픽셀 간격·자동 looks) `SEL-11`(정밀궤도) `SEL-12`(레이오버·셰도우 — 상승/하강 중 유리한 방향)
  `SEL-13`(크레딧 예산). 개념은 [concepts](../concepts/index.md), 임계값은 [ADR-0014](../adr/0014-precheck-rule-thresholds.md).
- 추천 스택이 마음에 들면 `data.relative_orbit` 과 `data.orbit_direction` 을 그 값으로 고정합니다.
- AOI 가 어떤 burst 와도 교차하지 않으면 "AOI 와 교차하는 스택 후보가 없습니다" 와 함께 빈 표가 나옵니다 —
  AOI 좌표 순서(경도, 위도)와 기간을 확인하세요.

## 5. 계획·견적 (plan)

```bash
wintersar plan --config config.yaml
```

`plan` 은 단계 표(캐시됨/실행 예정/건너뜀), 노드 해시, 예상 리소스와 **HyP3 크레딧 견적**을 보여 주고 아무것도
실행하지 않습니다. 크레딧 단가는 `hyp3_costs.yaml` 에서만 읽습니다(코드에 하드코딩하지 않음). 견적이
`selection.budget_credits` 를 넘으면 `SEL-13` WARN 입니다.

예상 출력(모양, 합성 설정에서 실제로 나온 형태):

```text
실행 계획: site-a-subsidence (<workdir>)
┃ 단계 ┃ 엔진 ┃ 버전 ┃ 상태 ┃ 노드 해시 ┃ 소요(s) ┃
│ search        │ -    │ -     │ 건너뜀    │ -        │ - │
│ precheck      │ -    │ -     │ 건너뜀    │ -        │ - │
│ fetch         │ fake │ …     │ 실행 예정 │ <hash>   │ … │
│ coregister    │ fake │ …     │ 실행 예정 │ -        │ … │
│ interferogram │ fake │ …     │ 실행 예정 │ -        │ … │
│ multilook     │ fake │ …     │ 실행 예정 │ -        │ … │
│ unwrap        │ fake │ …     │ 실행 예정 │ -        │ … │
│ timeseries    │ fake │ …     │ 실행 예정 │ -        │ … │
│ corrections   │ fake │ …     │ 실행 예정 │ -        │ … │
│ geocode       │ fake │ …     │ 실행 예정 │ -        │ … │
│ validate      │ -    │ -     │ 건너뜀    │ -        │ - │
실행 예정 8 · 캐시됨 0 · 건너뜀 3
예상 리소스: 시간 … · 메모리 … · 디스크 … · 크레딧 <없음|n>
│ 정보 │ PIPELINE-010 │ 검증 단계를 건너뜁니다: validate.leveling_csv 또는 validate.gnss 가 설정되지 않았습니다. │ … │
```

`search`/`precheck` 는 `run` 안에서 건너뛰고(위 3·4 절에서 따로 실행) `fetch` 부터 시작합니다. HyP3 설정에서는
`unwrap` 행도 "건너뜀" 이 됩니다 — 언래핑은 HyP3 산출물(`_unw_phase.tif`)에 이미 들어 있기 때문입니다
(`tests/integration/test_pipeline_hyp3_unwrap_skip.py`).

## 6. 실행 (run)

```bash
wintersar run --config config.yaml
```

HyP3 경로의 단계([ADR-0020](../adr/0020-hyp3-api-and-credits.md)): `interferogram` 단계 하나가 burst 쌍 작업
(`INSAR_ISCE_BURST` / `INSAR_ISCE_MULTI_BURST`)을 제출 → 폴링 → 다운로드 → 산출물 검증 → MintPy `prep_hyp3` 호환
디렉터리 구성까지 수행합니다. 제출된 작업 ID 는 즉시 `jobs.json` 에 기록되어 중단 후 재실행 시 재제출하지
않습니다(PERF-06). 그 뒤 `timeseries → corrections → geocode` 는 MintPy 어댑터가 `smallbaselineApp.py --dostep`
으로 단계별 실행합니다(템플릿은 머신 스펙에 맞춰 `compute.*` 를 채움, PERF-09,
[ADR-0021](../adr/0021-mintpy-template-and-perf09.md)).

지금 바로(합성) — 같은 명령을 세 번 치면 캐시 동작이 보입니다:

```bash
wintersar run --config config.yaml                                   # 1회: 8단계 실행
wintersar run --config config.yaml                                   # 2회: 8단계 캐시 재사용
wintersar run --config config.yaml --set unwrap.coherence_threshold=0.5   # unwrap 이후 4단계만 재실행 (PERF-03)
wintersar cache ls --config config.yaml                              # work/<stage>/<hash> 목록과 크기
wintersar cache gc --config config.yaml --keep 1 --dry-run           # 단계별 최신 1개만 남길 때 지워질 항목
```

예상 출력(모양):

```text
실행: site-a-subsidence (<workdir>)
┃ 단계 ┃ 엔진 ┃ 버전 ┃ 상태 ┃ 노드 해시 ┃ 소요(s) ┃
│ fetch         │ fake │ … │ 캐시됨 │ <hash> │ … │
│ …             │      │   │ 캐시됨 │        │   │
│ unwrap        │ fake │ … │ 실행됨 │ <새 hash> │ … │
│ timeseries    │ fake │ … │ 실행됨 │ <새 hash> │ … │
│ corrections   │ fake │ … │ 실행됨 │ <새 hash> │ … │
│ geocode       │ fake │ … │ 실행됨 │ <새 hash> │ … │
완료: 4단계 실행, 4단계 캐시 재사용 (…s)
```

`--set` 은 `--set <단계>.<키>=<값>` 형식이고 **그 단계의 파라미터에만** 얹힙니다(다른 단계로 전파되지 않음,
[ADR-0034](../adr/0034-stage-params-contract.md)). 산출물은 `work/<stage>/<hash>/out/`
(`interferogram|multilook → igrams.npz`, `unwrap → unw.npz`, `timeseries|corrections → timeseries.npz`,
`geocode → velocity.npy`), 실행 요약은 `work/runs/<run_id>.json`, 로그는 `work/<stage>/<hash>/logs/`
([ADR-0032](../adr/0032-workdir-layout-and-manifest.md)). `work/logs/` 라는 디렉터리는 없습니다.

부분 실행:

```bash
wintersar run --config config.yaml --until unwrap          # 언래핑까지만
wintersar run --config config.yaml --from timeseries       # 상류는 캐시에서, 시계열부터
wintersar run --config config.yaml --force timeseries      # 강제 재실행 (하류 포함)
wintersar run --config config.yaml --dry-run               # plan 과 같은 표만
wintersar --json run --config config.yaml                  # QGIS 플러그인이 읽는 봉투 {"ok","command","data","findings"}
```

`--json` 과 `--lang ko|en` 은 **전역 옵션**이라 하위 명령 **앞에** 씁니다. `run` 은 *동작* 명령이라 실패하면
종료 코드 1, 잘못된 입력(없는 단계 이름, 잘못된 `--set`)은 `PIPELINE-014` 와 함께 2 입니다.

## 7. 진단 (diagnose)

`run` 은 실패하면 자동으로 실패 노드의 로그 디렉터리에 `diagnose` 를 붙여 Finding 표에 함께 보여 줍니다
([ADR-0033](../adr/0033-failure-and-retry-semantics.md)). 따로 부를 때:

```bash
wintersar diagnose work/ --engine hyp3               # 작업 디렉터리 전체 (엔진 힌트)
wintersar diagnose work/interferogram/<hash>/logs    # 실패한 노드 하나
wintersar diagnose --list-kb                         # KB 항목 표 (ID·엔진·단계·심각도·검증·retry)
```

HyP3 경로에서 만나는 KB: `KB-HYP3-001`(크레딧 부족) `KB-HYP3-002`(입력 검증 실패) `KB-HYP3-003`(DEM 범위)
`KB-HYP3-004`(접근 권한), 다운로드 단계의 `KB-AUTH-001`, MintPy 의 `KB-MINTPY-001…004`. 전체 목록은
[진단 KB](../kb/index.md), 구조는 [KB 개요](../kb/overview.md).

지금 바로(합성) — fake 엔진에 실패를 주입합니다. 실패시킬 **단계 이름으로** 키를 써야 합니다:

```bash
wintersar run --config config.yaml --set unwrap.fail_stage=unwrap   # 종료 코드 1
wintersar diagnose work/                                            # 같은 로그를 다시 진단
wintersar run --config config.yaml --from unwrap                    # 조치 후 재개 (주입을 빼면 캐시가 적중)
```

예상 출력(모양):

```text
│ unwrap │ fake │ … │ 실패 │ <hash> │ … │
실패: 단계 'unwrap' (로그: <workdir>/unwrap/<hash>/logs)
│ 실패 │ PIPELINE-001 │ 단계 'unwrap'(엔진 fake)이(가) 실패했습니다: … │ 로그 … 확인하고 'wintersar diagnose …' 로 원인을 진단한 뒤 'wintersar run --from unwrap' 로 재개하세요. │
│ 경고 │ KB-UNKNOWN   │ 로그에서 오류 흔적(1건)은 보이지만 알려진 실패 패턴(KB)과 일치하지 않습니다. 엔진: snaphu. 발췌: ERROR: … │ … docs/kb/index.md 에서 비슷한 항목을 찾으세요 … │
```

`KB-UNKNOWN` 은 "패턴 미매칭 + 마스킹된 로그 발췌" 라는 뜻이며 Phase 3 DoD 의 "미분류" Finding 입니다.
`--set interferogram.fail_stage=unwrap` 처럼 다른 단계에 얹으면 `unwrap` 은 그 키를 보지 못해 아무것도
실패하지 않고 interferogram 해시만 바뀌어 하류가 통째로 재실행됩니다.

## 8. 검증 (validate)

실데이터 — `validate.leveling_csv`(및 `validate.gnss.path`)를 설정해 두면 `run` 의 마지막 `validate` 단계가
리포트를 만들고 `VAL-013` 요약을 남깁니다. 따로 부를 때는 MintPy 산출물을 직접 줍니다:

```bash
wintersar validate --ts work/timeseries/<hash>/out/timeseries.h5 --leveling data/leveling.csv --gnss data/gnss.csv --out work/validate
```

지금 바로(합성) — fake 시계열은 저장소 픽스처 CSV 와 같은 좌표계에 놓여 있어 그대로 맞물립니다:

```bash
mkdir -p data
cp <repo>/tests/fixtures/ground_truth/leveling_synth.csv data/leveling.csv
cp <repo>/tests/fixtures/ground_truth/gnss_synth.csv     data/gnss.csv
wintersar validate --ts work/timeseries/<hash>/out/timeseries.npz --leveling data/leveling.csv --out work/validate
wintersar validate --ts work/timeseries/<hash>/out/timeseries.npz --leveling data/leveling.csv --gnss data/gnss.csv --heading -12 --incidence 39 --out work/validate-gnss
```

예상 출력(모양):

```text
대조군 검증 — work/timeseries/<hash>/out/timeseries.npz
┃ 지점 ┃ 방법 ┃ n ┃ RMSE (mm) ┃ bias (mm) ┃ 상관 ┃ dv (mm/yr) ┃ 거리 (m) ┃
│ L01-center │ leveling │ … │ … │ … │ … │ … │ … │
│ G01        │ gnss     │ … │ … │ … │ … │ … │ … │
RMSE … mm · bias … mm · 지점 <n>개 · 시점 <n>개 · 반경 <r> m
│ 경고 │ VAL-009 [L99-outside] │ 지점 … 는 가장 가까운 InSAR 픽셀에서 … 떨어져 있어 한계 … 를 넘습니다 … │ 지점 좌표(lat/lon 순서, 기준계)를 확인하거나 AOI 를 넓히세요 … │
저장: work/validate/validation_report.md / .html / .json, work/validate/plots
```

GNSS 를 `--heading` 없이 주면 fake 시계열에는 heading 이 없어 `VAL-008`(FAIL)로 멈추고, 리포트를 만들지 못했으므로 종료 코드는 1 입니다(보고 명령이라도 결과물 자체가 없으면 1).
지표 읽는 법과 부호 규약은 [검증·튠 튜토리얼](validate-tune.md).

## 9. 기준점 추천 (refpoint)

```bash
wintersar refpoint --ts work/timeseries/<hash>/out/timeseries.npz --aoi aoi.geojson --top 5 --out work/refpoint.json
```

예상 출력(모양):

```text
기준점 추천 — work/timeseries/<hash>/out/timeseries.npz
시계열: 날짜 <n>개, <rows> x <cols> 픽셀
┃ 순위 ┃ 행 ┃ 열 ┃ 위도 ┃ 경도 ┃ 점수 ┃ 코히어런스 ┃ 성분 ┃
│ 1 │ … │ … │ … │ … │ … │ - │ dist=… velo=… │
적용: config.yaml의 timeseries.reference_point: [<lat>, <lon>]
저장: work/refpoint.json
```

"적용" 줄의 값을 `config.yaml` 에 적고 시계열부터 다시 돌립니다(상류는 캐시):

```yaml
timeseries:
  reference_point: [37.59532, 126.90636]
```

```bash
wintersar run --config config.yaml --from timeseries
```

코히어런스·연결성분·DEM 을 주면(`--coherence`, `--conncomp`, `--dem`) 점수에 반영되고, 가중치는
`--weights coherence=0.4,conncomp=0.3` 형식으로 바꿉니다([ADR-0041](../adr/0041-refpoint-scoring-weights.md)).

## 10. 파라미터 스윕 (sweep)

```yaml
# sweep.yaml
grid:
  unwrap.coherence_threshold: [0.3, 0.4]
  timeseries.troposphere: [era5, none]
objectives: [gt_rmse, closure_rms, wall_time_s]
```

```bash
wintersar sweep --config config.yaml --grid sweep.yaml --leveling data/leveling.csv --out work/sweep
```

예상 출력(모양):

```text
파라미터 스윕 — 파라미터 조합 4개
[1/4] {"unwrap.coherence_threshold": 0.3, "timeseries.troposphere": "era5"}
…
| # | 파라미터 | 폐합 RMS (rad) | 시간적 코히어런스 | 잔차 RMS (mm) | 대조군 RMSE (mm) | 실행 시간 (s) | 캐시 적중 | Pareto | 상태 |
| 0 | unwrap.coherence_threshold=0.3, timeseries.troposphere=era5 | … | … | … | … | … | <n> | * | 성공 |
스윕 완료: 4개 중 4개 성공, Pareto 전선 <n>개
저장: work/sweep/sweep.json, work/sweep/sweep.md, work/sweep/sweep_pareto.png
```

"캐시 적중" 열이 조합마다 0 이 아닌 것이 PERF-03 의 증거입니다(바뀐 섹션의 단계부터만 재실행). 지표 정의는
[ADR-0044](../adr/0044-sweep-design.md), 권장 반복 순서는 [검증·튠 튜토리얼](validate-tune.md).

## 11. 결과 확인

- 산출물: `work/<stage>/<hash>/out/`. QGIS 플러그인 패널 ④가 COG/GeoTIFF 를 레이어로 불러옵니다(`qgis_plugin/README.md`).
- 실행 요약: `work/runs/<run_id>.json` — 어떤 단계가 어느 해시로 실행/캐시됐는지.
- 벤치마크: 성능 주장은 `wintersar bench --site benchmarks/sites/S_synthetic.yaml --out bench_result.json` 결과로만 합니다.

## 다음

로컬에서 파라미터를 완전히 통제하려면 [ISCE2 로컬 튜토리얼](isce2-local.md), 검증·튠의 세부 절차는
[검증·튠 튜토리얼](validate-tune.md). 실패 메시지의 `KB-xxx` 는 [진단 KB](../kb/index.md) 에서 찾습니다.

## English summary

The HyP3 path needs an Earthdata Login (<https://urs.earthdata.nasa.gov>) with HyP3 access approved,
credentials in `~/.netrc` (preferred) or `EARTHDATA_TOKEN`, credits (see
<https://hyp3-docs.asf.alaska.edu/using/credits/>; the cost table copy lives in `hyp3_costs.yaml`, ADR-0020),
`uv sync --extra hyp3` and MintPy from conda-forge (subprocess only). Every step is shown twice: the real
command and a synthetic equivalent that runs right now with the built-in fake engine (`engine.interferogram:
fake`, `timeseries.engine: fake`). Flow: `check-install` -> `init` -> `search` (network only) -> `precheck`
(read the `SEL-xx` findings, pin the recommended track; `--baseline none` keeps it offline) -> `plan` (credit
estimate, nothing runs) -> `run` (HyP3 does coregistration, interferogram and unwrapping in the cloud, so the
local `unwrap` stage is skipped; MintPy runs `timeseries -> corrections -> geocode`; a second `run` is fully
cached and `--set unwrap.coherence_threshold=0.5` re-runs only downstream stages) -> `diagnose` (auto-attached
on failure; `--set unwrap.fail_stage=unwrap` injects a fake failure and yields `PIPELINE-001` + `KB-UNKNOWN`)
-> `validate` (fixture CSVs under `tests/fixtures/ground_truth` line up with the fake time series; GNSS needs
`--heading`) -> `refpoint` (paste the "적용" line into `timeseries.reference_point`, then `run --from
timeseries`) -> `sweep` (grid YAML, Pareto front, the "cache hits" column is the PERF-03 evidence). Console
excerpts in this page show shapes only, never measured numbers (rule 11.8); every command is checked against
the CLI by `tests/unit/qgis/test_docs.py` (ADR-0112).
