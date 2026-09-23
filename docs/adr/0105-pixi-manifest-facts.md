# ADR-0105: pixi 매니페스트 사실 확인 — 패키지·채널·플랫폼 표와 `pixi.toml` 구조

- 상태: 채택 (pixi 미설치 환경에서 문서·레지스트리로만 검증; 실제 `pixi lock` 은 open-questions #70)
- 날짜: 2026-09-23
- 관련 ID: PERF-12, 플랜 §4.2 (`pixi.toml`), §7 Phase 0, 규칙 11.2/11.3, open-questions #64/#67
- 검증 출처(전부 2026-09-23 조회):
  - pixi 매니페스트 레퍼런스: <https://pixi.prefix.dev/latest/reference/pixi_manifest/>
    (`pixi.sh/latest/…` 는 이 주소로 301 리다이렉트)
  - pixi CHANGELOG: <https://pixi.prefix.dev/latest/CHANGELOG/> (0.43.0 2025-03-20 "Renaming `[project]` to
    `[workspace]`", 0.57.0 2025-10-20 "`[project]`: should be replaced by `[workspace]`" 경고)
  - pixi 환경 문서: <https://pixi.prefix.dev/latest/workspace/environment/>, 락파일 문서:
    <https://pixi.prefix.dev/latest/workspace/lock_file/>, CLI: <https://pixi.prefix.dev/latest/reference/cli/pixi/install/>,
    <https://pixi.prefix.dev/latest/reference/cli/pixi/run/>, <https://pixi.prefix.dev/latest/reference/cli/pixi/lock/>,
    <https://pixi.prefix.dev/latest/reference/cli/pixi/shell-hook/>, 파이썬 튜토리얼 <https://pixi.prefix.dev/latest/python/tutorial/>
  - conda-forge 패키지 레코드: `https://api.anaconda.org/package/conda-forge/<pkg>` (anaconda.org 의 JSON API; HTML 페이지
    `https://anaconda.org/conda-forge/<pkg>` 는 플랫폼 목록을 정적으로 보여 주지 않음)
  - conda-forge 레시피: isce2-feedstock `recipe/meta.yaml`·`recipe/build.sh`·`recipe/scripts/activate.sh`,
    tophu-feedstock·mintpy-feedstock·dolphin-feedstock·snaphu-feedstock `recipe/meta.yaml`
    (<https://raw.githubusercontent.com/conda-forge/<name>-feedstock/main/recipe/…>)
  - ISCE2 저장소 git tree API (`contrib/stack/topsStack/stackSentinel.py` mode `100755`)

## 맥락

플랜 §4.2 는 `pixi.toml` 을 "재현 가능한 환경(isce2·snaphu-py·tophu·mintpy 는 conda-forge)" 으로 두고 §6.1 PERF-12 는
"pixi/conda-lock 락파일, Docker 이미지, `check_install`" 을 설치 실패율 대책으로 든다. 지금까지는 `uv.lock` 과
튜토리얼의 conda 명령만 있었다(open-questions #67). 이 머신에는 pixi·conda 가 없으므로(CLAUDE.md Environment)
매니페스트의 모든 키와 패키지 이름·플랫폼을 공식 문서와 레지스트리로 확인해 아래에 남긴다(규칙 11.3).

## 확인한 pixi 매니페스트 사실

| 항목 | 확인 내용 | 출처 |
|---|---|---|
| 최상위 표 | `[workspace]` (필수 키 `channels`, `platforms`; 선택 `name`, `version`, `description`, `license`, `license-file`, `readme`, `requires-pixi`, `preview`). `[project]` 는 0.43.0 에서 `[workspace]` 로 개명, 0.57.0 부터 사용 시 경고 | 매니페스트 레퍼런스, CHANGELOG |
| 플랫폼 이름 | `linux-64`, `osx-64`, `osx-arm64`, `win-64`, `win-arm64` … (rattler `Platform` enum 링크) | 매니페스트 레퍼런스 `platforms` |
| `[dependencies]` | conda MatchSpec 문자열(`">3.9,<=3.11"`, `"==1.72"`) 또는 `{ version=…, channel=… }` | 동 |
| `[pypi-dependencies]` | `pkg = ">=1.0"`, `pkg = { version=…, extras=[…] }`, 로컬 editable `pkg = { path = ".", editable = true }`, `git`/`url`; `"*"` 는 "any version" 을 뜻하는 pixi 확장 | 동 |
| conda ↔ PyPI 해상도 | pixi 는 uv 를 resolver 로 쓰고, 이미 해상된 conda 패키지를 "locked" 로 취급해 같은 이름의 PyPI 패키지를 다시 설치하지 않음 | 동 `pypi-dependencies` 절 |
| `[feature.<n>]` | `dependencies`, `pypi-dependencies`, `tasks`, `activation`, `channels`, `platforms`(워크스페이스 플랫폼의 부분집합), `target` | 동 |
| 환경 플랫폼 | "The `platforms` of the environment is the intersection of the `platforms` of all its features." | 동 |
| `[environments]` | `env = ["feat"]` 또는 `env = { features=[…], solve-group="…", no-default-feature=… }`; `default = { solve-group = "default" }` 형태 문서 예시 있음 | 매니페스트 레퍼런스, 파이썬 튜토리얼 |
| `[activation]` | `scripts=[…]`(bash 로 *호출* 후 결과 env 만 반영), `env={ VAR="$OTHER_ENV_VAR/unix-value" }`(기존 변수 참조 가능). **패키지 `etc/conda/activate.d` 스크립트·매니페스트 `scripts`·`env` 의 적용 순서와 확장 시점의 `CONDA_PREFIX` 가용성은 문서에 없음** → #69 | 매니페스트 레퍼런스 |
| 패키지 활성화 스크립트 | pixi 활성화는 "runs activation scripts that are presented by the installed packages" (`etc/conda/activate.d/*.sh`); `pixi shell-hook` 출력에 `export CONDA_PREFIX=…`, `PATH`, `CONDA_DEFAULT_ENV`, `PIXI_ENVIRONMENT_NAME`, `PIXI_PROJECT_ROOT` 포함 | 환경 문서 |
| 환경 디렉터리 | "All Pixi environments are by default located in the `.pixi/envs` directory of the workspace" (`.pixi/envs/<name>/{bin,conda-meta,etc,include,lib}`) — open-questions #64 가 묻던 레이아웃 | 환경 문서, 컨테이너 문서(`/app/.pixi/envs/prod`) |
| `[tasks]` | `name = "cmd"` 또는 `{ cmd=…, depends-on=…, cwd=…, inputs=…, outputs=…, env=… }`; deno_task_shell 로 실행 | 매니페스트 레퍼런스, `pixi run` |
| `[system-requirements]` | **deprecated** — 새 매니페스트에서 쓰지 않음 (플랫폼 항목에 inline-table 로 선언) | 매니페스트 레퍼런스 |
| 락파일 | `pixi install`/`run`/`shell`/`add`… 가 `pixi.lock` 생성·갱신, `pixi lock --check` 로 검사; "not meant to be edited by hand"; 재현성을 위해 커밋 권장 | 락파일 문서, `pixi lock` |
| CLI 플래그 | `pixi install -e <env>` / `--all` / `--locked`(락 불일치 시 중단) / `--frozen`(락대로만 설치) / `-m <manifest>`; `pixi run -e`, `pixi shell-hook -e <env> -s bash` | CLI 레퍼런스 |

## 확인한 conda-forge 패키지 사실 (2026-09-23)

| 요청된 이름 | conda-forge 실제 이름 | 최신 버전 | 플랫폼(레코드 `platforms`) | 라이선스(메타데이터) | `pixi.toml` 처리 |
|---|---|---|---|---|---|
| isce2 | `isce2` | 2.6.5 | **linux-64, osx-64 만** (osx-arm64 없음; 레시피 `skip: true  # [win or py>312]`) | Apache-2.0 | feature `isce2`, `platforms = ["linux-64", "osx-64"]`, `>=2.6,<3` |
| snaphu (C 바이너리) | 별도 패키지 없음 — `snaphu` 가 snaphu-py 이고 C 코어를 vendoring | — | — | — | 번들 금지(ADR-0001/0023); snaphu-py 로 대체 |
| snaphu-py | `snaphu` (레시피 `name: snaphu`, home isce-framework/snaphu-py) | 0.4.1 | linux-64, osx-64, osx-arm64 (레시피 skip `py<39 or not unix`) | (Apache-2.0 OR BSD-3-Clause) AND LicenseRef-SNAPHU | feature `unwrap`, `>=0.4,<1` |
| `snaphu-py` 라는 이름 | **없음** (HTTP 404) | — | — | — | 사용하지 않음 |
| tophu | `tophu` | 0.2.1 | `noarch: python` 이지만 run 요구사항에 `__linux  # [linux]` → **Linux 전용**; `isce3 >=0.12` 필요 | BSD-3-Clause OR Apache-2.0 | feature `tophu`, `platforms = ["linux-64"]`, `>=0.2,<1` |
| (tophu 의존) isce3 | `isce3` | 0.25.17 | linux-64, osx-64, osx-arm64, linux-aarch64 | Apache-2.0 | 직접 선언하지 않음 |
| mintpy | `mintpy` | 1.6.4 | noarch (linux-64/osx-64 의 1.3.1 은 구 빌드); run: asf_search, cartopy, cvxopt, dask, gdal, h5py, pyaps3, pysolid(0.3.4: linux-64/osx-64/osx-arm64/…), pyproj, pyresample, scikit-image, shapely … | GPL-3.0-or-later | feature `mintpy`, `>=1.5,<2`, subprocess 전용 |
| dolphin | `dolphin` (isce-framework/dolphin, entry point `dolphin = dolphin.cli:main`) | 0.42.5 | noarch; run: jax >=0.4.19(jaxlib 0.10.2: linux-64/osx-64/osx-arm64/linux-aarch64), gdal >=3.5, h5py, numba, opera-utils, pydantic >=2.1, pyproj, rasterio >=1.3, ruamel.yaml, scipy, **snaphu >=0.4.0**, threadpoolctl, tqdm, tyro | BSD-3-Clause OR Apache-2.0 | feature `dolphin`, `>=0.40,<1` |
| sentineleof | `sentineleof` | 0.13.0 | noarch | MIT | feature `aux`, `>=0.10` |
| sardem | `sardem` | 0.13.0 | noarch | MIT | feature `aux`, `>=0.11` |
| gdal | `gdal` | 3.13.3 | linux-64, osx-64, osx-arm64, win-64, linux-aarch64, linux-ppc64le (files API 의 subdir 집계) | MIT | feature `geo`, `>=3.5` |
| rasterio | `rasterio` | 1.5.1 | linux-64, osx-64, osx-arm64, win-64, linux-aarch64, linux-ppc64le | BSD-3-Clause | feature `geo`, `>=1.3` |
| hyp3-sdk | `hyp3_sdk` | 7.7.8 | noarch | BSD-3-Clause | PyPI `hyp3-sdk>=7.0` 로 유지(pyproject extra 와 비교 가능하도록) |
| spurt | **없음** (HTTP 404) | — | — | — | 제외 (ADR-0025, open-questions #22) |

ISCE2 세부(어댑터 `engines/isce2_topsstack.py` 가 `shutil.which("stackSentinel.py")` 로 감지하므로 중요):

- 레시피 `build.sh`: `mv $SRC_DIR/isce2/contrib/stack/* $PREFIX/share/isce2` (timeseries 도 동일) 후
  `etc/conda/activate.d/isce2-activate.sh` 설치.
- `scripts/activate.sh`: `export ISCE_HOME=…(python -c "import isce…")`, `export ISCE_STACK=$CONDA_PREFIX/share/isce2`.
  **PATH 에 topsStack 을 추가하지 않는다.**
- 상류 저장소에서 `stackSentinel.py` 는 mode `100755`(실행 비트 있음) → PATH 에만 있으면 `which` 로 찾힌다.
- 따라서 `pixi.toml` 은 `[feature.isce2.activation.env] PATH = "$CONDA_PREFIX/share/isce2/topsStack:$PATH"` 를 두고,
  `Dockerfile.engines` 는 shell-hook 생성 전에 `ENV PATH=/app/.pixi/envs/engines/share/isce2/topsStack:…` 로 고정한다.

## 선택지

1. conda-lock + `environment.yml` — 락은 되지만 PyPI editable 설치·태스크·다중 환경이 없음.
2. `pyproject.toml` 의 `[tool.pixi.*]` — 파일 하나로 uv 와 pixi 를 겸하지만 `pyproject.toml` 은 공용 수정 금지
   파일이고 uv 의 `[project]` 와 pixi 의 해석이 한 파일에서 섞인다.
3. 별도 `pixi.toml` + 핵심 목록 복제 + 드리프트 검사(`scripts/check_env.py`) — **채택** (ADR-0107).

## 결정

- `pixi.toml` 을 위 표대로 작성한다: `[workspace]` 채널 `conda-forge`, 플랫폼 `linux-64, osx-arm64, osx-64`,
  `requires-pixi = ">=0.43"`(`[workspace]` 도입 버전).
- default 환경 = 핵심(conda 는 `python >=3.11,<3.12` 만, 나머지는 `pyproject.toml` 과 동일한 PyPI 목록 +
  `wintersar = { path = ".", editable = true }`).
- feature: `geo`(conda gdal/rasterio/pyproj/shapely/h5py — 엔진 환경에서 GDAL/PROJ 를 하나로), `hyp3`(PyPI),
  `unwrap`(snaphu), `tophu`(linux-64), `isce2`(linux-64/osx-64), `mintpy`, `dolphin`, `aux`, `dev`.
  과제 설명의 "unwrap(snaphu + tophu)" 는 tophu 의 Linux 전용 사실 때문에 `unwrap` 과 `tophu` 두 feature 로 나눴다.
- 환경: `default`, `dev`, `engines`(전 feature; 교집합 규칙으로 linux-64 만), `engines-portable`(isce2·tophu 제외; 세
  플랫폼) — macOS/Apple silicon 사용자를 위한 추가 환경.
- 태스크: `check-install`(default), `test`/`lint`/`typecheck`(dev).
- 버전 조건은 어댑터의 `version_constraint` 와 동일하게 두고 테스트(`tests/unit/test_pixi_manifest.py`)가 이를 고정한다.

## 결과

- `pixi.lock` 은 아직 없다. pixi 가 있는 머신에서 첫 `pixi install`/`pixi lock` 이 만든 락파일을 커밋해야
  재현이 성립한다(open-questions #70). noarch 패키지(mintpy, dolphin)의 전이 의존성이 osx-64/osx-arm64 에서
  실제로 해상되는지도 그때 확인된다(위 표는 직접 의존성까지만 확인).
- `activation.env` 의 `$CONDA_PREFIX` 확장 시점은 미확인(#69). 실패 시 폴백은 `docs/install.md` 의 수동 `export PATH`
  와 `Dockerfile.engines` 의 `ENV PATH`.
- open-questions #64(플러그인의 pixi 환경 경로 힌트)는 위 "환경 디렉터리" 행으로 답이 확인됐다 — docs/qgis 담당이
  `.pixi/envs/<env>/bin/python` 직접 경로를 추가할 수 있다(이 ADR 은 해당 코드를 수정하지 않는다).
- 되돌리는 조건: conda-forge 가 isce2 osx-arm64 빌드를 내거나 tophu 가 `__linux` 제약을 풀면 feature `platforms` 를
  넓힌다(이 ADR 표를 갱신하고 테스트 `test_platform_exclusions_match_conda_forge_facts` 를 바꾼다).
