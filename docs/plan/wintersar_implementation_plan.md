# InSAR 오픈소스 툴킷 구현 계획서 (Claude Code 핸드오프)

- 문서 버전: 0.1 (2026-09-16)
- 대상: Claude Code(구현) / Taylor(발주·검수) / InSAR 연구자(도메인 검수)
- 작업명(가칭): `wintersar` — 저장소 생성 시 최종 이름 확정. SARscape는 NV5의 상표이므로 제품명·문서에 유사 명칭을 쓰지 않는다.

---

## 0. 이 문서를 읽는 방법 (Claude Code용)

- 순서: 요구사항(2장) → 재사용 전략(3장) → 아키텍처(4장) → 모듈 명세(5장) → 성능 개선(6장) → 단계별 작업(7장). 구현은 7장의 Phase 순서대로 진행하고, 각 Phase의 DoD(Definition of Done)를 만족하기 전에는 다음 Phase로 넘어가지 않는다.
- ID 체계: 요구사항 `R-xx`, 사전검증 규칙 `SEL-xx`, 성능 개선 `PERF-xx`, 진단 KB `KB-xx`. 커밋 메시지·이슈·테스트 이름에 그대로 사용해 추적 가능하게 한다.
- 11장(작업 지침)은 모든 Phase에 적용되는 불변 규칙이다.
- "확인 필요"로 표시된 항목은 구현 시 실제 공식 문서/소스코드로 검증하고 결과를 `docs/adr/`에 기록한다. 추측으로 채우지 않는다.
- 이 문서에 적힌 기대 효과는 전부 가설이다. `wintersar bench` 측정 결과 없이 README나 문서에 성능 수치를 쓰지 않는다.

---

## 1. 배경과 목표

### 1.1 문제 정의

- 연구자는 ENVI SARscape로 Sentinel-1 간섭 스태킹(SBAS)을 수행 중이나 라이선스 비용이 부담.
- 오픈소스 대안은 존재하지만
  1. 단계별 도구가 쪼개져 있어(검색 → 정합·간섭도 → 언래핑 → 시계열) 조합·설치·운영 부담이 크고,
  2. "정합 가능한 데이터 선별"과 "실패 원인 파악"에 실무 시간이 과도하게 든다(연구자 진술: 첫 1년을 이런 문제로 허비),
  3. 결과 검증(수준측량·GNSS 대조)과 반복 튠(4~5회) UX가 없다.
- 알고리즘 코어(언래핑, SBAS 역산)는 기존 오픈소스가 검증돼 있다. 따라서 새 프로젝트의 가치는 **선별·검증·진단·UX**, **성능(데이터 이동·중복 연산·스케줄링·IO)**, **대표위상 연구 모듈**에 있다.

### 1.2 목표

| ID | 목표 | 측정 |
|---|---|---|
| G1 | SARscape 없이 Sentinel-1 SBAS 시계열을 end-to-end 산출 | HyP3 경로: 설치 후 첫 결과까지 ≤ 1시간. 로컬 ISCE2 경로: 스모크 데이터로 재현 |
| G2 | 정합 가능성 사전검증 + 실패 진단으로 시행착오 최소화 | 알려진 실패 케이스 10개를 사전검증/진단이 잡아냄 |
| G3 | 국내 수준측량·GNSS 대조 기반 정량 검증 | 사이트 1곳 이상에서 RMSE/bias 리포트 |
| G4 | 기존 오픈소스 비효율 개선을 벤치마크로 입증 | 6장 PERF 항목별 before/after 표 |
| G5 | 대표위상 추출·타일 스티칭 연구 실험 프레임워크 | 합성+실데이터 벤치마크에서 기준선 대비 지표 산출 |
| G6 | QGIS 플러그인 UX | 플러그인만으로 검색 → 사전검증 → 실행 → 검증 흐름 완료 |

### 1.3 비목표 (v0.x)

- Level-0 포커싱, PolSAR, 위상 언래핑 코어(MCF/네트워크 플로우) 재구현, 클라우드 SaaS, KOMPSAT 이외 상용 X-band 센서 전용 리더.
- 언래핑 최적화 알고리즘 자체의 개량(연구자 판단대로 한계에 근접). 성능 개선은 6장의 범위(데이터 이동·중복·스케줄링·IO·사람 시간)에서 얻는다.

### 1.4 설계 원칙

- P1 재사용 우선: 검증된 엔진은 어댑터로 감싼다. fork 금지, 패치는 upstream으로.
- P2 엔진은 subprocess 경계 뒤에 둔다(GPL 격리 + 버전 독립).
- P3 모든 단계는 캐시 가능한 DAG 노드다(입력·파라미터 해시 → 변경된 단계 이후만 재실행).
- P4 사용자 메시지는 "원인 + 조치"를 한국어/영어로 제공(i18n YAML).
- P5 성능 주장은 `bench` 결과가 있을 때만.
- P6 도메인 오해를 코드로 방지(relative orbit 검사, looks 자동 계산, 레이오버 마스크).

---

## 2. 요구사항 (대화 근거 → 요구사항 ID)

| ID | 요구사항 | 대화 근거(요약) | 우선순위 | 담당 모듈 |
|---|---|---|---|---|
| R-01 | 정합 가능한 Sentinel-1 씬/burst 자동 선별 | 촬영모드·상승/하강·편파·burst 개념을 모르면 못 고름. 다 받아도 맞는 게 몇 개 없음 | P0 | select |
| R-02 | 정합 실패 원인 사전 판별·설명 | 같은 하강궤도인데 정합 프로그램이 abort. 기준점을 못 찾음 | P0 | select, diagnose |
| R-03 | 픽셀 비율 이상 탐지 + looks 자동 산출 | 애지머스 1:2 짜부. 메타데이터 훑어 looks 비율 맞춤 | P0 | select |
| R-04 | 레이오버·기하왜곡 지역 식별/마스킹 | 레이오버 개념 없으면 못 골라냄 | P1 | select |
| R-05 | 간섭도 생성~언래핑 파이프라인(단계 투명성) | 간섭도 생성부터 연습. 위상잔차 계산 이해 | P0 | pipeline, engines |
| R-06 | 타일(subarea) 언래핑 + 오버랩 단차 처리 | subarea 등분, 대표위상, 가중치, 30% 오버랩 | P1 | unwrap |
| R-07 | 대표위상 추출 방식의 원리적 개선(연구) | 산술평균이 아닌 "지역을 가장 잘 설명하는" 위상 | P2 | research |
| R-08 | SBAS 파라미터 조절(코히어런스 임계, 대기 필터, 윈도우/샘플링) | 윈도우사이즈, 샘플링크기, 긴밀도 필터, 기상필터 | P0 | pipeline, engines |
| R-09 | 영점(기준점)·GCP 보정 | 영점이 맞는지. 재평탄화 | P0 | validate |
| R-10 | 대조군(수준측량·GNSS) 연동 검증 | 주변 수준측량 결과 같은 대조군 | P0 | validate |
| R-11 | 반복 튠 UX(4~5회) | 파인튜닝 너댓번 | P1 | pipeline, validate |
| R-12 | QGIS 연동 | qgis 받아, 무료임 | P1 | qgis_plugin |
| R-13 | 연산 성능 개선 | 연산 성능을 올리는 방법도 고려 | P1 | 6장 전체 |
| R-14 | 초보자 온보딩·가이드 | 처음부터 알려주는 사람이 있었으면 | P1 | diagnose, docs |
| R-15 | compressed SAR(순차 추정기) 실효성 검증 | "실성능 오히려 떨어진다"는 경험 vs 문헌(CRLB 근접) | P2 | research, bench |

---

## 3. 기존 생태계 지도와 재사용 전략

### 3.1 단계별 엔진 선택

| 단계 | 1차 선택 | 대안 | 비고 |
|---|---|---|---|
| 검색·취득 | `asf_search`(ASF, Sentinel-1 **burst** 제품 우선) | Copernicus Data Space(CDSE) SLC | burst 제품은 Full Burst ID가 같으면 항상 같은 지역 → 선별 문제가 크게 단순화. Earthdata Login 필요 |
| 궤도·DEM | `sentineleof`(POEORB/RESORB), `sardem`(Copernicus GLO-30) | 국토지리정보원 DEM(선택) | 콘텐츠 캐시 필수(PERF-02) |
| 정합·간섭도 | (A) ASF HyP3 On-Demand — ISCE2 기반 Burst InSAR, 클라우드, 무료 크레딧 (B) 로컬 ISCE2 `topsStack` | isce3 + COMPASS(공통 격자 CSLC), GMTSAR | A = "첫 결과까지 시간" 최소화, B = 파라미터 완전 통제. 두 경로 모두 지원 |
| 언래핑 | `snaphu-py`(SNAPHU 래퍼, 타일·병렬), `tophu`(다중해상도 타일) | `spurt`(3D, ECMF), isce3 PHASS/ICU | SNAPHU 라이선스 검토(9장) |
| 시계열 | MintPy `smallbaselineApp` | dolphin(phase linking PS/DS, GPU) | MintPy는 GPL-3 → subprocess 호출(P2) |
| 대기 보정 | MintPy 내장(ERA5/PyAPS, 위상-고도 비) | GACOS | 모델 데이터 사전 캐시 |
| 시각화·GIS | QGIS 플러그인, COG 출력 | KMZ(MintPy save_kmz) | |
| 국내 센서 | ISCE2 KOMPSAT5 리더(stripmap) | — | Phase 8 이후 검토 |

### 3.2 참고할 기존 통합 프로젝트(재사용/차별화 기준)

- EZ-InSAR 3: Doris/SNAP/ISCE2 프로세서 + LiCSBAS/MintPy/StaMPS 통합, 단계별 시각화. GUI 통합의 선행 사례. 차별점: 사전검증·진단·국내 검증·성능.
- InSAR.dev(구 PyGMTSAR): burst 단위 처리, GPU, 대형 언래핑, SBAS/PSI. **코어 패키지는 source-available(연구비·기관·직업적 사용 시 구독)** → 의존하지 않음. 설계 아이디어(burst 단위 지리격자 처리, Zarr 저장)만 참고.
- LiCSBAS: loop closure로 언래핑 오류 많은 간섭도 자동 제거 → validate.closure의 참고 구현.
- dolphin/OPERA: phase linking → N−1 언래핑, compressed SLC, GPU → PERF-05/06의 A/B 대상.

### 3.3 재사용 원칙

- 엔진 출력 포맷 변환은 `wintersar.io` 한 곳에서만(ISCE 플랫 바이너리+XML, HyP3 GeoTIFF, MintPy HDF5, Zarr).
- 어댑터마다 지원 버전을 핀(pin)하고 스모크 테스트를 둔다. 엔진 버전 업 시 어댑터 테스트가 먼저 깨져야 한다.

---

## 4. 아키텍처

### 4.1 레이어

```
┌───────────────────────────────────────────────────┐
│ UI      : QGIS plugin · CLI(typer) · Python API    │
├───────────────────────────────────────────────────┤
│ Core    : select · pipeline(DAG+cache) · unwrap     │
│           diagnose · validate · research · bench   │
├───────────────────────────────────────────────────┤
│ Engines : hyp3 · isce2_topsstack · snaphu · tophu   │
│ (adapter, subprocess) spurt · mintpy · dolphin     │
├───────────────────────────────────────────────────┤
│ IO      : Zarr/HDF5 스택 · COG · 메타데이터(SQLite)  │
└───────────────────────────────────────────────────┘
```

### 4.2 저장소 구조

```
wintersar/
├── pyproject.toml              # 패키지 메타, ruff/pytest 설정
├── pixi.toml                   # 재현 가능한 환경(isce2·snaphu-py·tophu·mintpy는 conda-forge)
├── Dockerfile                  # 스모크 데이터 포함 이미지
├── src/wintersar/
│   ├── select/                 # search.py, metadata.py, rules.py(SEL-xx), looks.py, geometry_masks.py, network.py, report.py
│   ├── engines/                # base.py, hyp3.py, isce2_topsstack.py, snaphu.py, tophu.py, spurt.py, mintpy.py, dolphin.py
│   ├── pipeline/               # dag.py(해시 캐시), executor.py, stages/*.py, config.py(pydantic)
│   ├── unwrap/                 # scheduler.py, tiling.py, masks.py
│   ├── diagnose/               # parsers/*.py, kb/*.yaml(KB-xx), resources.py, report.py
│   ├── validate/               # refpoint.py, closure.py, ground_truth.py, los.py, sweep.py, report.py
│   ├── research/               # repr_phase.py, stitching.py, synth.py, metrics.py, experiments/*.yaml
│   ├── bench/                  # profiler.py, sites.py, report.py
│   ├── io/                     # formats.py, zarr_store.py, cog.py, schemas.py
│   ├── i18n/                   # ko.yaml, en.yaml
│   └── cli.py
├── qgis_plugin/wintersar_qgis/  # metadata.txt, plugin.py, dock_widget.py, processing_provider.py
├── tests/                      # unit/, integration/, network/(pytest -m network), regression/golden/
├── benchmarks/sites/*.yaml     # S/M/L 벤치마크 사이트 정의
├── docs/                       # mkdocs(ko 기본, en), adr/, tutorials/, kb/
└── examples/
```

### 4.3 데이터 모델 (pydantic, `wintersar/io/schemas.py`)

```python
class BurstRecord(BaseModel):
    granule_id: str
    platform: Literal["S1A", "S1B", "S1C", "S1D"]
    mode: Literal["IW", "EW", "SM"]
    subswath: str                       # IW1 | IW2 | IW3
    full_burst_id: str                  # 예: 052_109903_IW2 (ASF 표기 확인 필요)
    relative_orbit: int                 # 트랙 번호 (1~175)
    absolute_orbit: int
    flight_direction: Literal["ASCENDING", "DESCENDING"]
    polarization: str                   # VV | VH | HH | HV
    acquisition_time: datetime
    ipf_version: str | None
    range_pixel_spacing_m: float | None
    azimuth_pixel_spacing_m: float | None
    incidence_near_deg: float | None
    incidence_far_deg: float | None
    footprint_wkt: str
    url: str

class Pair(BaseModel):
    reference: date
    secondary: date
    temporal_baseline_days: int
    perp_baseline_m: float | None       # asf_search stack 값 또는 자체 계산

class StackCandidate(BaseModel):
    relative_orbit: int
    flight_direction: str
    polarization: str
    burst_ids: list[str]                # 모든 날짜에 공통으로 존재하는 AOI 교차 burst
    dates: list[date]
    reference_date: date | None
    coverage_of_aoi: float              # 0~1
    pairs: list[Pair]

class Finding(BaseModel):
    rule_id: str                        # SEL-xx | KB-xx
    severity: Literal["FAIL", "WARN", "INFO"]
    message_key: str                    # i18n 키
    params: dict
    evidence: dict                      # 판정에 쓰인 값
    fix_key: str | None
```

### 4.4 설정 파일 예시 (`config.yaml`)

```yaml
project:
  name: site-a-subsidence
  workdir: ./work
  language: ko
aoi: aoi.geojson
time_range: { start: 2023-01-01, end: 2025-12-31 }
data:
  source: asf                 # asf | cdse
  product: burst              # burst | slc
  polarization: VV
  orbit_direction: auto       # asc | desc | auto(둘 다 후보 생성)
  relative_orbit: auto
  credentials: env:EARTHDATA_TOKEN
selection:
  network: sbas               # sbas | sequential | single_reference
  max_perp_baseline_m: 150
  max_temporal_baseline_days: 48
  min_coverage: 0.95
engine:
  interferogram: hyp3         # hyp3 | isce2_topsstack | compass_isce3(후순위)
  looks: auto                 # 또는 [rg, az]
  target_pixel_m: 40
  filter: { type: goldstein, alpha: 0.6, window: 64 }
unwrap:
  method: auto                # snaphu | tophu | spurt | auto
  cost: defo
  coherence_threshold: 0.3    # 이하 픽셀은 마스크(SARscape 동작과 유사)
  mask: { water: true, layover: true }
  tiles: auto                 # 또는 { rows: 2, cols: 2, overlap: 0.25 }
timeseries:
  engine: mintpy              # mintpy | dolphin
  reference_point: auto_recommend   # 또는 [lat, lon]
  troposphere: era5           # era5 | gacos | height_correlation | none
  deramp: linear
  unwrap_error_correction: phase_closure
validate:
  leveling_csv: data/leveling.csv
  gnss: { source: csv, path: data/gnss.csv }   # ngii 어댑터는 확인 필요
compute:
  cores: auto
  memory_gb: auto
  gpu: auto                   # cupy 가용 시 사용, 아니면 CPU
```

### 4.5 CLI 스펙 (`wintersar`)

```
wintersar search    --config config.yaml                 # 후보 burst/씬 조회 → work/select/candidates.json
wintersar precheck  work/select/candidates.json          # SEL 규칙 → precheck_report.{md,html,json}
wintersar plan      --config config.yaml                 # DAG·예상 리소스·크레딧 견적 출력(실행 안 함)
wintersar run       --config config.yaml [--until unwrap] [--from timeseries] [--force STAGE]
wintersar diagnose  work/logs/ [--engine isce2|snaphu|mintpy|hyp3]
wintersar validate  --ts work/ts/timeseries.h5 --leveling data/leveling.csv [--gnss data/gnss.csv]
wintersar refpoint  --ts-dir work/ts --aoi aoi.geojson --top 5
wintersar sweep     --config config.yaml --grid sweep.yaml
wintersar bench     --site benchmarks/sites/S.yaml [--compare baseline.json]
wintersar research  repr-phase --igram ... --method ml|coh_weighted|shp|phase_link
wintersar research  stitch     --tiles ... --method coarse_ref|overlap_consensus
```

- 모든 명령은 `--json` 출력 옵션을 가진다(QGIS 플러그인이 파싱).
- `run`은 실패 시 자동으로 `diagnose`를 호출해 Finding을 리포트에 첨부한다.

---

## 5. 모듈 상세 명세

### 5.1 `select` — 검색·선별·사전검증 (R-01~04)

**5.1.1 search.py**
- `asf_search`로 AOI(WKT) × 기간 × 플랫폼 Sentinel-1 × 제품 BURST를 조회한다. burst 제품이 없는 지역/기간은 SLC로 fallback.
- 결과를 `BurstRecord`로 정규화. 픽셀 간격·IPF 버전은 burst 메타데이터 XML(또는 SAFE annotation)에서 읽는다. 어떤 필드가 검색 API에서 바로 오고 어떤 필드가 다운로드 후에만 있는지 구현 시 확인하고 `metadata.py`에 출처를 주석으로 남긴다.
- 인증: Earthdata Login 토큰(`EARTHDATA_TOKEN`) 또는 `.netrc`. 실패 시 KB-AUTH-001로 안내.

**5.1.2 grouping (network.py)**
- 그룹 키 = `(relative_orbit, flight_direction, polarization, subswath 집합)`. 그룹마다 `StackCandidate` 생성.
- `burst_ids` = AOI와 교차하는 burst 중 **모든 날짜에 공통**인 집합. 날짜별로 빠진 burst가 있으면 그 날짜를 제외한 경우와 burst를 제외한 경우의 커버리지를 모두 계산해 사용자에게 선택지를 준다.
- 네트워크 생성: `sbas`(시간·수직 기선 임계), `sequential`(n-연결), `single_reference`. 수직 기선은 `asf_search` stack API의 baseline 값을 우선 사용하고, 없으면 정밀궤도(POEORB)로 자체 계산(구현·검증 필요, 단위테스트로 알려진 쌍과 비교).
- 참조일 추천: 시간적으로 중앙에 가깝고 다른 날짜들과의 |B⊥| 합이 최소인 날짜.

**5.1.3 사전검증 규칙 (rules.py)** — 각 규칙은 `Finding`을 반환. 메시지는 i18n 키.

| ID | 검사 | 판정 | 사용자 메시지(ko 요지) |
|---|---|---|---|
| SEL-01 | 두 씬의 relative orbit 동일 | 불일치 → FAIL | 다른 트랙(예: 하강 61 vs 하강 134) 사이에는 간섭 불가. "같은 하강궤도"만으로는 부족하며 트랙 번호가 같아야 함 |
| SEL-02 | flight direction 동일 | 불일치 → FAIL | (SEL-01 통과 시 자동 충족) |
| SEL-03 | 모드(IW) 및 sub-swath 동일 | 불일치 → FAIL | |
| SEL-04 | 공통 burst ID가 AOI 커버 | 공통 없음 → FAIL, 커버리지 < min_coverage → WARN | 프레임 경계가 날짜마다 달라 IW SLC 단위로는 겹침이 보장되지 않음. burst 단위로 다시 선택 권고 |
| SEL-05 | 공통 편파(VV 또는 HH) | 없음 → FAIL | 교차편파(VH/HV) 간섭도는 지원하지 않음 |
| SEL-06 | 수직 기선 | > max_perp_baseline → WARN | 지형 잔차·코히어런스 저하 위험 |
| SEL-07 | 시간 기선·계절 | > max_temporal → WARN, 식생·강설 시기 교차 → INFO | |
| SEL-08 | IPF 버전 차이 | 메이저 차이 → INFO(KB 참조) | 알려진 처리기 이슈는 KB에서 관리 |
| SEL-09 | 픽셀 간격 메타데이터 | 스택 내 편차 > 1% → WARN; looks 자동 산출 결과 표시 | IW SLC는 원래 레인지 ~2.3 m × 애지머스 ~14 m로 비대칭. 이는 "가끔 짜부"가 아니라 항상 그러함 → looks로 정사각 근사 |
| SEL-10 | burst 수·라인 결손 | 불일치 → WARN | |
| SEL-11 | 정밀궤도(POEORB) 가용성 | 없음 → INFO(RESORB 사용) | |
| SEL-12 | 레이오버·셰도우 비율(DEM 기반) | AOI의 x% 초과 → WARN + 마스크 레이어 | 급경사 사면은 궤도 방향에 따라 다르게 찍히며 레이오버 영역은 신뢰 불가 |
| SEL-13 | 크레딧·리소스 견적(HyP3/로컬) | 예산 초과 → WARN | |

**5.1.4 looks.py**
- 입력: `range_pixel_spacing_m`(slant), `azimuth_pixel_spacing_m`, 중앙 입사각 θ, 목표 픽셀 크기(기본 40 m).
- ground range spacing = slant spacing / sin θ. `(rg_looks, az_looks)`를 목표 크기에 가장 가깝고 종횡비 ≤ 1.2가 되도록 선택. 결과와 실제 종횡비를 리포트에 표시.
- 단위테스트: IW 대표값(2.33 m, 14.1 m, θ≈39°)에서 목표 20/40/80 m 각각의 기대 looks 고정.

**5.1.5 geometry_masks.py (R-04)**
- 입력: DEM, 궤도 기하(heading, 입사각 맵 또는 상수), AOI.
- 경사·향(aspect) → 레이더 방향 국지 입사각 계산 → 레이오버(레이더를 향한 경사 > 입사각), 셰도우(반대 경사 > 90° − 입사각), foreshortening 지수(0~1) 산출.
- 출력: GeoTIFF 마스크 + AOI 비율 통계 + 상승/하강 각각의 마스크(사이트에 유리한 궤도 방향 추천).
- ISCE2 경로에서는 topsStack의 shadow/layover 산출물이 있으면 그것을 우선 사용하고 자체 계산과 비교 테스트.

**5.1.6 report.py**
- `precheck_report.md/html/json`: 후보 스택 표(트랙·방향·편파·날짜 수·커버리지·기선 분포), Finding 목록(FAIL/WARN/INFO), 추천 스택, 예상 리소스.

### 5.2 `engines` — 어댑터 (R-05, R-08)

공통 인터페이스(`base.py`):
```python
class Engine(Protocol):
    name: str; version_constraint: str
    def check_install(self) -> list[Finding]: ...
    def estimate(self, plan: Plan) -> Resources: ...          # 시간·메모리·디스크·크레딧
    def run(self, stage: str, inputs: Artifacts, params: dict, log_dir: Path) -> Artifacts: ...
    def parse_log(self, log_path: Path) -> list[Finding]: ... # diagnose가 사용
```

- **hyp3.py**: `hyp3_sdk`로 `INSAR_ISCE_BURST`/`INSAR_ISCE_MULTI_BURST` 작업 제출 → 폴링 → 다운로드 → 산출물 검증(필수 파일, 메타데이터) → MintPy `prep_hyp3` 호환 디렉터리 구성. 제출 전 크레딧 견적을 `plan`에서 보여준다(크레딧 정책 수치는 문서 확인 필요, 하드코딩 금지). looks·필터 옵션은 HyP3가 제공하는 범위 내에서만 노출.
- **isce2_topsstack.py**: burst GeoTIFF → `burst2safe`로 SAFE 구성(또는 SLC 직접) → `stackSentinel.py` 인자 생성(bbox, looks, 네트워크 옵션, ESD 옵션, 언래퍼 선택) → run_files를 **의존성을 지키며 병렬 실행**(PERF-07) → 산출물을 MintPy `prep_isce` 규약으로 정리. 중간 파일 정리 정책 옵션.
- **snaphu.py / tophu.py / spurt.py**: 공통 시그니처 `unwrap(igram, coh, mask, params) -> (unw, conncomp, stats)`. snaphu-py는 `ntiles`, `tile_overlap`, `nproc`, `cost`, `init`, `mask`를 노출(정확한 인자명은 설치 버전 문서로 확인). tophu는 `downsample_factor`, `ntiles`, `nlooks`, `unwrap_func` 사용. spurt는 스택 입력(위상 연결 결과) 전용.
- **mintpy.py**: `smallbaselineApp.cfg` 템플릿 생성 → subprocess로 단계별 실행(`--dostep`) → HDF5 결과 로딩(읽기는 `h5py`로 직접, MintPy 모듈 import 금지 — 9장 라이선스). `mintpy.compute.cluster/numWorker/maxMemory`를 머신 스펙으로 자동 설정(PERF-09).
- **dolphin.py**: 정합 SLC 스택 → `dolphin config`/`dolphin run` → 언래핑 스택·시계열 출력을 `io`로 정규화. GPU 가용 시 활성.
- 각 어댑터: 버전 핀, `check_install`, 스모크 테스트(합성 입력 또는 공개 튜토리얼 데이터).

### 5.3 `pipeline` — DAG·캐시·증분 (R-05, R-11, PERF-03/06)

- 단계: `search → precheck → fetch(bursts, orbits, dem) → coregister → interferogram → multilook/filter → unwrap → timeseries → corrections(tropo, deramp, dem_error) → geocode/export → validate`.
- 노드 식별자 = `hash(stage, 정규화된 params, 입력 아티팩트 해시, 엔진 버전)`. 출력은 `work/<stage>/<hash>/`, `manifest.json`(입출력 목록·해시·엔진 버전·시간·peak RSS·디스크).
- 캐시 정책: 동일 해시 존재 시 skip. `--force STAGE`로 강제 재실행. 파라미터 변경 시 변경 단계와 그 하류만 재실행(반복 튠의 핵심).
- 실행기: 로컬 프로세스 풀. 단계 내 병렬(간섭도 단위) + 단계 간 의존. 리소스 예약(메모리 합계 ≤ 예산).
- `plan`(dry-run): DAG, 재실행 대상, 예상 리소스·크레딧을 표로 출력.
- 실패 처리: stage 로그를 `diagnose`에 넘겨 Finding을 리포트에 첨부하고 재시도 가능 여부(예: 타일 파라미터 조정 후 assemble-only) 표시.
- 구현 선택: 자체 경량 DAG(수백 줄) 우선. Snakemake/Prefect 도입은 ADR로 결정.

### 5.4 `unwrap` — 언래핑 스케줄러 (R-06, PERF-04)

- 입력: 간섭도 M개(멀티룩 후), 코히어런스, 마스크(수역·저코히어런스·레이오버), 리소스(cores, RAM, GPU).
- **전략 자동 선택**
  1. 픽셀 수 P → 단일 타일 예상 메모리 `m ≈ c × P / 1e6` (SNAPHU 문서상 c ≈ 100 MB, 실측으로 보정하고 상수는 설정 파일에 둠).
  2. `m ≤ per-process 예산` → 단일 타일. 아니면 `ntiles = ceil(m / 예산)`을 정사각형에 가까운 격자로. 오버랩 기본 25%(최소 200 px, 설정 가능 — 연구자 경험치 30%와 문헌·포럼 예시 200 px 사이에서 벤치마크로 결정).
  3. 프린지 밀도(위상 기울기 통계)가 높거나 대형이면 tophu 다중해상도 우선.
- **병렬화 순서**: 간섭도 단위 병렬을 먼저 채우고(타일 경계 아티팩트 없음), 단일 간섭도가 메모리를 초과할 때만 타일 병렬. `n_parallel = min(floor(cores / nproc_per_igram), floor(RAM / m))`.
- **마스크**: 수역(수역 벡터/DEM 기반), 코히어런스 임계 이하, 레이오버 → 노드 수 축소. 마스크 픽셀은 NaN 출력(SARscape 동작과 동일).
- **재튠 재사용**: 타일 조립 파라미터만 바꿀 때는 SNAPHU assemble-only(`-A`)로 타일 재언래핑 생략. 비용 배열 저장(`--costoutfile`) 옵션.
- 출력 통계: 연결성분 수, 타일 경계 단차 검출(오버랩 구간 차이의 2π 정수배 분포), 실행 시간, peak RSS.

### 5.5 `diagnose` — 실패 진단·설명 (R-02, R-14, PERF-13)

- 엔진별 로그 파서(정규식 + 구조화 로그) → KB 매칭 → `Finding(원인, 조치, 참고 링크)`.
- KB 스키마(`kb/*.yaml`):
```yaml
- id: KB-SNAPHU-001
  engine: snaphu
  pattern: "Exceeded maximum number of secondary arcs"
  cause: { ko: "타일 조립 단계에서 보조 arc 수가 한도를 초과(신뢰 영역이 과도하게 분할됨)", en: "..." }
  fix:   { ko: "TILECOSTTHRESH를 낮추거나 MINREGIONSIZE를 높이고, assemble-only(-A)로 조립만 재실행", en: "..." }
  refs: ["SNAPHU man page"]
  severity: FAIL
```
- 시드 KB(구현 시 실제 로그 문자열로 패턴 확정):
  - KB-SNAPHU-001 secondary arcs 초과 / KB-SNAPHU-002 메모리 부족(타일 분할 권고)
  - KB-ISCE2-001 참조·보조 간 공통 burst 없음 / KB-ISCE2-002 ESD 저코히어런스 경고 / KB-ISCE2-003 궤도 파일 누락 / KB-ISCE2-004 DEM 범위 부족
  - KB-MINTPY-001 기준점 자동 선택 실패(코히어런스 > 0.85 픽셀 없음 → 임계 하향 또는 수동 지정) / KB-MINTPY-002 시간적 코히어런스 저조 / KB-MINTPY-003 대기모델 다운로드 실패
  - KB-HYP3-001 크레딧 부족 / KB-HYP3-002 작업 실패(입력 burst 편파 불일치)
  - KB-AUTH-001 Earthdata 인증 실패 / KB-ENV-001 GDAL·PROJ 버전 불일치
- 리소스 추정(`resources.py`): 단계별 시간·메모리·디스크 모델(초기값은 문서·경험치, `bench` 실측으로 계수 갱신). `plan`에서 표시.
- 문서 연동: 각 KB 항목은 `docs/kb/<id>.md`로도 렌더링(온보딩 가이드 = KB + 부록 개념 정리).

### 5.6 `validate` — 검증·튠 (R-09, R-10, R-11)

- **refpoint.py**: 후보 픽셀 점수 = w1·평균 코히어런스 + w2·(연결성분이 AOI와 동일) + w3·(1 − |표고 − AOI 대표 표고|/범위) + w4·(1 − 거리/최대거리) + w5·(선형 속도 사전 추정치의 절대값이 작음). 상위 k개를 지도·표로 제시, 사용자 선택을 config에 기록. MintPy 자동 선택(코히어런스만)과 결과 비교 테스트.
- **closure.py**: 간섭도 삼각 폐합 잔차 통계(픽셀별·간섭도별), 언래핑 오류 의심 간섭도 순위, MintPy phase-closure 보정 결과와 대조. 대시보드용 JSON.
- **ground_truth.py**: 입력 CSV 스키마
  `site_id, lat, lon, elev_m, date, up_m, east_m, north_m, method(leveling|gnss), sigma_mm`.
  수준측량은 `up_m`만, GNSS는 ENU 전부. 국토지리정보원 수준점·GNSS 상시관측소 어댑터는 데이터 접근 방식(파일/API) 확인 후 구현(확인 필요). 우선은 CSV 임포트만 필수.
- **los.py**: ENU → LOS 투영. 부호·헤딩 규약은 MintPy `enu2los`와 동일하게 맞추고 단위테스트로 고정(상승/하강 각각 알려진 케이스).
- 비교 지표: 지점별 시계열 RMSE, bias, 상관계수, 속도 차이; 공간 반경(기본 100 m) 내 InSAR 픽셀 평균과 비교; 리포트(표+플롯).
- **sweep.py**: 그리드(코히어런스 임계 × looks × 필터 alpha × 언래퍼 × 대기 보정) 실행 → 지표(폐합 RMS, 시간적 코히어런스, 잔차 RMS, 대조군 RMSE, 실행 시간) 표 + Pareto 플롯. DAG 캐시로 변경 단계만 재실행.

### 5.7 `research` — 대표위상·타일 스티칭 실험 (R-07, R-15)

- **synth.py**: 합성 간섭도 생성기 — 변형 모델(가우시안/선형 침하, 선택적으로 Okada), 난류 대기(파워법칙 스펙트럼), 지형 잔차, 코히어런스 맵 기반 원형 가우시안 잡음, 레이오버/수역 마스크. 진(true) 언래핑 위상 제공.
- **repr_phase.py**: 저해상도 "대표위상" 추정 방법을 플러그인화
  - `ml`: 복소 멀티룩(현행 tophu 기준선)
  - `coh_weighted`: 코히어런스 가중 복소 평균(가중 지수 p 파라미터)
  - `shp`: 통계적 동질 픽셀(SHP) 기반 적응 멀티룩(진폭 유사성 검정)
  - `phase_link`: 미니스택 phase linking(dolphin EMI/EVD) 결과를 저해상도 기준 위상으로 사용
  - `filtered`: Goldstein/비국소 필터 후 다운샘플
- **stitching.py**: 타일 간 2π 정수 오프셋 결정
  - `coarse_ref`: tophu 방식(저해상도 기준과의 차이 최소화)
  - `overlap_consensus`: 인접 타일 오버랩 구간에서 코히어런스 가중 최빈/중앙 오프셋 → 타일 그래프 최소제곱 조정
- **metrics.py**: 언래핑 오류 픽셀 비율(합성), 타일 경계 단차 수, 폐합 잔차 RMS(실데이터), 대조군 RMSE, 실행 시간·메모리.
- **experiments/**: YAML로 실험 정의(데이터·방법·지표) → 재현 스크립트 → `docs/research/`에 보고서. R-15는 `dolphin`(순차 추정기·compressed SLC) vs MintPy 전통 SBAS의 A/B로 수행.

### 5.8 `qgis_plugin` (R-12)

- 얇은 클라이언트: 설정된 conda/pixi 환경의 `wintersar` CLI를 subprocess로 호출하고 `--json` 결과를 파싱(PyQGIS 파이썬과 의존성 충돌 회피).
- 패널: ① AOI 그리기/불러오기 → 검색 → 후보 스택 표(FAIL/WARN 배지) ② 파라미터 폼(config.yaml 편집) ③ 실행·진행률·로그·Finding ④ 결과 레이어 로드(COG: 속도·누적 변위·코히어런스·마스크) ⑤ 기준점 후보 표시 및 지도 클릭 선택 ⑥ 검증 패널(대조군 점 로드, RMSE 표, 시계열 플롯).
- Processing Provider로도 노출(모델러 연계).

### 5.9 `bench`

- 사이트 정의(`benchmarks/sites/*.yaml`): S(2 burst × 30일), M(1 프레임 × 60일), L(2 프레임 × 120일). 국내 사이트 1곳은 대조군 포함.
- 측정: 단계별 wall time, peak RSS, 디스크 피크, 네트워크 바이트, 언래핑 오류 지표, 대조군 RMSE. `bench_result.json`으로 저장, `--compare`로 before/after 표 생성.
- CI: S 사이트의 합성 버전(네트워크 불필요)을 매 PR에서 실행해 회귀 감지.

---

## 6. 기존 오픈소스의 비효율 지점과 성능 개선 계획 (R-13)

원칙: 언래핑 코어 알고리즘(MCF 최적화)은 건드리지 않는다(연구자 판단과 일치). 개선은 (1) 데이터 이동, (2) 중복 연산, (3) 스케줄링·병렬화, (4) 저장 포맷·IO, (5) 사람의 반복 시간에서 얻는다. 아래 기대 효과는 전부 가설이며 6.3의 프로토콜로 측정한다.

### 6.1 비효율 지도

| ID | 대상 | 현재 비효율(근거) | 개선 방안 | 검증 지표 | 기대 효과(가설) | Phase |
|---|---|---|---|---|---|---|
| PERF-01 | 데이터 취득 | IW SLC 전체 SAFE(수 GB/씬)를 받아 AOI에 필요한 burst 몇 개만 사용. IW SLC 프레이밍은 날짜마다 달라 겹침도 불안정(ASF 문서) | burst 제품 우선 취득(Full Burst ID 고정) + 필요 시 `burst2safe`로 SAFE 재구성 | 씬당 다운로드 바이트, 첫 간섭도까지 시간 | AOI가 소수 burst일 때 전송량 수 배~수십 배 감소 | 1 |
| PERF-02 | 보조 데이터 | 궤도·DEM·대기모델(ERA5)을 실행마다 재다운로드, ERA5는 날짜별 요청으로 느림 | 콘텐츠 주소 캐시(`~/.cache/wintersar`), 사전 일괄 fetch, 오프라인 재실행 | 재실행 시 네트워크 바이트=0, 대기 보정 단계 시간 | 재실행 대기 시간 제거 | 2 |
| PERF-03 | 반복 튠 | 파라미터 하나 바꾸면 워크플로 전체 재실행(SNAP 그래프, 수동 run_files). 연구자 4~5회 튠 시 매번 전량 | DAG 해시 캐시로 변경 단계 하류만 재실행. 언래핑 조립 파라미터 변경은 SNAPHU assemble-only + 비용 배열 재사용 | 튠 1회당 wall time, 재실행 단계 수 | 튠 반복 비용을 변경 단계 비용으로 축소 | 2 |
| PERF-04 | 언래핑 실행 | 간섭도마다 SNAPHU를 개별·순차 실행, 타일 파라미터 수동, 마스크 미사용. 단일 타일 메모리 ≈ 100 MB/백만 픽셀(SNAPHU 문서), 최적화 복잡도가 높아 대형 간섭도에서 병목 | 스케줄러: 간섭도 단위 병렬 우선 → 메모리 초과 시 자동 타일·오버랩, 수역·저코히어런스·레이오버 마스크로 노드 축소, 대형·고프린지는 tophu 다중해상도 | M개 언래핑 총 wall time, peak RSS, 타일 경계 단차 수 | 코어 수에 비례한 처리량, OOM 제거 | 2 |
| PERF-05 | 언래핑 횟수 | SBAS는 M개 쌍 전부 언래핑(M ≫ N). 저코히어런스 쌍은 오류·재시도 비용까지 유발 | 옵션: phase linking(dolphin) 후 N−1개 위상 시계열만 언래핑, compressed SLC로 코히어런스 개선(문헌: CRLB 근접). 전통 SBAS와 A/B | 언래핑 작업 수, 총 시간, 폐합 잔차, 대조군 RMSE | 언래핑 작업 수 M → N−1. 정확도는 벤치로 판단(R-15) | 5 |
| PERF-06 | 증분 업데이트 | 새 영상 1장 추가 시 정합·간섭도·시계열 전체 재처리(MintPy는 배치형) | 정합 SLC·간섭도 캐시 재사용, 새 쌍만 생성·언래핑, 시계열만 재역산. dolphin 순차(미니스택) 모드 옵션 | 증분 1장 처리 시간 vs 전체 재처리 시간 | 모니터링 운영 시 처리 시간 대폭 감소 | 5 |
| PERF-07 | ISCE2 topsStack 실행·디스크 | run_files를 순서대로 돌리는 방식이 기본이라 병렬화가 사용자 책임. burst·날짜별 중간 파일이 많아 디스크 피크가 큼 | 의존성 인식 병렬 실행기(단계 내 job 병렬), 중간 파일 정리 정책(`--cleanup` 단계별), 완료 산출물 Zarr 압축 변환 | 단계별 wall time, 디스크 피크 | 멀티코어 활용률 상승, 디스크 피크 감소 | 2 |
| PERF-08 | 저장 포맷·IO | ISCE 플랫 바이너리+XML 다수 파일. 픽셀 시계열 조회 시 파일 수백 개 오픈. QGIS 로딩 느림 | 시간축 청크 Zarr 스택(간섭도·코히어런스·언래핑), 결과는 COG(오버뷰 포함) | 픽셀 시계열 조회 시간, QGIS 레이어 로드 시간, 저장 용량 | 대화형 검증·튠 응답성 개선 | 2 |
| PERF-09 | MintPy 실행 설정 | `compute.cluster/numWorker/maxMemory` 기본값이 머신에 맞지 않아 역산·DEM 오차 단계가 단일 코어로 돌거나 OOM | 머신 스펙 탐지 → 템플릿 자동 설정, 대기모델 사전 캐시 | 역산 단계 시간, OOM 발생 여부 | 별도 코드 없이 병렬화 이득 | 2 |
| PERF-10 | 픽셀 연산 GPU | 멀티룩·Goldstein 필터·코히어런스·기하 마스크가 CPU numpy. 선행연구(TOPS GPU 가속, dolphin GPU)는 큰 속도 향상 보고 | 선택적 CuPy 백엔드(자동 감지, CPU fallback). ISCE2 내부는 건드리지 않고 후처리 단계(필터·마스크·연구 모듈)에 적용 | 필터·마스크 단계 시간(CPU vs GPU) | GPU 보유 환경에서 후처리 단계 가속 | 5 |
| PERF-11 | 정합 기하 재계산 | 새 날짜 추가 시 참조 기하(rdr2geo 등) 재계산 여부와 재사용 가능성 미확인 | 참조 기하 산출물 캐시, 재사용 검증. 대안으로 isce3+COMPASS(C++) 백엔드 평가 | 날짜 추가 시 정합 단계 시간 | 증분 처리(PERF-06)의 전제 | 5 |
| PERF-12 | 설치·첫 결과(TTFR) | conda 환경 구성 실패·버전 충돌로 시작 전 이탈. 스모크 데이터 부재 | pixi/conda-lock 락파일, Docker 이미지, 내장 스모크 데이터(합성 + 소형 실데이터), `check_install` | 설치→첫 결과 시간, 설치 실패율 | TTFR ≤ 1시간 | 0 |
| PERF-13 | 사람 시간 | 기준점·GCP 수동 선택, 실패 원인 추적, 씬 선별 시행착오 | 5.1 사전검증, 5.5 진단 KB, 5.6 기준점 추천 | 실패 시도 횟수, 튠 반복 횟수(옵트인 사용 로그) | 시행착오 시간 감소 | 1~4 |

### 6.2 항목별 설계 메모

- **PERF-03 (캐시 DAG)**: 파라미터 정규화(정렬·기본값 채움) 후 해시. 엔진 버전을 해시에 포함해 업그레이드 시 자동 무효화. 캐시 크기 제한과 `wintersar cache gc`. SNAPHU assemble-only는 tile 임시 디렉터리를 보존해야 하므로 `unwrap` 단계 매니페스트에 tile dir 경로를 기록.
- **PERF-04 (스케줄러)**: 메모리 상수 c는 S 사이트에서 3개 이상 픽셀 수로 실측해 회귀선으로 갱신. 타일 경계 단차 검출기는 연구 모듈(stitching) 지표와 공용.
- **PERF-05/06 (dolphin 경로)**: MintPy 경로와 출력 스키마를 동일하게 정규화해야 A/B가 성립한다(`io.formats`에서 통일). 결론은 `docs/research/`에 사이트별로 기록하고, 어느 경로를 기본값으로 할지는 ADR로 결정.
- **PERF-07 (run_files 병렬)**: run_files 각 줄은 독립 job인 경우가 많지만 단계 간 의존이 있다. 단계 순서는 지키고 단계 내에서만 병렬. 실패 job은 재시도 후 diagnose로 넘김.
- **PERF-08 (Zarr/COG)**: 청크 기본값 `(time=전체, y=512, x=512)` 조회 패턴(시계열)과 `(time=1, y=2048, x=2048)` 표시 패턴을 각각 실측하고 선택. 압축 코덱은 zstd 기본.
- **PERF-10 (GPU)**: `wintersar.compute.xp` 추상화(numpy/cupy 스위치). 커널별 CPU 결과와 수치 일치 테스트(허용 오차 명시).

### 6.3 벤치마크 프로토콜

1. 사이트: S(합성 + 실데이터 2 burst × 30일), M(1 프레임 × 60일), L(2 프레임 × 120일). 국내 사이트 1곳은 대조군(수준측량/GNSS CSV) 포함.
2. 기준선(before): 기존 도구를 문서 기본값으로 실행(HyP3 + MintPy 기본 템플릿, ISCE2 topsStack 순차 실행 + SNAPHU 단일 타일).
3. 측정 항목: 단계별 wall time, peak RSS, 디스크 피크, 네트워크 바이트, 언래핑 오류 지표(폐합 RMS, 경계 단차 수), 대조군 RMSE. 3회 반복 중앙값.
4. 산출: `bench_result.json` → `wintersar bench --compare`로 표·플롯. README에는 표 링크만.
5. CI: S 합성 사이트를 매 PR에서 실행, 기준선 대비 15% 이상 느려지면 실패.

---

## 7. 단계별 작업 계획 (Phase / DoD)

각 Phase는 1개 이상의 PR로 구성한다. PR 설명에 해당 Phase의 DoD 체크리스트를 복사해 체크한다. 기간은 추정치이며 DoD가 기준이다.

### Phase 0 — 스캐폴딩 (PERF-12)
- 저장소 구조(4.2), `pyproject.toml`, `pixi.toml`(isce2·snaphu-py·tophu·mintpy·asf_search·hyp3_sdk·sentineleof·sardem·rasterio·xarray·zarr·h5py·typer·pydantic), ruff/mypy/pytest 설정, pre-commit.
- `wintersar --help`, `wintersar check-install`(엔진 설치·버전·인증 상태).
- 합성 데이터 생성기 최소 버전(`research/synth.py`)과 "fake engine"(합성 간섭도 반환) — 네트워크 없는 통합 테스트용.
- Dockerfile, CI(lint + unit + 합성 통합).
- 문서 골격(mkdocs, ko/en), ADR 템플릿.
- **DoD**: CI 녹색, Docker 빌드, `check-install`이 미설치 엔진을 Finding으로 보고, 합성 파이프라인 end-to-end 테스트 통과.

### Phase 1 — select: 검색·선별·사전검증 (R-01~04, PERF-01, PERF-13)
- `asf_search` 연동(burst 우선, SLC fallback), `BurstRecord` 정규화, 메타데이터 출처 문서화.
- 그룹핑·커버리지·네트워크·참조일 추천, 수직 기선(stack API → 없으면 자체 계산).
- 규칙 SEL-01~13, looks 자동 산출, 레이오버·셰도우 마스크, 리포트(md/html/json).
- **DoD**: 실제 AOI 3곳(도시 침하·산지 사면·해안)에서 후보 스택 산출 및 FAIL/WARN이 의미 있게 나옴. 규칙마다 단위테스트(양성·음성). looks 테스트 고정값 통과. 마스크는 상승/하강 결과가 지형에 맞게 다르게 나옴(수동 검수 1회 + 골든 파일).

### Phase 2 — engines + pipeline + unwrap 스케줄러 (R-05, R-08, PERF-02/03/04/07/08/09)
- 순서: ① hyp3 어댑터(가장 빨리 결과) ② mintpy 어댑터 ③ DAG·캐시·`plan` ④ snaphu-py/tophu 어댑터 + 스케줄러 ⑤ isce2_topsstack 어댑터 + run_files 병렬 실행기 ⑥ Zarr/COG IO ⑦ 보조 데이터 캐시.
- **DoD**: S 사이트를 HyP3 경로로 설치 후 1시간 내 속도 지도 산출. 동일 사이트를 ISCE2 경로로 재현하고 두 결과의 속도 차이 통계 기록. 파라미터 1개 변경 시 변경 단계 하류만 재실행됨을 테스트로 확인. 언래핑 스케줄러가 메모리 예산 초과 입력을 자동 타일링하고 OOM 없이 완료. bench S 기준선 수치 기록.

### Phase 3 — diagnose (R-02, R-14)
- 로그 파서(isce2, snaphu, mintpy, hyp3, asf_search), KB 시드 12개 이상, 리소스 추정 모델, `run` 실패 시 자동 첨부, `docs/kb/` 렌더링.
- **DoD**: 알려진 실패 케이스 10개(로그 픽스처)에서 정확한 KB 매칭. 매칭 실패 시 "미분류" Finding과 로그 발췌를 남김.

### Phase 4 — validate (R-09, R-10, R-11)
- 기준점 추천, 폐합 대시보드 데이터, 대조군 CSV 임포트·LOS 투영·지표·리포트, 파라미터 스윕.
- 국토지리정보원 수준점/GNSS 접근 방식 조사 → 가능하면 어댑터, 아니면 CSV 변환 가이드.
- **DoD**: 국내 사이트 1곳에서 대조군 RMSE/bias 리포트 생성. LOS 투영 부호 테스트(상승/하강) 통과. 스윕 8개 조합이 캐시 덕분에 전량 재실행 시간의 절반 이하로 완료(측정 기록).

### Phase 5 — 성능 (PERF-05/06/10/11)
- dolphin 어댑터 + MintPy 경로와 출력 정규화 → A/B(R-15). 증분 업데이트 모드. CuPy 백엔드(필터·마스크). 참조 기하 캐시 검증 또는 COMPASS 평가.
- **DoD**: PERF 표의 각 항목에 before/after 측정치(6.3 프로토콜). 채택/기각을 ADR로 기록. 기각된 최적화는 코드에서 제거하거나 실험 플래그 뒤로 이동.

### Phase 6 — research (R-07, R-15)
- 합성 생성기 완성, 대표위상 5개 방법, 스티칭 2개 방법, 지표, 실험 YAML, 보고서.
- **DoD**: 합성 3종 + 실데이터 2 사이트에서 방법별 지표 표. 기준선(복소 멀티룩 + coarse_ref)보다 나은 조합이 있으면 `unwrap` 스케줄러 옵션으로 승격(기본값 변경은 ADR).

### Phase 7 — QGIS 플러그인 (R-12)
- 패널 6종, Processing Provider, 플러그인 패키징(zip), 설치 문서.
- **DoD**: 플러그인만으로 검색 → 사전검증 → 실행(HyP3 경로) → 결과 로드 → 기준점 선택 → 검증 리포트까지 완료. QGIS LTR 버전 2종에서 동작 확인.

### Phase 8 — 릴리스 v0.1
- 튜토리얼(ko/en) 3종(HyP3 빠른 시작, ISCE2 로컬, 검증·튠), KB 정리, 라이선스 표 확정(9장), 릴리스 노트, 벤치마크 표 링크.
- 이후 백로그: KOMPSAT-5 리더 경로, GACOS, spurt 3D 언래핑 기본 옵션화, CDSE 데이터 소스.

---

## 8. 테스트 전략

| 층 | 대상 | 데이터 | 실행 |
|---|---|---|---|
| unit | 규칙(SEL), looks, 기하 마스크, LOS 투영, 스키마, 해시, KB 매칭 | 고정값·합성 | 매 PR |
| integration | fake engine으로 전체 DAG, 캐시 무효화, 스케줄러 타일 결정 | 합성 | 매 PR |
| network (`-m network`) | asf_search 소규모 쿼리, HyP3 제출은 mock + 월 1회 실제 | 실데이터 소량 | 수동/야간 |
| regression | 골든 산출물(속도 지도 통계, Finding 목록) | S 사이트 | 야간 |
| perf | bench S 합성 | 합성 | 매 PR(15% 회귀 시 실패) |

- 수치 비교는 허용 오차를 명시(`np.testing.assert_allclose`). GPU 커널은 CPU 결과와 일치 테스트.
- 로그 픽스처는 `tests/fixtures/logs/`에 실제 로그 발췌로 저장(개인정보·경로 마스킹).

---

## 9. 라이선스·법적 확인 사항 (Phase 0에서 표 작성, Phase 8에서 확정)

| 구성요소 | 알려진 라이선스 | 취급 |
|---|---|---|
| ISCE2 | Apache-2.0(EAR99 수출 분류 고지 포함) | subprocess 호출. 배포 시 고지 문구 포함 |
| SNAPHU(C 코어) | Debian에서 non-free로 분류. Stanford 라이선스 조건 확인 필요 | 번들 금지. 사용자 설치(conda-forge snaphu-py) 후 어댑터가 감지. 상용 이용 조건은 법률 검토 |
| snaphu-py, tophu | 확인 필요(tophu는 BSD-3 OR Apache-2.0으로 표기됨) | import 가능 후보 |
| MintPy | GPL-3 | **import 금지**, subprocess만. 결과 HDF5는 h5py로 직접 읽음 |
| dolphin, spurt, COMPASS, isce3 | 확인 필요 | 확인 후 import/subprocess 결정 |
| GMTSAR | GPL-3 | 사용 시 subprocess |
| LiCSBAS | 확인 필요 | 알고리즘 참고만, 코드 복사 금지 |
| InSAR.dev 코어 | source-available(구독) | 의존·코드 참조 금지 |
| asf_search, hyp3_sdk, sentineleof, sardem | 확인 필요(관용적 라이선스로 알려짐) | import 가능 후보 |
| Copernicus Sentinel 데이터 | 무료·개방(귀속 표기) | 산출물에 출처 표기 |
| 국토지리정보원 자료 | 자료별 이용 조건 확인 필요 | 재배포 금지 가능성 → 사용자 로컬 파일로만 취급 |

- 본 프로젝트 라이선스: Apache-2.0 제안(GPL 구성요소는 subprocess 경계로 분리). 최종 결정은 ADR-0001.

---

## 10. 리스크와 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| ISCE2 설치·버전 충돌 | Phase 2 지연 | conda-forge 핀 + pixi 락 + Docker. HyP3 경로를 먼저 완성해 사용자 가치 선확보 |
| HyP3 크레딧 한도·정책 변경 | 실행 실패 | `plan`에서 견적, KB-HYP3-001, 로컬 경로 fallback |
| SNAPHU 라이선스 | 배포 제약 | 번들 금지, 대체 언래퍼(isce3 PHASS/ICU, spurt) 어댑터 준비 |
| 국내 대조군 데이터 접근 | Phase 4 DoD 미달 | CSV 스키마를 필수로 두고 어댑터는 선택. 연구자 보유 수준측량 자료로 우선 검증 |
| 성능 개선이 가설대로 안 나옴 | PERF 항목 기각 | 6.3 프로토콜로 조기 측정, 기각은 정상 결과로 기록 |
| 순차 추정기 A/B에서 정확도 저하 확인 | PERF-05 기각 | 기본값 MintPy 유지, dolphin은 옵션 |
| 대용량 디스크 | 로컬 처리 실패 | PERF-07 정리 정책, `plan` 디스크 견적 |
| 도메인 오류(부호·기하 규약) | 잘못된 결과 | LOS·looks·마스크 단위테스트 고정, 연구자 검수 체크포인트(Phase 1, 4, 6) |

---

## 11. Claude Code 작업 지침 (불변 규칙)

1. Python 3.11+, 타입 힌트 필수, `ruff` + `mypy --strict`(엔진 어댑터 경계는 예외 허용), `pytest`.
2. 외부 엔진은 subprocess 어댑터로만 호출한다. GPL 코드를 import하지 않는다(9장).
3. 절대 하지 말 것: SNAPHU/MCF·SBAS 역산 코어 재구현, URL·API 인자·크레딧 수치 추측(문서 확인 후 출처 주석), 테스트 없는 알고리즘 변경, 대용량 데이터 커밋(픽스처는 수 MB 이하, 그 이상은 다운로드 스크립트).
4. 네트워크가 필요한 테스트는 `@pytest.mark.network`로 분리. 기본 CI는 합성 데이터만.
5. 각 Phase는 PR 단위, PR마다 DoD 체크리스트. 커밋 메시지에 관련 ID(R-/SEL-/PERF-/KB-) 명시.
6. 사용자에게 보이는 문구는 전부 `i18n/ko.yaml`, `en.yaml`에 두고 코드에 하드코딩하지 않는다. 진단 문구는 "원인 → 조치" 순서.
7. 설계 결정은 `docs/adr/NNNN-제목.md`(맥락·선택지·결정·결과). "확인 필요" 항목의 검증 결과도 ADR로.
8. 성능 수치는 `bench_result.json` 없이는 어디에도 쓰지 않는다.
9. 미확정 사항은 `docs/open-questions.md` 표(항목·담당·기한)에 남기고 진행을 막지 않는다.
10. 도메인 검수 체크포인트(Phase 1 규칙표, Phase 4 부호 규약, Phase 6 실험 설계)에서는 연구자 확인을 요청하는 PR 코멘트를 남기고, 확인 전에는 기본값을 변경하지 않는다.
11. 로그·리포트에 개인 경로·토큰이 남지 않도록 마스킹 유틸을 공용으로 사용한다.

---

## 12. 부록

### 12.1 도메인 개념 정리 (온보딩 문서 초안, `docs/concepts/`)

- **relative orbit(트랙)**: Sentinel-1은 12일 주기에 175개의 relative orbit을 가진다. 간섭은 같은 relative orbit(같은 관측 기하) 사이에서만 성립한다. "같은 하강궤도"라는 표현은 트랙 번호가 같다는 뜻이어야 한다. 몇 아크도 같은 휴리스틱은 트랙 번호·burst ID 검사로 대체된다.
- **burst / Full Burst ID**: IW 모드는 sub-swath 3개, 각각 burst 단위로 촬영된다. burst의 지리적 위치는 궤도마다 거의 동일해 같은 ID는 같은 지역을 의미한다. IW SLC 프레임 경계는 날짜마다 달라 프레임 단위 겹침은 보장되지 않는다.
- **픽셀 간격과 looks**: IW SLC는 레인지 약 2.3 m × 애지머스 약 14 m(해상도 약 5 × 20 m)로 비대칭이다. 이는 결함이 아니라 설계이며, 레인지 방향 looks를 더 주어(예: 4:1, 5:1) 정사각 근사 픽셀을 만든다.
- **레이오버·foreshortening·셰도우**: 측면 관측 레이더의 거리 기하 왜곡. 레이더를 향한 사면 경사가 입사각보다 급하면 산 정상이 산기슭보다 먼저 되돌아와 순서가 뒤집힌다(레이오버). 완만하면 압축(foreshortening), 반대 사면이 너무 급하면 신호가 닿지 않는다(셰도우). 도플러 효과로 설명하는 것은 부정확하다. 궤도 방향(상승/하강)에 따라 왜곡되는 사면이 달라진다.
- **TOPS 정합**: 애지머스 스위핑 때문에 약 1/1000 픽셀의 애지머스 정합 정확도가 필요하다. 정밀궤도 + DEM 기반 기하 정합 후 ESD(burst 오버랩 구간 위상차)로 미세 보정하는 것이 표준이다. 픽셀을 회전시켜 맞추는 방식이 아니다. 정합이 abort 나는 주원인은 다른 트랙·burst 불일치·궤도 파일 누락이다.
- **언래핑 타일·다중해상도**: 큰 간섭도는 타일로 나눠 독립 언래핑 후 재조립한다(SNAPHU tile 모드: 픽셀 단위 오버랩, 신뢰 영역 분할, 보조 네트워크). 다중해상도(tophu, SARscape decomposition level)는 멀티룩한 저해상도 위상을 먼저 언래핑하고 타일마다 2π 사이클을 가감해 경계 단차를 없앤다. 여기서 저해상도 "대표위상"의 품질이 결과를 좌우한다(연구 모듈 R-07).
- **기준점·재평탄화**: 언래핑 위상은 상대값이므로 안정적이고 코히어런스가 높고 AOI와 표고가 비슷한 기준점을 잡아야 한다. SARscape의 refinement/re-flattening(GCP로 오프셋·램프 제거)에 대응하는 것이 MintPy의 reference point + deramp이다.
- **loop closure(위상 폐합)**: 세 날짜의 간섭도를 순환 합산하면 이론상 0이어야 한다. 잔차가 큰 간섭도는 언래핑 오류가 의심되므로 자동 제외·보정한다.
- **순차 추정기·compressed SLC**: 스택을 미니스택으로 나눠 공분산을 재귀 추정하고 압축 SLC로 과거 배치와 최신 취득을 연결한다. 문헌은 정확도 손실이 작다고 보고하지만 연구자 경험은 다르므로 A/B(R-15)로 판단한다.

### 12.2 참고 자료 (구현 시 접속 확인)

- MintPy: https://mintpy.readthedocs.io/ , https://github.com/insarlab/MintPy
- SNAPHU 매뉴얼: https://manpages.ubuntu.com/manpages/bionic/man1/snaphu.1.html , 릴리스 노트: https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/README_releasenotes.txt
- snaphu-py: https://github.com/isce-framework/snaphu-py
- tophu: https://github.com/isce-framework/tophu , https://tophu.readthedocs.io/
- dolphin: https://github.com/isce-framework/dolphin
- ISCE2: https://github.com/isce-framework/isce2
- ASF HyP3 InSAR 가이드: https://hyp3-docs.asf.alaska.edu/guides/insar_product_guide/ , Burst InSAR: https://hyp3-docs.asf.alaska.edu/guides/burst_insar_product_guide/
- ASF 검색 API 키워드: https://docs.asf.alaska.edu/api/keywords/
- Sentinel-1 Burst ID Map: https://www.earthdata.nasa.gov/data/catalog/asf-sentinel-1-burst-map-1
- Sentinel-1 SLC 사양(ESA): https://sentinel.esa.int/web/sentinel/user-guides/sentinel-1-sar/resolutions/level-1-single-look-complex
- SARscape SBAS 튜토리얼(파라미터 대응 참고): https://www.sarmap.ch/tutorials/SBAS_Tutorial_562.pdf , E-SBAS: https://www.sarmap.ch/tutorials/SBAS_ESBAS_Tutorial_610.pdf
- EZ-InSAR 3 논문: https://link.springer.com/article/10.1007/s12145-026-02115-9
- InSAR.dev(라이선스 확인용): https://insar.dev/
- Sequential Estimator(Ansari 외, 2017): https://ieeexplore.ieee.org/document/8024151/
- 근실시간 순차 phase linking(arXiv 2511.12051): https://arxiv.org/pdf/2511.12051
- TOPS 정합(기하 + ESD) 논문: https://doi.org/10.3390/rs10091405
- TOPS GPU 가속 논문: https://www.sciencedirect.com/science/article/abs/pii/S0098300418311658
- DL 언래핑 벤치마크(arXiv 2605.00896): https://arxiv.org/html/2605.00896 , O'Grady 2025 박사논문: https://etheses.whiterose.ac.uk/id/eprint/38958/
- 아래는 문서 작성 시 직접 확인하지 못한 저장소이므로 구현 시 존재·라이선스를 확인: spurt, COMPASS, s1-reader(isce-framework), burst2safe·hyp3-sdk(ASFHyP3), sentineleof·sardem(scottstanie), LiCSBAS(yumorishita), asf_search(asfadmin)

### 12.3 미확정 사항 초기 목록 (`docs/open-questions.md` 시드)

| 항목 | 확인 방법 | 영향 |
|---|---|---|
| asf_search burst 제품에서 픽셀 간격·IPF 버전·burst ID 필드 이름과 가용 시점 | 라이브러리 문서·실제 응답 검사 | SEL-08/09 구현 |
| asf_search stack API의 Sentinel-1 수직 기선 계산 가능 여부 | 실제 호출 + 알려진 값 비교 | SEL-06 |
| HyP3 크레딧 정책·작업별 비용 | HyP3 문서 | plan 견적 |
| SNAPHU 라이선스 조건, snaphu-py/dolphin/spurt 라이선스 | LICENSE 파일 | 9장 |
| topsStack 참조 기하 재사용 가능성 | 소스 코드 확인 | PERF-11 |
| 국토지리정보원 수준점·GNSS 데이터 접근 방식과 이용 조건 | 기관 안내 | Phase 4 어댑터 |
| MintPy enu2los 부호·헤딩 규약 | 소스 코드 확인 | validate.los |
| SNAPHU 메모리 상수(100 MB/백만 픽셀) 실측치 | bench S | unwrap 스케줄러 |
| 타일 오버랩 기본값(25% vs 30% vs 200 px) | bench + 연구 모듈 지표 | unwrap 기본값 |
