# ADR-0054: 벤치마크 프로토콜 구현 (`wintersar bench`, bench_result.json, 회귀 게이트)

- 상태(Status): 채택 (기준선 파일·CI 머신 정책은 미확정)
- 날짜(Date): 2026-09-16
- 관련 ID: 플랜 §5.9, §6.3, §8(perf 층), R-13, PERF-04(메모리 상수 재보정 데이터), 규칙 11.8
- 검증 출처(Sources):
  - psutil 7.2.2 소스 `.venv/lib/python3.11/site-packages/psutil/__init__.py`: `Process.cpu_times()` →
    `pcputimes(user, system, children_user, children_system)`, `Process.memory_info().rss`,
    `net_io_counters()` → `snetio(bytes_sent, bytes_recv, …)`(시스템 전체 합).
  - `git rev-parse HEAD`(subprocess, 실패 시 None); `wintersar.util.sysinfo.detect()`.
  - 플랜 §6.3: 3회 반복 중앙값, 단계별 wall/peak RSS/디스크 피크/네트워크 바이트, 폐합 RMS·언래핑 오류
    지표·대조군 RMSE, S 합성 사이트를 매 PR 실행, 기준선 대비 15% 느려지면 실패.
  - 구현 `src/wintersar/bench/{profiler,sites,runner,report,cli}.py`, `benchmarks/sites/*.yaml`,
    `benchmarks/README.md`; 테스트 `tests/unit/bench/`, `tests/integration/test_bench_synthetic.py`.

## 맥락 (Context)

플랜은 측정 항목과 회귀 기준만 정했다. 사이트 정의 스키마, 측정 방식(무엇을 어디서 샘플링하는지),
반복 간 캐시 처리, 결과 JSON 스키마, 회귀 판정 대상(wall time만인지 RSS도인지)을 정해야
`bench_result.json`이 문서·PR·QGIS에서 같은 의미를 갖는다.

## 선택지 (Options)

1. 파이프라인 전체를 한 번에 재고 단계 구분 없이 총합만 기록.
2. 단계별 측정: 파이프라인 실행기의 `StageRecord.resources`(wall·RSS)를 재사용하고, 전체 실행을 한
   프로파일러로 감싸 디스크·네트워크를 얻는다. 테스트/스모크용으로는 fake engine을 단계별 직접 호출하며
   단계마다 프로파일러를 건다.
3. 외부 벤치 프레임워크(pytest-benchmark, asv).

## 결정 (Decision)

선택지 2.

- **Site YAML**(`bench.sites.Site`, pydantic, extra 금지): `name, size(S|M|L), synthetic, network,
  runner(pipeline|fake), aoi, time_range, config(config.yaml 덮어쓰기), param_overrides(단계별 파라미터),
  ground_truth(§5.6 CSV), repeats(기본 3), stages, metrics, max_wall_time_s, regression_threshold(0.15),
  baseline`. `<…>` 자리표시자가 남아 있으면 템플릿(BENCH-004), `network: true`는 `--allow-network`
  없이는 거부(BENCH-002). 합성 사이트는 네트워크를 요구할 수 없다.
- **StageProfiler**: wall(`perf_counter`), CPU(user+system+children), peak RSS(데몬 스레드 샘플링,
  자식 프로세스 포함; 기본 간격 50 ms, 시작·끝 샘플 보장), 디스크 피크·증가량(작업 디렉터리 재귀 크기
  샘플링; 순회가 간격보다 오래 걸리면 간격을 늘림), 네트워크 바이트(`net_io_counters` 차분 — **머신 전체**
  카운터이므로 동시 다운로드가 섞일 수 있음, 문서화).
- **반복**: 반복마다 새 작업 디렉터리(`run<i>/`)를 써서 DAG 캐시(PERF-03)가 반복을 건너뛰지 않게 한다.
  fake 간섭도 `seed`는 반복 번호로 달리 준다. 단계별 값은 반복 중앙값(원값은 `*_runs`에 보존).
- **지표**: `closure_rms` — 랩된 삼각 폐합 `φ_ij + φ_jk − φ_ik`의 RMS(rad, 마스크 제외, 삼각형 없으면
  None); `unwrap_error_fraction` — 쌍별 중앙값 오프셋 제거 후 |오차| ≥ π 픽셀 비율의 쌍 평균(진실 위상이
  있는 합성 사이트만, `research.synth`와 같은 정의); `gt_rmse` — `validate.metrics.compare`의 `rmse_m`
  (대조군 CSV·validate 모듈이 있을 때만; 없으면 None + BENCH-006 INFO).
- **bench_result.json v1**: `schema_version, site(summary), git_sha, wintersar_version, machine, created_at,
  repeats, runner, regression_threshold, stages{name: {wall_time_s, cpu_time_s, peak_rss_gb, disk_peak_gb,
  network_bytes, n_runs, *_runs}}, total, metrics, runs[], findings[], compare?`. 저장 전
  `mask_mapping`(규칙 11.11).
- **회귀 판정**(`bench.report.compare`): 기준선과 현재의 공통 단계(+total)에서 `after > before·(1+threshold)`
  이면 회귀, 기본 임계 0.15, 대상은 **wall time만**(RSS·디스크·네트워크는 표에 % 변화로 보고하되 게이트하지
  않음; `regress_on`으로 확장 가능). 빨라진 것은 회귀가 아니다. CLI `--fail-on-regression`이면 BENCH-001을
  FAIL로 올려 종료 코드 1, 아니면 WARN.
- **CI**: `benchmarks/sites/S_synthetic.yaml`(fake engine, 6 dates × 96×96, 3회)을 pipeline runner로
  실행 — 통합 테스트가 60초 예산을 확인한다. 기준선은 `benchmarks/baselines/<site>.json`에 두고 의도한
  변경 시 갱신한다(아직 생성하지 않음: 기준선을 만드는 머신을 정해야 함 → open-questions).

## 결과 (Consequences)

- pipeline runner의 단계별 CPU 시간·디스크는 실행기가 주지 않으므로 0/None이고 총합에만 있다.
  실행기 `StageRecord.resources`에 `cpu_time_s`가 추가되면 그대로 채운다.
- 실데이터 S/M/L 템플릿은 §6.3 2항의 "기존 도구 기본값" 기준선(`baseline` 필드)과 짝을 이룬다.
  기준선 실행 스크립트(HyP3+MintPy 기본 템플릿, topsStack 순차 + SNAPHU 단일 타일)는 Phase 5 항목이다.
- 이 ADR과 README에는 성능 수치가 없다. 수치는 `bench_result.json`과 `--compare` 표로만 배포한다.
