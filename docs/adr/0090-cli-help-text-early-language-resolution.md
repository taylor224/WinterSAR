# ADR-0090: CLI 도움말 문구의 조기 언어 결정과 렌더 시점 재번역

- 상태(Status): 채택
- 날짜(Date): 2026-09-23
- 관련 ID: 규칙 11.6, 플랜 §4.5, 리뷰 발견 34/35/52, R-12(QGIS 플러그인이 `--json --lang` 으로 호출)
- 검증 출처(Sources):
  - `.venv/lib/python3.11/site-packages/typer/main.py` — `get_command_from_info`
    (`use_help = command_info.help` 가 없으면 `inspect.getdoc(callback)`; `cls = command_info.cls or TyperCommand`),
    `Typer.command`(`cls is None` 이면 `TyperCommand` 로 채움), `get_group_from_info`(`cls = solved_info.cls or TyperGroup`),
    `Typer.__call__`(예외를 다시 던짐)
  - `.venv/lib/python3.11/site-packages/typer/core.py` — `TyperCommand.format_help` / `TyperGroup.format_help`
    가 `rich_utils.rich_format_help(obj=self, ctx=ctx, …)` 로 렌더, `_main`(non-standalone 모드)
  - `.venv/lib/python3.11/site-packages/typer/rich_utils.py` — `rich_format_help` 는 `obj.help`,
    `param.help`(`_get_parameter_help`), `command.short_help or command.help`(`_print_commands_panel`) 를 읽음
  - `.venv/lib/python3.11/site-packages/typer/_click/core.py` — `iter_params_for_processing`:
    eager 파라미터를 먼저, 명령줄에 나온 순서대로 처리; `Group.invoke` 는 그룹 콜백(`super().invoke(ctx)`)을
    실행한 뒤 하위 명령의 `make_context` 를 호출(하위 명령 `--help` 는 그 안에서 렌더)
  - typer 릴리스 노트 https://typer.tiangolo.com/release-notes/ — 0.26.0 "Vendor Click … Typer no longer
    depends on Click as a third party dependency, it vendors (includes the source code of) Click"(PR #1774);
    설치본 typer 0.27.2 의 `typer/_click/__init__.py` 머리말 "Code taken and adapted from Click 8.3.1"
  - `tests/conftest.py` `_lang_ko` autouse fixture(`WINTERSAR_LANG=ko`), `src/wintersar/util/clistate.py`
    `CliState.apply()`(환경변수로 내보냄)

## 맥락 (Context)

규칙 11.6 은 사용자에게 보이는 문구를 전부 `i18n/{ko,en}` 카탈로그에 두라고 한다. 그런데 명령줄
도움말(`--help`)만은 지금까지 영어 리터럴이었다(리뷰 발견 34/35/52). 이유는 typer 의 실행 순서다.

1. `help="..."` 문자열은 `typer.Option(...)`/`@app.command(...)` 가 **모듈 임포트 시점**에 평가한다.
   `register(app)` 은 `wintersar.cli` 임포트 중에 돌므로 `--lang` 은 아직 파싱되지 않았다.
2. 최상위 `wintersar --help` 는 click 의 eager 옵션 콜백이 **그룹 콜백보다 먼저** 실행해 렌더한다.
   그룹 콜백(`_main_callback`)이 `state.lang` 을 정하기 전이다.
3. 임포트 시점에 `t()` 로 번역하면 기본 언어(ko)로 굳어 `wintersar --lang en --help` 에 한국어가 섞인다
   (`tests/unit/pipeline/test_cli.py::test_help_is_english_whatever_the_language` 가 이 회귀를 막고 있었다).

## 선택지 (Options)

1. 도움말은 영어 리터럴로 둔다(현상 유지) — 규칙 11.6 위반, 한국어 사용자에게 도움말만 영어.
2. `h(key)` 가 임포트 시점에 `sys.argv`/`WINTERSAR_LANG` 을 읽어 언어를 정한다 — 실제 프로세스에서는 충분하지만
   `typer.testing.CliRunner`(인자를 `sys.argv` 에 두지 않음)와 `wintersar --lang en plan --help`
   (하위 명령 도움말 렌더 시점에는 이미 `--lang` 이 파싱되어 있음)에서는 굳은 언어를 바꿀 수 없다.
3. **2 + 렌더 시점 재번역**: `h()` 가 두 언어의 렌더 결과를 `문구 → (키, 파라미터)` 레지스트리에 등록하고,
   `cls=` 로 끼운 `HelpGroup`/`HelpCommand.format_help` 가 렌더 직전에 `help_lang()` 으로 다시 번역한다.
4. 레이지 문자열 객체 — typer 가 `inspect.cleandoc(help)` 로 일반 `str` 로 바꿔 버리므로 불가.

## 결정 (Decision)

선택지 3. 구현은 `src/wintersar/util/clihelp.py` 와 `i18n/{ko,en}/cli_help.yaml`.

- **키 규약**: `cli_help.<command>.help`(명령 설명), `cli_help.<command>.<option>`(옵션/인자),
  `cli_help.common.*`(여러 명령이 공유하는 `--config`, `--ts`, `--until` 등). 모든 옵션이 도움말을 갖는다
  (이전에 비어 있던 `--min-overlap-px`, `--seed` 등도 채움). 문구 안의 ID(R-xx/PERF-xx/플랜 §)는 그대로 둔다.
- **언어 결정 순서** `help_lang()`: 그룹/eager 콜백이 이미 채택한 `--lang`(`state.lang_explicit`) →
  `sys.argv` 의 `--lang X`/`--lang=X` → `WINTERSAR_LANG` → `ko`. 값이 `ko|en` 이 아니면 무시한다.
- **`--lang` 은 eager**(`is_eager=True`, 콜백 `_lang_callback` → `state.set_lang`). click 은 eager 파라미터를
  명령줄 순서대로 먼저 처리하므로 `wintersar --lang en --help` 는 렌더 전에 언어를 안다. 반대 순서
  `wintersar --help --lang en` 은 `sys.argv` 스캔이 받친다(CliRunner 에서는 스캔이 불가능하므로 테스트가
  `sys.argv` 를 monkeypatch 한다).
- **렌더 시점 재번역**: `clihelp.install(app)` 이 마운트 후 모든 `CommandInfo.cls`(typer 가 `TyperCommand` 로
  채운 것)와 하위 `Typer.info.cls` 를 `HelpCommand`/`HelpGroup` 으로 바꾼다. `format_help` 는 `self.help`,
  `param.help`, 하위 명령의 `help`/`short_help` 를 레지스트리로 되찾아 `help_lang()` 언어로 다시 쓴다.
  click 객체는 호출마다 새로 만들어지므로 상태가 남지 않는다.
- **`lang_explicit`**: `CliState` 에 "언어를 골랐는가" 플래그를 추가했다(`validate.api._report_lang` 이 이미
  `getattr(state, "lang_explicit", False)` 로 기다리던 필드). `--lang` 없이 실행하면 `current_lang()`(호출 시점의
  `WINTERSAR_LANG` 또는 ko)이다 — 이전에는 임포트 시점의 값이었다.
- **프로세스 내 격리**: 그룹 콜백은 라이브러리 코드를 위해 `WINTERSAR_LANG` 을 내보낸다(`state.apply`).
  `HelpGroup.main` 이 호출 전 값을 기억했다가 `finally` 에서 되돌려, CliRunner 로 연속 호출할 때
  `--lang en` 한 번이 다음 `--lang` 없는 호출을 영어로 만들지 않는다. 실제 프로세스에서는 차이가 없다.
- **의존성 사실**: typer 0.26.0 부터 click 을 vendoring 한다(`typer._click`). 어댑터가 잡아야 하는 예외는
  `typer._click.exceptions.*` 이지 별도 설치된 `click` 패키지의 클래스가 아니다. `pyproject.toml` 의
  `typer>=0.12` 핀은 `>=0.26` 으로 올려야 한다(통합자, `needs_from_others`).

## 결과 (Consequences)

- `wintersar --lang ko --help` 는 한국어, `--lang en` 은 영어, 둘 다 없으면 `WINTERSAR_LANG`/ko.
  `tests/unit/test_cli_i18n.py` 가 (a) 언어 결정 순서, (b) 마운트된 모든 명령/옵션의 도움말이 카탈로그 키에서
  왔는지(리터럴 금지), (c) ko/en 키 집합·플레이스홀더 동일, (d) 두 언어 렌더를 검사한다.
- `tests/unit/pipeline/test_cli.py::test_help_is_english_whatever_the_language` 의 마지막 케이스
  (`["plan", "--help"]` 가 `--lang` 없이도 영어)는 이 결정과 모순이므로 해당 소유자가 갱신해야 한다
  (`--lang` 없이 = `WINTERSAR_LANG` 또는 ko). 다른 네 케이스(`--lang en …`)는 그대로 통과한다.
- 도움말 문구를 바꾸려면 `cli_help.yaml` 두 파일을 같이 고친다. `h()` 는 키가 없어도 키 문자열로 폴백해
  CLI 를 깨뜨리지 않고, 테스트(`missing_help_keys`)가 누락을 잡는다.
- 되돌리는 조건: typer 가 도움말 렌더 훅(예: 콜백 기반 help)을 공식 제공하면 레지스트리·`cls` 교체를 제거한다.

## English summary

Typer evaluates `help="..."` strings at import time and renders the top-level `--help` from
an eager option before the app callback that parses `--lang` runs, which is why the help
texts were the last English literals left in the CLI (rule 11.6). `wintersar.util.clihelp`
now (1) resolves the help language early — a parsed `--lang`, then a `sys.argv` scan for
`--lang X`/`--lang=X`, then `WINTERSAR_LANG`, then `ko` — through `h(key)`, which every
`help=` of every CLI uses with keys from `i18n/{ko,en}/cli_help.yaml`; (2) makes `--lang`
eager so `--lang en --help` sees the choice; and (3) re-translates the help of a command
right before it is rendered (`HelpGroup`/`HelpCommand.format_help`, installed through the
typer `cls` hooks) from a registry of rendered text → key, so `wintersar --lang en plan --help`
is English even under `typer.testing.CliRunner`. `CliState.lang_explicit` records whether the
language was chosen; `HelpGroup.main` restores `WINTERSAR_LANG` after each invocation.
Verified against typer 0.27.2, which vendors click (since 0.26.0), so the exception classes
come from `typer._click`; the `typer>=0.12` pin should be raised to `>=0.26`.
