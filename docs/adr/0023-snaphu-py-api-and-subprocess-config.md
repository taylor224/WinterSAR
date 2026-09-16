# ADR-0023: snaphu-py API 사실 확인과 SNAPHU 설정 파일 subprocess 경로

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-06, PERF-03, PERF-04, KB-SNAPHU-001/002, 규칙 11.2·11.3
- 검증 출처(Sources):
  - snaphu-py v0.4.1 (최신 릴리스, 2024-09-16): https://github.com/isce-framework/snaphu-py/releases/tag/v0.4.1
    - `src/snaphu/_unwrap.py` (태그 v0.4.1 및 main 동일): https://raw.githubusercontent.com/isce-framework/snaphu-py/v0.4.1/src/snaphu/_unwrap.py
    - `src/snaphu/_check.py`: https://raw.githubusercontent.com/isce-framework/snaphu-py/v0.4.1/src/snaphu/_check.py
    - `src/snaphu/_snaphu.py`: https://raw.githubusercontent.com/isce-framework/snaphu-py/v0.4.1/src/snaphu/_snaphu.py
    - `src/snaphu/__init__.py`, `README.md`, `pyproject.toml`, `LICENSE-Apache-2.0`, `LICENSE-BSD-3-Clause`
  - PyPI `snaphu` 0.4.1 (https://pypi.org/pypi/snaphu/json), conda-forge `snaphu-feedstock` `recipe/meta.yaml`
    (license `(Apache-2.0 OR BSD-3-Clause) AND LicenseRef-SNAPHU`)
  - SNAPHU C 코어 v2.0.7 (2024-02): https://web.stanford.edu/group/radar/softwareandlinks/sw/snaphu/
    - `snaphu.conf.full`(설정 키워드 전체), `README`(라이선스), `README_releasenotes.txt`
    - man page: https://manpages.ubuntu.com/manpages/noble/man1/snaphu.1.html (snaphu 2.0.6-2);
      플랜 12.2의 bionic 페이지는 구버전(`--assemble dirname` 문법)이라 noble 페이지로 대체

## 맥락

플랜 5.2는 `snaphu.py`가 snaphu-py의 `ntiles`, `tile_overlap`, `nproc`, `cost`, `init`, `mask`를
노출하되 "정확한 인자명은 설치 버전 문서로 확인"하라고 했다. 또한 5.4/PERF-03은 타일 조립
파라미터만 바꿀 때 SNAPHU assemble-only(`-A`)와 비용 배열 저장(`--costoutfile`)을 요구한다.
snaphu-py는 개발 환경에 설치되어 있지 않으므로(ADR-0001) GitHub 소스를 직접 읽어 확인했다.

## 확인한 사실

### `snaphu.unwrap` 시그니처 (v0.4.1, main 동일)

```python
def unwrap(
    igram, corr, nlooks, cost="smooth", init="mcf", *,
    mask=None, min_conncomp_frac=0.01, phase_grad_window=(7, 7),
    ntiles=(1, 1), tile_overlap=0, nproc=1, tile_cost_thresh=500, min_region_size=100,
    single_tile_reoptimize=True, regrow_conncomps=True,
    scratchdir=None, delete_scratch=True, unw=None, conncomp=None,
) -> tuple[unw, conncomp]
```

- `igram` 2-D 복소, `corr` 실수(코히어런스), `nlooks` 등가 룩 수(필수). 입력은 `nan_to_zero`
  변환 후 `complex64`/`float32`로 파일에 기록된다.
- `mask`: "Binary mask of valid pixels. **Zeros** in this raster indicate interferogram pixels
  that should be masked out" (bool 또는 8-bit 정수). → wintersar의 `mask`(True=제외)와 반대이므로
  어댑터가 `~masked`를 넘긴다.
- `cost`: `_check.py::check_cost_mode`가 `{"defo", "smooth"}`만 허용하고 **`"topo"`는
  `NotImplementedError`** ("'topo' cost mode is not currently supported"). `init`은 `{"mst","mcf"}`.
- `tile_overlap`: 정수 또는 `(row, col)` 튜플. `ntiles`는 양의 정수 쌍(`check_2d_shapes`).
- 출력: `unw`가 None이면 `float32` 0 배열, `conncomp`가 None이면 **`uint32`** 0 배열을 만들어
  채운다. 반환은 `(unw, conncomp)`.
- 생성되는 SNAPHU 설정 키워드: `INFILE, INFILEFORMAT, CORRFILE, CORRFILEFORMAT, OUTFILE,
  OUTFILEFORMAT, CONNCOMPFILE, CONNCOMPOUTTYPE, LINELENGTH, NCORRLOOKS, STATCOSTMODE, INITMETHOD,
  MINCONNCOMPFRAC, KPARDPSI, KPERPDPSI, NTILEROW, NTILECOL, ROWOVRLP, COLOVRLP, NPROC,
  TILECOSTTHRESH, MINREGIONSIZE` (+ 조건부 `BYTEMASKFILE`, `SINGLETILEREOPTIMIZE`).
  **`TILEDIR`/`ASSEMBLEONLY`/`COSTOUTFILE`은 쓰지 않으며 추가 설정 줄을 넘길 인자도 없다.**
  SNAPHU 2.0에서 타일 임시 파일은 기본 삭제(릴리스 노트)이므로 snaphu-py 경로로는 assemble-only
  재사용이 불가능하다.
- 실행: `_snaphu.py::run_snaphu(config_file)`가 패키지 안에 번들된 실행 파일
  (`importlib.resources.files(__package__) / "snaphu"`)을 `[snaphu, "-f", config_file]`로 호출.
  `get_snaphu_version()`은 `snaphu -h` 출력에 정규식 `^snaphu v(?P<version>[0-9]+(?:\.[0-9]+)*)$`
  (MULTILINE)를 적용한다. 실행 파일은 PATH에 올라가지 않는다.
- 설치: `conda install -c conda-forge snaphu` 또는 `pip install snaphu` (README). Python ≥3.9,
  numpy ≥1.20. Windows 미지원.
- 라이선스: snaphu-py 자체는 "BSD-3-Clause OR Apache-2.0". README: "The SNAPHU source code
  (which is included as a Git submodule) is subject to different license terms … parts of the
  SNAPHU codebase are subject to terms that prohibit commercial use." SNAPHU `README`(Stanford):
  사용·복사·수정·배포 허용(저작권 고지 유지) + **cs2 MCF 솔버는 "strictly noncommercial
  purposes"**, 상용은 IG Systems에 별도 문의. conda-forge 표기 `LicenseRef-SNAPHU`.

### SNAPHU 설정 파일 키워드 (`snaphu.conf.full`, v2.0.7)

| 키워드 | 값 | 비고 |
|---|---|---|
| `STATCOSTMODE` | `TOPO` / `DEFO` / `SMOOTH` / `NOSTATCOSTS` | wintersar `cost` |
| `INITMETHOD` | `MST` / `MCF` | wintersar `init` |
| `INFILEFORMAT` | `COMPLEX_DATA`(기본) | re/im 교대 float32, 기계 native byte order |
| `CORRFILEFORMAT`, `OUTFILEFORMAT` | `FLOAT_DATA` 선택 | 기본은 `ALT_LINE_DATA` |
| `BYTEMASKFILE` | signed byte, 0=제외, 1=유효 | |
| `CONNCOMPFILE`, `CONNCOMPOUTTYPE` | `UCHAR`(기본) / `UINT` | wintersar는 `UINT`로 읽어 uint32 유지 |
| `NCORRLOOKS` | double | nlooks |
| `NTILEROW NTILECOL ROWOVRLP COLOVRLP NPROC` | 타일 격자·오버랩(px)·프로세스 | |
| `TILECOSTTHRESH`, `MINREGIONSIZE` | long | KB-SNAPHU-001 조치 대상 |
| `TILEDIR`, `RMTMPTILE`, `ASSEMBLEONLY` | 디렉터리, TRUE/FALSE, TRUE/FALSE | 2.0에서 `ASSEMBLEONLY`는 불리언 + `TILEDIR` 분리(릴리스 노트) |
| `SINGLETILEREOPTIMIZE` | TRUE/FALSE | |
| `COSTOUTFILE`, `COSTINFILE` | 파일 | PERF-03 비용 배열 재사용 |
| `LOGFILE`, `VERBOSE` | 파일, TRUE/FALSE | |

man page 옵션(2.0.6): `-f configfile`, `-l logfile`, `-d/-s/-t`, `--mst/--mcf`,
`--tile ntilerow ntilecol rowovrlp colovrlp`, `--nproc n`, `--tiledir dirname`, `--assemble`,
`--costoutfile/--costinfile`, `-M bytemaskfile`, `-g maskfile`. 플랜의 `-A`는 man page에서
"read **power** data"이며 assemble-only가 아니다 → 어댑터는 설정 파일 키워드만 사용한다.

## 선택지

1. snaphu-py만 사용 (topo·assemble-only·costoutfile 포기).
2. SNAPHU 실행 파일 + 자체 설정 파일 생성만 사용 (snaphu-py의 재라벨링·검증 포기).
3. **두 백엔드 병행**: 기본은 snaphu-py, `topo`·assemble-only·`save_cost_file`·추가 키워드가
   필요하면 실행 파일(snaphu-py 번들 바이너리 → `$WINTERSAR_SNAPHU_EXE` → PATH)을
   `snaphu -f <conf>`로 호출.

## 결정

선택지 3. `wintersar/engines/snaphu.py`:

- `SnaphuEngine(name="snaphu", stages=("unwrap",), version_constraint=">=0.4,<1")`.
  실행 파일만 있을 때는 `>=2.0,<3`으로 검사하고 INFO `UNW-013`을 낸다.
- 파라미터 매핑: `cost→cost/STATCOSTMODE`, `init→init/INITMETHOD`, `tiles{rows,cols,overlap,
  min_overlap_px}→ntiles/(NTILEROW,NTILECOL)`와 `tile_overlap/(ROWOVRLP,COLOVRLP)`
  (오버랩 = max(min_overlap_px, overlap×타일 변) px, 타일 변 미만으로 캡 → WARN `UNW-006`),
  `nproc_per_igram→nproc/NPROC`, `coherence_threshold`+수역/레이오버 마스크+NaN → `mask`
  (`~masked`) / `BYTEMASKFILE`. `nlooks`는 `params.nlooks` → `looks=[rg,az]` → 스택 attrs →
  1.0 (WARN `UNW-007`).
- 출력: `UnwrapResult(unw float32 NaN-마스크, conncomp uint32, stats)`; `stats`에
  `wall_time_s`, `backend`, 타일 사용 시 `tile_dir`·`assemble_only_capable`.
- 설정 파일 생성기 `SnaphuConfig`는 위 표의 키워드만 쓴다. 타일 모드에서는 `TILEDIR`+
  `RMTMPTILE FALSE`로 보존하고, `assemble_only=True`+`tile_dir`이면 `ASSEMBLEONLY TRUE`.
- 검증: 가짜 `snaphu` 모듈(동일 시그니처)과 가짜 실행 파일(설정 파일 해석)로 인자 매핑·마스크
  부호·dtype·assemble-only 왕복을 테스트(`tests/unit/engines_unwrap/test_snaphu.py`).

## 결과

- snaphu-py 설치만으로는 `cost=topo`와 assemble-only가 불가능하다(`UNW-004`). 재튠 재사용
  (PERF-03)은 `backend=subprocess`+`keep_tile_dir`에서만 동작한다.
- 상용 이용은 cs2 조항 때문에 법률 검토가 필요하다(플랜 9장). 코어는 번들하지 않는다.
- SNAPHU 1.x 실행 파일(`ASSEMBLEONLY dirname` 문법)은 지원 범위 밖 → `docs/open-questions.md`.
- 저장 시 `save_igram_stack`이 `conncomp`를 uint8로 캐스팅한다(라벨 >255면 WARN `UNW-008`);
  공용 IO 변경은 needs_from_others로 보고.
