# loop closure(위상 폐합) — 언래핑 오류를 자동으로 찾아내기 (R-09, validate.closure)

세 날짜 A, B, C 의 간섭도 φ_AB, φ_BC, φ_AC 를 순환 합산하면 이론상 `φ_AB + φ_BC − φ_AC = 0` 이어야 합니다.
접힌(wrapped) 위상에서는 잡음과 필터·멀티룩의 비선형성으로 작은 잔차가 남지만, **언래핑 후** 잔차가
2π 의 정수배로 튀면 세 간섭도 중 어딘가에서 언래핑이 잘못됐다는 뜻입니다.

## 어떻게 쓰나

- **간섭도별 순위**: 여러 삼각형(triplet)에 걸쳐 잔차가 큰 간섭도를 세어 "언래핑 오류 의심" 순위를 만듭니다.
  LiCSBAS 가 이 방식으로 불량 간섭도를 자동 제외하며, wintersar 의 `validate.closure` 는 그 아이디어를 참고해
  구현합니다(코드 복사 금지, 규칙 11.2).
- **픽셀별 통계**: 특정 지역(저코히어런스, 급경사, 수역 경계)에서 잔차가 몰리면 마스크나 타일 경계 문제를
  의심합니다. 타일 경계 단차 검출기(ADR-0048)와 같은 지표를 씁니다.
- **MintPy 와의 대조**: `timeseries.unwrap_error_correction: phase_closure`(기본) 는 MintPy 의 phase-closure
  기반 언래핑 오류 보정을 켭니다. `bridging` 은 연결성분 사이를 다리로 잇는 방식, `no` 는 끕니다.
  wintersar 는 보정 전후의 폐합 잔차를 대시보드 JSON 으로 남겨 효과를 확인합니다.

## 대시보드 지표

`validate.closure` 가 내는 값: 삼각형별 잔차 RMS, 간섭도별 오류 의심 점수, 픽셀별 잔차 지도, 잔차가
2π 정수배인 픽셀 비율. 이 지표는 파라미터 스윕(`sweep`)의 목적 함수 중 하나입니다 — 코히어런스 임계·
looks·필터·언래퍼·대기 보정 조합별로 "폐합 RMS, 시간적 코히어런스, 대조군 RMSE, 실행 시간"을 표로 비교합니다.

관련: [언래핑 타일](unwrap-tiling-multiresolution.md), [기준점](reference-point-deramp.md),
[검증·튠 튜토리얼](../tutorials/validate-tune.md), `docs/kb/KB-MINTPY-002.md`(시간적 코히어런스 저조).

## English summary

For three dates the interferometric phases should close: `phi_AB + phi_BC - phi_AC = 0`. After
unwrapping, residuals equal to whole multiples of 2-pi reveal unwrapping errors in one of the three
interferograms; counting such residuals over all triplets ranks suspect interferograms (the LiCSBAS
idea, re-implemented, not copied), and per-pixel residual maps expose mask or tile-seam problems.
`timeseries.unwrap_error_correction: phase_closure` enables MintPy's closure-based correction;
wintersar records closure statistics before and after as a dashboard JSON and uses closure RMS as one
objective in parameter sweeps.
