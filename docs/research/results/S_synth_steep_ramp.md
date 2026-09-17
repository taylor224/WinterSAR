# S_synth_steep_ramp

Representative (low-resolution) phase of a 96x96 synthetic interferogram dominated by a steep linear LOS ramp (0.7 m of range change across the scene = 4pi/lambda * 0.7 rad, one fringe per ~3.8 pixels, so a factor-3 block spans ~0.79 cycle) with a moderate turbulent atmosphere and 16 looks, at downsample factor 3. This is the regime where the complex block mean loses magnitude to the fringe rate; compared with the block mean of the true phase.

R-07, PERF-04

생성 원본: `S_synth_steep_ramp.json` (표만 기록 — 수치 해석은 ADR·연구 노트에서, 규칙 11.8)

- 시드: 0, 1, 2
- 데이터: `{"atmosphere_std_rad": 0.5, "coherence_base": 0.85, "deformation_kind": "linear", "deformation_ramp": [0.0, 0.7], "kind": "synthetic_igram", "looks": 16, "shape": [96, 96]}`
- factor: 3

실행 시간·메모리(wall_s, peak_rss_mb)는 결과 JSON에만 둔다: 규칙 11.8은 bench_result.json이 있을 때만 성능 수치를 본문·표에 쓸 수 있게 한다.

| 방법 | phase_rmse_rad | phase_mae_rad | mean_magnitude |
|---|---|---|---|
| ml | 0.4206 ± 0.1145 (n=3) | 0.2472 ± 0.0761 (n=3) | 0.1670 ± 0.0254 (n=3) |
| coh_weighted(p=1.0) | 0.4206 ± 0.1145 (n=3) | 0.2472 ± 0.0761 (n=3) | 0.2687 ± 0.0032 (n=3) |
| coh_weighted(p=2) | 0.4518 ± 0.1163 (n=3) | 0.2752 ± 0.0780 (n=3) | 0.2720 ± 0.0036 (n=3) |
| filtered(alpha=0.5,overlap=0.5,window=16) | 0.2204 ± 0.0464 (n=3) | 0.1646 ± 0.0325 (n=3) | 0.1737 ± 0.0250 (n=3) |
| filtered(alpha=0.8) | 0.2494 ± 0.0372 (n=3) | 0.1929 ± 0.0298 (n=3) | 0.1753 ± 0.0247 (n=3) |
