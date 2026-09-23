# 튜토리얼 2 — ISCE2 로컬 경로 (search → precheck → plan → run → diagnose → validate → refpoint → sweep)

목표: 같은 사이트를 **로컬 ISCE2 topsStack** 으로 처리해 파라미터(looks, 필터, ESD, 언래퍼, 타일)를 완전히
통제하고, HyP3 결과와의 차이 통계를 기록합니다(Phase 2 DoD). 장점은 통제력, 대가는 설치·디스크·시간입니다.

> **상태**: `engines/isce2_topsstack.py` 어댑터는 트리에 있고 `engine.interferogram: isce2_topsstack` 으로
> 등록되어 있습니다(Phase 2 ⑤, [ADR-0026](../adr/0026-isce2-topsstack-flags-and-run-files.md) ·
> [ADR-0027](../adr/0027-topsstack-parallel-safety-and-cleanup.md)). 다만 개발 환경에는 ISCE2 가 설치되어
> 있지 않아 플래그·`run_files` 이름·업데이트 모드는 상류 소스로만 검증했습니다 — 실제 설치 환경에서의 실행
> 재확인은 open-questions #46 입니다. 어댑터가 없으면 `wintersar check-install` 이 `ENV-001` 로 알려 줍니다.
> 성능 수치는 `bench_result.json` 없이는 적지 않습니다(규칙 11.8). 각 단계의 **지금 바로(합성)** 명령은
> [HyP3 튜토리얼](hyp3-quickstart.md)과 같으므로 여기서는 ISCE2 경로에서 달라지는 부분만 적습니다.

## 0. 준비물

| 항목 | 비고 |
|---|---|
| ISCE2 (`stackSentinel.py` 포함 topsStack) | conda-forge `isce2` — [설치 안내](../install.md)의 pixi `engines` 환경(linux-64) 또는 `Dockerfile.engines` 이미지. macOS 의 `engines-portable` 에는 ISCE2·tophu 가 없습니다. Apache-2.0 + EAR99 고지(플랜 §9, [ADR-0001](../adr/0001-license-and-engine-boundaries.md)). `PATH` 에 `topsStack` 이 하나만 있어야 하며, `check-install` 이 `ENV-001` 을 내면 조치 문구의 `export` 를 따릅니다 |
| 언래퍼 | `snaphu-py`(conda-forge `snaphu`; SNAPHU C 코어는 번들하지 않음) 또는 `tophu`(conda-forge, isce3 의존) — [ADR-0023](../adr/0023-snaphu-py-api-and-subprocess-config.md)/[ADR-0024](../adr/0024-tophu-api-facts.md) |
| MintPy | conda-forge `mintpy`, subprocess 전용 |
| 궤도·DEM | `sentineleof`(POEORB/RESORB), `sardem`(Copernicus GLO-30) — 콘텐츠 주소 캐시 `~/.cache/wintersar` (PERF-02, [ADR-0018](../adr/0018-dem-source-and-cache.md)/[ADR-0022](../adr/0022-aux-data-cache.md)). 둘 다 의존성 정책 예외 대기(open-questions #10) |
| Earthdata Login | burst/SLC 다운로드에 필요 (`.netrc` 우선, [ADR-0013](../adr/0013-earthdata-auth-policy.md)) |
| 디스크 | burst·날짜별 중간 파일이 많습니다. `engine.cleanup: stage|aggressive` 로 단계별 정리(PERF-07) |

```bash
wintersar check-install --engine isce2_topsstack --engine snaphu --engine tophu --engine mintpy
```

예상 출력(모양): 엔진 표에 `isce2_topsstack | - | >=2.6,<3 | 미설치 | fetch, coregister, interferogram, multilook`
행이 있고, 미설치 엔진마다 `ENV-001` 행이 "설치 방법: conda install -c conda-forge isce2; export
ISCE_STACK=…" 식의 조치와 함께 나옵니다. 설치 후에는 상태가 `사용 가능` 으로 바뀌고 버전 열이 채워집니다.

## 1. 설정

어댑터 전용 옵션은 `engine.isce2:` 아래에 둡니다(`EngineCfg.isce2`, `pipeline/config.py`). 아래 값은 모두
어댑터가 실제로 읽는 키이며, 주석의 기본값은 `engines/isce2_topsstack.py` 의 기본값입니다.

```yaml
data:
  product: burst              # burst GeoTIFF → burst2safe 로 SAFE 재구성 → topsStack 입력 (플랜 §5.2)
engine:
  interferogram: isce2_topsstack
  looks: auto                 # SEL-09 의 자동 looks (예: [10, 3]); 직접 지정 가능
  filter: { type: goldstein, alpha: 0.6, window: 64 }   # alpha → stackSentinel --filter_strength
  esd: true                   # true → -C NESD, false → -C geometry (TOPS 정합 개념 참고)
  cleanup: stage              # none | stage | aggressive (PERF-07, ADR-0027)
  isce2:
    slc_dir: /data/SLC        # 이미 받아 둔 SAFE 디렉터리. 비우면 burst2safe 로 <workdir>/SLC 에 재구성
    orbit_dir: /data/orbits   # 기본 <workdir>/orbits (비어 있으면 ISCE2-014 INFO)
    aux_dir: /data/aux_cal    # 기본 <workdir>/aux_cal
    dem: /data/dem/glo30.dem  # 없으면 sardem 캐시에서 해결 (ADR-0018)
    bbox: [35.0, 35.5, 128.0, 128.6]   # S, N, W, E. 없으면 AOI WKT → ISCE2-006 WARN
    reference_date: "2023-01-05"       # 없으면 stack.json 의 참조 날짜
    num_connections: 4        # 기본: 네트워크 설정에서 산출 (ISCE2-013 INFO 로 표시)
    num_overlap_connections: 3
    esd_coherence_threshold: 0.85
    unwrap_in_isce: false     # true 면 run_files 의 unwrap 단계까지 ISCE2 가 수행
    workflow: interferogram   # stackSentinel -W
    max_parallel_per_step: { run_01_unpack_topo_reference: 1 }   # 단계별 동시 실행 상한 (ADR-0027)
    retries: 1                # 실패 job 재시도 횟수
    regenerate_run_files: false   # true 면 업데이트 모드를 무시하고 run_files 재생성
unwrap:
  method: auto                # snaphu | tophu | spurt | auto (스케줄러가 결정, ADR-0045)
  cost: defo
  coherence_threshold: 0.3
  mask: { water: true, layover: true, coherence: true }
  tiles: auto                 # 또는 { rows: 2, cols: 2, overlap: 0.25, min_overlap_px: 200 }
  memory_mb_per_mpixel: 100   # SNAPHU man page 초기값; bench 로 재적합 (open-questions #8)
timeseries:
  engine: mintpy
compute:
  cores: auto
  memory_gb: auto
```

## 2. 검색과 사전검증은 동일

```bash
wintersar search   --config config.yaml
wintersar precheck work/select/candidates.json --config config.yaml
```

ISCE2 경로에서는 `SEL-11`(정밀궤도 가용성)과 `SEL-12`(레이오버·셰도우) 가 특히 중요합니다. topsStack 의
`geom_reference/IW*/shadowMask_*.rdr` 산출물이 있으면 자체 마스크보다 우선 사용하고 비교합니다
([ADR-0019](../adr/0019-isce2-shadow-layover-comparison.md)). 리포트의 모양은 HyP3 튜토리얼 4 절과 같습니다.

## 3. 계획과 실행

```bash
wintersar plan --config config.yaml
wintersar run  --config config.yaml --until unwrap     # 정합·간섭도·멀티룩·언래핑까지
```

`plan` 표에서 `fetch · coregister · interferogram · multilook` 네 행의 엔진이 `isce2_topsstack`, `unwrap` 행이
`snaphu|tophu`(스케줄러 결정), `timeseries · corrections · geocode` 가 `mintpy` 로 나오면 설정이 맞은 것입니다.
크레딧 열은 "없음" 입니다(로컬 경로).

어댑터가 하는 일: (burst 제품이면) `burst2safe` 로 SAFE 재구성 → `stackSentinel.py` 인자 생성(bbox, looks,
네트워크 옵션, ESD, 언래퍼) → `run_files` 를 **단계 순서는 지키고 단계 안에서만 병렬** 실행(PERF-07,
`engines/runfiles.py`) → 산출물을 MintPy `prep_isce` 규약(`igrams_manifest.json`)으로 정리. 이미 끝난
`run_file` 은 건너뛰고(`coreg_secondarys/` 기준 업데이트 모드, PERF-11 ·
[ADR-0029](../adr/0029-reference-geometry-reuse-and-dolphin-normalisation.md)), 실패 job 은
`engine.isce2.retries` 만큼 재시도한 뒤 `diagnose` 로 넘깁니다.

### 언래핑 스케줄러

언래핑은 wintersar 스케줄러가 맡습니다([개념: 타일과 다중해상도](../concepts/unwrap-tiling-multiresolution.md)).
실행 전에 전략과 이유를 먼저 봅니다 — 이 명령은 엔진 없이도 지금 바로 돕니다:

```bash
wintersar unwrap plan --shape 4000 6000 --n 30 --memory-gb 32 --cores 8
wintersar unwrap plan --shape 4000 6000 --n 30 --memory-gb 32 --cores 8 --method tophu --tiles 2x2
```

예상 출력(모양):

```text
언래핑 실행 계획
머신: CPU <n>코어 · 메모리 예산 <n> GB
│ 방법                       │ snaphu    │
│ 타일 (행×열)               │ 1x1       │
│ 타일 오버랩 (px)           │ 0         │
│ 타일 크기 (px)             │ 4000x6000 │
│ 동시 실행 간섭도 수        │ <n>       │
│ 간섭도당 타일 프로세스 수  │ 1         │
│ 단일 타일 예상 메모리 (MB) │ …         │
│ 메모리 예산 (MB)           │ …         │
결정 근거
  • 단일 타일 예상 메모리 … ≤ 예산 … → 타일 분할 없음.
  • 동시 실행 간섭도 수 <n> = floor(코어 / 간섭도당 프로세스) (코어 제한).
  • 사용 가능한 언래핑 백엔드가 없습니다. 'snaphu'로 계획하되 실행 시 ENV-001이 보고됩니다.
```

메모리 예산을 넘는 입력은 "타일 분할" 근거와 함께 `rows x cols` 와 오버랩 픽셀이 채워집니다
([ADR-0045](../adr/0045-unwrap-scheduler-strategy-rules.md), [ADR-0046](../adr/0046-unwrap-tile-overlap-default.md),
[ADR-0047](../adr/0047-unwrap-parallelisation-order.md)). 스택 단위 수동 실행:

```bash
wintersar unwrap run work/multilook/<hash>/out/igrams.npz --out work/unwrap_manual --method auto
```

언래퍼가 설치되어 있지 않으면 `ENV-001` + `UNW-001`(FAIL) 두 행이 나오고 아무것도 쓰지 않습니다 — 조치 문구의
설치 명령을 따르거나 `unwrap.method` 를 설치된 엔진으로 바꿉니다.

## 4. 실패 진단

```bash
wintersar diagnose work/ --engine isce2                 # 작업 디렉터리 전체
wintersar diagnose work/coregister/<hash>/logs          # 실패한 노드 하나 (ADR-0032)
```

| KB | 원인 요지 |
|---|---|
| `KB-ISCE2-001` | 참조·보조 사이 공통 burst 없음 ("No common bursts found …") |
| `KB-ISCE2-002` | ESD 저코히어런스 — `esd_coherence_threshold` 완화 또는 `esd: false`(`-C geometry`) |
| `KB-ISCE2-003` | 취득 시각을 덮는 궤도 파일 없음 |
| `KB-ISCE2-004` | DEM 이 처리 영역을 덮지 못함 |
| `KB-ISCE2-005` | 기간·bbox 를 만족하는 SAFE 없음 |
| `KB-SNAPHU-001/002/003` | 타일 조립 한도 초과 / 메모리 부족 / 타일 파라미터 오류 — 조치에 `--assemble`, `TILECOSTTHRESH`, `MINREGIONSIZE` |
| `KB-ENV-002` | 프로세스 OOM — 재시도 힌트 `tile` (스케줄러가 타일을 늘려 재시도) |

지금 바로(합성): HyP3 튜토리얼 7 절의 `--set unwrap.fail_stage=unwrap` 주입과 `KB-UNKNOWN` 출력이 그대로 적용됩니다.

## 5. 시계열·검증·기준점·스윕

```bash
wintersar run --config config.yaml --from timeseries
wintersar validate --ts work/timeseries/<hash>/out/timeseries.h5 --leveling data/leveling.csv --out work/validate
wintersar refpoint --ts work/timeseries/<hash>/out/timeseries.h5 --aoi aoi.geojson --top 5 --out work/refpoint.json
wintersar sweep --config config.yaml --grid sweep.yaml --leveling data/leveling.csv --out work/sweep
```

ISCE2 경로의 스윕은 `engine.filter.alpha`, `engine.esd`, `unwrap.coherence_threshold`, `unwrap.tiles` 처럼
HyP3 에서는 만질 수 없는 키를 격자에 넣을 수 있습니다. 바뀐 섹션이 `engine` 이면 `fetch` 는 캐시에서 오고
`coregister` 부터 다시 돕니다. 절차·지표는 [검증·튠 튜토리얼](validate-tune.md).

HyP3 결과와 같은 AOI·기간이면 두 속도 지도의 차이 통계를 `docs/research/` 에 기록합니다(Phase 2 DoD). 수치는
`bench_result.json` 을 근거로만 씁니다:

```bash
wintersar bench --site benchmarks/sites/S.yaml --allow-network --out bench_result.json   # S.yaml 의 <PLACEHOLDER> 를 채운 뒤 (BENCH-004)
wintersar bench --site benchmarks/sites/S_synthetic.yaml --out bench_synthetic.json   # 엔진 없이 지금 바로
```

## English summary

The local ISCE2 topsStack path gives full control over looks, filtering, ESD, the unwrapper and
tiling at the cost of installation, disk and time. It needs conda-forge `isce2` (`ISCE_STACK` and a single
`topsStack` on `PATH`; see the install guide), `snaphu` (snaphu-py) or `tophu`, `mintpy` (subprocess only), plus
`sentineleof`/`sardem` for orbits and DEM cached under `~/.cache/wintersar`. Selection is identical to the HyP3
path; the adapter (`engines/isce2_topsstack.py`, registered as `isce2_topsstack` and covering `fetch`,
`coregister`, `interferogram`, `multilook`) builds `stackSentinel.py` arguments from the `engine.isce2:` keys
(`slc_dir`, `orbit_dir`, `aux_dir`, `dem`, `bbox`, `reference_date`, `num_connections`,
`esd_coherence_threshold`, `unwrap_in_isce`, `max_parallel_per_step`, `retries`, ...), runs the `run_files`
with intra-step parallelism and hands the products to MintPy via `prep_isce`. ISCE2 is not installed in the
development environment, so the flags were verified against upstream sources only (ADR-0026/0027,
open-questions #46). Unwrapping is scheduled by wintersar: `wintersar unwrap plan --shape 4000 6000 --n 30
--memory-gb 32 --cores 8` prints the strategy and its reasons without any engine, `wintersar unwrap run`
executes it (or reports `ENV-001` + `UNW-001` when no unwrapper is installed). Failures are explained by
`wintersar diagnose work/ --engine isce2` through `KB-ISCE2-00x` / `KB-SNAPHU-00x` / `KB-ENV-002`; the
synthetic failure injection from the HyP3 tutorial applies unchanged. Finish with `run --from timeseries`,
`validate`, `refpoint` and `sweep`; numbers only from `bench_result.json`.
