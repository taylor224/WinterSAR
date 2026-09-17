# S_synth_seq_estimator_ab

A/B of the sequential estimator path (dolphin: mini-stack phase linking, compressed SLCs, N-1 unwrapping jobs) against the conventional SBAS path (M pair interferograms unwrapped independently, MintPy inversion) on the S benchmark site. Literature reports near-CRLB accuracy; the researcher's experience is a loss of real-world performance. The table below is filled only from measured bench_result.json files.

R-15, PERF-05, PERF-06

생성 원본: `S_synth_seq_estimator_ab.json` (표만 기록 — 수치 해석은 ADR·연구 노트에서, 규칙 11.8)

상태: 건너뜀 — 필요 패키지 dolphin, mintpy 가 설치되어 있지 않습니다

미설치 패키지: dolphin, mintpy

## 프로토콜

1. Inputs: identical coregistered SLC stack (S site, 2 bursts x 30 days) for A and B; same
   DEM, orbits, water/layover masks (wintersar select geometry), same reference point.
2. A (baseline): SBAS network (max temporal 48 d, max Bperp 150 m), multilook per SEL-09,
   SNAPHU per interferogram (wintersar unwrap scheduler, PERF-04), MintPy inversion with the
   template of ADR-0021. Record: number of unwrap jobs (M), wall time per stage, peak RSS.
3. B (candidate): dolphin sequential mode (mini-stack 10, EMI, compressed SLCs), N-1 linked
   interferograms unwrapped with the same SNAPHU settings, time series through the same
   MintPy inversion so that only the phase estimation differs (plan 6.2 PERF-05/06:
   outputs normalised to the same schema by io.formats).
4. Metrics (plan 6.3, 3 repetitions, median): closure RMS of the unwrapped stack
   (validate.closure, unw mode), unwrap-error indicators (non-zero integer closure
   fraction, tile-seam jumps), ground-truth RMSE at levelling/GNSS sites
   (validate.metrics.compare), total wall time and peak RSS per path, number of unwrap
   jobs. Write bench_result.json per path; wintersar bench --compare renders the table.
5. Decision rule (ADR-0064): B is adopted as an option only if its ground-truth RMSE is not
   worse than A beyond the levelling sigma and closure RMS is not higher; the default path
   changes only through a separate ADR after the researcher signs off (rule 11.10).

