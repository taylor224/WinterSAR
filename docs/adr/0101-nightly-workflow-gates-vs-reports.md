# ADR-0101: nightly 워크플로 — 무엇이 게이트이고 무엇은 보고만 하는가

- 상태(Status): 채택 (기준선 비교의 게이트 전환은 open-questions #58 해결 후)
- 날짜(Date): 2026-09-23
- 관련 ID: 플랜 §8 표 "regression"(야간) / "perf"(매 PR, 15 % 회귀 시 실패) 행, §6.3 항목 5, §5.9,
  규칙 11.8, R-13, ADR-0054(bench 프로토콜), ADR-0100(골든 층), ADR-0102(허용 오차)
- 검증 출처(Sources):
  - GitHub Docs "Events that trigger workflows": `on.schedule` 는 POSIX cron 5필드, "By default, scheduled
    workflows run in UTC", "Scheduled workflows run on the latest commit on the default branch",
    "The shortest interval you can run scheduled workflows is once every 5 minutes", `on: workflow_dispatch`
    <https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows>
  - GitHub Docs "Workflow syntax": `jobs.<job_id>.continue-on-error` — "Prevents a workflow run from failing when a
    job fails. Set to `true` to allow a workflow run to pass when this job fails."; `jobs.<job_id>.needs` — "Use
    `needs` to identify any jobs that must complete successfully before this job will run."; `jobs.<job_id>.if`;
    `timeout-minutes`(기본 360) <https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions>
  - GitHub Docs "Workflow commands": 출력 `echo "{name}={value}" >> "$GITHUB_OUTPUT"` 후
    `steps.<id>.outputs.<name>`; 요약 `>> $GITHUB_STEP_SUMMARY`
    <https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions>
  - `actions/runner-images` README: Ubuntu 24.04 의 라벨은 `ubuntu-latest` 또는 `ubuntu-24.04`(현재 `-latest` 가
    24.04 를 가리킴) <https://github.com/actions/runner-images>
  - `actions/download-artifact` v4 README: `with: name, path`, "Downloading artifacts that were created from
    `action/upload-artifact@v3` and below are not supported" (ci.yml 은 upload-artifact@v4)
    <https://github.com/actions/download-artifact/blob/v4/README.md>
  - GitHub Docs "Workflow syntax" `permissions`: 값은 `read`/`write`/`none`, "If you specify the access for any of these
    permissions, all of those that are not specified are set to `none`", 예시 `permissions:\n  contents: read`
    <https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions>
  - `actions/download-artifact` v4 README: `github-token` 은 "required when downloading artifacts from a different
    repository or from a different workflow run" 일 때만; `actions/upload-artifact` v4 README 에는 토큰 권한 요구가 없다
    <https://github.com/actions/upload-artifact/blob/v4/README.md>
  - 이 저장소 `.github/workflows/ci.yml`(checkout@v4, setup-uv@v5, `uv sync --extra dev`, upload-artifact@v4),
    `src/wintersar/bench/cli.py`(`--json bench --site --out --repeats`), `src/wintersar/bench/report.py`
    (`compare`, `load_result`, `CompareReport.ok/to_markdown`)

## 맥락 (Context)

플랜은 regression 층을 "야간", perf 층을 "매 PR, 기준선 대비 15 % 느려지면 실패"로 두었다. 그러나 기준선을
어느 머신에서 만들지(#58)가 열려 있어 ci.yml 은 측정만 하고 게이트하지 않는다. 야간 워크플로는 (a) 골든
통계 회귀를 확실히 게이트하고, (b) 성능은 측정·보관하며, (c) 기준선이 있을 때만 비교 표를 만들되 워크플로를
빨갛게 만들지 않아야 한다. 또한 CI 로그·요약에 성능 수치를 산문으로 적지 않는다(11.8).

## 선택지 (Options)

1. ci.yml 에 회귀 테스트와 기준선 비교를 모두 추가 — PR 마다 실행되지만 #58 이 열린 채로 게이트가 흔들린다.
2. 별도 `nightly.yml`: cron + 수동 실행, 3개 잡(regression / bench / compare-baseline), 비교 잡만
   `continue-on-error`.
3. 기준선 비교를 bench 잡 안의 한 스텝으로(`--compare --fail-on-regression`) — 스텝 실패가 잡을 실패시켜
   "보고만"이 되지 않거나, `continue-on-error` 를 스텝에 걸면 bench 실행 자체의 실패(BENCH-005)도 묻힌다.

## 결정 (Decision)

선택지 2. `.github/workflows/nightly.yml`:

| 잡 | 내용 | 실패 시 |
|---|---|---|
| `regression` | `pytest tests/regression -m "not network and not engine_real and not gpu"` + `scripts/check_golden.py --lang en --markdown --json`(요약·아티팩트 `golden-check-ubuntu-24.04`) | **워크플로 실패** (게이트) |
| `bench` | `wintersar --json bench --site benchmarks/sites/S_synthetic.yaml --out bench_result.json --repeats 3` → 아티팩트 `bench-S_synthetic-ubuntu-24.04` | **워크플로 실패** — 단, 실행 자체(BENCH-005/BENCH-004 등)만; 수치는 게이트하지 않음 |
| `compare-baseline` | `needs: bench`, `continue-on-error: true`; `benchmarks/baselines/S_synthetic.${RUNNER_CLASS}.json` 이 있으면 아티팩트를 내려받아 `bench.report.compare` 표를 `$GITHUB_STEP_SUMMARY` 에 쓰고 회귀가 있으면 exit 1(잡만 빨감); 없으면 요약에 "기준선 없음" 한 줄 | **워크플로는 통과** (보고만) |

- 트리거: `cron: "0 3 * * *"`(UTC 03:00, 기본 브랜치 최신 커밋) + `workflow_dispatch`.
- 토큰 권한(2026-09-23 리뷰 2차): 워크플로 최상위에 `permissions: contents: read`. 잡은 체크아웃과 같은 실행 안의
  아티팩트 업로드/다운로드만 하므로 다른 scope 는 필요 없고, 하나라도 지정하면 나머지는 `none` 이 된다(위 출처).
  액션은 ci.yml 과 같은 major 태그(`@v4`/`@v5`)로 고정한다 — 커밋 SHA 고정은 SHA 를 갱신할 dependabot/renovate 설정이
  이 저장소에 없어 채택하지 않았다(후속: 그 설정을 추가할 때 두 워크플로를 함께 전환). ci.yml 에도 같은 `permissions`
  블록이 필요하다(다른 담당 파일이라 이번 라운드에는 제안만). `tests/unit/bench/test_nightly_workflow.py` 가 권한과
  게이트/보고 구조를 고정한다.
- 러너 클래스는 `ubuntu-24.04` 로 명시(`RUNNER_CLASS` env, `runs-on` 과 같은 값). `ubuntu-latest` 를 쓰지 않는
  이유: 기준선 파일 이름(`S_synthetic.<runner>.json`)이 러너 클래스를 뜻해야 하는데 `-latest` 는 OS 가 바뀌면
  같은 이름으로 다른 머신을 가리킨다. 기준선 생성·갱신 정책은 `benchmarks/baselines/README.md`.
- `check_golden.py` 종료 코드는 *action* 규약(CLAUDE.md): 0 일치, 1 불일치·골든 없음·실행 실패, 2 잘못된 입력.
  회귀 테스트와 같은 비교를 두 번 하는 셈이지만(둘 다 1초 미만) 하나는 pytest 보고, 하나는 사람이 읽는 표·JSON
  아티팩트라는 역할이 다르다.
- `tests/regression` 은 `pytest` 기본 `testpaths` 에 포함되므로 ci.yml 의 매 PR 실행에도 걸린다(`slow` 마커지만
  선택 해제하지 않음). 비용이 1초 수준이라 야간까지 기다릴 이유가 없다; "야간"은 `check_golden.py` 보고와
  perf 층의 실행 시점을 뜻한다.
- 기준선 비교는 워크플로 안의 짧은 파이썬 스니펫(`bench.report.compare` → Markdown)으로 한다. `wintersar bench
  --compare` 는 bench 를 다시 실행하므로 쓰지 않았고, bench CLI 에 "비교만" 옵션을 추가하는 것은 이번 라운드에
  다른 담당이 편집 중인 파일이라 미뤘다(후속: `wintersar bench compare <before> <after>` 서브커맨드).

## 결과 (Consequences)

- 성능 수치는 아티팩트(`bench_result.json`)와 자동 생성 표에만 존재한다. 이 ADR·README·워크플로 주석에는 없다.
- #58 이 닫히면(변동 측정 후 기준선 커밋) `compare-baseline` 의 `continue-on-error` 를 제거하고 `regress_on`·
  임계값을 확정하는 ADR 로 이 문서를 대체한다. ci.yml 로 게이트를 옮기는 것도 그때 결정한다.
- 골든이 바뀌어야 하는 PR 은 `scripts/make_golden.py` 재생성 커밋을 동반하고, 그렇지 않으면 다음 야간 실행이
  실패한다(그리고 PR CI 에서도 이미 실패한다).
- 야간 실행이 Linux x86_64 에서 골든(macOS arm64 생성)과 처음 비교되므로 첫 실행 결과로 ADR-0102 의 허용
  오차를 확인한다(open-questions 참조).
