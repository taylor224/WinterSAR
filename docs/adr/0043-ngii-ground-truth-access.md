# ADR-0043: 국토지리정보원(NGII) 수준점·GNSS 상시관측소 데이터 접근 방식 조사 결과

- 상태(Status): 제안 (조사 결과 기록; 어댑터 미구현 — CSV 임포트만 필수)
- 날짜(Date): 2026-09-16
- 관련 ID: R-10 / 플랜 §5.6 `ground_truth.py`, Phase 4 "가능하면 어댑터, 아니면 CSV 변환 가이드" / open-questions #6
- 검증 출처(Sources) (2026-09-16 WebSearch/WebFetch, 내용은 검색 결과·페이지 요약 수준 — 실제 다운로드는 미시도):
  - 국토정보플랫폼 https://map.ngii.go.kr — 68종 공간정보 공개, "측량기준점"(국가·지적 기준점 설치 현황·성과) 조회,
    Open-API(배경지도 WMTS + 검색 API: POI·지명·**기준점**·지오코더), "국토정보플랫폼 Open API 서비스는 회원 전용"
  - 국토지리정보원 공공데이터 개방 안내 https://www.ngii.go.kr/kor/content.do?sq=77 — 저작권법 제24조의2(공공저작물
    자유이용)에 따라 NGII가 저작권 전부를 가진 자료는 별도 허락 없이 이용 가능하나, 수치지도·항공사진 등 공간정보는
    별도 절차로 신청
  - 공공데이터포털 "국토지리정보원_지적기준점 성과정보" https://www.data.go.kr/data/15122681/fileData.do (파일 데이터)
  - 공공데이터포털 "국토지리정보원_상시관측소_RINEX_좌표_분석_데이터" https://www.data.go.kr/data/15086134/fileData.do
    — CSV, 갱신 주기 연간, **이용허락범위 제한 없음**, Open API(XML/JSON) 제공, GAMIT/GLOBK 정밀좌표(IGb14) 결과
  - NGII GNSS 서비스포털 https://geodesy.ngii.go.kr — RINEX 일/시간 단위 다운로드, 상시관측소 상태 목록, 로그인 필요,
    "Copyright 2025 NGII All Rights reserved", API/FTP 언급 없음
  - GNSS 데이터 통합센터 https://www.gnssdata.or.kr (8개 기관 협약) — "일단위 및 시간단위 GNSS 후처리 데이터(RINEX)를
    수신할 수 있습니다", 실시간 RTCM(NTRIP), 회원가입/로그인 메뉴, 이용약관·API 문서는 서비스 소개 페이지에 없음
  - 서울시 네트워크 RTK https://gnss.eseoul.go.kr/service_sub2_01 — RINEX 2.11/3 제공(서울 지역)

## 맥락

플랜 §5.6: "국토지리정보원 수준점·GNSS 상시관측소 어댑터는 데이터 접근 방식(파일/API) 확인 후 구현(확인 필요).
우선은 CSV 임포트만 필수." 실제 수준측량 시계열(반복 수준측량 성과 차이)과 GNSS 좌표 시계열이 필요하다.

## 조사 요약

| 자료 | 제공 경로 | 형식 | 인증 | 대량/자동화 | 이용 조건 |
|---|---|---|---|---|---|
| 수준점 성과(현재값) | 국토정보플랫폼 기준점 조회, 검색 Open-API | 웹 조회 / API(JSON) | 회원 | API 키(회원 전용) | 저작권법 24조의2 자유이용 추정(확인 필요) |
| 수준점 **시계열**(재측량 성과) | 공개 데이터셋 미확인 | — | — | — | 기관 문의 필요 |
| GNSS 상시관측소 RINEX | geodesy.ngii.go.kr, gnssdata.or.kr | RINEX 2/3 일·시간 | 로그인 | FTP/API 미문서화 | 약관 미확인 |
| GNSS 정밀좌표 분석(GAMIT) | 공공데이터포털 15086134 | CSV(연간) + Open API | 공공데이터포털 키 | Open API | 이용허락범위 제한 없음 |

핵심 발견: (1) 검증에 필요한 **수준점 시계열**(같은 점의 반복 성과)은 공개 데이터셋으로 확인되지 않았고 성과 현재값만
조회된다. (2) GNSS는 RINEX 원시 데이터(후처리 필요) 또는 연간 갱신 GAMIT 좌표 CSV가 있어, 12일 주기 InSAR 시계열과
직접 비교하려면 일 단위 좌표 시계열을 자체 산출하거나 별도 제공을 받아야 한다. (3) 어느 포털도 문서화된 대량
다운로드 API/FTP를 공개하지 않는다(회원 전용 Open-API 제외).

## 선택지

1. 지금 어댑터(스크레이핑/로그인 세션) 구현 — 약관 미확인, 취약.
2. **CSV 임포트만 필수**로 하고, GAMIT 좌표 CSV(공공데이터포털)를 플랜 스키마로 바꾸는 변환 가이드를 튜토리얼에 기록.
3. 공공데이터포털 Open API(15086134) 어댑터 — 연간 갱신이라 시계열 검증에 부적합.

## 결정

선택지 2. `ground_truth.load_csv`가 유일한 입력 경로이며 `config.yaml`의 `validate.gnss.source: ngii`는 예약만 해 둔다
(`api.run_validate`는 `source == "csv"`일 때만 GNSS 파일을 읽는다). 변환 가이드는 `docs/tutorials/validate-tune.md`에 초안.

## 결과

- open-questions #6은 "진행 중 → ADR-0043"으로 두고 신규 행에 후속 과제(수준점 시계열 입수 경로·GNSS 일 단위 좌표
  산출 방법·이용약관 원문 확인)를 적는다. 담당: 연구자(기관 문의).
- 어댑터를 만들 조건: 이용약관 원문 확보 + 문서화된 API/파일 배포 경로 확인. 그 전까지 코드에 URL을 넣지 않는다(규칙 11.3).
