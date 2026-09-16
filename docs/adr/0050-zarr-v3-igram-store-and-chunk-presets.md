# ADR-0050: Zarr v3 간섭도 스택 저장소와 청크 프리셋 (PERF-08)

- 상태(Status): 채택 (프리셋 선택은 bench 실측 후 확정)
- 날짜(Date): 2026-09-16
- 관련 ID: PERF-08, R-13, 플랜 §6.2, §12.3 행 11 (zarr 3.x API 채택 여부)
- 검증 출처(Sources):
  - 설치된 zarr 3.1.6 소스 `.venv/lib/python3.11/site-packages/zarr/`:
    `api/synchronous.py:818 create_array(store, *, name, shape, dtype, chunks, shards, filters,
    compressors, serializer, fill_value, zarr_format=3, attributes, dimension_names, overwrite, …)`,
    `api/synchronous.py:478 open_group(store, mode='r'|'r+'|'a'|'w'|'w-', zarr_format, …)`,
    `core/group.py:2613 Group.create_array(name, *, shape, dtype, chunks, compressors, fill_value,
    dimension_names, overwrite, …)`, `core/array.py:4032 Array.resize(new_shape)`,
    `codecs/zstd.py:38 ZstdCodec(level=0, checksum=False)`, `codecs/__init__.py` (`zstd` 코덱 등록),
    `storage/_local.py LocalStore` (파일시스템 경로 기본 스토어).
  - zstd 기본 압축 레벨: <https://github.com/facebook/zstd/blob/dev/lib/zstd.h>
    `#define ZSTD_CLEVEL_DEFAULT 3` (zarr의 `ZstdCodec` 기본값은 0).
  - 구현: `src/wintersar/io/zarr_store.py`, 테스트 `tests/unit/io/test_zarr_store.py`.

## 맥락 (Context)

플랜 §6.1 PERF-08: ISCE/HyP3는 간섭도마다 파일이 따로 있어 픽셀 시계열 조회가 파일 수백 개를
열고, QGIS 로딩이 느리다. §6.2는 시계열 조회용 `(time=전체, y=512, x=512)`와 표시용
`(time=1, y=2048, x=2048)` 청크를 각각 실측해 고르고, 압축은 zstd를 기본으로 하라고 한다.
미확정 사항 11번: 설치된 zarr가 3.1.6인데 `pyproject.toml`은 `zarr>=2.18`이라 2.x 호환 API를
쓸지 3.x 전용 API를 쓸지 정해야 했다.

## 선택지 (Options)

1. zarr 2 호환 API(`zarr.open(..., zarr_format=2)`, numcodecs 압축기)로 작성해 2.18~3.x 모두 지원.
2. zarr 3 전용 API(`create_array`, `compressors=[ZstdCodec]`, `dimension_names`, v3 메타데이터)로
   작성하고 의존성을 `zarr>=3.0`으로 올린다.
3. HDF5(h5py) 청크 데이터셋. MintPy와 같은 포맷이지만 동시 쓰기·클라우드 스토어에 불리하다.

## 결정 (Decision)

선택지 2. `IgramZarr`는 한 그룹에 `wrapped`(float32, NaN 채움), `coherence`(float32),
`mask`(uint8, 1 = 마스크됨), 선택적 `unw`(float32)·`conncomp`(uint8) 배열을 `(pair, y, x)`
차원 이름으로 만든다. 쌍 키·날짜·프리셋·압축기·`n_written`은 그룹 attrs(JSON)에 둔다.

- **청크 프리셋** `CHUNK_PRESETS = {"timeseries": (None, 512, 512), "display": (1, 2048, 2048)}`.
  `None`은 "쌍 전체"이며 실제 청크는 배열 크기로 클램프한다(`resolve_chunks`). 임의의
  `(t, y, x)` 튜플도 받는다.
- **압축**: `ZstdCodec(level=3)` — zstd 라이브러리 기본 레벨(`ZSTD_CLEVEL_DEFAULT`). zarr의
  자체 기본값 0은 "라이브러리 기본"을 뜻하지만 명시해 재현성을 확보한다. `gzip`·무압축도 선택 가능.
- **성장**: 용량을 넘겨 `append_pair`하면 `Array.resize`로 쌍 축을 1 늘린다.
  timeseries 프리셋에서는 이때 두 번째 시간 청크가 생기므로 가능하면 `n_pairs`를 미리 준다.
- **조회 API**: `read_pixel_series(row, col)`(PERF-08 질의 패턴, 배열당 청크 1개 접근),
  `read_slice(i)`(표시 패턴), `read_window`, `to_igram_stack`/`from_igram_stack`.
- **측정 도구**: `benchmark_chunk_presets(stack, out_dir)`는 프리셋별로 쓰기 시간, 무작위
  픽셀 시계열·슬라이스 읽기의 중앙값, 저장 바이트를 **숫자로만** 돌려준다. 어떤 프리셋이 빠른지는
  S 사이트 실측(`bench_result.json`)으로 판단한다(규칙 11.8) — 이 ADR은 수치를 적지 않는다.

## 결과 (Consequences)

- `pyproject.toml`의 `zarr>=2.18`은 `zarr>=3.0`으로 올려야 한다(공유 파일이라 통합자에게 요청,
  needs_from_others). zarr 2.x에서는 `create_array`·`ZstdCodec`이 없어 import 시점에 실패한다.
- timeseries 프리셋은 쌍이 많을수록 청크가 커진다(쌍 100개 × 512 × 512 × 4 B ≈ 105 MB/청크).
  픽셀 하나를 읽어도 청크 하나를 통째로 푼다. 쌍 수가 큰 M/L 사이트에서는 `(None, 256, 256)`이나
  zarr 3의 `shards`가 나을 수 있다 — `docs/open-questions.md`에 실측 항목으로 남긴다.
- 불리언 마스크를 uint8로 저장한다. 읽을 때 `bool`로 돌려주므로 API 사용자는 차이를 못 본다.
- v3 메타데이터(`zarr.json`)는 zarr 2.x 리더가 읽지 못한다. QGIS 등 외부 리더 호환은 COG
  내보내기(ADR-0051)가 담당한다.
