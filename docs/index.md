# wintersar — Sentinel-1 InSAR(SBAS) 오픈소스 툴킷

`wintersar` 는 검증된 오픈소스 엔진(ASF HyP3, ISCE2 topsStack, SNAPHU/tophu, MintPy, dolphin)을 어댑터로
감싸고, 기존 생태계에 빠져 있던 네 가지를 채웁니다 (플랜 §1, 요구사항 R-01~R-15):

| 문제 (연구자 진술) | wintersar 의 답 | 모듈 |
|---|---|---|
| "다 받아도 맞는 게 몇 개 없다", "같은 하강궤도인데 정합이 abort" | burst 단위 검색 → 스택 그룹핑 → 사전검증 규칙 `SEL-01…13`(원인 → 조치, 한/영) | `select` |
| 파라미터 하나 바꾸면 전부 다시 | 해시 캐시 DAG: 바뀐 단계와 그 하류만 재실행(`PERF-03`); 날짜가 늘면 새 날짜에 닿는 쌍만(`run --incremental`, `PERF-06`) | `pipeline` |
| 큰 간섭도 언래핑이 OOM·느림 | 간섭도 단위 병렬 우선, 메모리 예산 안에서 자동 타일(`PERF-04`) | `unwrap` |
| "첫 1년을 실패 원인 찾는 데 허비" | 엔진 로그 파서 + 지식 베이스 `KB-xx`(원인 → 조치 → 참고) | `diagnose` |
| 기준점·대조군 검증·반복 튠 UX 부재 | 기준점 추천, 폐합 대시보드, 수준측량/GNSS 대조(LOS 투영), 파라미터 스윕 | `validate` |
| 대표위상·타일 스티칭 연구 | 합성 간섭도 생성기 + 실험 프레임워크 | `research` |
| "QGIS 받아, 무료임" | CLI `--json` 위의 얇은 QGIS 플러그인 | `qgis_plugin` |

설계 원칙(플랜 §1.4): 재사용 우선(fork 금지), 엔진은 subprocess 경계 뒤(GPL 격리), 모든 단계는 캐시 가능한
DAG 노드, 메시지는 한국어/영어로 "원인 + 조치", 성능 주장은 `bench` 결과가 있을 때만, 도메인 오해는 코드로 방지.
현재 버전이 무엇을 충족하고 무엇을 못 했는지는 [릴리스 노트](release-notes.md), 다음 할 일은 [로드맵](roadmap.md).

## 설치

세 경로(uv core · pixi engines · Docker engines)와 각 경로가 실행할 수 있는 것은 [설치 안내](install.md)에
있습니다. 개발·HyP3 원격 경로의 최소 설치:

```bash
git clone https://github.com/taylor224/WinterSAR && cd WinterSAR
uv sync --extra dev                 # Python 3.11 venv (.venv)
uv run wintersar --help
uv run wintersar check-install      # 엔진·인증·하드웨어 상태 (미설치 엔진은 ENV-001 Finding, 종료 코드 0)
```

외부 엔진(ISCE2, SNAPHU/snaphu-py, tophu, MintPy, dolphin, hyp3-sdk, sentineleof, sardem)은 **번들하지
않습니다**. 로컬 엔진은 pixi `engines` 환경이나 `Dockerfile.engines` 이미지로 설치하고, 어댑터가 감지합니다
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
([ADR-0032](adr/0032-workdir-layout-and-manifest.md)). 로그는 `work/<stage>/<hash>/logs/` 에 있고
(`work/logs/` 라는 디렉터리는 없습니다), `run` 은 실패하면 그 디렉터리로 `diagnose` 를 자동으로 붙입니다.

날짜가 하나씩 늘어나는 모니터링 운영은 `run --incremental`(PERF-06, [ADR-0080](adr/0080-incremental-update-per-pair-cache-and-hash-rule.md)):
처음부터 `--incremental` 로 돌려 두면 이후 실행은 새 날짜에 닿는 쌍만 계산하고 시계열만 다시 역산하며, `plan --incremental`
이 "부분 캐시 (쌍 캐시 N개 · 신규 M개)" 로 미리 보여 줍니다. 합성 레시피(`--set interferogram.n_dates=7` 로 날짜 추가)는
[HyP3 튜토리얼](tutorials/hyp3-quickstart.md) §6.

`--set` 의 형식은 `--set <단계>.<키>=<값>` 이고 **그 단계의 파라미터에만** 얹힙니다(다른 단계로 전파되지
않습니다). 그래서 fake 엔진의 실패 주입은 실패시킬 단계 이름으로 써야 합니다:

```bash
wintersar run --config config.yaml --set unwrap.fail_stage=unwrap   # unwrap 에서 실패 → PIPELINE-001 + KB Finding
```

`--set interferogram.fail_stage=unwrap` 처럼 다른 단계에 얹으면 `unwrap` 단계는 그 키를 보지 못해 아무것도
실패하지 않고(종료 코드 0), interferogram 해시만 바뀌어 하류가 통째로 재실행됩니다.

## 명령 목록 (`wintersar --help`)

| 명령 | 하는 일 | 종류 |
|---|---|---|
| `version` | 버전 출력 | 보고 |
| `check-install [--engine NAME]… [--strict]` | 엔진 설치·버전·인증·하드웨어 상태 (`ENV-00x`) | 보고 |
| `init [PATH] [--force]` | 예시 `config.yaml` 작성 (플랜 §4.4) | 동작 |
| `search --config` | ASF burst/SLC 후보 검색 → `work/select/candidates.json` | 보고 |
| `precheck CANDIDATES --config [--out] [--geometry] [--baseline auto|asf|orbit|none] [--no-fail]` | `SEL-01…13` 규칙 → `precheck_report.{md,html,json}` | 동작 |
| `plan --config [--until] [--from] [--force STAGE]… [--set k=v]… [--incremental]` | DAG·캐시 상태·예상 리소스·크레딧 견적 (실행 없음); `--incremental` 은 캐시된 쌍·신규 쌍 수까지 | 동작 |
| `run --config [--until] [--from] [--force STAGE]… [--set k=v]… [--dry-run] [--incremental]` | 파이프라인 실행; 바뀐 단계와 하류만 (`PERF-03`), `--incremental` 은 날짜 추가 시 새 쌍만 (`PERF-06`) | 동작 |
| `cache ls|gc --config [--workdir] [--stage] [--keep N] [--max-size GB] [--dry-run]` | 캐시 목록·정리 | 동작 |
| `diagnose [PATH] [--engine] [--out] [--assume-failed] [--list-kb]` | 엔진 로그 → KB 매칭 → 원인·조치 | 보고 |
| `validate --ts --leveling [--gnss] [--out] [--radius] [--method] [--align] [--max-gap-days] [--heading] [--incidence] [--no-plots]` | 수준측량·GNSS 대조 리포트 (R-10) | 보고 |
| `refpoint --ts --aoi [--top] [--coherence] [--conncomp] [--dem] [--weights] [--min-coherence] [--mintpy-threshold] [--out]` | 기준점 추천 + MintPy 자동 규칙 비교 (R-09) | 보고 |
| `closure --igrams [--unw] [--out] [--wrapped] [--top]` | 위상 폐합 통계·의심 간섭도 | 보고 |
| `sweep --config --grid [--out] [--leveling] [--gnss] [--radius] [--set]…` | 파라미터 격자 → 캐시 DAG → Pareto 순위 (R-08/R-11) | 동작 |
| `unwrap plan --shape R C --n N [--memory-gb] [--cores] [--method] [--tiles] …` | 언래핑 전략 dry run (R-06, PERF-04) | 보고 |
| `unwrap run IGRAMS [--out] [--method] [--tiles] …` | 스택 언래핑 실행 | 동작 |
| `research synth|repr-phase|stitch|experiment|experiments` | 합성 데이터, 대표위상, 타일 스티칭, YAML 실험 (R-07, R-15) | 동작 |
| `bench --site [--compare] [--out] [--repeats] [--fail-on-regression] [--threshold] [--runner] [--workdir] [--allow-network] [--markdown]` | 벤치마크 → `bench_result.json` (플랜 §5.9) | 동작 |

`--json`(안정된 봉투 `{"ok","command","data","findings"}`)과 `--lang ko|en` 은 **전역 옵션**이라 하위 명령
**앞에** 씁니다 — `wintersar --json run --config config.yaml`. 하위 명령 뒤에 쓰면
(`wintersar run --json`) `No such option: --json` 으로 종료 코드 2 가 납니다. *보고* 명령은 FAIL Finding 이
있어도 종료 코드 0 에 `ok:false`(호출자가 findings 를 읽음), *동작* 명령은 `ok` 가 아니면 1, 잘못된 입력은 2 입니다.
`wintersar --help` 가 현재 트리에 실제로 있는 명령의 목록이며, 이 표와 튜토리얼의 명령은 테스트가 CLI 와 대조합니다
([ADR-0112](adr/0112-docs-test-policy.md)).

## 실데이터 흐름 (요약)

```bash
wintersar search   --config config.yaml            # ASF burst 검색 → work/select/candidates.json
wintersar precheck work/select/candidates.json --config config.yaml   # SEL-01…13 → precheck_report.{md,html,json}
wintersar plan     --config config.yaml
wintersar run      --config config.yaml [--until unwrap] [--from timeseries] [--force STAGE]
wintersar diagnose work/ [--engine isce2|snaphu|mintpy|hyp3]   # 또는 work/<stage>/<hash>/logs
wintersar validate --ts work/timeseries/<hash>/out/timeseries.h5 --leveling data/leveling.csv [--gnss data/gnss.csv]
wintersar refpoint --ts work/timeseries/<hash>/out/timeseries.h5 --aoi aoi.geojson --top 5
wintersar sweep    --config config.yaml --grid sweep.yaml
wintersar bench    --site benchmarks/sites/S_synthetic.yaml [--compare baseline.json]
```

단계별 설명과 지금 바로 돌려 볼 수 있는 합성 대체 명령은 튜토리얼에 있습니다.

## 어디서부터 읽을까

- 설치: [설치 안내](install.md) — uv · pixi · Docker, Earthdata 인증, 승인이 필요한 항목.
- 처음이라면 [개념 정리](concepts/index.md): relative orbit, burst, looks, 레이오버, TOPS 정합, 타일
  언래핑, 기준점, loop closure, compressed SLC.
- 튜토리얼: [HyP3 빠른 시작](tutorials/hyp3-quickstart.md) → [ISCE2 로컬](tutorials/isce2-local.md) →
  [검증·튠](tutorials/validate-tune.md). 흐름은 셋 다 search → precheck → plan → run → diagnose → validate →
  refpoint → sweep 이고, 각 단계에 "지금 바로(합성)" 명령이 있습니다.
- 실패했을 때: [진단 KB](kb/index.md) — 메시지의 `KB-xxx` ID 로 찾습니다. 구조와 확장 방법은 [KB 개요](kb/overview.md).
- 이 버전의 상태: [릴리스 노트](release-notes.md)(Phase 별 DoD 충족/미충족), [로드맵](roadmap.md).
- 설계 근거: [ADR 색인](adr/README.md). 미확정 사항: [open-questions](open-questions.md).
- 기여: [contributing](contributing.md). QGIS 플러그인: `qgis_plugin/README.md`. 연구 모듈: [research](research/index.md).

## English summary

wintersar is an Apache-2.0 Sentinel-1 InSAR (SBAS) toolkit that wraps proven engines (HyP3, ISCE2
topsStack, SNAPHU/tophu, MintPy, dolphin) behind subprocess adapters and adds what researchers spend
their time on: burst-level selection with precheck rules `SEL-01..13`, a hash-cached DAG that re-runs
only the stages downstream of a changed parameter (and, with `run --incremental`, only the pairs touching a
newly added date — PERF-06), an unwrapping scheduler that tiles within a memory
budget, a log-parsing diagnosis knowledge base `KB-xx` (cause -> fix), ground-truth validation and a
thin QGIS plugin over the CLI's `--json` envelope. Install with `uv sync --extra dev` (local engines via
pixi or Docker, see the install guide); run the network-free synthetic pipeline with `wintersar init
config.yaml`, set `engine.interferogram: fake` and `timeseries.engine: fake`, then `wintersar plan` /
`wintersar run`. The command table above mirrors `wintersar --help`; `--json` and `--lang ko|en` are global
options and go *before* the sub-command. Report commands exit 0 with `ok:false` on FAIL findings, action
commands exit 1. Tutorials follow search -> precheck -> plan -> run -> diagnose -> validate -> refpoint ->
sweep with a synthetic equivalent for every step; the release notes state which plan DoDs are met, and the
roadmap lists the backlog and open questions by owner.
