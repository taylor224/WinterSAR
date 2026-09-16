# ADR-0071: 플러그인의 툴킷 환경 탐색 (explicit python → conda/pixi/uv/venv 힌트 → PATH)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-12, PERF-12, ENV-004, 플랜 §5.8
- 검증 출처(Sources): 2026-09-16 WebFetch
  - conda run: https://docs.conda.io/projects/conda/en/stable/commands/run.html —
    `usage: conda run [-h] [-n ENVIRONMENT | -p PATH] [-v] [--dev] [--debug-wrapper-scripts] [--cwd CWD] [-s] ...`;
    `-n, --name` "Name of environment."; `-p, --prefix` "Full path to environment location (i.e. prefix)."; 
    `-s, --no-capture-output, --live-stream` "Don't capture stdout/stderr"
  - conda 환경 목록: https://docs.conda.io/projects/conda/en/stable/user-guide/tasks/manage-environments.html —
    `conda info --envs` 가 `~/.conda/environments.txt` 의 환경 접두사(prefix) 목록을 사용
  - pixi run: https://pixi.prefix.dev/latest/reference/cli/pixi/run/ — `pixi run [OPTIONS] [TASK]...`,
    `pixi run --manifest-path ~/myworkspace/pixi.toml python`, `--environment`
  - uv run: https://docs.astral.sh/uv/reference/cli/#uv-run — `uv run [OPTIONS] [COMMAND]`; `--project`
    "Discover a project in the given directory … as will the project's virtual environment (`.venv`)";
    `--no-sync` "Avoid syncing the virtual environment … Implies `--frozen`"
  - `QgsApplication.qgisSettingsDirPath()` "Returns the path to the settings directory in user's home dir"
    (https://qgis.org/pyqgis/3.44/core/QgsApplication.html) — 설정 JSON 저장 위치
  - rich 가 `NO_COLOR` 를 존중함: `.venv/lib/python3.11/site-packages/rich/console.py`
    (`self._environ.get("NO_COLOR", "") != ""`) — stderr 로그에 ANSI 색 코드 제거
  - 콘솔 스크립트 이름: `pyproject.toml [project.scripts] wintersar = "wintersar.cli:main"`

## 맥락 (Context)

플러그인은 QGIS 파이썬이 아닌 "툴킷이 설치된 환경"의 파이썬을 찾아야 한다(ADR-0070). 사용자는
conda(연구자 대부분), pixi(플랜 §4.2 의 재현 환경), uv(이 저장소의 개발 환경), 일반 venv 중 하나를 쓴다.
QGIS 안의 `PATH` 는 셸 초기화 파일을 거치지 않아 `conda`/`pixi`/`uv` 실행 파일이 보이지 않을 수 있다.

## 선택지 (Options)

1. 항상 `conda run -n <env>` 류의 런처를 통해 실행.
2. **인터프리터 경로를 우선 결정하고 런처는 폴백으로만 사용**: 파일 시스템에서 환경의 `bin/python`
   (`python.exe`/`Scripts/python.exe`)을 찾으면 그것을 직접 실행한다.
3. 플러그인이 자체 venv 를 만들고 `pip install wintersar` 를 수행.

## 결정 (Decision)

선택지 2. `cli_client.resolve_command(python_exe, env_hint)` 의 순서:

1. **명시적 파이썬 실행 파일**(설정 `python_exe`) → `[python_exe, "-m", "wintersar.cli"]`.
2. **환경 힌트**(설정 `env_hint`, 형식 `<kind>:<target>`):
   - `conda:<prefix 경로>` → `<prefix>/bin/python`; 없으면 `conda run -p <prefix> --no-capture-output python -m wintersar.cli`
   - `conda:<이름>` → `~/.conda/environments.txt` 에서 마지막 경로 요소가 이름과 같은 접두사를 찾아 그
     파이썬을 실행; 없으면 `conda run -n <이름> --no-capture-output python -m wintersar.cli`
   - `pixi:<디렉터리|pixi.toml>` → `pixi run --manifest-path <pixi.toml> python -m wintersar.cli`
     (pixi 환경 디렉터리 레이아웃은 검증하지 않았으므로 직접 경로 추정을 하지 않는다)
   - `uv:<프로젝트 디렉터리>` → `<dir>/.venv/bin/python` 이 있으면 직접 실행(uv 문서가 `.venv` 를 프로젝트
     환경으로 명시); 없으면 `uv run --project <dir> --no-sync python -m wintersar.cli`
   - `venv:<디렉터리>` → `<dir>/bin/python` 또는 `Scripts/python.exe`
   - `python:<실행 파일>` → 1 과 동일
3. `PATH` 의 `wintersar` 콘솔 스크립트.
4. 아무것도 없으면 `CLI_NOT_FOUND` Finding(원인: 환경, 조치: 실행 파일/힌트 지정).

`discover_environments()` 는 subprocess 없이 파일 시스템만 훑어 `bin/wintersar` 콘솔 스크립트가 있는
환경(`~/.conda/environments.txt`, `~/{miniconda3,anaconda3,miniforge3,mambaforge,micromamba}/envs/*`,
`$CONDA_PREFIX`, `$VIRTUAL_ENV`, `./.venv`)을 후보로 제시하고, 실행 환경 상자의 콤보에서 고르면
`python_exe` 가 채워진다. 설정(`settings.py::PluginSettings`)은 QGIS 프로필 디렉터리
(`qgisSettingsDirPath()`)의 `wintersar_qgis.json` 에 원자적으로 저장한다(QGIS 밖에서는 OS 별 사용자 설정
디렉터리 또는 `WINTERSAR_QGIS_SETTINGS_DIR`).

실행 환경 변수: `WINTERSAR_LANG=<lang>`, `PYTHONIOENCODING=utf-8`, `NO_COLOR=1`(기본값, 덮어쓰기 가능).
Windows 에서는 `CREATE_NO_WINDOW` 로 콘솔 창을 띄우지 않는다.

## 결과 (Consequences)

- 런처 없이도(예: QGIS `PATH` 에 conda 가 없어도) 환경 접두사만 알면 동작한다. 반대로 `conda run` 폴백은
  QGIS 가 `conda` 를 찾을 수 있을 때만 성립한다 → `설치 점검` 버튼이 `resolved` 라벨에 실제 명령을 보여 준다.
- 파이썬을 직접 실행하므로 conda 활성화 스크립트가 설정하는 환경 변수(`PROJ_LIB`, `GDAL_DATA` 등)는 적용되지
  않는다. conda-forge 의 rasterio/pyproj 휠은 자체 데이터 경로를 쓰므로 보통 문제가 없지만, ISCE2 처럼
  활성화 훅에 의존하는 엔진은 `conda run` 경로가 필요할 수 있다 → open-questions 에 남긴다.
- `environments.txt` 는 conda 의 내부 파일(문서상 `conda info --envs` 의 근거)이며 형식이 바뀌면
  `conda_prefixes()` 만 고치면 된다. 없으면 빈 목록으로 조용히 폴백한다.
