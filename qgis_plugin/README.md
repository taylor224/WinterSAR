# wintersar QGIS plugin (thin client)

QGIS 안에서 `wintersar` CLI를 호출해 검색 → 사전검증 → 실행 → 결과 레이어 → 기준점 → 검증
흐름을 완료하는 얇은 클라이언트입니다 (플랜 §5.8, R-12, ADR-0070/0071).
QGIS 파이썬 안에는 **아무것도 설치하지 않습니다**: 툴킷은 별도의 conda/pixi/uv 환경에 두고
플러그인은 그 환경의 파이썬으로 `python -m wintersar.cli --json ...` 를 subprocess 실행한 뒤
JSON 봉투 `{"ok","command","data","findings"}` 를 파싱합니다.

## 지원 QGIS 버전

- `qgisMinimumVersion=3.44` (현재 LTR 3.44.x), `qgisMaximumVersion=4.99` (Qt6 기반 4.x 라인).
  Qt 클래스는 `qgis.PyQt` 호환 계층으로 import 하고 열거형은 완전 한정형(`Qt.DockWidgetArea.RightDockWidgetArea`)만
  사용합니다. 근거: ADR-0070.
- `experimental=True`: 플러그인 관리자에서 "Show also Experimental Plugins" 를 켜야 보입니다.

## 설치

1. 툴킷 환경 준비 (예: uv)

   ```bash
   git clone https://github.com/taylor224/WinterSAR && cd wintersar
   uv sync --extra dev
   .venv/bin/wintersar --json version        # {"ok": true, "command": "version", ...}
   ```

2. 플러그인 ZIP 만들기

   ```bash
   .venv/bin/python qgis_plugin/build_zip.py            # -> qgis_plugin/dist/wintersar_qgis-<version>.zip
   ```

3. QGIS → 플러그인 → 플러그인 관리 및 설치 → **ZIP에서 설치** 탭에서 위 파일 선택.
   설치된 플러그인은 활성 사용자 프로필의 `python/plugins` 폴더에 풀립니다.
   프로필 폴더 기본 위치(QGIS 3.44 사용자 안내서 기준):
   - Linux `~/.local/share/QGIS/QGIS3/profiles/default`
   - Windows `%APPDATA%\QGIS\QGIS3\profiles\default`
   - macOS `~/Library/Application Support/QGIS/QGIS3/profiles/default`

4. 플러그인 패널 상단 **실행 환경** 상자에서 툴킷 환경을 지정:
   - `Python 실행 파일`: 예 `<repo>/.venv/bin/python` (가장 확실한 방법), 또는
   - `환경 힌트`: `conda:wintersar` · `conda:/opt/conda/envs/wintersar` · `pixi:/path/to/project` ·
     `uv:/path/to/repo` · `venv:/path/to/.venv`.
   - `설치 점검` 버튼이 `wintersar check-install` 을 실행해 엔진·인증·하드웨어 상태를 로그에 보여 줍니다.

개발 중에는 ZIP 대신 `qgis_plugin/wintersar_qgis` 를 프로필의 `python/plugins/wintersar_qgis` 로
심볼릭 링크하면 체크아웃의 `src/wintersar/i18n/*.yaml` 을 직접 읽습니다 (QGIS 파이썬에 PyYAML 필요).

## 패널

| # | 패널 | CLI |
|---|---|---|
| 1 | AOI 그리기/불러오기(캔버스 범위·활성 레이어 → GeoJSON) → 검색 → 후보 스택 표(FAIL/WARN/양호 배지, 추천 스택) | `search`, `precheck --no-fail` |
| 2 | 파라미터 폼: `config.yaml` 을 YAML 왕복 편집(`wintersar init` 으로 새로 만들기) | `init` |
| 3 | 실행: `--until/--from/--force`, 진행 상태, 단계 표, Finding(원인 → 조치), 로그, 실패 시 로그 진단 | `plan`, `run`, `diagnose` |
| 4 | 결과 레이어: 마지막 실행(`work/runs/<run_id>.json`)의 COG/GeoTIFF 산출물을 `QgsRasterLayer` 로 로드 | — |
| 5 | 기준점: 추천 후보 표 + 지도 클릭 선택 → `timeseries.reference_point` 에 적용 | `refpoint` |
| 6 | 검증: 대조군 CSV 를 점 레이어로 로드, 지점별 RMSE/편향 표, 시계열 플롯(matplotlib 없으면 표) | `validate` |

Processing 툴박스에도 `wintersar` 공급자(검색·사전검증·실행·검증)가 등록되어 모델러에서 연결할 수 있습니다.

## 오류 메시지 (원인 → 조치)

| ID | 뜻 |
|---|---|
| `CLI_NOT_FOUND` | 지정한 환경에서 파이썬/`wintersar` 를 시작하지 못함 → 실행 파일 또는 환경 힌트 수정 |
| `TIMEOUT` | 제한 시간 초과 → 설정에서 늘리거나 긴 단계는 터미널에서 실행 |
| `INVALID_JSON` | CLI 가 JSON 봉투를 내지 않음 → 터미널에서 `--json` 으로 재실행해 stderr 확인, 버전 불일치 점검 |
| `CLI_ERROR` | 0 이 아닌 종료 코드 → 로그 확인 후 `diagnose` |

## 테스트

플러그인의 순수 파이썬 부분(클라이언트·설정·파라미터 매핑·표 행 생성·metadata.txt)은 QGIS 없이 테스트합니다.

```bash
.venv/bin/python -m pytest tests/unit/qgis -q
```

QGIS 자체에서의 동작(두 LTR 버전)은 Phase 7 DoD 항목으로 수동 확인합니다.
