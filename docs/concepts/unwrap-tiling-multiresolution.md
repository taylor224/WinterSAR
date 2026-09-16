# 언래핑 타일과 다중해상도 — 큰 간섭도를 나누고 다시 붙이기 (R-06, R-07, PERF-04)

위상 언래핑(phase unwrapping)은 −π~π 로 접힌 위상에 2π 의 정수배를 더해 연속 위상을 복원하는
최적화 문제입니다. SNAPHU 는 이를 네트워크 흐름(MCF) 문제로 풀며, 픽셀 수가 커질수록 메모리와 시간이
급격히 늘어납니다(SNAPHU man page 의 단일 타일 메모리 근사치 c ≈ 100 MB/백만 픽셀을 `unwrap.memory_mb_per_mpixel`
초기값으로 쓰고 bench 로 재적합합니다 — open-questions #8). wintersar 는 언래핑 알고리즘 자체를 건드리지
않습니다(규칙 11.3). 개선은 **스케줄링·타일·마스크**에서 얻습니다.

## 타일 언래핑 (SNAPHU tile 모드)

큰 간섭도를 `rows × cols` 타일로 나눠 각각 언래핑한 뒤 재조립합니다. 타일 사이에는 **오버랩**(픽셀 단위)을
두고, 오버랩 구간에서 두 타일의 결과가 2π 정수배만큼 어긋나면 그 정수를 맞춰 붙입니다(신뢰 영역 분할 +
보조 네트워크 최적화). 오버랩이 너무 작거나 지역이 잘게 쪼개지면 조립이 실패합니다(`KB-SNAPHU-001`
"Exceeded maximum number of secondary nodes", `KB-SNAPHU-003` 타일 파라미터 오류).

연구자 경험치 30 %, 문헌·포럼의 200 px 사이에서 wintersar 의 기본값은 **오버랩 25 %, 최소 200 px**
(`unwrap.tiles: {rows, cols, overlap, min_overlap_px}`)이며 bench 로 결정합니다(ADR-0046, open-questions #9).

## 다중해상도 (tophu, SARscape 의 decomposition level 에 대응)

먼저 크게 멀티룩한 **저해상도 위상**을 언래핑하고, 각 고해상도 타일을 독립 언래핑한 뒤 타일마다
저해상도 결과와의 차이가 최소가 되도록 2π 사이클을 가감해 경계 단차를 없앱니다(tophu 의 `coarse_ref`
방식). 여기서 저해상도 "**대표위상**"이 타일 안의 위상을 얼마나 잘 대표하느냐가 결과를 좌우합니다 —
단순 복소 멀티룩(산술 평균)이 최선인지, 코히어런스 가중·SHP·phase linking 이 나은지가 연구 모듈
`research.repr_phase` 의 질문입니다(R-07, ADR-0048 단차 검출기 공용).

## wintersar 스케줄러의 결정 순서 (ADR-0045, ADR-0047)

1. 멀티룩 후 픽셀 수 P → 단일 타일 예상 메모리 `m ≈ c × P / 1e6` (MB).
2. `m ≤ 프로세스당 예산` 이면 **타일 없이** 언래핑(타일 경계 아티팩트 없음). 아니면 `ntiles = ceil(m / 예산)`
   을 정사각형에 가까운 격자로.
3. 병렬화는 **간섭도 단위를 먼저 채우고**, 단일 간섭도가 예산을 넘을 때만 타일 병렬:
   `n_parallel = min(floor(cores / nproc_per_igram), floor(RAM / m))`.
4. 프린지 밀도가 높거나 대형이면 tophu 다중해상도를 우선 선택(`unwrap.method: auto`).
5. 마스크(수역·저코히어런스 `unwrap.coherence_threshold`·레이오버 `unwrap.mask.*`)로 노드 수를 줄이고,
   마스크 픽셀은 NaN 으로 출력합니다(SARscape 동작과 같음).
6. 조립 파라미터만 바꿀 때는 SNAPHU `--assemble`(타일 임시 디렉터리 보존)로 타일 재언래핑을 건너뜁니다
   (PERF-03; snaphu-py 경로 제약은 open-questions #18).

`wintersar unwrap plan --shape NY NX --n N [--memory-gb G] [--cores C]` 가 이 결정과 이유를 표로 보여 줍니다.
출력 통계: 연결성분 수, 타일 경계 단차 수(오버랩 구간 차이의 2π 정수배 분포), 실행 시간, peak RSS.

관련: [픽셀 간격과 looks](pixel-spacing-looks.md), ADR-0023(snaphu-py), ADR-0024(tophu), `docs/kb/KB-SNAPHU-00x.md`.

## English summary

Unwrapping cost grows quickly with pixel count, so large interferograms are split into overlapping
tiles that are unwrapped independently and re-assembled by resolving 2-pi integer offsets in the
overlaps (SNAPHU tile mode; assembly failures are `KB-SNAPHU-001/003`). Multi-resolution unwrapping
(tophu) first unwraps a coarse, heavily multilooked phase and then shifts each fine tile by whole
cycles to match it; the quality of that coarse "representative phase" drives the result (research
module, R-07). The wintersar scheduler estimates single-tile memory from `unwrap.memory_mb_per_mpixel`,
unwraps without tiles when it fits, otherwise picks a near-square grid with 25 % / >= 200 px overlap
(defaults pending bench), fills interferogram-level parallelism before tile parallelism, masks water,
low-coherence and layover pixels (NaN output) and reuses SNAPHU `--assemble` when only assembly
parameters change. `wintersar unwrap plan` prints the decision and its reasons.
