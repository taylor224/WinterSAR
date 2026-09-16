# ADR-0047: 언래핑 병렬화 순서 (간섭도 단위 병렬 우선, 타일 병렬은 메모리 초과 시에만)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-06, PERF-04, PERF-07
- 검증 출처(Sources):
  - SNAPHU man page `--nproc`: "Use n parallel processes when in tile mode. The program forks a
    new process for each tile so that tiles can be unwrapped in parallel; at most n processes will
    run concurrently." <https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/snaphu_man1.html>
  - snaphu-py `nproc`: "Maximum number of child processes to spawn for parallel tile unwrapping."
    <https://github.com/isce-framework/snaphu-py/blob/main/src/snaphu/_unwrap.py>
  - 플랜 §5.4 "병렬화 순서: 간섭도 단위 병렬을 먼저 채우고(타일 경계 아티팩트 없음), 단일 간섭도가
    메모리를 초과할 때만 타일 병렬. `n_parallel = min(floor(cores / nproc_per_igram), floor(RAM / m))`"
  - 구현: `scheduler.choose_strategy`, `api._execute` (`concurrent.futures`,
    `multiprocessing.get_context("spawn")`), 테스트 `tests/unit/unwrap/test_scheduler.py`,
    `tests/integration/test_unwrap_scheduler.py`

## 맥락 (Context)

SBAS는 간섭도 M개를 독립적으로 언래핑하므로 간섭도 단위 병렬은 결과를 바꾸지 않는 "공짜" 병렬이다.
타일 병렬(SNAPHU `nproc`)은 타일 경계 단차 위험을 안고 가므로 필요할 때만 써야 한다. 그러나 타일이
불가피할 때(단일 간섭도가 RAM을 초과) 코어를 놀리면 PERF-04의 "코어 수에 비례한 처리량" 목표를
놓친다.

## 선택지 (Options)

1. 항상 간섭도 병렬만 사용(타일 병렬 없음).
2. 사용자가 `nproc_per_igram`을 직접 정하고 스케줄러는 계산만.
3. 간섭도 병렬 우선; 타일이 불가피하면 (사용자가 `nproc_per_igram > 1`을 지정하지 않은 한) 타일
   워커 수를 코어까지 올려 예산 안에서 타일 크기를 정함.

## 결정 (Decision)

선택지 3.

- **단일 타일**(m ≤ 예산): `nproc = cfg.nproc_per_igram`(기본 1),
  `n_parallel = max(1, min(floor(cores / nproc), floor(예산 / m), n_igrams))`.
  어떤 항이 결정했는지 `parallel_cores_bound` / `parallel_memory_bound` /
  `parallel_igrams_bound` 사유로 남긴다.
- **타일**(m > 예산): `nproc_target = cfg.nproc_per_igram`이 1이면 `cores`, 아니면 설정값.
  타일 수와 워커 수를 예산에 맞춘 뒤(ADR-0045) 간섭도당 메모리 `est = tile_mb · nproc`로 같은
  식을 적용한다. 보통 `n_parallel = 1`이 되고 타일 워커가 코어를 채운다. 설정값 1에서 올렸으면
  `nproc_raised` 사유를 남긴다.
- **명시 타일**(`unwrap.tiles`): `nproc`은 설정값을 그대로 쓴다(사용자 의도 존중).
- **실행기**(`api.run_unwrap`): 간섭도 단위 작업을 `ProcessPoolExecutor(max_workers=n_parallel,
  mp_context="spawn")`로 실행한다. spawn을 고정한 이유는 (a) macOS 기본값과 일치, (b) peak RSS 샘플러
  스레드가 있는 부모를 fork하지 않기 위해서다. 테스트 백엔드(`truth`/`identity`)는 스레드 풀,
  `n_parallel == 1`이면 인라인 실행. 워커는 결과를 `out_dir/parts/*.npy`로 쓰고 부모가 조립하므로
  큰 배열을 피클로 넘기지 않는다.
- **peak RSS**: 부모 + 자식 프로세스의 `psutil.Process().memory_info().rss` 합을 0.1 s 간격으로
  샘플링한 최댓값을 `stats.json["peak_rss_mb"]`에 기록한다
  (`psutil.Process.children(recursive=True)`, 소스 `.venv/lib/python3.11/site-packages/psutil/__init__.py`).

## 결과 (Consequences)

- 파이프라인 실행기(§5.3)의 리소스 예약과 이중 계산되지 않도록, 파이프라인은 `unwrap` 단계에
  `_cores`/`_memory_gb`를 넘기고 스케줄러가 그 안에서 병렬도를 정한다(`api._machine_from_params`).
- `_n_parallel` 사설 키로 병렬도를 강제할 수 있다(bench의 스케일링 실험용).
- 워커가 npz 전체 배열을 읽는 현재 방식은 PERF-08 Zarr 스토어로 바뀌어야 대형 스택에서 유효하다.
- 병렬 이득 수치는 bench S의 `bench_result.json` 없이는 기록하지 않는다(규칙 11.8).
