# ADR-0064: R-15 순차 추정기(dolphin) vs 전통 SBAS(MintPy) A/B 프로토콜

- 상태(Status): 제안 (실행은 dolphin·MintPy 설치 승인 후 — 오픈 항목 #63)
- 날짜(Date): 2026-09-16
- 관련 ID: R-15, PERF-05, PERF-06, R-10, 규칙 11.2·11.8·11.10
- 검증 출처(Sources):
  - 플랜 §2 R-15("compressed SAR(순차 추정기) 실효성 검증 — '실성능 오히려 떨어진다'는 경험 vs
    문헌(CRLB 근접)"), §6.1 PERF-05/06, §6.2("PERF-05/06 (dolphin 경로): MintPy 경로와 출력 스키마를
    동일하게 정규화해야 A/B가 성립"), §6.3 벤치 프로토콜, §12.1 "순차 추정기·compressed SLC"
  - Ansari, De Zan, Bamler 2017, "Sequential Estimator: Toward Efficient InSAR Time Series Analysis",
    IEEE TGRS 55(10):5637–5652, doi:10.1109/TGRS.2017.2711037 (플랜 §12.2 ieeexplore 8024151) — Crossref 확인
  - Ansari, De Zan, Bamler 2018, IEEE TGRS 56(7):4109–4125, doi:10.1109/TGRS.2018.2826045 (EMI) — Crossref 확인
  - dolphin `phase_link/_core.py`(EVD/EMI 정의, ADR-0061 인용)
  - 구현: `src/wintersar/research/experiments/S_synth_seq_estimator_ab.yaml`(`requires: [dolphin, mintpy]`,
    `protocol:` 본문), `experiments.run_experiment`(미설치 → `skipped`/`RES-002`; 설치돼도
    `manual_protocol`로 건너뜀), ADR-0021(MintPy 템플릿), ADR-0042(폐합), `validate.metrics.compare`(R-10)

## 맥락 (Context)

순차 추정기는 언래핑 작업 수를 M(쌍) → N−1(날짜)로 줄이고 compressed SLC로 코히어런스를
개선한다고 보고되지만, 연구자는 실데이터에서 성능 저하를 경험했다. 결론은 실측 A/B로만
내리며(플랜 §6.2: 어느 경로를 기본값으로 할지는 ADR로 결정), 실험 정의는 지금 코드에 남겨
런너가 조건 미충족 시 명확히 건너뛰게 한다.

## 선택지 (Options)

1. 합성 SLC 스택에서 `research.repr_phase.phase_link`(numpy)로 순차/전체 링크를 비교 —
   가능하고 이미 `S_synth_repr_phase`에 포함되지만, R-15의 질문(실데이터 실성능)에는 답하지 못한다.
2. **실데이터 S 사이트에서 dolphin 본체 vs MintPy 전통 SBAS(채택)** — 플랜 §6.2·§6.3 그대로.
3. 문헌값 인용으로 대체 — 규칙 11.8 위반(측정 없는 수치).

## 결정 (Decision)

`S_synth_seq_estimator_ab.yaml`의 `protocol`:

1. **입력 동일화**: 같은 정합 SLC 스택(S: 2 burst × 30일), 같은 DEM·궤도·수역/레이오버 마스크
   (`select` 기하), 같은 기준점.
2. **A(기준선)**: SBAS 네트워크(시간 48일, B⊥ 150 m), SEL-09 멀티룩, 간섭도별 SNAPHU(`unwrap`
   스케줄러, PERF-04), ADR-0021 템플릿의 MintPy 역산. 기록: 언래핑 작업 수 M, 단계별 wall time,
   peak RSS.
3. **B(후보)**: dolphin 순차 모드(미니스택 10, EMI, compressed SLC), N−1 링크 간섭도를 **같은
   SNAPHU 설정**으로 언래핑, 시계열은 **같은 MintPy 역산**(위상 추정 단계만 다르게; 출력 스키마는
   `io.formats`로 통일).
4. **지표**(3회 반복 중앙값): 언래핑 스택의 폐합 RMS(`validate.closure` unw 모드), 언래핑 오류
   지표(정수 폐합 비율·경계 단차), 대조군 RMSE(`validate.metrics.compare`, 수준측량/GNSS), 경로별
   총 wall time·peak RSS, 언래핑 작업 수. 경로별 `bench_result.json` → `wintersar bench --compare`.
5. **판정**: B는 대조군 RMSE가 A보다 수준측량 σ 이상 나쁘지 않고 폐합 RMS가 높지 않을 때만
   **옵션**으로 채택. 기본 경로 변경은 연구자 서명 후 별도 ADR(규칙 11.10).

런너 동작: `requires` 중 하나라도 `importlib.util.find_spec`으로 찾지 못하면 `status: skipped,
reason: requires`(+ `RES-002` WARN, 결과 md에 프로토콜 본문 포함); 모두 있어도
`reason: manual_protocol`(실데이터·외부 엔진 실행은 자동화 대상이 아님).

## 결과 (Consequences)

- dolphin(pip 설치 가능, 채택 기준 확인 필요)과 MintPy(GPL-3: **import 금지**, 서브프로세스
  실행·h5py 읽기만)의 설치 정책 예외 승인이 선행된다(#63, ADR-0001).
- 실행 후 결과 표는 `docs/research/results/S_synth_seq_estimator_ab.md`에 채워지고, 채택/기각은
  이 ADR을 갱신(상태 변경)해 기록한다.
- 합성 순차 추정기 비교(선택지 1)는 `S_synth_repr_phase`의 `phase_link(evd, sequential m=4)`
  행으로 계속 제공된다(ADR-0061).
