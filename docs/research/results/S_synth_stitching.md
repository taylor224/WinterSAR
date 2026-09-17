# S_synth_stitching

3x3 tiles (overlap 16 px) of a 144x144 synthetic interferogram (Gaussian bowl + 1 rad turbulent atmosphere), each tile shifted by a random integer number of cycles in [-3, 3] and perturbed with 0.3 rad Gaussian noise. coarse_ref uses the block mean of the true phase at factor 3 plus 0.5 rad noise as the low-resolution reference; overlap_consensus uses the synthetic coherence as weights.

R-06, R-07, PERF-04

생성 원본: `S_synth_stitching.json` (표만 기록 — 수치 해석은 ADR·연구 노트에서, 규칙 11.8)

- 시드: 0, 1, 2, 3, 4
- 데이터: `{"atmosphere_std_rad": 1.0, "coherence_base": 0.8, "cols": 3, "max_offset_cycles": 3, "noise_std_rad": 0.3, "overlap": 16, "ref_noise_std_rad": 0.5, "rows": 3, "shape": [144, 144]}`
- factor: 3

실행 시간·메모리(wall_s, peak_rss_mb)는 결과 JSON에만 둔다: 규칙 11.8은 bench_result.json이 있을 때만 성능 수치를 본문·표에 쓸 수 있게 한다.

| 방법 | offsets_exact | n_wrong_offsets | seam_boundaries_with_jump | seam_jump_pixels | unwrap_error_fraction |
|---|---|---|---|---|---|
| coarse_ref | 1.000 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) |
| coarse_ref(median) | 1.000 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) |
| overlap_consensus | 1.000 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) |
| overlap_consensus(median) | 1.000 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) |
