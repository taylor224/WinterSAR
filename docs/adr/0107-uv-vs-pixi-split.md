# ADR-0107: uv 와 pixi 의 역할 분담 — `pyproject.toml` 이 핵심 의존성의 원천, `pixi.toml` 은 엔진 환경

- 상태: 채택
- 날짜: 2026-09-23
- 관련 ID: PERF-12, 플랜 §4.2, §7 Phase 0, ADR-0001, ADR-0105, ADR-0106, 규칙 11.1/11.3, CLAUDE.md Environment
- 검증 출처: pixi 매니페스트 레퍼런스 <https://pixi.prefix.dev/latest/reference/pixi_manifest/> (`pixi.toml` 과
  `pyproject.toml`(`[tool.pixi.*]`) 두 형식이 같은 표 구조를 가짐, 같은 디렉터리에 둘 다 있으면 `pixi.toml` 우선),
  pixi 파이썬 튜토리얼 <https://pixi.prefix.dev/latest/python/tutorial/>, 이 저장소 `pyproject.toml`·`uv.lock`·
  `.github/workflows/ci.yml`

## 맥락

이 저장소는 uv 로 개발한다(`uv sync --extra dev`, CI, QGIS 플러그인의 환경 힌트). 엔진 환경은 pixi 로 재현해야 한다
(ADR-0105). 두 도구가 각각 의존성 목록을 가지면 어긋나기 쉽다. 무엇이 어디에 살고, 어느 쪽이 원천인지 정한다.

## 선택지

1. **`pyproject.toml` 에 `[tool.pixi.*]` 를 넣어 파일 하나로 통합.** pixi 가 `[project].dependencies` 를 PyPI 의존성으로
   읽으므로 복제가 없다. 그러나 `pyproject.toml` 은 병렬 작업에서 수정 금지 공용 파일이고, uv 의 `[project]`·
   `[tool.uv]` 와 pixi 의 `[tool.pixi]` 가 한 파일에 섞이며, 엔진 feature 가 늘 때마다 공용 파일을 건드린다.
2. **별도 `pixi.toml` + 핵심 목록 복제 + 드리프트 검사** — 채택.
3. pixi 를 유일한 도구로 삼고 uv 를 버림 — CI·플러그인·개발 흐름 전부 재작성, conda 가 없는 개발자(현재 이 머신)를
   배제.

## 결정

| 관심사 | 사는 곳 | 이유 |
|---|---|---|
| 핵심 런타임 의존성 목록(이름·버전 조건) | **`pyproject.toml` `[project].dependencies`** (원천) | 패키지 메타데이터·휠 빌드·uv·CI 가 전부 이것을 읽음 |
| optional extras (`hyp3`, `orbits`, `dem`, `unwrap`, `dev`, …) | `pyproject.toml` `[project.optional-dependencies]` | ADR-0001 의 정책 예외 단위 |
| 핵심 목록의 pixi 사본 | `pixi.toml` `[pypi-dependencies]` (+ `wintersar` editable) | 독립 `pixi.toml` 은 다른 파일의 표를 참조할 수 없음 |
| `dev`·`hyp3` extras 의 pixi 사본 | `[feature.dev.pypi-dependencies]`, `[feature.hyp3.pypi-dependencies]` | PyPI 로 설치되는 extras 는 그대로 복제 |
| `orbits`/`dem`/`unwrap` extras | pixi 에서는 conda feature `aux`(sentineleof, sardem)·`unwrap`(snaphu) | conda-forge 빌드를 쓰기 위해; 이름·조건은 PyPI 와 같게 유지하고 검사 대상에 포함(2차 리뷰) |
| `plots` extra | 별도 feature 없음 — `[feature.dev.pypi-dependencies]` 의 부분집합 | `dev` extra 가 이미 matplotlib/pandas 를 포함; 검사는 포함 여부·조건만 |
| uv 전용 extras (`spurt`, `gpu`, `docs`) | `pyproject.toml` 만 — `scripts/check_env.py` `UV_ONLY_EXTRAS` 에 명시 | spurt 는 conda-forge 패키지 없음(ADR-0025), gpu 는 엔진 환경·`Dockerfile.engines` 가 CPU 전용(docs/install.md), docs 는 uv/CI 경로 |
| conda 전용 엔진(isce2, tophu, mintpy, dolphin)·`geo` | `pixi.toml` feature 만 — `scripts/check_env.py` `PIXI_ONLY_FEATURES` 에 명시 | PyPI 에 없거나(ADR-0001) 정책상 핵심이 아님 |
| 락파일 | `uv.lock`(core), `pixi.lock`(전 환경; 생성 대기 #70) | 각 도구의 재현 단위 |
| 드리프트 검사 | `scripts/check_env.py` ← `tests/unit/test_pixi_manifest.py` | 그룹 core/dev/hyp3/unwrap/aux(orbits+dem)/plots 의 이름(PEP 503 정규화)·조건(공백 제거, 절 정렬, `"*"`≡무조건)이 다르면 테스트 실패. `--group` 없이 전체를 비교할 때는 분류 검사도 수행: `GROUPS`·`UV_ONLY_EXTRAS`·`PIXI_ONLY_FEATURES` 어디에도 없는 extra/feature 는 드리프트로 보고(새 extra/feature 추가 시 결정을 강제). 종료 코드 0 일치 · 1 드리프트 · 2 입력 파일 없음/TOML 오류/요구사항 파싱 실패(stderr 한 줄, 트레이스백 없음) |
| Docker | 루트 `Dockerfile` = uv core(CI 빌드), `Dockerfile.engines` = pixi engines(수동, ADR-0106) | 라이선스 격리·빌드 비용 |

운영 규칙:

- 핵심 의존성이나 미러된 extra(dev, hyp3, unwrap, orbits, dem, plots)를 추가·변경할 때는 `pyproject.toml` 과
  `pixi.toml` 을 **함께** 고친다. 하나만 고치면 `test_check_env_script_passes_on_repo` 가 실패해 알려 준다.
- 새 extra 나 새 pixi feature 는 `scripts/check_env.py` 의 `GROUPS`(미러 대상)나 `UV_ONLY_EXTRAS`/
  `PIXI_ONLY_FEATURES`(한쪽에만 두기로 결정) 중 한 곳에 반드시 등록한다 — 등록 없이는 검사가 실패한다.
- conda feature 에 미러된 extra 의 버전 조건은 PEP 440 표기(`>=0.4,<1`)를 그대로 쓴다. conda MatchSpec 만의
  표기(`0.4.*` 등)는 문자열 비교에서 드리프트로 잡힌다(의도된 동작: 두 파일의 표기를 같게 유지).
- `pixi.toml` 의 default 환경은 conda 로 인터프리터만 받고 나머지는 PyPI 에서 받는다 — uv 환경과 같은 휠을 쓰므로
  두 환경의 동작 차이가 생기지 않는다. conda 빌드(GDAL/PROJ 공유)는 엔진 환경의 `geo` feature 에서만 켠다.
- 엔진 버전 조건은 어댑터의 `version_constraint` 가 원천이고 `pixi.toml` 이 이를 복제한다(테스트로 고정).

## 결과

- 파일이 둘이지만 검사가 자동이라 드리프트 비용은 테스트 1개 실패로 수렴한다.
- `[tool.pixi]` 통합(선택지 1)은 `pyproject.toml` 수정 금지가 풀리고 uv/pixi 공존 규칙이 확정되면 다시 검토한다;
  그때도 검사 스크립트는 `[tool.pixi.pypi-dependencies]` 경로만 바꾸면 된다.
- 후속: `.gitignore` 에 `.pixi/` 추가, `mkdocs.yml` nav 와 ADR 색인에 `install.md`·0105–0107 등록(공용 파일이라
  통합자가 반영).
