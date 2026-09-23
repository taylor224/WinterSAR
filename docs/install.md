# 설치 (Install) — uv · pixi · Docker 세 경로

`wintersar` 는 세 가지 방법으로 설치할 수 있고, 각 경로가 **실행할 수 있는 것**이 다릅니다 (PERF-12,
[ADR-0105](adr/0105-pixi-manifest-facts.md) · [ADR-0106](adr/0106-docker-engines-image-policy.md) ·
[ADR-0107](adr/0107-uv-vs-pixi-split.md)). 어느 경로든 마지막에 `wintersar check-install` 로 무엇이 빠졌는지
확인합니다 — 미설치 엔진은 `ENV-001` Finding 으로 보고되고 설치 힌트가 함께 나옵니다.

| 경로 | 명령 | 실행할 수 있는 것 | 실행할 수 없는 것 |
|---|---|---|---|
| **uv (core)** — 이 저장소의 기본·개발 환경 | `uv sync --extra dev` | 검색·선별·사전검증(`search`/`precheck`), `plan`, fake 엔진 합성 파이프라인, `diagnose`/`validate`/`bench`, 문서 빌드. **HyP3 원격 처리**는 `--extra hyp3` 승인 뒤 가능 | 로컬 엔진 전부(ISCE2, SNAPHU/snaphu-py, tophu, MintPy, dolphin) — conda-forge 전용이라 uv 로는 설치되지 않음 |
| **pixi (engines)** — 로컬 엔진 재현 환경 | `pixi install -e engines` (linux-64) · `pixi install -e engines-portable` (macOS) | 위 전부 + **로컬 ISCE2 topsStack**, snaphu-py, tophu, MintPy, dolphin, 궤도(sentineleof)·DEM(sardem) | `engines-portable`(macOS·Apple silicon)에는 ISCE2·tophu 가 없음 — conda-forge 에 osx-arm64 빌드가 없고 tophu 는 Linux 전용 |
| **Docker (engines)** — pixi `engines` 환경을 담은 이미지 | `docker build -f Dockerfile.engines -t wintersar-engines .` | pixi `engines` 와 동일(linux-64) — 호스트를 건드리지 않음 | GPU(CUDA 베이스 아님), spurt(conda-forge 패키지 없음, ADR-0025) |

## 1. uv — core (기본)

```bash
git clone https://github.com/taylor224/WinterSAR && cd WinterSAR
uv sync --extra dev                 # Python 3.11 venv (.venv), pyproject.toml 이 의존성의 원천
uv run wintersar --help
uv run wintersar check-install      # 엔진·인증·하드웨어 상태 (미설치 엔진은 ENV-001)
```

이 환경으로 할 수 있는 실제 처리는 **HyP3 원격 경로**입니다(로컬 ISCE2 없음). `hyp3-sdk`·`sentineleof`·`sardem`·
`snaphu` 는 정책상(월 다운로드 기준, [ADR-0001](adr/0001-license-and-engine-boundaries.md)) 핵심 의존성이 아니라
**optional extra** 로만 선언되어 있고, 개발 환경에 설치하려면 발주자 승인이 필요합니다
([open-questions #10](open-questions.md)). 승인 뒤:

```bash
uv sync --extra dev --extra hyp3            # HyP3 원격 처리 (hyp3-sdk, BSD-3-Clause)
uv sync --extra dev --extra orbits --extra dem   # 궤도(sentineleof), DEM(sardem)
uv sync --extra dev --extra unwrap          # snaphu-py PyPI 휠 (SNAPHU C 코어 포함 — 라이선스 §9 참고)
```

## 2. pixi — engines (로컬 엔진)

[pixi](https://pixi.prefix.dev/) 는 conda-forge 패키지와 PyPI 패키지를 한 락파일로 재현합니다. `pixi.toml` 의
핵심 의존성 목록과 미러된 extras(`dev`, `hyp3`, `unwrap`, `orbits`+`dem`→`aux`, `plots`)는 `pyproject.toml` 과
이름·버전 조건까지 같아야 하며 `scripts/check_env.py`(테스트 `tests/unit/test_pixi_manifest.py`)가 어긋나면
실패시킵니다 (ADR-0107). `spurt`·`gpu`·`docs` extra 는 uv 전용이라 pixi 환경에 없습니다(스크립트의
`UV_ONLY_EXTRAS`). 종료 코드: 0 일치, 1 드리프트, 2 입력 파일 없음/TOML 오류.

```bash
# pixi 설치: https://pixi.prefix.dev/latest/installation/
pixi install -e engines             # linux-64: ISCE2 + snaphu-py + tophu + MintPy + dolphin + 궤도/DEM
pixi install -e engines-portable    # macOS(osx-64, osx-arm64): ISCE2·tophu 를 뺀 나머지
pixi run -e engines wintersar check-install
pixi run -e engines wintersar check-install --strict --engine isce2_topsstack --engine snaphu --engine mintpy
pixi install -e dev && pixi run -e dev test    # 개발 환경(uv 와 같은 목록) + pytest
```

환경별 내용 (`pixi.toml` `[environments]`):

| 환경 | 구성 feature | 플랫폼 |
|---|---|---|
| `default` | core(PyPI, `pyproject.toml` 과 동일) + `wintersar` editable | linux-64, osx-arm64, osx-64 |
| `dev` | default + ruff/mypy/pytest… (`pyproject` dev extra 와 동일) | 동일 |
| `engines` | geo(conda GDAL/PROJ) + hyp3 + unwrap(snaphu-py) + tophu + isce2 + mintpy + dolphin + aux | **linux-64 만** (isce2·tophu 제약) |
| `engines-portable` | engines 에서 isce2·tophu 제외 | linux-64, osx-arm64, osx-64 |

알아 둘 점:

- conda-forge 의 `snaphu` 패키지가 곧 snaphu-py 입니다(`snaphu-py` 라는 패키지는 없음). SNAPHU C 코어는
  snaphu-py 가 vendoring 하며 wintersar 는 아무것도 번들하지 않습니다 (ADR-0001/0023).
- ISCE2 의 topsStack(`stackSentinel.py`)은 conda 패키지의 `share/isce2/topsStack` 에 설치되고 PATH 에는
  올라가지 않습니다. `pixi.toml` 의 `[feature.isce2.activation.env]` 가 PATH 에 추가하지만 pixi 가
  `$CONDA_PREFIX` 를 확장하는 시점은 문서에 없어 확인 대기입니다(open-questions #69). `check-install` 이
  `isce2_topsstack` 을 `ENV-001` 로 보고하면 수동으로:
  `export PATH="$PWD/.pixi/envs/engines/share/isce2/topsStack:$PATH"`.
- `pixi.lock` 은 이 저장소에 아직 없습니다(작성 머신에 pixi 가 없어 생성 못 함, open-questions #70). 처음
  `pixi install`/`pixi lock` 을 실행한 사람이 생성된 `pixi.lock` 을 커밋해야 그 뒤의 설치가 재현됩니다.
- MintPy(GPL-3)는 subprocess 로만 실행합니다. 같은 환경에 설치되어도 wintersar 가 import 하지 않습니다(규칙 11.2).
- `spurt` 는 conda-forge 에 없어(ADR-0025) 어느 환경에도 들어 있지 않습니다. 필요하면 승인(#22) 뒤 `pip install spurt`.

## 3. Docker — engines 이미지

`Dockerfile.engines` 는 `ghcr.io/prefix-dev/pixi` 위에서 pixi `engines` 환경을 설치하고, 마지막 단계에서
`wintersar check-install --strict` 로 모든 엔진이 감지되는지 확인합니다. **CI 에서는 빌드하지 않습니다** — 빌드
시간과 라이선스 격리 때문이며 이유는 ADR-0106 에 있습니다. 직접 빌드:

```bash
docker build -f Dockerfile.engines -t wintersar-engines .
docker run --rm wintersar-engines wintersar check-install
docker run --rm -v "$PWD/work:/work" -v "$HOME/.netrc:/root/.netrc:ro" wintersar-engines \
    wintersar run --config /work/config.yaml
# pixi.lock 이 커밋된 뒤에는 해상도를 고정:
docker build -f Dockerfile.engines --build-arg PIXI_INSTALL_FLAGS=--locked -t wintersar-engines .
```

- 이미지는 linux-64 입니다. Apple silicon 에서는 `docker build --platform linux/amd64 …` 처럼 플랫폼을
  지정해야 합니다(에뮬레이션).
- 기존 `Dockerfile`(루트)은 **core 이미지**(uv, 엔진 없음, CI 에서 빌드)이며 그대로 유지됩니다.

## 4. Earthdata 인증

HyP3 원격 처리와 ASF 다운로드에는 NASA Earthdata Login 이 필요합니다 ([ADR-0013](adr/0013-earthdata-auth-policy.md)):

1. `~/.netrc` 에 `machine urs.earthdata.nasa.gov login <id> password <pw>` — **우선** (SLC BURST 추출에 필요한
   쿠키는 토큰 인증으로는 설정되지 않음).
2. 또는 환경변수 `EARTHDATA_TOKEN` (설정 `data.credentials: env:EARTHDATA_TOKEN`).

Docker 에서는 `-v "$HOME/.netrc:/root/.netrc:ro"` 또는 `-e EARTHDATA_TOKEN` 으로 넘깁니다. 자격 증명이 없으면
`check-install` 이 `ENV-003`(WARN) 을 내고 HyP3 실행 시 `KB-AUTH-001` 로 안내합니다. 자격 증명·홈 경로는
로그·리포트에서 마스킹됩니다(규칙 11.11).

## 5. 무엇을 승인해야 하나 (연구자·발주자)

| 항목 | 이유 | 근거 |
|---|---|---|
| `hyp3-sdk`, `sentineleof`, `sardem`, `snaphu`(PyPI) 를 개발 환경에 설치 | 의존성 채택 정책(월 ~1만 다운로드) 미달 → optional extra | ADR-0001, open-questions #10 |
| `spurt` (`pip install spurt`, ortools) | conda-forge 패키지 없음 | ADR-0025, #22 |
| `dolphin`·`mintpy` 설치(R-15 A/B) | MintPy GPL-3 subprocess 전용, dolphin 채택 기준 | ADR-0064, #53 |
| `pixi.lock` 생성·커밋 | 엔진 환경 재현의 전제 | ADR-0105, #70 |

성능·설치 시간 수치는 `bench_result.json` 없이는 적지 않습니다(규칙 11.8).

---

## English summary

Three install paths, each with a different reach:

- **uv (core, default)** — `uv sync --extra dev`. Runs search/precheck/plan, the synthetic fake-engine
  pipeline, diagnose/validate/bench and, after the optional-extra approval (ADR-0001, OQ #10),
  the **HyP3 remote** path (`--extra hyp3`). No local engines: ISCE2, snaphu-py, tophu, MintPy and
  dolphin are conda-forge only.
- **pixi (engines)** — `pixi install -e engines` on linux-64 gives local ISCE2 topsStack + snaphu-py +
  tophu + MintPy + dolphin + orbits/DEM; `engines-portable` is the macOS/Apple-silicon variant without
  ISCE2 (no osx-arm64 build) and tophu (Linux-only recipe). `pixi.toml` mirrors the core list and the
  `dev`/`hyp3`/`unwrap`/`orbits`+`dem`/`plots` extras of `pyproject.toml`; `scripts/check_env.py` fails
  the tests when they drift and when a new extra or feature is not classified (ADR-0107); the `spurt`,
  `gpu` and `docs` extras are uv-only. `pixi.lock` is not committed yet (OQ #70) — the first person
  with pixi should commit it.
- **Docker (engines)** — `docker build -f Dockerfile.engines -t wintersar-engines .`, multi-stage on
  `ghcr.io/prefix-dev/pixi`, smoke-tested with `wintersar check-install --strict`. Not built in CI
  (build time, licence isolation: GPL-3 MintPy and the SNAPHU core are co-installed — ADR-0106).
  linux-64 only; use `--platform linux/amd64` on Apple silicon.

Earthdata: `~/.netrc` (`machine urs.earthdata.nasa.gov …`, preferred) or `EARTHDATA_TOKEN`
(ADR-0013); pass either into Docker with `-v`/`-e`. Optional extras (`hyp3-sdk`, `sentineleof`,
`sardem`, `snaphu`), `spurt`, `dolphin`/`mintpy` and the lock file each need the approvals listed in
section 5. No performance claims without `bench_result.json`.
