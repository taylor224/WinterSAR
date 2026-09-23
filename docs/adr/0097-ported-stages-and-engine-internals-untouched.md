# ADR-0097: PERF-10 으로 이식한 단계와 ISCE2/SNAPHU 내부를 건드리지 않는 이유

- 상태(Status): 채택
- 날짜(Date): 2026-09-23
- 관련 ID: PERF-10, PERF-04(스케줄러), R-04/SEL-12(기하 마스크), R-06(언래핑 마스크), R-07(대표위상),
  플랜 §6.1 "ISCE2 내부는 건드리지 않고 후처리 단계(필터·마스크·연구 모듈)에 적용", 규칙 11.2/11.3
- 검증 출처(Sources):
  - 이식 대상 소스: `src/wintersar/unwrap/masks.py`(`combine_masks`), `src/wintersar/unwrap/scheduler.py`
    (`fringe_density`), `src/wintersar/select/geometry_masks.py`(`slope_aspect`, `local_incidence`,
    `range_slope`, `compute_geometry_masks`, `masks_for_both_directions`), `src/wintersar/research/repr_phase.py`
    (`multilook`, `goldstein_filter`, `repr_filtered`).
  - 호출 지점: `src/wintersar/unwrap/api.py` (`stack_fringe_density`, `_unwrap_one` 의 `combine_masks`),
    `src/wintersar/select/` precheck·CLI(기하 마스크), `src/wintersar/research/{cli,experiments}.py`
    (`representative_phase`).
  - 엔진 경계: ADR-0001(라이선스·엔진 경계), ADR-0023(snaphu-py/SNAPHU subprocess), ADR-0026(ISCE2 topsStack
    run_files), ADR-0024(tophu), 플랜 §6 원칙 "언래핑 코어 알고리즘(MCF 최적화)은 건드리지 않는다".

## 맥락 (Context)

플랜 §6.1 PERF-10 은 "멀티룩·Goldstein 필터·코히어런스·기하 마스크가 CPU numpy" 라는 비효율을 들고,
선택적 CuPy 백엔드를 **후처리 단계**에만 적용하라고 한다. 리뷰에서 `compute.{xp,kernels}` 가 어디에서도
쓰이지 않는다는 점이 지적되었다. 어떤 코드를 이식하고, 어디까지는 이식하지 않는지 기록한다.

## 선택지 (Options)

1. wintersar 가 소유한 픽셀 연산만 이식(마스크·프린지 밀도·기하·연구 필터).
2. ISCE2 topsStack 의 필터/멀티룩 단계(`filter`, `multilook` run_files)나 SNAPHU 비용 계산을 CuPy 버전으로
   대체.
3. 아무것도 이식하지 않고 커널을 벤치 전용으로 둠.

## 결정 (Decision)

선택지 1. 이식한 것과 그 이유:

| 함수 | 단계/호출자 | 이식 내용 | 이식 이유 |
|---|---|---|---|
| `unwrap.masks.combine_masks` | unwrap 단계(`api._unwrap_one`), 스케줄러 샘플링(`stack_fringe_density`) | `xp` 로 작성, `gpu=` 키워드, numpy 반환 | 간섭도마다 전체 해상도로 호출되는 픽셀 비교 연산 |
| `unwrap.scheduler.fringe_density` | unwrap 계획(`resolve_plan` 입력) | `xp` 로 작성(`extract` 로 불리언 인덱싱), float 반환 | 복소 이웃 곱·`angle` 이 전체 해상도로 3장 이상 |
| `select.geometry_masks.slope_aspect` / `local_incidence` / `range_slope` / `compute_geometry_masks` | select precheck(SEL-12), `compute_from_dem_file`, 골든 회귀 | 한 백엔드에서 전체 체인 계산 후 다운로드; `masks_for_both_directions` 는 `gpu=` 전달 | DEM 창 전체의 삼각함수·gradient. CPU 산술 순서는 그대로여서 골든 파일 무변경 |
| `research.repr_phase.multilook` | `ml`/`coh_weighted`/`shp`/`phase_link`/`filtered` 방법, `truth_lowres_phase`, `sample_coherence_matrix` | `kernels.multilook(a, f, f)` 위임(입력 배열의 백엔드를 따름) | 중복 구현 제거(비트 동일) |
| `research.repr_phase.goldstein_filter` / `repr_filtered` | `filtered` 방법, 실험 러너 | `kernels.goldstein_filter(weight="bartlett", pad="zero", smooth_mode="wrap", keep_magnitude=True)` + `gpu=` | 중복 FFT 구현 제거(ADR-0096 등가 테스트) |

- 백엔드 결정은 **이식 함수의 경계에서 한 번**(`resolve_backend(gpu)`) 하고, 안에서는 `asarray(a, xp)` 로
  올려 계산한 뒤 `to_numpy` 로 내린다. 공개 함수의 반환은 항상 numpy — 호출자(`api.py`, GeoTIFF 쓰기,
  NPZ 저장, 통계)는 바뀌지 않는다.
- `research.repr_ml`/`coh_weighted`/`shp`/`phase_link` 는 numpy 입력을 그대로 넘기므로 CPU 에서 돈다
  (`multilook` 은 커널을 거치지만 업로드는 하지 않음). 작은 저해상도 배열에 호스트↔장치 왕복을 넣는 것은
  이득이 없고, `filtered` 만 FFT 비용이 있어 백엔드 선택을 받는다.
- `compute.kernels.coherence_estimate` 는 아직 호출자가 없다: 파이프라인의 코히어런스는 엔진 산출물
  (ISCE2 `filt_*.cor`, HyP3 `*_corr.tif`, 합성 데이터)이고 wintersar 가 SLC 쌍에서 직접 추정하는 단계가
  없다. 커널·패리티 테스트·벤치는 유지하고, 향후 dolphin/phase-linking 경로(PERF-05)에서 SLC 를 다룰 때
  연결한다.

**이식하지 않은 것과 이유**

- **ISCE2 topsStack 내부**(정합·간섭도 생성·`filter`/`multilook` run_files·`topozero` shadow/layover):
  subprocess 로만 실행하는 외부 엔진이다(ADR-0026, 규칙 11.2). 그 안의 numpy/C 코드를 CuPy 로 바꾸려면
  ISCE2 를 포크하거나 monkeypatch 해야 하며, 결과 재현성·버전 추적(`engine_version` 해시, ADR-0031)이
  깨진다. wintersar 는 run_files 의 **병렬화·정리**(PERF-07)로만 개선한다. `topozero` 의 shadow/layover
  는 우리 능동 마스크와 비교 대상(ADR-0019)이지 대체 대상이 아니다.
- **SNAPHU/snaphu-py, tophu 내부**: 플랜 §6 원칙과 규칙 11.3("SNAPHU/MCF 재구현 금지"). 비용 배열·MCF
  최적화는 GPU 로 옮기지 않는다. wintersar 가 하는 것은 **마스크로 노드 수 줄이기**(이 ADR 의 `combine_masks`)
  와 타일·병렬 스케줄링(PERF-04)뿐이다. tophu 의 자체 저해상도 멀티룩(`_multilook.py`)도 그대로 두고, 우리
  연구 모듈이 **대안 대표위상**을 계산할 때만 커널을 쓴다.
- **MintPy 역산**: GPL-3(규칙 11.2) — import 도 하지 않는다(ADR-0021). GPU 화 대상이 아니다.
- **엔진 어댑터의 `use_gpu`/`_gpu`**(`engines/isce2_topsstack.py` `use_gpu`, `engines/dolphin.py` `gpu`): 엔진
  자체의 GPU 옵션(ISCE2 `--useGPU`, dolphin 의 jax 백엔드)을 켜는 플래그로, 이 ADR 의 커널과 별개다. 같은
  `_gpu` private param 을 읽지만 서로 간섭하지 않는다.

## 결과 (Consequences)

- 이식 함수의 CPU 결과는 이식 전과 비트 동일(ADR-0096 표)이라 기존 테스트·골든 파일은 변경되지 않았다.
- `unwrap/api.py`(다른 담당)가 `gpu=params.get("_gpu")` 를 넘기고 ENV-005 findings 를 보고서에 붙이는 연결이
  남아 있다(ADR-0095 "결과"). 그때까지는 환경변수·자동 감지로 백엔드가 정해진다.
- 새로운 픽셀 연산(예: 수역 마스크 래스터화, 대기 보정 격자 보간)을 추가할 때는 같은 경계 규칙(경계에서
  `resolve_backend`, 안에서 `xp`, 밖으로 numpy)을 따르고 ADR-0096 표에 허용 오차 행을 추가한다.
- 벤치(`bench_kernels`)의 CPU/GPU 시간은 `bench_result.json` 에만 기록한다(규칙 11.8); 이 ADR 은 어떤 수치도
  주장하지 않는다.
