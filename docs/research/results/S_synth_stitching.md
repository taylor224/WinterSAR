# S_synth_stitching

3x3 tiles (overlap 16 px) of a 144x144 synthetic interferogram (Gaussian bowl + 1 rad turbulent atmosphere), each tile shifted by a random integer number of cycles in [-3, 3] and perturbed with 0.3 rad Gaussian noise. coarse_ref uses the block mean of the true phase at factor 3 plus 0.5 rad noise as the low-resolution reference; overlap_consensus uses the synthetic coherence as weights.

R-06, R-07, PERF-04

Generated from `S_synth_stitching.json` (tables only — interpretation belongs in ADRs / research notes, rule 11.8)

- Seeds: 0, 1, 2, 3, 4
- Data: `{"atmosphere_std_rad": 1.0, "coherence_base": 0.8, "cols": 3, "max_offset_cycles": 3, "noise_std_rad": 0.3, "overlap": 16, "ref_noise_std_rad": 0.5, "rows": 3, "shape": [144, 144]}`
- factor: 3

| method | offsets_exact | n_wrong_offsets | seam_boundaries_with_jump | seam_jump_pixels | unwrap_error_fraction | wall_s | peak_rss_mb |
|---|---|---|---|---|---|---|---|
| coarse_ref | 1.000 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0017 ± 0.0022 (n=5) | 58.416 ± 0.6726 (n=5) |
| coarse_ref(median) | 1.000 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0011 ± 0.0001 (n=5) | 58.661 ± 0.1246 (n=5) |
| overlap_consensus | 1.000 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0011 ± 0.0001 (n=5) | 58.697 ± 0.0472 (n=5) |
| overlap_consensus(median) | 1.000 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0 ± 0.0 (n=5) | 0.0009 ± 0.0001 (n=5) | 58.714 ± 0.0319 (n=5) |
