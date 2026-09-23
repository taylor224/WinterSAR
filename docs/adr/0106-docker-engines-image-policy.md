# ADR-0106: Docker engines 이미지 정책 — pixi 다단계 빌드, CI 미빌드, `check-install --strict` 스모크

- 상태: 채택
- 날짜: 2026-09-23
- 관련 ID: PERF-12, 플랜 §7 Phase 0 DoD("Docker 빌드"), §9 라이선스 표, 규칙 11.2, ADR-0001, ADR-0105
- 검증 출처(2026-09-23):
  - pixi 컨테이너 배포 문서: <https://pixi.prefix.dev/latest/deployment/container/> — 예시
    `FROM ghcr.io/prefix-dev/pixi:0.81.0 AS build`, `pixi install --locked -e prod`,
    `pixi shell-hook -e prod -s bash > /shell-hook`, 런타임 `FROM ubuntu:24.04`,
    `COPY --from=build /app/.pixi/envs/prod /app/.pixi/envs/prod` ("경로는 두 스테이지에서 같아야 함")
  - pixi-docker 저장소 README: <https://github.com/prefix-dev/pixi-docker> — 이미지 `ghcr.io/prefix-dev/pixi:<tag>`,
    태그 `latest`(Ubuntu 24.04 noble), `focal`, `bullseye`, `noble-cuda-*`, 버전 태그 `0.x.y[-base]`;
    README 예시는 `RUN echo 'exec "$@"' >> /shell-hook.sh` + `ENTRYPOINT ["/bin/bash", "/shell-hook.sh"]`
  - pixi 최신 릴리스: <https://github.com/prefix-dev/pixi/releases/latest> — v0.81.0 (2026-09-15)
  - `pixi shell-hook` CLI: <https://pixi.prefix.dev/latest/reference/cli/pixi/shell-hook/> ("pixi 실행 파일 없이도
    source 할 수 있는 활성화 스크립트 출력")
  - 이 저장소: `src/wintersar/cli.py` `check-install`(`--strict` 는 FAIL 이 있을 때만 exit 1; 자격 증명 부재
    `ENV-003` 과 GDAL/PROJ 불일치 `ENV-004` 는 WARN), `.github/workflows/ci.yml`(core `Dockerfile` 만 빌드)

## 맥락

Phase 0 DoD 는 "Docker 빌드" 를 요구하고, 루트 `Dockerfile` 은 엔진 없는 core 이미지로 CI 에서 빌드된다. 로컬
ISCE2 경로를 재현하려면 conda-forge 엔진을 담은 두 번째 이미지가 필요하다. 어떤 베이스로, 무엇을 스모크로,
CI 에서 빌드할지를 정한다.

## 선택지

1. `condaforge/miniforge3` 위에 `environment.yml` — 락 없음, `pixi.toml` 과 이중 관리.
2. **`ghcr.io/prefix-dev/pixi` 다단계 빌드**(빌드 스테이지에서 `pixi install -e engines`, 런타임은 `ubuntu:24.04`
   에 환경만 복사) — pixi 문서의 권장 패턴. **채택**.
3. 단일 스테이지 pixi 이미지 — 캐시(`~/.cache/rattler`)와 pixi 바이너리가 그대로 남아 이미지가 커짐.

## 결정

`Dockerfile.engines`:

- `ARG PIXI_VERSION=0.81.0` → `FROM ghcr.io/prefix-dev/pixi:${PIXI_VERSION} AS build`. 문서 예시와 최신 릴리스가 같은
  버전이라 이를 기본값으로 고정한다(`latest` 태그는 재현성이 없어 쓰지 않는다).
- 복사 대상은 git 추적 경로만: `pixi.toml pyproject.toml README.md LICENSE pixi.loc[k]`(글롭이라 락파일이 없어도
  COPY 성공), `src/`, `scripts/`, `benchmarks/`.
- `RUN pixi install -e engines ${PIXI_INSTALL_FLAGS}` — 락파일이 커밋되기 전까지는 빌드 시 해상(비재현);
  커밋 뒤 `--build-arg PIXI_INSTALL_FLAGS=--locked` 로 고정한다.
- `pixi shell-hook -e engines -s bash > /shell-hook` + `echo 'exec "$@"' >> /shell-hook` → 런타임 `ENTRYPOINT ["/bin/bash", "/shell-hook"]`.
- 런타임 스테이지는 `/app` 전체를 복사한다(환경 디렉터리만이 아니라): editable 설치가 `/app/src` 를 가리키고 훅이
  `/app/.pixi/envs/engines` 절대 경로를 품기 때문.
- topsStack PATH: shell-hook 생성 *전에* `ENV PATH=/app/.pixi/envs/engines/share/isce2/topsStack:…` 를 두어 pixi 의
  `activation.env` 확장 순서(미확인, #69)와 무관하게 훅에 구워 넣는다.
- 스모크(Phase 0 DoD "check-install 이 미설치 엔진을 Finding 으로 보고"의 역방향 검증):
  `wintersar --json check-install --strict --engine fake --engine hyp3 --engine snaphu --engine tophu --engine isce2_topsstack --engine mintpy --engine dolphin`.
  `spurt` 는 conda-forge 에 없어 환경에 없으므로 제외(ADR-0025); 자격 증명 없음(`ENV-003`)은 WARN 이라 `--strict` 를
  깨지 않는다.

**CI 에서 빌드하지 않는다** (`.github/workflows/ci.yml` 은 core `Dockerfile` 만):

1. *빌드 비용* — 매 PR 마다 isce2·mintpy·dolphin(jax)·isce3 스택을 해상·다운로드해야 한다. 수치는 측정 전이라
   적지 않는다(규칙 11.8); 락파일이 생긴 뒤 수동 dispatch 또는 주기 워크플로로 재검토한다.
2. *라이선스 격리* — 이미지는 GPL-3 MintPy 와 SNAPHU C 코어(`LicenseRef-SNAPHU`, cs2 솔버 비상업 조항)를 Apache-2.0
   wintersar 와 한 파일시스템에 담는다. 사용자가 자기 머신에서 빌드해 쓰는 것과, 프로젝트가 CI 에서 빌드해 레지스트리에
   올리는 것(= 배포)은 다르다. 플랜 §9 "번들 금지" 와 ADR-0001 의 subprocess 경계를 이미지 배포로 우회하지 않는다.
3. *재현성 부재* — `pixi.lock` 이 없는 동안 빌드 결과가 매번 달라 CI 게이트로서 의미가 없다(#70).

## 결과

- `docs/install.md` §3 에 빌드·실행 명령을 둔다. 이미지는 linux-64 전용(ADR-0105 의 isce2·tophu 제약); Apple silicon
  에서는 `--platform linux/amd64`.
- 테스트 `tests/unit/test_pixi_manifest.py::test_dockerfile_*` 가 베이스 이미지·`pixi install -e engines`·
  `check-install --strict`·스모크 엔진 목록(등록된 엔진 ⊆ 매니페스트 feature)을 고정한다.
- 되돌리는 조건: 락파일 커밋 + 빌드 시간 측정치(`bench_result.json`)가 있으면 CI 수동/주기 빌드를 추가하는 ADR 을 쓴다.
  레지스트리 게시는 라이선스 검토(#4) 없이는 하지 않는다.
