# ADR-0001: 프로젝트 라이선스와 엔진 경계 (Apache-2.0, subprocess 격리)

- 상태: 채택
- 날짜: 2026-09-16
- 관련 ID: P1, P2, 플랜 §9, 규칙 11.2
- 검증 출처:
  - PyPI 메타데이터 (2026-09-16 조회): `asf-search` 14.0.0 = BSD, `hyp3-sdk` 7.7.8 = BSD-3-Clause,
    `sentineleof` 0.13.0 = MIT, `snaphu` 0.4.1 / `sardem` 0.13.0 = 라이선스 필드 비어 있음(저장소 LICENSE 확인 필요)
  - MintPy: GPL-3 (플랜 §9)
  - ISCE2: Apache-2.0 + EAR99 고지 (플랜 §9)

## 맥락

플랜 §9는 GPL 구성요소(MintPy, GMTSAR)를 프로세스 경계 뒤에 두고 프로젝트 자체는 Apache-2.0으로
배포할 것을 제안한다. 또한 이 저장소의 의존성 정책(전역 CLAUDE.md)은 월 다운로드 약 1만 미만인
패키지를 핵심 의존성으로 두지 않는다.

## 선택지

1. 모든 엔진을 핵심 의존성으로 설치하고 파이썬 API를 직접 import.
2. 엔진은 optional extra + 런타임 감지(`check_install`), 호출은 subprocess 또는 지연 import.
3. 엔진을 fork하여 내장.

## 결정

선택지 2.

- 프로젝트 라이선스: **Apache-2.0** (`LICENSE`).
- `pyproject.toml` 핵심 의존성은 널리 쓰이는 패키지만(numpy, scipy, pydantic, typer, pyyaml,
  shapely, pyproj, xarray, zarr, h5py, rasterio, rich, psutil, jinja2, markdown, requests,
  asf-search). `asf-search`는 월 4.4만 다운로드로 정책 임계치를 넘는다.
- 다음 패키지는 정책 임계치 미달 또는 conda-forge 전용이므로 **optional extra** 로만 선언하고
  개발 환경에 설치하지 않는다: `hyp3-sdk`(월 ~7.5k), `sentineleof`, `sardem`(~300),
  `snaphu`(~1.2k), `burst2safe`(~1.3k), `tophu`(PyPI 없음), `dolphin`, `mintpy`, `isce2`.
  어댑터는 `importlib`/`shutil.which`로 존재를 감지하고 없으면 `ENV-001` Finding을 낸다.
  발주자(Taylor)가 이 예외를 승인하면 `uv sync --extra hyp3` 등으로 설치한다.
- MintPy는 절대 import하지 않는다. `smallbaselineApp.py`를 subprocess로 실행하고 HDF5는 `h5py`로 읽는다.
- SNAPHU C 코어는 번들하지 않는다(Debian non-free 분류). 사용자가 `snaphu-py`(conda-forge)를
  설치하면 어댑터가 감지한다.

## 결과

- `wintersar check-install`이 미설치 엔진을 Finding으로 보고해야 한다(Phase 0 DoD).
- 엔진 없는 CI에서는 fake 엔진 + 합성 데이터로 통합 테스트를 돌린다.
- 각 엔진 저장소의 LICENSE 원문 확인은 `docs/open-questions.md`에 남기고 Phase 8에서 표를 확정한다.
