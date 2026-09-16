# ADR-0070: QGIS 플러그인 아키텍처 — CLI `--json` 위의 얇은 클라이언트

- 상태(Status): 채택
- 날짜(Date): 2026-09-16
- 관련 ID: R-12, R-14, 플랜 §5.8, §4.5, Phase 7
- 검증 출처(Sources): 모두 2026-09-16 WebFetch
  - PyQGIS Developer Cookbook, Structuring Python Plugins:
    https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/plugins/plugins.html
    — `__init__.py` "has to have the classFactory() method"; `metadata.txt` 필수 키 `name,
    qgisMinimumVersion, description, about, version, author, email, repository`; 선택 키
    `qgisMaximumVersion, changelog, experimental, deprecated, tags, homepage, tracker, icon,
    category, plugin_dependencies, server, hasProcessingProvider`; 플러그인 클래스 `__init__(iface)`,
    `initGui()` "called when the plugin is loaded", `unload()` "called when the plugin is unloaded"
  - PyQGIS Cookbook, Writing a Processing plugin:
    https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/processing.html
    — `hasProcessingProvider=yes`; `QgsApplication.processingRegistry().addProvider()/removeProvider()`;
    provider `id()/name()/icon()/loadAlgorithms()`+`addAlgorithm()`; 알고리즘
    `createInstance/name/displayName/group/groupId/shortHelpString/initAlgorithm/processAlgorithm`;
    `parameterAsString/Bool/File/Enum`
  - API 시그니처: `QgsProcessingProvider` (abstract `id, name, loadAlgorithms`),
    `QgsProcessingAlgorithm` (`parameterAsFile(parameters, name, context) -> str` 등),
    `QgsProcessingParameterFile(name, description='', behavior=File, extension='', defaultValue=None,
    optional=False, fileFilter='')`, `QgsProcessingParameterEnum(name, description='', options=[],
    allowMultiple=False, defaultValue=None, optional=False, usesStaticStrings=False)`,
    `QgsProcessingFeedback.pushInfo/pushWarning/reportError(error, fatalError=False)`,
    `QgsFeedback.isCanceled()/setProgress(progress: float)`, `QgisInterface.addDockWidget(area, dockwidget)`
    /`removeDockWidget`/`addPluginToMenu(name, action)`/`removePluginMenu`/`addToolBarIcon`/`removeToolBarIcon`
    /`mapCanvas()`/`activeLayer()`/`messageBar()`, `QgsMessageBar.pushMessage(title, text, level=Qgis.MessageLevel.Info,
    duration=-1)`, `QgsMapToolEmitPoint(canvas)` + signal `canvasClicked(point: QgsPointXY, button: Qt.MouseButton)`,
    `QgsMapCanvas.extent()/mapSettings()/setMapTool(mapTool, clean=False)/unsetMapTool(mapTool)`,
    `QgsMapSettings.destinationCrs()`, `QgsGeometry.fromRect(rect)/transform(ct, ...)/asJson(precision=17)`,
    `QgsApplication.qgisSettingsDirPath() -> str` — https://qgis.org/pyqgis/3.44/{core,gui}/<Class>.html
  - 레이어 로드·CRS 변환·CSV 점 레이어: cookbook `loadlayer.html`(`QgsRasterLayer(path, name)` →
    `isValid()` → `QgsProject.instance().addMapLayer()`, delimitedtext URI
    `file:///…csv?delimiter=,&crs=epsg:4326&xField=lon&yField=lat`), `crs.html`
    (`QgsCoordinateTransform(crsSrc, crsDest, QgsProject.instance().transformContext())`, `xform.transform(pt)`)
  - QGIS 버전: https://qgis.org/download/ — 2026-09-16 기준 최신 4.2.2, **LTR 3.44.14 'Solothurn'**;
    https://qgis.org/resources/roadmap/ — 3.44.0(2025-06-20) LTR, 4.2.3(2026-09-25) LTR 예정
  - Qt5/Qt6 호환: https://plugins.qgis.org/docs/migrate-qgis4 ("Set qgisMaximumVersion to cover QGIS 4.x",
    `qgisMaximumVersion=4.99`, `supportsQt6` 는 더 이상 필요 없음, `qgis.PyQt` 호환 계층 사용) ·
    https://github.com/qgis/QGIS/wiki/Plugin-migration-to-be-compatible-with-Qt5-and-Qt6
    ("Working for both PyQt5 and PyQt6: Qt.ItemDataRole.UserRole, …" — 완전 한정 열거형)
  - 설치 위치: https://docs.qgis.org/latest/en/docs/user_manual/plugins/plugins.html — "Installed external
    python plugins are placed under the python/plugins folder of the active user profile path";
    "The Install from ZIP tab provides a file selector widget to import plugins in a zipped format";
    프로필 폴더(3.44 사용자 안내서 `qgis_configuration.html`): Linux `~/.local/share/QGIS/QGIS3/profiles/`,
    Windows `AppData\Roaming\QGIS\QGIS3\profiles\`, macOS `Library/Application Support/QGIS/QGIS3/profiles/`
  - 봉투 형식: `src/wintersar/util/output.py::emit_json` (`{"ok","command","data","findings"}`),
    `src/wintersar/cli.py`(전역 옵션 `--json/--lang` 이 하위 명령 앞), 각 모듈 `cli.py` 의 인자명

## 맥락 (Context)

플랜 §5.8 은 "얇은 클라이언트: 설정된 conda/pixi 환경의 `wintersar` CLI 를 subprocess 로 호출하고
`--json` 결과를 파싱(PyQGIS 파이썬과 의존성 충돌 회피)" 을 요구한다. QGIS 파이썬은 배포판마다 다른
numpy/GDAL/PROJ 를 가지며(ENV-004 참조), rasterio·h5py·zarr·pydantic 을 그 안에 설치하는 것은 사용자
환경을 깨뜨리기 쉽다. 또한 이 저장소에는 QGIS 가 설치되어 있지 않으므로 플러그인 로직의 대부분을 QGIS
없이 테스트할 수 있어야 한다.

## 선택지 (Options)

1. QGIS 파이썬에 `wintersar` 를 pip 로 설치하고 파이썬 API(`wintersar.pipeline.api` 등)를 직접 import.
2. **subprocess + `--json` 봉투 파싱**: 플러그인은 `wintersar` 를 import 하지 않고 별도 환경의 파이썬으로
   `python -m wintersar.cli --json --lang <ko|en> <command>` 를 실행한다.
3. 로컬 HTTP/RPC 서버를 띄우고 플러그인이 REST 로 통신.

## 결정 (Decision)

선택지 2.

- **프로세스 경계**: `qgis_plugin/wintersar_qgis/cli_client.py::WintersarClient.run(args, timeout, on_line)`
  가 `Popen` 으로 실행하고 stdout 은 봉투 파싱(`parse_envelope`: 마지막 최상위 JSON 객체, 앞선 잡음 무시),
  stderr 는 줄 단위로 `on_line` 콜백에 흘린다(진행 로그). 실패는 `Finding` 모양의 dict 로 통일한다:
  `CLI_NOT_FOUND` / `TIMEOUT` / `INVALID_JSON`(종료 0인데 봉투 없음) / `CLI_ERROR`(0 이 아닌 종료 + 봉투 없음)
  / `CANCELLED`, 메시지 키 `qgis.error.<ID>.cause|fix`. 봉투가 있으면 종료 코드와 무관하게 봉투의 `ok` 를
  따른다(`precheck` 는 FAIL 이 있으면 종료 1 이지만 봉투를 낸다).
- **argv 는 트리의 CLI 시그니처에서만**: `search_args/precheck_args/plan_args/run_args/diagnose_args/
  check_install_args/init_args/cache_ls_args` 는 `select/cli.py`, `pipeline/cli.py`, `diagnose/cli.py`,
  `cli.py`, `validate/cli.py` 의 옵션명을 그대로 쓰고 `# source:` 주석을 단다(`validate --ts --leveling
  --gnss`, `refpoint --ts --aoi --top` — 플랜 §4.5 의 `--ts-dir` 와 달리 구현은 `--ts` 파일 인자를 받는다).
  `STAGE_ORDER` 는 `pipeline/stages.py` 사본이고 테스트가 동일성을 검사한다.
- **QGIS 없는 테스트 가능성**: 모듈 최상위에서 `qgis`/`PyQt` 를 import 하지 않는다. Qt 클래스는
  `build()`·핸들러·팩토리 함수(`make_algorithm_classes`, `make_provider_class`) 안에서 import 하고,
  `QObject` 서브클래스 대신 **`queue.Queue` + `QTimer` 폴링**으로 워커 스레드 결과를 GUI 스레드로 넘긴다.
  `dock_widget.py` 의 순수 부분(설정 폼 필드표 `CONFIG_FIELDS`, 값 파싱, 표 행 생성)은
  `tests/unit/qgis/` 에서 검증한다.
- **패널 6종**(플랜 §5.8): ① AOI(캔버스 범위·활성 레이어 → GeoJSON)·검색·후보 표(FAIL/WARN/양호 배지,
  추천 스택) ② `config.yaml` 폼(YAML 왕복, 빈 칸 = 키 제거 → pydantic 기본값) ③ 실행(진행·단계 표·
  Finding 원인→조치·로그·실패 시 `diagnose`) ④ 결과 레이어(`work/runs/<run_id>.json` 의 COG/GeoTIFF 를
  `QgsRasterLayer`) ⑤ 기준점(추천 표 + `QgsMapToolEmitPoint` 지도 클릭 → EPSG:4326 변환 →
  `timeseries.reference_point`) ⑥ 검증(대조군 CSV 점 레이어, 지점별 RMSE/편향 mm 표, matplotlib
  `backend_qtagg` 플롯 — 없으면 표).
- **Processing Provider**: id `wintersar`, 알고리즘 search/precheck/run/validate. 파라미터 정의는
  선언적 `ALGORITHMS` 스펙에서 생성하고 `build_cli_args()` 로 argv 를 만든다. `FlagNoThreading` 은
  쓰지 않는다(알고리즘은 GUI 를 건드리지 않고 subprocess 만 실행).
- **버전 범위**: `qgisMinimumVersion=3.44`(현 LTR), `qgisMaximumVersion=4.99`(Qt6 기반 4.x, 차기 LTR 4.2).
  Qt 는 `qgis.PyQt` 로만 import 하고 열거형은 완전 한정형(`Qt.DockWidgetArea.RightDockWidgetArea`,
  `QHeaderView.ResizeMode.Stretch`, `Qgis.MessageLevel.Warning`)만 사용한다. `supportsQt6` 키는 폐기됐으므로
  쓰지 않는다. `experimental=True`.
- **i18n**: 플러그인은 `wintersar.i18n` 을 import 할 수 없으므로 자체 `i18n.py` 가 (a) 패키징 시
  `build_zip.py` 가 생성한 `i18n/{ko,en}.json`(`qgis.*`+`common.*` 평탄화) 또는 (b) 체크아웃의
  `src/wintersar/i18n/{lang}.yaml + {lang}/qgis.yaml` 을 읽는다. 키·문구의 단일 원본은 여전히
  `src/wintersar/i18n/{ko,en}/qgis.yaml` 이다(ADR-0072).

## 결과 (Consequences)

- 플러그인은 툴킷 버전과 느슨하게 결합된다: 봉투 4 키와 각 명령의 `data` 스키마가 계약이다.
  `data` 의 세부 키가 바뀌면 표 행 생성 함수(`candidate_rows`, `stage_rows`, `refpoint_rows`,
  `validation_rows`)만 고치면 된다. 이 함수들은 방어적으로 파싱한다.
- 진행률은 "실행 중/완료" 수준이다: `--json` 모드의 `run` 은 마지막에 봉투 하나만 내므로 단계별
  진행률은 stderr 로그로만 보인다. 단계별 진행 이벤트가 필요하면 CLI 에 `--progress` 스트림(JSON lines)
  을 추가하는 것을 후속 작업으로 둔다(pipeline 소유).
- Phase 7 DoD 의 "QGIS LTR 2종에서 동작 확인"(3.44 + 4.2) 은 이 환경에서 수행할 수 없어 수동 검수 항목으로
  남는다(open-questions).
- 되돌리는 조건: 사용자가 QGIS 파이썬에 툴킷을 설치하는 것을 표준 배포로 삼게 되면(예: conda 기반 QGIS
  배포에서 `wintersar` 를 같은 환경에 설치) 파이썬 API 직접 호출 경로를 추가할 수 있다. 그때도 subprocess
  경로는 기본값으로 남긴다.
