# 순차 추정기와 compressed SLC — 언래핑 횟수를 M 에서 N−1 로 (R-15, PERF-05/06)

전통 SBAS 는 N 개 날짜에서 만든 **M 개 간섭도 쌍을 전부 언래핑**한 뒤(M ≫ N) 역산합니다. 저코히어런스
쌍은 언래핑 오류와 재시도 비용까지 유발합니다. 대안은 **phase linking**(분산 산란체 DS 처리) 입니다:
한 픽셀 주변의 통계적 동질 픽셀(SHP)로 N×N 공분산(코히어런스) 행렬을 추정하고, 그 행렬을 가장 잘
설명하는 N 개의 "정합된 위상"(N−1 자유도)을 최대우도(EMI, EVD 등)로 뽑습니다. 그러면 언래핑은
**N−1 개**만 하면 되고, 코히어런스가 개선됩니다(문헌은 CRLB 에 근접한다고 보고).

## 순차(sequential) 추정기와 compressed SLC

스택이 계속 자라는 모니터링에서는 매번 전체 공분산을 다시 추정할 수 없습니다. Ansari 외(2017) 의 순차
추정기는 스택을 **미니스택**으로 나눠 각 미니스택에서 phase linking 을 수행하고, 그 결과를 하나의
**compressed SLC**(미니스택을 대표하는 가상 SLC)로 압축해 다음 배치와 연결합니다. 새 취득이 오면 최신
미니스택만 다시 계산하면 되므로 증분 처리(PERF-06)의 전제가 됩니다. `dolphin`(OPERA) 이 이 방식을
구현하며 GPU 를 쓸 수 있습니다.

## 왜 A/B 로 판단하나

문헌은 정확도 손실이 작다고 하지만, 연구자 경험은 "실성능이 오히려 떨어진다" 입니다. 어느 쪽이 맞는지는
사이트·코히어런스 조건에 따라 다를 수 있으므로 wintersar 는 **기본값을 바꾸지 않고**(MintPy 전통 SBAS 유지)
`timeseries.engine: dolphin` 을 옵션으로 두어 같은 입력에서 두 경로를 비교합니다:

| 지표 | 측정 |
|---|---|
| 언래핑 작업 수·총 시간·peak RSS | bench (`PERF-05`) |
| 폐합 잔차 RMS, 시간적 코히어런스 | [loop closure](loop-closure.md) 대시보드 |
| 대조군 RMSE/편향 | [검증](../tutorials/validate-tune.md) |
| 증분 1장 처리 시간 vs 전체 재처리 | bench (`PERF-06`) |

두 경로의 출력은 `wintersar.io` 에서 같은 스키마로 정규화해야 비교가 성립합니다. 결론은 `docs/research/`
에 사이트별로 기록하고, 기본값 변경은 ADR 로 결정합니다(플랜 §6.2, 규칙 11.10).

관련: [언래핑 타일](unwrap-tiling-multiresolution.md)(대표위상 = 저해상도 phase linking 결과를 쓰는 옵션),
플랜 §12.2 참고 문헌(Ansari 2017, arXiv 2511.12051).

## English summary

Classic SBAS unwraps all M interferogram pairs (M >> N dates). Phase linking instead estimates the
N x N coherence matrix of each distributed-scatterer pixel and extracts N-1 linked phases, so only
N-1 unwrappings are needed and coherence improves (literature: near the CRLB). The sequential
estimator (Ansari et al. 2017) splits the stack into ministacks and carries a compressed SLC from one
batch to the next, enabling incremental updates (`dolphin`). Because the researcher's experience
contradicts the literature, wintersar keeps MintPy SBAS as the default and offers
`timeseries.engine: dolphin` for a like-for-like A/B (unwrapping count/time, closure RMS, temporal
coherence, ground-truth RMSE); results go to `docs/research/` and any default change needs an ADR.
