# relative orbit(트랙) — "같은 하강궤도"로는 부족하다 (SEL-01, SEL-02)

Sentinel-1 은 위성 한 대 기준 **12일 주기**로 지구를 돌며 그 한 주기 안에 **175개의 relative orbit**
(트랙, 1~175)을 지납니다. 같은 트랙은 매 주기 거의 같은 지상 궤적을 따라가므로 관측 기하(입사 방향·
입사각)가 같습니다. 간섭(interferometry)은 두 취득의 위상 차이를 보는 것이라 **관측 기하가 같아야**
성립하고, 따라서 **같은 relative orbit 번호** 사이에서만 간섭도를 만들 수 있습니다.

## 흔한 오해

- "둘 다 하강(descending) 궤도니까 정합될 것이다" → 아닙니다. 하강 궤도는 여러 트랙(예: 61, 134, …)이
  있고 서로 다른 트랙은 지상 궤적이 수백 km 떨어져 있습니다. 정합 프로그램이 "기준점을 못 찾음"으로
  중단(abort)되는 가장 흔한 원인이 트랙 불일치입니다.
- "몇 아크(arc)까지 비슷하면 된다"는 휴리스틱 → 필요 없습니다. 트랙 번호와 burst ID 검사로 대체됩니다.

## wintersar 에서

- 검색 결과는 `BurstRecord.relative_orbit`(1~175), `flight_direction`(ASCENDING/DESCENDING) 을 갖습니다.
  스택은 `(relative_orbit, flight_direction, polarization, sub-swath 집합)` 으로 그룹핑되고 스택 ID 는
  `T052D_VV` 처럼 `T<트랙 3자리><A|D>_<편파>` 입니다.
- `SEL-01` 은 스택 안의 트랙이 하나인지, `SEL-02` 는 비행 방향이 하나인지 검사합니다(트랙이 같으면
  방향은 자동으로 같습니다). 위반은 **FAIL** — 간섭이 불가능하므로 경고가 아니라 실패입니다.
- 같은 방향 후보가 여러 트랙으로 나뉘면 INFO 로 알려 주고, `config.yaml` 의 `data.relative_orbit` 에
  트랙 번호를 적으면 검색 단계에서 한 트랙만 남깁니다. `data.orbit_direction: asc|desc|auto` 로 방향을
  고정할 수도 있습니다.
- AOI 가 두 트랙에 걸쳐 있으면 트랙마다 스택을 따로 만듭니다. 상승/하강 두 방향을 모두 쓸 수 있으면
  각각 처리해 LOS 성분을 분해합니다([기하](geometry.md), [기준점](reference-point-deramp.md)).

관련: [burst](burst.md) (트랙 안에서 "같은 지역"을 보장하는 단위), ADR-0014(임계값·심각도), ADR-0016(그룹핑).

## English summary

Sentinel-1 repeats every 12 days per satellite along 175 relative orbits (tracks). Interferometry
needs the same viewing geometry, so only acquisitions from the **same track number** can be
interfered; "both descending" is not enough because descending passes belong to many different
tracks. Coregistration aborts with "no reference points" most often for this reason. `SEL-01`
(one track per stack) and `SEL-02` (one flight direction) are FAIL rules; pin `data.relative_orbit`
in `config.yaml` to keep a single track at search time.
