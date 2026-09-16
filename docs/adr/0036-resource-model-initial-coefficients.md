# ADR-0036: 리소스 추정 모델의 초기 계수 (Resource model initial coefficients)

- 상태(Status): 채택 (계수는 잠정; bench 실측 후 갱신)
- 날짜(Date): 2026-09-16
- 관련 ID: PERF-04, PERF-09, PERF-13, 플랜 §5.5 "리소스 추정", §6.2 PERF-04 메모, 규칙 11.8
- 검증 출처(Sources):
  - HyP3 크레딧: https://hyp3-docs.asf.alaska.edu/using/credits/ (2026-09-16 WebFetch)
  - SNAPHU 메모리 상수 100 MB/Mpixel: `src/wintersar/pipeline/config.py` `UnwrapCfg.memory_mb_per_mpixel`
    기본값(플랜 §6.1 PERF-04 "SNAPHU 문서" 인용, `docs/open-questions.md` #8 로 실측 대기)
  - dtype 크기: numpy `float32` 4 B, `complex64` 8 B
  - snaphu-py `unwrap()` 인자(ntiles, nproc 등): https://github.com/isce-framework/snaphu-py/blob/main/src/snaphu/_unwrap.py

## 맥락 (Context)

`wintersar plan` 은 실행 전에 단계별 시간·메모리·디스크·크레딧을 보여 줘야 하고(플랜 §4.5),
unwrap 스케줄러는 메모리 초과 시 자동 타일링을 결정해야 한다(PERF-04). 그러나 이 저장소에는 아직
`bench_result.json` 이 없고, 규칙 11.8 은 실측 없는 성능 수치 기재를 금지한다. 플랜은 "초기값은
문서·경험치, bench 실측으로 계수 갱신"을 허용한다.

## 선택지 (Options)

1. 실측 전까지 추정을 하지 않는다(모두 None) → plan/스케줄러가 동작하지 않음.
2. 코드에 상수를 박아 넣는다 → 실측 후 갱신 경로가 불명확하고 "측정값"처럼 읽힐 위험.
3. **YAML(`diagnose/resources.yaml`)에 계수를 두고 파일 전체를 `status: initial_guess` 로 표시,
   각 항목에 `basis`(guess / config_default / dtype)를 붙이며, `Resources.notes["coefficients"]`
   가 항상 그 상태를 전달한다. 검증된 값(HyP3 크레딧 표)은 별도 섹션에 출처와 함께 둔다.**

## 결정 (Decision)

선택지 3. 모델(`resources.estimate(stage, n_pairs, pixels, engine, machine, params)`):

```
per_job_time_s = time_s_per_mpixel * Mpix + time_fixed_s
per_job_mem_gb = mem_mb_per_mpixel * Mpix / (ntiles_r * ntiles_c) / 1024 + mem_fixed_gb
workers        = min(cores, n_pairs, floor(memory_gb / per_job_mem_gb), params.nproc)
wall_time_s    = per_job_time_s * ceil(n_pairs / workers)   # parallel: pairs
peak_rss_gb    = per_job_mem_gb * workers
disk_gb        = disk_bytes_per_pixel * pixels * n_pairs / 1e9
```

- 시간·메모리·디스크 계수는 **전부 초기 추정치**이며 어떤 실측도 주장하지 않는다. 유일한
  근거 있는 값은 unwrap 의 `mem_mb_per_mpixel = 100`(config 기본값, open question #8)과 dtype 크기다.
- HyP3(`engine == "hyp3"`): 로컬 시간/메모리는 모델링하지 않고(`wall_time_s = None`, 노트에 사유),
  크레딧은 검증된 표로 계산한다.
  - 무료 할당: **월 8,000 크레딧** ("HyP3 Basic On Demand users are given a free allotment of 8,000 credits per month").
  - Sentinel-1 InSAR(SLC): 80 m(20x4 looks) 10 크레딧, 40 m(10x2) 15 크레딧.
  - Burst InSAR(작업당, "pairs" = 한 INSAR_ISCE_MULTI_BURST 작업의 burst 쌍 수):
    20x4: 1–4쌍 1, 5–12쌍 5, 13–15쌍 10 · 10x2: 1–3쌍 1, 4–9쌍 5, 10–15쌍 10 ·
    5x1: 1쌍 1, 2쌍 5, 3쌍 10, 4쌍 15, 5쌍 20, 6쌍 25, 7쌍 30, 8쌍 35, 9쌍 40, 10쌍 45, 11쌍 90, 12쌍 95, 13쌍 100, 14쌍 105, 15쌍 110.
  - 표에 없는 looks/burst 수는 `credits = None` + `notes["credits_unknown"] = True`.
  - 크레딧 리셋 시점은 문서에 없음 → open question #23.
- 알 수 없는 엔진은 `default` 표로, 알 수 없는 단계는 `notes["model"] == "none"` 으로 응답한다.

## 결과 (Consequences)

- `bench` 모듈은 S 사이트에서 픽셀 수 3개 이상으로 실측한 뒤 `resources.yaml` 의 계수를 회귀선으로
  갱신하고 `status` 를 `measured` 로 바꾸며 `bench_result.json` 경로를 `source` 에 적는다(§6.2 PERF-04).
- `plan` 출력은 `Resources.notes["coefficients"]` 가 `initial_guess` 인 동안 "추정(미실측)" 표시를 붙여야 한다.
- HyP3 요금표가 바뀌면 `hyp3_credits` 섹션의 `fetched` 날짜와 값을 갱신하고 `test_resources.py` 의 표를 맞춘다.
