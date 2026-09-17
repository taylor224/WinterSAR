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

## 플랜 §9 라이선스 표 보강 (LICENSE 원문 확인, 2026-09-17)

플랜 §9 표에서 "확인 필요"로 남아 있던 COMPASS·isce3·LiCSBAS·tophu를 각 저장소의 LICENSE 원문으로
확인했다(open-questions #66). 아래 행은 전부 원문에서 읽은 값이며, 출처 URL을 함께 남긴다.

| 구성요소 | 확인된 라이선스 | 확인 출처(LICENSE 원문) | 취급 |
|---|---|---|---|
| COMPASS (opera-adt/COMPASS) | Apache-2.0 (표준 Apache License 2.0 전문) | <https://github.com/opera-adt/COMPASS/blob/main/LICENSE> | 허용적 → import 가능. 현재 어댑터 없음(백로그). 채택 시 NOTICE 고지 |
| isce3 (isce-framework/isce3) | Apache-2.0. 첫 줄 `COPYRIGHT (C) 2008-2019 CALIFORNIA INSTITUTE OF TECHNOLOGY`, 본문에 `'EAR99 NLR'` 수출 통제 고지 포함 | <https://github.com/isce-framework/isce3/blob/develop/LICENSE> | ISCE2와 같은 취급: 허용적이므로 import 가능하되 **배포 시 EAR99 고지 문구를 포함**한다. 제3자 코드는 하위 디렉터리의 개별 LICENSE를 따른다(LICENSE 본문 명시) |
| LiCSBAS (yumorishita/LiCSBAS) | **GPL-3.0** (`GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007`) | <https://github.com/yumorishita/LiCSBAS/blob/master/LICENSE> | **코드 복사·import 금지**(플랜 §3.2/§9). loop closure는 알고리즘만 참고하고 `wintersar.validate.closure`로 자체 구현한다. 실행이 필요하면 subprocess 경계 뒤로 |
| tophu (isce-framework/tophu) | **BSD-3-Clause OR Apache-2.0** 이중 라이선스(사용자 선택). `setup.cfg` `license = BSD-3-Clause OR Apache-2.0`, README `SPDX-License-Identifier: BSD-3-Clause OR Apache-2.0`, 저장소에 `LICENSE-BSD-3-Clause`/`LICENSE-Apache-2.0` 두 파일 | <https://github.com/isce-framework/tophu/blob/main/LICENSE-BSD-3-Clause>, <https://github.com/isce-framework/tophu/blob/main/LICENSE-Apache-2.0>, <https://github.com/isce-framework/tophu/blob/main/setup.cfg> | 두 라이선스 모두 허용적 → 지연 import 가능(`engines/tophu.py`의 `importlib.import_module("tophu")`, `license_note`와 일치). conda-forge 전용이라 optional extra로만 선언(ADR-0024) |

- 여전히 **확인 필요**(이 ADR에서 확인하지 않음, open-questions #4로 유지): SNAPHU C 코어의 Stanford
  라이선스 조건, `snaphu-py`·`dolphin`·`spurt`·`sardem` 저장소의 LICENSE 원문. PyPI 메타데이터만으로는
  `snaphu`/`sardem`의 라이선스 필드가 비어 있다(위 검증 출처).

## 결과

- `wintersar check-install`이 미설치 엔진을 Finding으로 보고해야 한다(Phase 0 DoD).
- 엔진 없는 CI에서는 fake 엔진 + 합성 데이터로 통합 테스트를 돌린다.
- COMPASS·isce3·LiCSBAS·tophu는 위 표로 확정했다(open-questions #66 완료). 나머지 엔진 저장소의
  LICENSE 원문 확인은 `docs/open-questions.md` #4에 남기고 Phase 8에서 표 전체를 확정한다.
