# ADR-0112: 문서 테스트 정책 (`tests/unit/qgis/test_docs.py` 가 무엇을 검사하고 무엇은 검사하지 않는가)

- 상태(Status): 채택
- 날짜(Date): 2026-09-23
- 관련 ID: 규칙 11.3·11.4·11.8·11.9, ADR-0072, ADR-0110, ADR-0111, R-14
- 검증 출처(Sources):
  - `typer.main.get_command(app)` 가 돌려주는 `typer.core.TyperGroup`; 하위 명령은 `list_commands(ctx)` /
    `get_command(ctx, name)`, 파라미터는 `get_params(ctx)`(`--help` 포함), 인자 판별은 `Parameter.param_type_name ==
    "argument"` — `.venv/lib/python3.11/site-packages/typer/core.py`, `typer/_click/core.py` (Typer 0.27.2, Click 8.5.0).
    `isinstance(cmd, click.Group)` 는 이 조합에서 False 이므로 쓰지 않는다(2026-09-23 실측).
  - `click.Parameter.nargs`, `Option.is_flag`, `Option.count` — Click 8.5.0 `click/core.py`
  - `mkdocs.yml` 은 순수 YAML(파이썬 태그 없음) → `yaml.safe_load` 로 읽는다
  - MkDocs `README.md`/`index.md` 규칙 — ADR-0110 출처

## 맥락 (Context)

문서는 CLI·작업 디렉터리 규약·open-questions 표에 대한 주장을 담는다. 이 주장들은 코드가 바뀌면 조용히 틀려진다.
기존 `test_docs.py` 는 (a) open-questions 번호 참조가 올바른 행을 가리키는지, (b) `diagnose work/logs` 처럼 존재하지
않는 경로를 쓰지 않는지, (c) `--json/--lang` 이 하위 명령 앞에 오는지, (d) English summary 존재, (e) `--set
<stage>.fail_stage` 예제가 실제로 실패하는지를 검사했다. Phase 8 에서 페이지가 늘고(설치·릴리스 노트·로드맵·KB 개요),
`mkdocs.yml` nav 가 바뀌고, 튜토리얼에 명령이 많이 추가되어 검사 범위를 정해야 한다.

## 선택지 (Options)

1. `mkdocs build --strict` 를 CI 에서 돌린다 — docs extra 미설치, 빌드는 nav·링크만 잡고 명령은 못 잡는다.
2. `--help` 텍스트를 파싱한다 — rich 박스 출력이 `COLUMNS` 에 따라 줄바꿈되어 불안정.
3. **Click 트리로 명령·옵션을 직접 대조하고, nav·링크·open-questions 참조·로드맵 담당 열을 파일로 검사** — 선택.

## 결정 (Decision)

`tests/unit/qgis/test_docs.py` 는 다음을 검사한다(모두 네트워크·엔진 없이, 매 PR).

| 검사 | 대상 | 방법 |
|---|---|---|
| nav 항목 존재 | `mkdocs.yml` | `nav` 를 재귀로 걸어 문자열 값마다 `docs/<path>` 가 파일인지 |
| 명령·옵션 유효성 | `README.md`, `docs/index.md`, `docs/tutorials/*.md`, `docs/kb/overview.md`, `docs/release-notes.md`, `docs/roadmap.md` 의 ```` ```bash/sh/console ```` 블록 | `wintersar` 로 시작하는 각 명령을 토큰화(줄 연속 `\`, 주석 `#`, `;`/`&&`/`\|\|`/` \| ` 분리, `[...]` 선택 표기 제거)해 Click 트리를 따라가며 하위 명령 이름과 옵션 이름을 확인. 값 옵션은 `nargs` 만큼 건너뛰고(`--shape 4000 6000`, `--heading -12`), 플래그·count 옵션은 값을 소비하지 않음. 위치 인자 수는 명령의 인자 수 이하 |
| 상대 링크 해석 | 위 페이지 + ADR 색인·개념 색인 | `[..](path)` 의 상대 경로가 파일로 존재(앵커·http·mailto 제외) |
| open-questions 참조 | 소유 페이지 | `open-questions #N` 형식의 N 이 표의 행이고, 등록된 문구가 그 행에 있는지(`OPEN_QUESTION_REFS`) |
| 로드맵 담당 열 | `docs/roadmap.md` | `\| #N \| … \| 담당 \|` 행마다 N 이 표에 있고 담당 열이 open-questions 의 담당 열과 같은지(이스케이프된 `\|` 를 고려해 분리) |
| 기존 검사 | 소유 페이지 | English summary, diagnose 대상 경로, 전역 옵션 순서, fail_stage 예제 실행 |

검사하지 **않는** 것과 이유:

- 옵션 **값**(파일 이름, 단계 이름, `ko|en`) — 자리표시자가 많고 문맥 의존. 값 검증은 CLI 자체가 한다.
- "예상 출력" 블록의 열 이름 — 손으로 채록한 모양이며 i18n 카탈로그 변경 시 사람이 갱신(ADR-0110 결과).
- 성능 수치 금지(규칙 11.8) — 정규식으로 잡기엔 오탐이 많아 리뷰 항목으로 둔다.
- `mkdocs build` — docs extra 가 설치되면 `mkdocs build --strict` 를 CI 에 추가한다(open-questions #73).
- 모든 ADR 파일이 색인에 있는지 — 다른 모듈이 ADR 을 추가하는 순간 깨지므로 색인 갱신은 통합자 리뷰 항목으로 둔다.

새 페이지를 추가할 때: `OWNED_PAGES`(공통 검사)와 필요하면 `COMMAND_PAGES`(명령 검사)·`ENGLISH_SUMMARY_PAGES` 에
넣고, open-questions 를 인용하면 `OPEN_QUESTION_REFS` 에 번호와 문구를 등록한다.

## 결과 (Consequences)

- 명령 이름·옵션 이름을 바꾸면 문서 테스트가 먼저 깨진다. 이것이 목적이다(플랜 §3.3 "엔진 API 변경은 어댑터 테스트가
  먼저 깨진다" 와 같은 원리를 문서에 적용).
- 문서 안의 셸 예제는 `wintersar` 로 시작하는 줄만 검사되므로, `cp`/`printf` 같은 보조 줄은 자유롭게 쓴다. 명령
  검사에서 빼고 싶은 출력 예시는 반드시 ```` ```text ```` 로 표시한다.
- `docs/install.md` 는 다른 담당의 페이지라 이 테스트의 명령 검사에 넣지 않았다. 담당이 원하면 `COMMAND_PAGES` 에
  추가만 하면 된다.
- 로드맵 표는 open-questions 의 담당 열 문자열과 **정확히** 같아야 한다(예: `Taylor / 통합자`). 담당 열이 바뀌면
  로드맵도 같이 고친다.
