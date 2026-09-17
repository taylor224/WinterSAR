# S_synth_repr_phase

Representative (low-resolution) phase of pair (0, 3) from a 12-date synthetic SLC stack (exponential coherence model tau=4, floor 0.15, left/right amplitude regions with scale 1 and 3) at downsample factor 3, compared with the block mean of the true phase.

R-07, PERF-04

생성 원본: `S_synth_repr_phase.json` (표만 기록 — 수치 해석은 ADR·연구 노트에서, 규칙 11.8)

- 시드: 0, 1, 2
- 데이터: `{"coherence_floor": 0.15, "kind": "synthetic_slc_stack", "n_dates": 12, "pair": [0, 3], "region_kind": "halves", "region_scales": [1.0, 3.0], "shape": [48, 48], "tau_dates": 4.0}`
- factor: 3

| 방법 | phase_rmse_rad | phase_mae_rad | mean_magnitude | wall_s | peak_rss_mb |
|---|---|---|---|---|---|
| ml | 0.4271 ± 0.0346 (n=3) | 0.3107 ± 0.0097 (n=3) | 2.888 ± 0.0135 (n=3) | 0.0018 ± 0.0031 (n=3) | 99.931 ± 35.903 (n=3) |
| coh_weighted(p=1.0) | 0.5578 ± 0.0446 (n=3) | 0.4258 ± 0.0228 (n=3) | 0.5038 ± 0.0148 (n=3) | 0.0001 ± 0.0000 (n=3) | 99.953 ± 35.865 (n=3) |
| shp(alpha=0.05,test=ks) | 0.6286 ± 0.0622 (n=3) | 0.4410 ± 0.0279 (n=3) | 3.074 ± 0.1764 (n=3) | 0.2305 ± 0.3965 (n=3) | 119.5 ± 2.051 (n=3) |
| phase_link(link_method=evd) | 0.4497 ± 0.0432 (n=3) | 0.3271 ± 0.0122 (n=3) | 0.9254 ± 0.0060 (n=3) | 0.0050 ± 0.0001 (n=3) | 120.4 ± 0.5307 (n=3) |
| phase_link(emi) | 1.363 ± 0.0516 (n=3) | 0.8908 ± 0.0541 (n=3) | 0.5224 ± 0.0162 (n=3) | 0.0058 ± 0.0003 (n=3) | 120.4 ± 0.5307 (n=3) |
| phase_link(evd, sequential m=4) | 0.3365 ± 0.0116 (n=3) | 0.2622 ± 0.0049 (n=3) | 0.9099 ± 0.0071 (n=3) | 0.0047 ± 0.0002 (n=3) | 120.7 ± 0.1986 (n=3) |
| filtered(alpha=0.5,overlap=0.5,window=16) | 0.2001 ± 0.0122 (n=3) | 0.1598 ± 0.0093 (n=3) | 3.819 ± 0.0464 (n=3) | 0.0011 ± 0.0000 (n=3) | 120.7 ± 0.1230 (n=3) |
