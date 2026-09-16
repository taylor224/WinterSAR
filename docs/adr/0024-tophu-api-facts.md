# ADR-0024: tophu 다중해상도 언래핑 API 사실 확인과 어댑터 설계

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-06, R-07, PERF-04, 규칙 11.2·11.3
- 검증 출처(Sources):
  - tophu 릴리스: https://api.github.com/repos/isce-framework/tophu/releases
    (v0.1.0 2023-09-20, v0.2.0 2023-10-10, **v0.2.1 2024-02-14**; main의 `__version__`은 "0.2.0")
  - `src/tophu/_multiscale.py` (v0.2.1, main 동일):
    https://raw.githubusercontent.com/isce-framework/tophu/v0.2.1/src/tophu/_multiscale.py
  - `src/tophu/_unwrap.py` (UnwrapCallback, SnaphuUnwrap/ICUUnwrap/PhassUnwrap):
    https://raw.githubusercontent.com/isce-framework/tophu/v0.2.1/src/tophu/_unwrap.py
  - `src/tophu/_io.py` (DatasetReader/DatasetWriter 프로토콜), `test/test_multiscale.py`
  - `setup.cfg` (license = "BSD-3-Clause OR Apache-2.0", install_requires), `README.md`,
    `LICENSE-Apache-2.0`, `LICENSE-BSD-3-Clause`
  - conda-forge `tophu-feedstock` `recipe/meta.yaml` (tophu 0.2.1; run: dask ≥2022.05.1, h5py ≥3,
    **isce3 ≥0.12**, numpy ≥1.21, python ≥3.8, rasterio ≥1.3, scipy ≥1.5)
  - PyPI: `tophu` 없음(404) → ADR-0001의 "PyPI 없음" 재확인
  - 참고 사용자: dolphin `src/dolphin/unwrap/_tophu.py`, `workflows/config/_unwrap_options.py`
    (`TophuOptions.ntiles=(1,1)`, `downsample_factor=(1,1)`, `init_method="mcf"`, `cost="smooth"`)

## 맥락

플랜 5.2: "tophu는 `downsample_factor`, `ntiles`, `nlooks`, `unwrap_func` 사용". 5.4 항목 3:
대형·고프린지 간섭도는 tophu 다중해상도 우선. 설치되어 있지 않으므로 소스로 인자명을 확인했다.

## 확인한 사실

```python
def multiscale_unwrap(
    unwrapped: DatasetWriter, conncomp: DatasetWriter,
    igram: DatasetReader, coherence: DatasetReader,
    nlooks: float, unwrap_func: UnwrapCallback,
    downsample_factor: tuple[int, int], ntiles: tuple[int, int],
    min_conncomp_overlap: float = 0.5, scratchdir: str | os.PathLike | None = None, *,
    do_lowpass_filter: bool = True, shape_factor: float = 1.5, overhang: float = 0.5,
    ripple: float = 0.01, attenuation: float = 40.0,
) -> None
```

- 결과는 반환되지 않고 `unwrapped`(float32)·`conncomp`(uint32) `DatasetWriter`에 제자리 기록.
  프로토콜은 `dtype, shape, ndim, __setitem__`(Writer) / `__getitem__`(Reader)만 요구하므로
  **numpy 배열이 그대로 통과**한다(tophu 테스트도 numpy float32/uint32 배열 사용).
- `downsample_factor`: "The number of looks to take along each axis in order to form the
  low-resolution interferogram". `ntiles`: "number of tiles along each axis". 타일 오버랩 인자는
  없다(저해상도 해에 2π 정합하므로 불필요).
- `UnwrapCallback.__call__(igram, coherence, nlooks, scratchdir) -> (unwphase, conncomp)`.
  tophu 제공 콜백: `SnaphuUnwrap(cost: Literal["topo","defo","smooth","p-norm"]="smooth",
  cost_params=None, init_method: Literal["mst","mcf"]="mcf")` → `isce3.unwrap.snaphu.unwrap(...)`,
  `ICUUnwrap(...)`, `PhassUnwrap(coherence_thresh=0.2, good_coherence=0.7, min_region_size=200)`.
- **`_unwrap.py`가 `import isce3`를 모듈 최상단에서 수행** → tophu import 자체가 isce3를 요구.
  isce3는 conda-forge 전용. 따라서 tophu는 conda-forge(`conda install -c conda-forge tophu`)로만
  설치 가능하다.
- 마스크 인자는 없다. dolphin은 `zero_where_masked`로 마스크 픽셀의 igram·corr을 0으로 만들어
  넘긴다.
- 라이선스: "licensed under your choice of BSD-3-Clause or Apache-2.0" (SPDX `BSD-3-Clause OR
  Apache-2.0`). import 가능(플랜 9장 표의 "확인 필요" 해소).
- tophu 자체 테스트는 `downsample_factor=(3,3), ntiles=(2,2)`를 쓴다; dolphin 기본은 (1,1)/(1,1).

## 선택지

1. tophu 콜백 `SnaphuUnwrap`(isce3 SNAPHU) 고정.
2. wintersar가 snaphu-py 기반 콜백을 만들어 넘김(isce3 SNAPHU 비의존).
3. **둘 다 지원**: `tophu_unwrap_func = auto|snaphu|snaphu-py|icu|phass`, 기본 `auto`
   (tophu의 `SnaphuUnwrap`; 없으면 snaphu-py 콜백).

## 결정

선택지 3. `wintersar/engines/tophu.py`:

- `TophuEngine(name="tophu", stages=("unwrap",), version_constraint=">=0.2,<1")`,
  `install_hint="conda install -c conda-forge tophu"`.
- 호출: `tophu.multiscale_unwrap(unwrapped=unw f32, conncomp=cc u32, igram=complex64,
  coherence=float32, nlooks=..., unwrap_func=..., downsample_factor=(a,b), ntiles=(rows,cols),
  min_conncomp_overlap=0.5, scratchdir=...)` — 모두 키워드 인자로 전달(이름은 위 시그니처).
- `cost`/`init` → `SnaphuUnwrap(cost=..., init_method=...)`; `snaphu-py` 콜백은 ADR-0023의
  `snaphu.unwrap(igram, corr, nlooks, cost, init, scratchdir, delete_scratch=False)`를 감싼다
  (`topo`는 snaphu-py 미지원 → `UNW-004`).
- 마스크: 코히어런스 임계·수역·레이오버·NaN → igram/corr 0으로 만든 뒤 호출, 결과는 NaN/라벨 0.
- **wintersar 기본값 `downsample_factor=(3,3)`** (tophu 테스트값; 상류 기본값이 아님).
  `(1,1)`(dolphin)은 저해상도 단계가 사라져 다중해상도의 의미가 없다. 벤치마크로 재조정
  (`docs/open-questions.md`).
- 타일 모드에서도 SNAPHU assemble-only는 없다(`assemble_only_capable=False`).
- 검증: 가짜 `tophu` 모듈(동일 시그니처·클래스)로 키워드 집합·dtype·콜백 선택·NaN 마스킹·
  `run()` 산출물 테스트(`tests/unit/engines_unwrap/test_tophu.py`).

## 결과

- tophu 경로는 isce3 설치를 전제한다(대용량 conda 환경). 스케줄러가 tophu를 자동 선택할 때는
  `check_install()`의 ENV-001을 먼저 확인해야 한다.
- 연구 모듈(R-07)의 "대표위상" 실험은 `downsample_factor`와 `do_lowpass_filter`를 바꿔가며
  같은 API로 수행할 수 있다.
