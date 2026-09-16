# wintersar — Sentinel-1 InSAR(SBAS) 오픈소스 툴킷

`wintersar` 는 검증된 오픈소스 엔진(ASF HyP3, ISCE2 topsStack, SNAPHU/tophu, MintPy, dolphin)을 어댑터로
감싸고, 기존 생태계에 빠져 있던 네 가지를 채웁니다 (플랜 §1, 요구사항 R-01~R-15):

| 문제 (연구자 진술) | wintersar 의 답 | 모듈 |
|---|---|---|
| "다 받아도 맞는 게 몇 개 없다", "같은 하강궤도인데 정합이 abort" | burst 단위 검색 → 스택 그룹핑 → 사전검증 규칙 `SEL-01…13`(원인 → 조치, 한/영) | `select` |
| 파라미터 하나 바꾸면 전부 다시 | 해시 캐시 DAG: 바뀐 단계와 그 하류만 재실행(`PERF-03`) | `pipeline` |
| 큰 간섭도 언래핑이 OOM·느림 | 간섭도 단위 병렬 우선, 메모리 예산 안에서 자동 타일(`PERF-04`) | `unwrap` |
| "첫 1년을 실패 원인 찾는 데 허비" | 엔진 로그 파서 + 지식 베이스 `KB-xx`(원인 → 조치 → 참고) | `diagnose` |
| 기준점·대조군 검증·반복 튠 UX 부재 | 기준점 추천, 폐합 대시보드, 수준측량/GNSS 대조(LOS 투영), 파라미터 스윕 | `validate` |
| 대표위상·타일 스티칭 연구 | 합성 간섭도 생성기 + 실험 프레임워크 | `research` |
| "QGIS 받아, 무료임" | CLI `--json` 위의 얇은 QGIS 플러그인 | `qgis_plugin` |

설계 원칙(플랜 §1.4): 재사용 우선(fork 금지), 엔진은 subprocess 경계 뒤(GPL 격리), 모든 단계는 캐시 가능한
DAG 노드, 메시지는 한국어/영어로 "원인 + 조치", 성능 주장은 `bench` 결과가 있을 때만, 도메인 오해는 코드로 방지.

## 설치

```bash
git clone https://github.com/wintersar/wintersar && cd wintersar
uv sync --extra dev                 # Python 3.11 venv (.venv)
uv run wintersar --help
uv run wintersar check-install      # 엔진·인증·하드웨어 상태 (미설치 엔진은 ENV-001 Finding)
```

외부 엔진(ISCE2, SNAPHU/snaphu-py, tophu, MintPy, dolphin, hyp3-sdk, sentineleof, sardem)은 **번들하지
않습니다**. 필요한 경로만 conda-forge/pip 로 따로 설치하면 어댑터가 감지합니다
([ADR-0001](adr/0001-license-and-engine-boundaries.md)). `wintersar check-install` 이 무엇이 빠졌고 어떻게
설치하는지 알려 줍니다.

## 5분 합성 실행 (네트워크·엔진 없이)

내장 **fake 엔진**은 합성 간섭도로 전체 DAG 를 돌립니다. 설치가 제대로 됐는지, 캐시가 어떻게 동작하는지
확인하는 가장 빠른 방법입니다.

```bash
mkdir demo && cd demo
wintersar init config.yaml                         # 예시 설정 (플랜 §4.4)
printf '{"type":"FeatureCollection","features":[]}' > aoi.geojson
```

`config.yaml` 에서 두 엔진을 `fake` 로 바꾸고, 예시의 `validate:` 절(대조군 CSV 경로)은 지웁니다
(대조군이 없으면 `validate` 단계는 `PIPELINE-010` INFO 와 함께 건너뜁니다):

```yaml
engine:
  interferogram: fake
timeseries:
  engine: fake
# validate: 절 삭제 (또는 아래처럼 --until geocode 로 검증 단계 전까지만 실행)
```

```bash
wintersar plan --config config.yaml                # dry run: 단계 표, 캐시/실행 예정, 예상 리소스
wintersar run  --config config.yaml                # search/precheck 는 건너뛰고 fetch → geocode 실행
wintersar run  --config config.yaml --until geocode  # validate: 절을 남겨 둔 경우
wintersar run  --config config.yaml                # 두 번째: 전부 캐시됨
wintersar run  --config config.yaml --set unwrap.coherence_threshold=0.5   # unwrap 이후만 재실행 (PERF-03)
wintersar cache ls --config config.yaml            # work/<stage>/<hash> 목록
wintersar --json run --config config.yaml          # QGIS 플러그인이 읽는 봉투 {"ok","command","data","findings"}
```

산출물은 `work/<stage>/<hash>/out/`, 실행 요약은 `work/runs/<run_id>.json`
([ADR-0032](adr/0032-workdir-layout-and-manifest.md)). 실패를 재현해 보려면
`--set interferogram.fail_stage=unwrap` 을 주면 `run` 이 `diagnose` 를 자동으로 붙여 Finding 을 보여 줍니다.

## 실데이터 흐름 (요약)

```bash
wintersar search   --config config.yaml            # ASF burst 검색 → work/select/candidates.json
wintersar precheck work/select/candidates.json --config config.yaml   # SEL-01…13 → precheck_report.{md,html,json}
wintersar plan     --config config.yaml
wintersar run      --config config.yaml [--until unwrap] [--from timeseries] [--force STAGE]
wintersar diagnose work/logs/ [--engine isce2|snaphu|mintpy|hyp3]
```

```bash
wintersar validate --ts work/ts/timeseries.h5 --leveling data/leveling.csv [--gnss data/gnss.csv]
wintersar refpoint --ts work/ts/timeseries.h5 --aoi aoi.geojson --top 5
wintersar sweep    --config config.yaml --grid sweep.yaml
wintersar bench    --site benchmarks/sites/S_synthetic.yaml [--compare baseline.json]
wintersar research repr-phase --igram igrams.npz --out repr.npz --method ml|coh_weighted|shp|phase_link|filtered
wintersar research stitch     --tiles tiles.npz --out merged.npz --method coarse_ref|overlap_consensus
wintersar unwrap plan --shape 4000 6000 --n 30      # 언래핑 스케줄러 dry run
```

모든 명령은 `--json`(안정된 봉투 `{"ok","command","data","findings"}`)과 `--lang ko|en` 을 받습니다.
`wintersar --help` 가 현재 트리에 실제로 있는 명령의 목록입니다.

## 어디서부터 읽을까

- 처음이라면 [개념 정리](concepts/index.md): relative orbit, burst, looks, 레이오버, TOPS 정합, 타일
  언래핑, 기준점, loop closure, compressed SLC.
- 튜토리얼: [HyP3 빠른 시작](tutorials/hyp3-quickstart.md) → [ISCE2 로컬](tutorials/isce2-local.md) →
  [검증·튠](tutorials/validate-tune.md).
- 실패했을 때: [진단 KB](kb/index.md) — 메시지의 `KB-xxx` ID 로 찾습니다.
- 설계 근거: [ADR 색인](adr/README.md). 미확정 사항: [open-questions](open-questions.md).
- 기여: [contributing](contributing.md). QGIS 플러그인: `qgis_plugin/README.md`.

## English summary

wintersar is an Apache-2.0 Sentinel-1 InSAR (SBAS) toolkit that wraps proven engines (HyP3, ISCE2
topsStack, SNAPHU/tophu, MintPy, dolphin) behind subprocess adapters and adds what researchers spend
their time on: burst-level selection with precheck rules `SEL-01..13`, a hash-cached DAG that re-runs
only the stages downstream of a changed parameter, an unwrapping scheduler that tiles within a memory
budget, a log-parsing diagnosis knowledge base `KB-xx` (cause -> fix), ground-truth validation and a
thin QGIS plugin over the CLI's `--json` envelope. Install with `uv sync --extra dev`; run the
network-free synthetic pipeline with `wintersar init config.yaml`, set `engine.interferogram: fake`
and `timeseries.engine: fake`, then `wintersar plan` / `wintersar run`. Every command accepts
`--json` and `--lang ko|en`; messages are Korean by default with English available.
