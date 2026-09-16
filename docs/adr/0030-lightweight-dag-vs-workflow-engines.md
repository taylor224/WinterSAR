# ADR-0030: 자체 경량 DAG 채택 (Snakemake/Prefect 미도입)

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-05, R-11, PERF-03, PERF-06
- 검증 출처(Sources):
  - 플랜 §5.3 "구현 선택: 자체 경량 DAG(수백 줄) 우선. Snakemake/Prefect 도입은 ADR로 결정."
  - `src/wintersar/pipeline/{stages,dag,cache,executor}.py` (본 ADR과 함께 구현)

## 맥락 (Context)

파이프라인은 11개 단계의 **선형** 순서(`STAGE_ORDER`)를 가지며, 단계 안의 병렬성(간섭도 단위, 타일,
run_files)은 각 엔진 어댑터가 담당한다(플랜 §5.4, PERF-07). 필요한 것은 (1) 파라미터 변경 시 변경 단계와
그 하류만 재실행하는 콘텐츠 주소 캐시(PERF-03), (2) `plan` dry-run, (3) 실패 시 로그 → diagnose 연동,
(4) QGIS 플러그인이 파싱하는 `--json` 출력이다. 워크플로 엔진 도입 여부를 지금 결정해야 다른 모듈의
계약(`Engine.run`, `StageRecord`)이 고정된다.

## 선택지 (Options)

1. **자체 경량 DAG**: `stages.py`(정적 단계 표) + `dag.py`(노드 해시·해석) + `cache.py`(manifest) +
   `executor.py`(순차 실행·예산·진단). 외부 의존성 없음.
2. **Snakemake**: 규칙 파일 기반. 파일 mtime 기반 무효화(파라미터·엔진 버전 변경은 `params`/`--rerun-triggers`로
   우회 필요), 파이썬 API로 임베드하기 어렵고 CLI 중심. conda/pixi 환경 가정이 강함.
3. **Prefect/Dagster**: 서버·에이전트 구성 필요, 작업 캐시 키를 직접 정의해야 하며 의존성 크기가 크다.
   오프라인 연구용 CLI에는 과하다.

## 결정 (Decision)

선택지 1. 근거:

- 그래프가 선형이고 단계 수가 고정이므로 스케줄러(토폴로지 정렬·병렬 실행)가 필요 없다. 필요한 것은
  해시와 캐시이며 이는 수백 줄로 충분하다(`dag.py` ≈ 400줄, `cache.py` ≈ 250줄).
- 캐시 키에 **엔진 버전**과 **입력 아티팩트 해시**를 넣어야 한다(PERF-03 설계 메모). Snakemake의 파일
  타임스탬프 모델과 맞지 않는다.
- `Finding`/`StageRecord` 스키마와 i18n 규칙(11.6)을 그대로 따르므로 QGIS 플러그인·리포트가 추가 변환 없이
  같은 JSON을 읽는다.
- 의존성 정책(전역 CLAUDE.md)과 무관하게 외부 워크플로 엔진의 라이선스·버전 고정 문제가 없다.

## 결과 (Consequences)

- 단계 간 병렬은 없다(단계 안 병렬만). 리소스 예산(`ResourceBudget`)은 sum(reserved) ≤ budget 형태로 두어
  나중에 단계 병렬 실행기를 붙여도 회계가 유지된다.
- **재검토 조건**(다음 중 하나가 생기면 Snakemake/Prefect를 다시 평가한다):
  1. 단계 그래프가 비선형이 되어(예: 다중 스택·다중 트랙 동시 처리, PERF-06 증분 처리에서 날짜별 분기)
     토폴로지 정렬과 단계 간 병렬이 필요해질 때.
  2. 원격 실행(클러스터·클라우드 배치)이 요구될 때.
  3. `dag.py`+`executor.py`가 1,500줄을 넘거나 재시도·백오프·이벤트 훅 같은 오케스트레이션 기능이 3개 이상
     추가될 때.
- 후속: 노드 해시 규칙은 ADR-0031, 디렉터리·manifest 레이아웃은 ADR-0032, 실패/재시도 의미론은 ADR-0033,
  단계 파라미터 계약은 ADR-0034.
