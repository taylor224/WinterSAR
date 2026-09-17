# S_synth_strong_atmosphere

Representative (low-resolution) phase of a 96x96 synthetic interferogram with a 3 rad turbulent atmosphere (Kolmogorov-like k^-8/3) over a Gaussian subsidence bowl, base coherence 0.6 and single-look noise drawn from the exact phase distribution (noise_model: exact), at downsample factor 3, compared with the block mean of the true phase.

R-07, PERF-04

생성 원본: `S_synth_strong_atmosphere.json` (표만 기록 — 수치 해석은 ADR·연구 노트에서, 규칙 11.8)

- 시드: 0, 1, 2
- 데이터: `{"atmosphere_std_rad": 3.0, "coherence_base": 0.6, "deformation_amplitude_m": -0.05, "deformation_kind": "gaussian", "kind": "synthetic_igram", "looks": 1, "noise_model": "exact", "shape": [96, 96]}`
- factor: 3

실행 시간·메모리(wall_s, peak_rss_mb)는 결과 JSON에만 둔다: 규칙 11.8은 bench_result.json이 있을 때만 성능 수치를 본문·표에 쓸 수 있게 한다.

| 방법 | phase_rmse_rad | phase_mae_rad | mean_magnitude |
|---|---|---|---|
| ml | 1.177 ± 0.1594 (n=3) | 0.8832 ± 0.1529 (n=3) | 0.1671 ± 0.0358 (n=3) |
| coh_weighted(p=1.0) | 1.177 ± 0.1594 (n=3) | 0.8832 ± 0.1529 (n=3) | 0.3572 ± 0.0264 (n=3) |
| coh_weighted(p=2) | 1.178 ± 0.1577 (n=3) | 0.8818 ± 0.1519 (n=3) | 0.3602 ± 0.0260 (n=3) |
| filtered(alpha=0.5,overlap=0.5,window=16) | 1.162 ± 0.1667 (n=3) | 0.8685 ± 0.1591 (n=3) | 0.1774 ± 0.0412 (n=3) |
