"""SEL/ADR-0016 seam: the SBAS perpendicular threshold may split the network.

``with_baselines`` re-applies ``selection.max_perp_baseline_m`` (the prescribed SBAS
remedy, ADR-0016), which can leave the interferogram network in several connected
components. That is not a rule verdict (changing the plan §11 rule table needs a domain
review, rule 11.10) but it must be visible: the network summary records it, the candidate
table shows it and ``recommend_stack`` prefers a connected stack on a tie.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.unit.select.conftest import AOI_WKT, full_stack, make_candidate
from wintersar.io.schemas import Pair, StackCandidate
from wintersar.pipeline.config import Config
from wintersar.select.network import (
    connected_components,
    group_candidates,
    network_components,
    with_baselines,
)
from wintersar.select.report import (
    precheck_payload,
    recommend_stack,
    render_html,
    render_markdown,
    stack_summary,
)

DATES = ["2024-01-01", "2024-01-13", "2024-01-25", "2024-02-06", "2024-02-18", "2024-03-01"]
OUTLIER = date(2024, 1, 25)


def _split_candidate(cfg: Config) -> tuple[StackCandidate, StackCandidate]:
    """(candidate before baselines, candidate after) with one date at B_perp = +200 m."""
    records = full_stack(DATES)
    c = group_candidates(records, AOI_WKT, cfg.selection, cfg.data)[0]
    perp = {date.fromisoformat(d): 0.0 for d in DATES}
    perp[OUTLIER] = 200.0  # > max_perp_baseline_m (150) against every other date
    pairs = [
        p.model_copy(update={"perp_baseline_m": perp[p.secondary] - perp[p.reference]})
        for p in c.pairs
    ]
    return c, with_baselines(c, pairs, cfg.selection)


def test_connected_components_groups_dates() -> None:
    d = [date(2024, 1, 1), date(2024, 1, 13), date(2024, 2, 6), date(2024, 2, 18)]
    pairs = [
        Pair(reference=d[0], secondary=d[1], temporal_baseline_days=12),
        Pair(reference=d[2], secondary=d[3], temporal_baseline_days=12),
    ]
    assert connected_components(d, pairs) == [[d[0], d[1]], [d[2], d[3]]]
    assert network_components(d, pairs) == 2
    assert connected_components([], []) == []


def test_with_baselines_records_pairs_dropped_by_the_perp_threshold(cfg: Config) -> None:
    before, after = _split_candidate(cfg)
    assert before.notes["network"]["n_components"] == 1
    net = after.notes["network"]
    # The five pairs of the outlier date are removed, so the network falls apart.
    assert net["n_pairs_dropped_perp"] == 5
    assert all(OUTLIER.strftime("%Y%m%d") in k for k in net["pairs_dropped_perp"])
    assert net["n_components"] == 2
    assert net["dates_disconnected"] == [OUTLIER.isoformat()]
    assert len(after.dates) == len(before.dates)  # dates are never dropped silently


def test_candidate_table_and_report_show_the_split_network(cfg: Config) -> None:
    _, after = _split_candidate(cfg)
    row = stack_summary(after)
    assert row["n_components"] == 2
    assert row["n_pairs_dropped_perp"] == 5
    assert row["dates_disconnected"] == [OUTLIER.isoformat()]

    payload = precheck_payload([after], [], "en")
    md = render_markdown(payload, [], "en")
    assert "Network components" in md and "Pairs dropped (B-perp)" in md
    assert "not connected" in md and OUTLIER.isoformat() in md
    html = render_html(payload, "ko")
    assert "네트워크 연결성분" in html and "네트워크 분리됨" in html


def test_stack_summary_reports_one_component_for_a_plain_candidate() -> None:
    row = stack_summary(make_candidate())  # no notes["network"] -> recomputed
    assert row["n_components"] == 1
    assert row["n_pairs_dropped_perp"] == 0 and row["dates_disconnected"] == []


def test_recommend_stack_prefers_a_connected_network_on_a_tie(cfg: Config) -> None:
    _, split = _split_candidate(cfg)
    connected = split.model_copy(
        update={
            "relative_orbit": 61,
            "pairs": list(_split_candidate(cfg)[0].pairs),
            "notes": {**split.notes, "network": {"n_components": 1}},
        }
    )
    assert split.coverage_of_aoi == connected.coverage_of_aoi
    assert len(split.dates) == len(connected.dates)
    best = recommend_stack([split, connected], [])
    assert best is not None and best.stack_id == connected.stack_id
    # ... but a higher coverage still wins over connectivity.
    better = split.model_copy(update={"relative_orbit": 134, "coverage_of_aoi": 1.0})
    worse = connected.model_copy(update={"coverage_of_aoi": 0.9})
    chosen = recommend_stack([better, worse], [])
    assert chosen is not None and chosen.stack_id == better.stack_id


@pytest.mark.parametrize("method", ["sequential", "single_reference"])
def test_non_sbas_networks_keep_every_pair(cfg: Config, method: str) -> None:
    """Only sbas re-applies the threshold (ADR-0016); the others report via SEL-06 WARN."""
    selection = cfg.selection.model_copy(update={"network": method})
    c = make_candidate(pairs=[])
    pairs = [
        Pair(
            reference=date(2024, 1, 1),
            secondary=date(2024, 1, 13),
            temporal_baseline_days=12,
            perp_baseline_m=500.0,
        )
    ]
    out = with_baselines(c, pairs, selection)
    assert len(out.pairs) == 1
    assert out.notes["network"]["n_pairs_dropped_perp"] == 0
