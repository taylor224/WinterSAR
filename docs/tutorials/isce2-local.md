# 튜토리얼 2 — ISCE2 로컬 경로

목표: 같은 사이트를 **로컬 ISCE2 topsStack** 으로 처리해 파라미터(looks, 필터, ESD, 언래퍼, 타일)를 완전히
통제하고, HyP3 결과와의 속도 차이 통계를 기록합니다(Phase 2 DoD). 장점은 통제력, 대가는 설치·디스크·시간입니다.

> **상태**: `engines/isce2_topsstack.py` 어댑터는 트리에 있고 `engine.interferogram: isce2_topsstack` 으로
> 등록되어 있습니다(Phase 2 ⑤, [ADR-0026](../adr/0026-isce2-topsstack-flags-and-run-files.md) ·
> [ADR-0027](../adr/0027-topsstack-parallel-safety-and-cleanup.md)). 다만 이 환경에는 ISCE2 가 설치되어
> 있지 않아 플래그·`run_files` 이름·업데이트 모드는 상류 소스로만 검증했습니다 — 실제 설치 환경에서의 실행
> 재확인은 open-questions #46 입니다. 어댑터가 없으면 `wintersar check-install` 이 `ENV-001` 로 알려 줍니다.
> 성능 수치는 `bench_result.json` 없이는 적지 않습니다.

## 0. 준비물

| 항목 | 비고 |
|---|---|
| ISCE2 (`stackSentinel.py` 포함 topsStack) | conda-forge `isce2`. Apache-2.0 + EAR99 고지(플랜 §9). 설치 확인: `wintersar check-install` 의 `isce2` 행 |
| 언래퍼 | `snaphu-py`(conda-forge `snaphu`; SNAPHU C 코어는 번들하지 않음) 또는 `tophu`(conda-forge, isce3 의존) — ADR-0023/0024 |
| MintPy | conda-forge `mintpy`, subprocess 전용 |
| 궤도·DEM | `sentineleof`(POEORB/RESORB), `sardem`(Copernicus GLO-30) — 콘텐츠 주소 캐시 `~/.cache/wintersar` (PERF-02, ADR-0018/0022) |
| 디스크 | burst·날짜별 중간 파일이 많습니다. `engine.cleanup: stage|aggressive` 로 단계별 정리(PERF-07) |

```bash
wintersar check-install --engine isce2 --engine snaphu --engine tophu --engine mintpy
```

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

## 2. 선별은 동일

```bash
wintersar search   --config config.yaml
wintersar precheck work/select/candidates.json --config config.yaml
```

ISCE2 경로에서는 `SEL-11`(정밀궤도 가용성)과 `SEL-12`(레이오버·셰도우) 가 특히 중요합니다. topsStack 의
`geom_reference/IW*/shadowMask_*.rdr` 산출물이 있으면 자체 마스크보다 우선 사용하고 비교합니다(ADR-0019).

## 3. 실행

```bash
wintersar plan --config config.yaml
wintersar run  --config config.yaml --until unwrap     # 정합·간섭도·멀티룩·언래핑까지
```

어댑터가 맡는 단계는 `fetch`, `coregister`, `interferogram`, `multilook` 입니다. 하는 일: (burst 제품이면)
`burst2safe` 로 SAFE 재구성 → `stackSentinel.py` 인자 생성(bbox, looks, 네트워크 옵션, ESD, 언래퍼) →
`run_files` 를 **단계 순서는 지키고 단계 안에서만 병렬** 실행(PERF-07, `engines/runfiles.py`) → 산출물을
MintPy `prep_isce` 규약(`igrams_manifest.json`)으로 정리. 이미 끝난 `run_file` 은 건너뛰고
(`coreg_secondarys/` 기준 업데이트 모드, PERF-11 · ADR-0029), 실패 job 은 `engine.isce2.retries` 만큼
재시도한 뒤 `diagnose` 로 넘깁니다.

언래핑은 wintersar 스케줄러가 맡습니다([개념: 타일과 다중해상도](../concepts/unwrap-tiling-multiresolution.md)):

```bash
wintersar unwrap plan --shape 4000 6000 --n 30 --memory-gb 32 --cores 8   # 전략과 이유를 먼저 확인
wintersar unwrap run igrams.npz --out work/unwrap_manual --method auto     # 스택 단위 수동 실행
```

## 4. 실패 진단

```bash
wintersar diagnose work/ --engine isce2      # 또는 work/<stage>/<hash>/logs (ADR-0032)
```

| KB | 원인 요지 |
|---|---|
| `KB-ISCE2-001` | 참조·보조 사이 공통 burst 없음 ("No common bursts found …") |
| `KB-ISCE2-002` | ESD 저코히어런스 — `-e/--esd_coherence_threshold` 완화 또는 `-C geometry` |
| `KB-ISCE2-003` | 취득 시각을 덮는 궤도 파일 없음 |
| `KB-ISCE2-004` | DEM 이 처리 영역을 덮지 못함 |
| `KB-ISCE2-005` | 기간·bbox 를 만족하는 SAFE 없음 |
| `KB-SNAPHU-001/002/003` | 타일 조립 한도 초과 / 메모리 부족 / 타일 파라미터 오류 — 조치에 `--assemble`, `TILECOSTTHRESH`, `MINREGIONSIZE` |

## 5. 시계열과 비교

```bash
wintersar run --config config.yaml --from timeseries
wintersar validate --ts work/ts/timeseries.h5 --leveling data/leveling.csv
```

HyP3 결과와 같은 AOI·기간이면 두 속도 지도의 차이 통계를 `docs/research/` 에 기록합니다(Phase 2 DoD). 수치는
`bench_result.json` 을 근거로만 씁니다.

## English summary

The local ISCE2 topsStack path gives full control over looks, filtering, ESD, the unwrapper and
tiling at the cost of installation, disk and time. It needs conda-forge `isce2`, `snaphu`
(snaphu-py) or `tophu`, `mintpy` (subprocess only), plus `sentineleof`/`sardem` for orbits and DEM
cached under `~/.cache/wintersar`. Selection is identical to the HyP3 path; the adapter
(`engines/isce2_topsstack.py`, registered as `isce2_topsstack` and covering `fetch`, `coregister`,
`interferogram`, `multilook`) builds `stackSentinel.py` arguments from the `engine.isce2:` keys
(`slc_dir`, `orbit_dir`, `aux_dir`, `dem`, `bbox`, `reference_date`, `num_connections`,
`esd_coherence_threshold`, `unwrap_in_isce`, `max_parallel_per_step`, `retries`, ...), runs the
`run_files` with intra-step parallelism and hands the products to MintPy via `prep_isce`. ISCE2 is not
installed in this environment, so the flags were verified against upstream sources only
(ADR-0026/0027, open-questions #46). Unwrapping is
scheduled by wintersar (`wintersar unwrap plan/run`); failures are explained by
`wintersar diagnose work/ --engine isce2` through `KB-ISCE2-00x` / `KB-SNAPHU-00x`.
