# ADR-0110: 튜토리얼 구조와 언어 정책 (실데이터/합성 두 줄, 출력은 모양만, 명령은 트리에서)

- 상태(Status): 채택
- 날짜(Date): 2026-09-23
- 관련 ID: R-14, 플랜 §7 Phase 8, §4.5, 규칙 11.3·11.8·11.11, ADR-0072(상위 문서 정책), ADR-0112(테스트)
- 검증 출처(Sources):
  - `COLUMNS=200 .venv/bin/wintersar <cmd> --help` (2026-09-23) 와 Click 트리
    (`typer.main.get_command(app)` → `list_commands`/`get_params`) — 튜토리얼의 모든 명령·옵션은 이 트리에 있는 것만 씀
  - 합성 실행 기록: 스크래치 디렉터리에서 `init → plan → run ×3 → cache ls/gc → run --set unwrap.fail_stage=unwrap →
    diagnose → precheck(--baseline none) → validate(수준측량/GNSS) → refpoint → closure → sweep → unwrap plan/run →
    research synth/repr-phase/experiments → bench S_synthetic` 를 실제로 실행해 콘솔 형태를 채록
  - MkDocs 사용자 안내 "Writing your docs → Index pages": "If both an `index.md` file and a `README.md` file are
    found in the same directory, then the `index.md` file is used and the `README.md` file is ignored."
    <https://www.mkdocs.org/user-guide/writing-your-docs/>
  - `docs/adr/0013`(Earthdata 인증), `docs/adr/0020`(HyP3 크레딧 URL `https://hyp3-docs.asf.alaska.edu/using/credits/`),
    `docs/install.md`(설치 경로 세 가지)

## 맥락 (Context)

Phase 8 은 튜토리얼 3종(HyP3 빠른 시작, ISCE2 로컬, 검증·튠)을 요구한다. 그러나 이 저장소의 개발 환경에는 외부
엔진·자격증명·실데이터가 없고(CLAUDE.md Environment), 독자 대부분도 처음에는 없다. 튜토리얼이 "설치되어 있다고
치고" 명령만 나열하면 독자는 첫 단계에서 멈추고, 문서와 CLI 가 어긋나도 아무도 모른다. 또한 규칙 11.8 은 성능
수치를, 규칙 11.11 은 개인 경로·호스트명을 문서에 남기지 못하게 한다.

## 선택지 (Options)

1. 실데이터 경로만 적는다 — 독자가 지금 실행할 수 있는 것이 없고, 문서 검증도 불가능.
2. 합성(fake 엔진) 경로만 적는다 — 실제 사용법(Earthdata, 크레딧, MintPy)을 배울 수 없음.
3. **각 단계를 "실데이터" 와 "지금 바로(합성)" 두 줄로 적고, 출력은 값 없이 모양만, 명령은 테스트로 CLI 와 대조** — 선택.

## 결정 (Decision)

1. **흐름 고정.** 세 튜토리얼 모두 플랜 §4.5 의 순서 `search → precheck → plan → run → diagnose → validate →
   refpoint → sweep` 을 절 제목으로 쓴다. ISCE2 튜토리얼은 HyP3 와 같은 절은 반복하지 않고 달라지는 부분(설정 키,
   스케줄러, KB 표)만 적는다. 검증·튠 튜토리얼은 `validate/refpoint/closure/sweep` 를 세부 절차로 펼친다.
2. **두 줄 원칙.** 외부 엔진·자격증명이 필요한 단계는 (a) 정확히 무엇이 필요한지 — Earthdata Login URL, HyP3 사용
   승인, `.netrc` 우선 규칙(ADR-0013), 크레딧 페이지 URL(ADR-0020), 설치 절차는 `docs/install.md` 링크 — 를 표로
   적고, (b) 같은 명령을 fake 엔진·저장소 픽스처(`tests/fixtures/ground_truth/*.csv`)로 지금 돌리는 방법을 적는다.
   `search` 처럼 합성 대체가 없는 단계는 "없다" 고 쓰고 개발자용 우회(테스트 헬퍼)만 가리킨다.
3. **출력은 모양.** "예상 출력" 은 실제로 실행해 채록한 표 구조·상태 단어(`실행 예정/캐시됨/실행됨/실패/건너뜀`)·
   Finding ID 만 남기고, 수치는 `…`/`<n>`, 경로는 `<workdir>`/`<hash>` 로 바꾼다(규칙 11.8·11.11 — 스크래치 경로에
   사용자명이 들어 있어도 문서에는 절대 옮기지 않는다). 출력 블록은 ```` ```text ```` 로 표시해 명령 검사에서 제외한다.
4. **명령은 트리에서.** 명령줄 블록(```` ```bash ````)에는 `wintersar --help` 트리에 실제로 있는 명령·옵션만 쓴다.
   자리표시자(`<hash>`, `<repo>`)는 값 자리에만 둔다. 이 규칙은 `tests/unit/qgis/test_docs.py` 가 Click 트리로
   검사한다(ADR-0112). 옵션 값에 공백이 필요하면 따옴표로 묶는다(`--set "interferogram.shape=[16,16]"`).
5. **언어.** ADR-0072 를 그대로 따른다: 본문 한국어, 페이지 끝 `## English summary`(핵심 명령 포함, 30단어 이상),
   용어는 처음 병기 후 영어 원어. 완전한 영어판은 만들지 않는다(릴리스 노트에 명시).
6. **KB 개요의 위치.** `docs/kb/index.md` 는 생성물이므로 손으로 쓰는 개요는 `docs/kb/overview.md` 에 둔다. MkDocs 는
   `index.md` 가 있는 디렉터리의 `README.md` 를 무시하므로(위 출처) 이 파일은 GitHub 에서 읽는 안내문이며 nav 에는
   넣지 않는다 — `mkdocs.yml` 에 그 이유를 주석으로 남긴다.
7. **설치 문서와의 경계.** 설치 절차는 `docs/install.md` 한 곳에만 있고 튜토리얼은 링크만 한다(중복 금지). 튜토리얼의
   준비물 표는 "무엇이 필요한가" 까지만 적는다.

## 결과 (Consequences)

- 튜토리얼은 CLI 가 바뀌면 테스트로 깨진다 — 의도한 결과다. 명령 추가·옵션 이름 변경 시 문서를 같이 고친다.
- "예상 출력" 은 손으로 채록한 것이라 표 열 이름이 바뀌면 조용히 오래된다. 열 이름은 i18n 카탈로그 키에서 오므로,
  카탈로그를 바꾸는 사람이 튜토리얼의 `text` 블록을 함께 갱신해야 한다(자동화는 open-questions 에 두지 않고
  ADR-0112 의 후속 항목으로 남긴다).
- 실데이터 경로는 한 번도 실행되지 않았다. 처음 실행하는 사람이 어긋나는 부분(예: HyP3 산출물 파일명, MintPy 단계
  이름)을 고치고 그 ADR(0020/0021/0026)에 기록한다.
- 합성 검증 흐름이 픽스처 CSV 좌표와 fake 시계열 좌표의 일치에 의존한다. 둘 중 하나를 바꾸면 검증·튠 튜토리얼 0절이
  깨지므로 `tests/unit/validate` 의 픽스처 테스트와 함께 유지한다.
