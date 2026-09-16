# ADR-0019: ISCE2 topsStack shadow/layover 산출물과의 비교 계획

- 상태(Status): 제안 (ISCE2 설치 후 실행)
- 날짜(Date): 2026-09-16
- 관련 ID: R-04 / SEL-12 / 플랜 §5.1.5 ("topsStack의 shadow/layover 산출물이 있으면 그것을 우선 사용하고 자체 계산과 비교 테스트")
- 검증 출처(Sources):
  - ISCE2 topsStack `topo.py`: https://raw.githubusercontent.com/isce-framework/isce2/main/contrib/stack/topsStack/topo.py
    — 출력 디렉터리 `geom_reference/IW{n}/`, burst별 파일 `lat_%02d.rdr, lon_%02d.rdr, hgt_%02d.rdr, los_%02d.rdr,
    shadowMask_%02d.rdr (topo.maskFilename), incLocal_%02d.rdr (topo.incFilename)`
  - ISCE2 `topozero.f90`: https://raw.githubusercontent.com/isce-framework/isce2/main/components/zerodop/topozero/src/topozero.f90
    — 마스크 값: 레인지 라인을 따라 고도각(elevang)이 단조 증가하지 않는 픽셀 `mask = 1`(레이오버),
    cross-track 정렬 좌표에서 시선각이 단조성을 잃는 픽셀 `omask + 2`(셰도우), 합산으로 `3`(둘 다);
    `incLocal` = `acos(costheta)`(법선·시선 내적)
  - HyP3 RTC 제품 가이드: https://hyp3-docs.asf.alaska.edu/guides/rtc_product_guide/
    — `_ls_map.tif`(단일 밴드 uint8) 존재는 확인, 비트 값 정의는 페이지에서 확인 못함 → open-questions #12
  - 본 모듈 `wintersar/select/geometry_masks.py` (ADR-0017 규약)

## 맥락

자체 마스크는 DEM 픽셀별 **능동(active)** 판정(레인지 방향 경사 vs 입사각)이다. ISCE2 `topozero`는 레이더 좌표계의
각 레인지 라인을 따라 고도각의 단조성을 검사하므로 **수동(passive)** 레이오버·셰도우(앞 사면에 가려지는 뒤쪽 평지)
까지 포함한다. 두 결과는 정의가 달라 완전히 같을 수 없고, 어느 정도 차이가 "정상"인지 수치로 알아야
SEL-12 임계값과 마스크 사용 정책을 정할 수 있다. 현재 ISCE2는 설치되어 있지 않다(ADR-0001).

## 선택지

1. 자체 마스크만 사용하고 ISCE2 산출물은 무시.
2. ISCE2 경로에서는 `shadowMask`를 우선 사용하고, 자체 마스크는 사전검증(SEL-12) 용도로만 사용 + 정량 비교 테스트.
3. 자체 계산에 레인지 방향 ray-cast(수동 레이오버·셰도우)를 추가해 ISCE2와 정의를 맞춤.

## 결정

선택지 2를 채택하고, 비교 결과에 따라 3을 Phase 5에서 재검토한다.

비교 프로토콜(ISCE2 설치 후 `tests/integration/test_geometry_vs_isce2.py`, `@pytest.mark.engine`):

1. **입력 통일**: 같은 DEM(ADR-0018 캐시 파일)과 같은 참조 씬. ISCE2 `geom_reference/IW*/shadowMask_NN.rdr`,
   `incLocal_NN.rdr`, `lat/lon_NN.rdr`을 읽어(ISCE 플랫 바이너리 + XML; `rasterio`의 ENVI/ISCE 드라이버 또는
   numpy memmap, MintPy import 금지) burst별로 lat/lon을 이용해 DEM 격자로 최근접 재배열한다.
2. **인코딩 정합**: ISCE2 값 1 = 레이오버, 2 = 셰도우, 3 = 둘 다. 자체 `GeometryMaskResult.ls_map()`이 같은 인코딩을 쓴다.
   HyP3 `_ls_map.tif`는 비트 정의 확인 후(open-questions #12) 같은 표로 변환한다.
3. **지표**(AOI 내, DEM 유효 픽셀):
   - 픽셀 일치율(Jaccard) — 레이오버, 셰도우 각각.
   - 비율 차이 `|layover_fraction_ours − layover_fraction_isce|` (SEL-12는 비율만 쓰므로 가장 중요).
   - 자체 마스크가 놓친 픽셀(ISCE2만 1)의 경사 분포: 대부분이 평지·완경사(수동 레이오버)이면 정의 차이로 설명됨.
   - 자체 마스크만 1인 픽셀의 분포: DEM 해상도·경사 계산 차이(중앙차분 vs ISCE 내부) 또는 heading 오차 신호.
   - `incLocal` vs `local_incidence_deg`: 평균·RMS 차이(도). 0.5° 이상이면 heading/입사각 입력 오류 의심.
4. **합격 기준(초안, 연구자 확인 후 확정 — 규칙 11.10)**: 레이오버 비율 차이 ≤ AOI의 2 %p, Jaccard(레이오버) ≥ 0.6,
   `incLocal` RMS 차이 ≤ 1°. 미달 시 heading을 POEORB에서 계산한 값으로 바꿔 재비교(open-questions #13).
5. **우선순위 규칙**: ISCE2 파이프라인 실행 시 `unwrap` 마스크 입력은 ISCE2 `shadowMask`를 사용하고, 자체 마스크는
   사전검증 리포트와 HyP3 경로(ISCE2 산출물 없음)에서 사용한다.

## 결과

- 비교 스크립트가 없는 동안 SEL-12 임계값(기본 10 %)은 잠정값이다.
- 비교 결과(사이트 3곳, Phase 1 DoD)는 `docs/research/geometry_vs_isce2.md`에 표로 남기고 이 ADR을 "채택"으로 갱신한다.
- 수동 레이오버가 비율에 크게 기여하면 선택지 3(레인지 방향 ray-cast)을 Phase 5 성능 작업(PERF-10 GPU 후처리)과 함께 구현한다.
