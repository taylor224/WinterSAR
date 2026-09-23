# ADR-0111: 릴리스 노트와 DoD 보고 정책 (무엇을 "충족" 이라 부를 수 있는가)

- 상태(Status): 채택
- 날짜(Date): 2026-09-23
- 관련 ID: 플랜 §7(Phase 0–8 DoD), §9(라이선스 표), 규칙 11.5·11.7·11.8·11.9, ADR-0001, ADR-0054, ADR-0101
- 검증 출처(Sources):
  - 플랜 §7 각 Phase 의 DoD 문장(`docs/plan/wintersar_implementation_plan.md` 458–505행)
  - 트리 상태(2026-09-23): `wintersar version` = `0.1.0.dev0`; `wintersar diagnose --list-kb` = 20항목;
    `tests/fixtures/logs/manifest.yaml` 25 픽스처(`tests/unit/qgis/test_docs.py` 가 릴리스 노트의 개수와 대조);
    `docs/research/results/*.md` 5개; `.github/workflows/{ci,nightly}.yml` (`ci.yml` 의 `docs` 잡이 `mkdocs build --strict`);
    `pixi.toml` 있음 / `pixi.lock` 없음; `benchmarks/baselines/` 에 README 만 있음
  - CLAUDE.md Environment: 외부 엔진 미설치, 네트워크·자격증명 없음
  - `docs/open-questions.md` #4, #10, #22, #53, #58, #60, #67, #68, #70, #71

## 맥락 (Context)

Phase 8 은 "릴리스 노트" 와 "라이선스 표 확정", "벤치마크 표 링크" 를 요구한다. 이 트리에는 모든 Phase 의 코드가
있지만, DoD 의 상당수는 실데이터·외부 엔진·연구자 검수를 전제로 한다(Phase 1 실제 AOI 3곳, Phase 2 S 사이트 HyP3/
ISCE2, Phase 4 국내 사이트 RMSE, Phase 5 before/after, Phase 6 실데이터 2 사이트, Phase 7 QGIS LTR 2종). 코드가
있다는 이유로 이를 "완료" 라고 적으면 릴리스 노트가 독자와 발주자를 속인다. 반대로 전부 "미완" 이라고만 적으면 무엇이
실제로 검증됐는지 알 수 없다.

## 선택지 (Options)

1. Phase 단위로 "완료/미완" 두 값 — 근거 없이 뭉뚱그려짐.
2. **DoD 문장 단위로 네 값(충족·부분·미충족(환경)·미충족)과 근거 열** — 선택.
3. 릴리스 노트에 DoD 를 싣지 않고 PR 본문에만 둔다 — 규칙 11.5 는 PR 본문을 요구하지만 릴리스 독자는 PR 을 보지 않는다.

## 결정 (Decision)

1. **단위는 DoD 문장.** 플랜 §7 의 각 DoD 문장을 한 행으로 두고 상태와 근거를 적는다. Phase 요약 상태는 행들의
   최솟값이다(하나라도 미충족이면 Phase 는 "부분" 이하).
2. **상태 값의 정의.**
   - **충족**: 근거(테스트 파일, 실행 기록, ADR, `bench_result.json`)가 트리에 있고 이 환경에서 재현된다.
   - **부분**: 일부 하위 항목만 충족. 어느 부분이 빠졌는지 적는다.
   - **미충족(환경)**: 필요한 엔진·데이터·자격증명·소프트웨어(QGIS, pixi, Docker)가 개발 환경에 없어 확인할 수 없다.
     코드·ADR 이 있어도 이 값이다. 무엇이 있으면 확인 가능한지 open-questions 번호로 적는다.
   - **미충족**: 아직 하지 않았다.
3. **"코드 존재 ≠ 충족".** 어댑터가 상류 소스로 검증됐더라도(`# source:` 주석) 실행 기록이 없으면 "미충족(환경)" 이다.
   mock/fake 로 통과한 테스트는 그 테스트가 검사하는 범위(예: "파라미터 변경 시 하류만 재실행")만 충족으로 친다.
4. **수치 금지.** 릴리스 노트에는 실행 시간·메모리·크레딧·정확도 수치를 쓰지 않는다(규칙 11.8). 픽스처 개수, KB 항목
   수, 테스트 파일 이름처럼 트리에서 셀 수 있는 사실은 쓴다. "벤치마크 표 링크" 는 `bench_result.json` 이 커밋되거나
   CI 아티팩트로 존재할 때만 그 위치를 가리키고, 없으면 "없음" 이라고 적는다.
5. **고정 절.** 릴리스 노트는 항상 다음 절을 가진다: 한 줄 요약 · 들어 있는 것(모듈 표) · Phase 별 DoD 상태 ·
   알려진 제한 · 라이선스(ADR-0001 표 링크 + 미확인 행의 open-questions 번호) · 의존성 정책 예외(승인 대기 목록) ·
   검증 방법(CI 가 실제로 돌리는 명령) · 다음 릴리스(로드맵 링크) · English summary.
6. **버전 문자열.** 제목의 버전은 `wintersar version` 출력(`src/wintersar/__init__.py`)과 같아야 한다. 태그를 만들 때
   `0.1.0.dev0 → 0.1.0` 으로 바꾸고 이 문서를 `v0.1.0` 으로 갱신한다.
7. **개인·머신 정보 금지.** 릴리스 노트에는 호스트명·홈 경로·커널 버전·CPU/메모리 사양을 쓰지 않는다(규칙 11.11).
   `check-install` 출력을 인용할 때는 자리표시자로 바꾼다.

## 결과 (Consequences)

- v0.1.0-dev 릴리스 노트의 Phase 요약은 0 부분·1 부분·2 부분·3 충족(픽스처 기준)·4 부분·5 미충족(환경)·6 부분·7 부분·
  8 부분 이다. 정식 v0.1.0 은 최소한 Phase 2 HyP3 경로 실데이터 1회(#10 승인 후)와 라이선스 표(#4)가 닫혀야 한다는
  것이 이 정책의 함의다 — 그 판단은 발주자가 한다.
- 다른 모듈이 DoD 를 새로 충족하면 릴리스 노트의 해당 행을 근거와 함께 고친다. 근거 없는 상태 변경은 리뷰에서 되돌린다.
- 릴리스 노트가 인용하는 open-questions 번호는 `tests/unit/qgis/test_docs.py` 의 `OPEN_QUESTION_REFS` 로 행 내용과
  대조된다(ADR-0112).
