# ADR-0091: CLI 오류 경로의 봉투 계약과 `CLI-xxx` 규칙 ID 카탈로그

- 상태(Status): 채택
- 날짜(Date): 2026-09-23
- 관련 ID: 규칙 11.6·11.11, 플랜 §4.5("모든 명령은 `--json` 출력 옵션을 가진다 — QGIS 플러그인이 파싱"),
  CLAUDE.md "Envelope vs exit code", 리뷰 발견 34/35/52, R-12
- 검증 출처(Sources):
  - `.venv/lib/python3.11/site-packages/typer/core.py` `_main`: standalone 모드는 `ClickException` 을
    `rich_utils.rich_format_error(e)` 로 stderr 에 그리고 `sys.exit(e.exit_code)`; non-standalone 모드는
    `ClickException` 을 다시 던지고 `Exit` 는 `e.exit_code` 를 반환; `TyperGroup.parse_args` 는 인자가
    없으면 `NoArgsIsHelpError(ctx)` 를 던진다
  - `.venv/lib/python3.11/site-packages/typer/_click/exceptions.py`: `UsageError.exit_code = 2`,
    `ClickException.exit_code = 1`, `NoArgsIsHelpError.__init__` 이 `ctx.get_help()` 를 호출(=도움말이 생성 시점에
    stdout 으로 출력됨), `UsageError.ctx`
  - `.venv/lib/python3.11/site-packages/typer/rich_utils.py` `rich_format_error`(NoArgsIsHelpError 는 무시)
  - `.venv/lib/python3.11/site-packages/typer/testing.py` — `CliRunner.invoke` 는 `get_command(app).main(...)`
    을 부르므로 `wintersar.cli.main()` 의 예외 처리는 테스트 경로에서 실행되지 않는다
  - `src/wintersar/util/output.py` `emit_json`(봉투 `{ok, command, data, findings}`),
    `qgis_plugin/wintersar_qgis/cli_client.py`(봉투 파서), `src/wintersar/io/schemas.py` `Finding`

## 맥락 (Context)

CLAUDE.md 의 계약: `ok` 는 "대상에 FAIL 이 없는가", 종료 코드는 "명령에 무슨 일이 있었는가"(0 실행됨,
1 요청한 동작 실패, 2 잘못된 입력/사용법). `--json` 모드에서는 **항상** 봉투가 나와야 QGIS 플러그인이
읽을 수 있다. 리뷰(34/35/52)에서 봉투 없이 끝나는 경로가 여러 곳 발견됐다.

- 모듈 CLI 가 `err_console.print(...)` 후 `typer.Exit(code=2)` 로 끝냄(select 의 설정/후보 파일 오류,
  unwrap 의 `UnwrapCfg` 검증 실패, diagnose 의 경로/엔진 오류, validate 의 `_require`, `refpoint --weights`).
- 영어 리터럴 오류(`--baseline: … not in auto|asf|orbit|none`, `--runner must be one of …`,
  `typer.BadParameter("--method mean|median, …")`).
- click 자체의 사용법 오류(필수 옵션 `--ts` 누락, 알 수 없는 옵션, 인자 없는 그룹): typer 가 stderr 패널만
  그리고 종료 코드 2 — stdout 은 비어 있다.

## 선택지 (Options)

1. 각 명령이 개별적으로 `if state.json: emit_json(...)` 을 반복 — 누락이 반복될 것이 뻔하다.
2. **공용 헬퍼 + click 예외 가로채기**: `output.cli_finding`/`exit_with_findings` 로 모듈 오류를 통일하고,
   루트 그룹 클래스(`clihelp.HelpGroup.main`)가 typer 를 non-standalone 모드로 돌려 `ClickException` 을
   `CLI-003` 봉투로 바꾼다.
3. `wintersar.cli.main()` 에서만 처리 — `CliRunner` 는 `main()` 을 거치지 않아 테스트가 계약을 검증하지 못한다.

## 결정 (Decision)

선택지 2.

### 헬퍼 (`src/wintersar/util/output.py`)

- `cli_finding(rule_id, *, message_key=None, fix_key=None, **params)` — 기본 키는 `cli.<ID>.cause`/`.fix`.
  모듈이 더 구체적인 원인 문구를 이미 갖고 있으면(`validate.cli.ts_not_found`, `select.cli.no_records`,
  `diagnose.cli.unknown_engine`) 그 키를 `message_key` 로 유지하고 조치(fix)만 공용 `CLI-xxx` 를 쓴다.
  문자열 파라미터는 `mask_text` 를 거친다(규칙 11.11).
- `exit_with_findings(command, findings, code=2, data=None) -> typer.Exit` — `--json` 이면 봉투만
  stdout 에(stderr 는 비움), 아니면 `ID: 원인` 을 빨간색으로, 다음 줄에 조치를 stderr 에 쓴다(원인 → 조치 순서).
  pipeline 의 `usage_error`(PIPELINE-014, 기존 테스트가 고정)도 이 헬퍼 위에 다시 구현했다.

### 규칙 ID 카탈로그 (`cli.*`, 텍스트는 `i18n/{ko,en}/cli_help.yaml` 의 `cli:` 블록에서 병합)

| ID | 뜻 | 종료 코드 | 파라미터 |
|---|---|---|---|
| CLI-001 | 명령 안에서 예상 못 한 예외(기존) | 1 | error |
| CLI-002 | `init` 대상 파일이 이미 있음(기존) | 1 | path |
| CLI-003 | click 사용법 오류(필수 옵션/인자 누락, 알 수 없는 옵션, 잘못된 형식, 인자 없는 그룹) | 2 | command, detail |
| CLI-004 | 설정 파일 없음 | 2 | path, option |
| CLI-005 | 설정 파일 오류(YAML/pydantic) | 2 | path, error, option |
| CLI-006 | 입력 파일 없음 | 2 | path, option |
| CLI-007 | 입력 파일을 읽을 수 없음/형식 오류 | 2 | path, option, error, command |
| CLI-008 | 옵션 값이 허용 목록 밖 | 2 | option, value, allowed, command |
| CLI-009 | 입력에 레코드가 없음 | 2 | path, option |
| CLI-010 | 명령에 필요한 구성 요소를 불러올 수 없음 | 2 | command, error |

모듈 고유 ID 는 그대로 쓴다: `PIPELINE-014`(plan/run/cache 사용법), `UNW-005`(스택 읽기 실패, 1),
`BENCH-004`(사이트 YAML, 2), `RES-006/010`(연구 명령 값·실험 이름, 1), `VAL-0xx`(1).

### click 예외 가로채기 (`clihelp.HelpGroup`)

- `main(standalone_mode=True)` 는 `super().main(standalone_mode=False)` 를 감싼다. `ClickException` →
  `report_click_error`: `CLI-003` finding(`command` 는 `exc.ctx.command_path` 에서 프로그램 이름을 뺀
  값, `detail` 은 `exc.format_message()`), `--json`(`state.json` 또는 argv 의 `--json`)이면 봉투, 아니면 typer 와
  같은 rich 오류 패널, 종료 코드는 `exc.exit_code`(UsageError 2). `Exit` 의 코드는 그대로 `sys.exit`.
- 명령 안에서 새어 나온 `Exception` 도 여기서 `CLI-001` 로 바꾼다(`-v` 면 다시 던짐). `wintersar.cli.main()`
  의 처리는 typer 자체에서 새는 경우를 위해 남긴다.
- `NoArgsIsHelpError` 는 생성자에서 도움말을 stdout 에 이미 출력하므로, `--json` 모드에서는 `parse_args`
  에서 먼저 `UsageError(ctx.get_usage())` 로 바꿔 stdout 에 문서가 둘 생기지 않게 한다.
  텍스트 모드는 이전과 같이 도움말을 보여 주고 2 로 끝난다.

### 검증

`tests/unit/test_cli_envelopes.py`: 36 개의 깨진 입력을 모든 명령에 대해 `--json` 으로 호출해
stdout 이 JSON 객체 하나이고 봉투 키 4개, `ok:false`, 종료 코드 1/2, stderr 비어 있음, finding 의
cause/fix 키가 두 언어에 존재함을 확인하고, 같은 입력을 `--lang ko`/`en` 텍스트 모드로 돌려
원시 i18n 키(`[a-z_]+\.[A-Za-z0-9_-]+\.(cause|fix)`)와 `Traceback` 이 없음을 확인한다.

## 결과 (Consequences)

- 새 오류 경로를 추가할 때는 `raise exit_with_findings(command, [cli_finding(...)])` 한 줄이면 계약을 지킨다.
  `err_console.print` + `typer.Exit` 조합은 더 이상 쓰지 않는다.
- 텍스트 모드의 오류 줄 형식이 `ID: 원인` / `조치` 로 통일됐다(이전에는 원인만). 기존 테스트가 보던
  문구(`Interferogram stack not found`, `--force`, `SEL-05` 등)는 유지된다.
- `--lang` 값이 `ko|en` 이 아니면 이전처럼 조용히 기본값으로 떨어진다(행동 유지; 오류로 바꾸려면 CLI-008).
- 되돌리는 조건: typer 가 non-standalone 모드에서 `Exit` 와 반환값을 구분하는 API 를 제공하면
  `sys.exit(rv if isinstance(rv, int) else 0)` 휴리스틱을 제거한다.

## English summary

Every CLI error path now ends in a `Finding` with a `CLI-xxx` (or module) rule id whose
cause/fix text lives in both catalogues: `--json` prints exactly one envelope (`ok: false`)
and nothing on stderr, text mode prints "ID: cause" then the fix on stderr, and the exit
code follows the CLAUDE.md rule (2 for bad input, 1 for a failed action). The shared helpers
are `output.cli_finding` and `output.exit_with_findings`; the ids are CLI-003 (click usage
error: missing option/argument, unknown option, group without arguments), CLI-004/005
(config missing/invalid), CLI-006/007 (input missing/unreadable), CLI-008 (value outside the
allowed set), CLI-009 (empty input) and CLI-010 (component unavailable). Click's own usage
errors are intercepted in `clihelp.HelpGroup.main`, which runs typer in non-standalone mode
(verified in typer 0.27.2 `_main`), and `NoArgsIsHelpError` is replaced under `--json` because
its constructor already prints the help page. `tests/unit/test_cli_envelopes.py` drives every
command with broken input in JSON and in both text languages.
