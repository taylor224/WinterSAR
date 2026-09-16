# ADR-0013: Earthdata Login 인증 정책 (.netrc 우선, 토큰 보조, 검색은 무인증)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-01, KB-AUTH-001, 규칙 11.11
- 검증 출처(Sources):
  - `.venv/lib/python3.11/site-packages/asf_search/ASFSession.py`
    (`auth_with_token` docstring: "this does not set the asf-urs token, which is necessary for
    downloading certain products like SLC BURSTS"; `auth_with_creds`는 `find_or_create_token` 후
    `_set_asf_urs_cookie`)
  - `.venv/lib/python3.11/site-packages/asf_search/constants/INTERNAL.py` (`EDL_HOST = 'urs.earthdata.nasa.gov'`)
  - `.venv/lib/python3.11/site-packages/asf_search/exceptions.py` (`ASFAuthenticationError`)
  - 실측: CMR 검색·stack API는 인증 없이 성공 (2026-09-16)

## 맥락

플랜 §5.1.1: "인증: Earthdata Login 토큰(`EARTHDATA_TOKEN`) 또는 `.netrc`. 실패 시 KB-AUTH-001로 안내".
설정 기본값은 `data.credentials: env:EARTHDATA_TOKEN`.

## 결정

- `auth.find_credentials`(오프라인)는 `.netrc`의 `urs.earthdata.nasa.gov` 항목과 `env:VAR` 토큰을 모두
  찾고, **둘 다 있으면 `.netrc`를 우선**한다. 이유: asf_search 문서상 토큰 인증은 SLC BURST 추출기가
  요구하는 `asf-urs` 쿠키를 설정하지 않는다. `data.credentials: netrc`면 토큰을 무시한다.
- `auth.earthdata_session`은 `.netrc` → `auth_with_creds`, 토큰 → `auth_with_token`을 호출하고,
  `ASFAuthenticationError`는 `invalid_token`/`netrc_error`(FAIL), `requests` 예외는 `network`(WARN),
  자격증명 없음은 `missing`(WARN)으로 `KB-AUTH-001` Finding을 만든다. 예외를 밖으로 던지지 않는다.
- 메시지는 `select_search.KB-AUTH-001.cause_<reason>` / `fix*` 키(원인 → 조치), 오류 문자열은
  `mask_text`로 마스킹한다.
- 검색(`search_from_config`)은 세션을 만들지 않고 존재 여부만 경고한다. 세션은 다운로드 직전에 만든다.

## 결과

- 토큰만 설정한 사용자는 검색·사전검증까지는 문제없지만 burst 다운로드에서 실패할 수 있다 →
  `docs/open-questions.md` #20(토큰만으로 burst 추출기 다운로드 가능 여부 실측)로 남긴다.
- 기존 공통 키 `env.ENV-003`(자격증명 없음)은 `check-install`용으로 유지하고, 검색 흐름에서는
  KB-AUTH-001만 사용한다.
