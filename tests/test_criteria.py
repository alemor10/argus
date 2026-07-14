"""Scout screening rules (Quality-GARP forward): rule-by-rule boundaries,
None-fails-rule, the value-trap guard at exactly -30, humanized reason
strings pinned verbatim, watchlist exclusion (incl. the dotted/dashed
class-share regression), deterministic forward-PEG ranking, top_n cap, and
strict scout.yaml loading."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from argus.scout.criteria import (
    ScoutCriteria,
    ScreenedCandidate,
    load_scout_criteria,
    screen,
)
from argus.scout.screener import ScreenerRow

RULE_ORDER = [
    "forward_pe",
    "revenue_growth",
    "gross_margin",
    "operating_margin",
    "roe",
    "debt_to_equity",
    "value_trap",
]

# Values that pass every default rule with room to spare.
_GOOD = {
    "ticker": "GOOD",
    "exchange": "NASDAQ",
    "market_cap": 5e10,
    "avg_volume_30d": 5e6,
    "fwd_pe": 18.0,
    "revenue_growth_ttm_pct": 15.0,
    "gross_margin_pct": 55.0,
    "operating_margin_pct": 25.0,
    "roe_pct": 30.0,
    "debt_to_equity": 0.5,
    "eps_growth_ttm_pct": 12.0,
}


def make_row(**overrides) -> ScreenerRow:
    """Build a ScreenerRow, tolerating extra optional fields on the real
    model (only keys ScreenerRow declares are passed through)."""
    values = {**_GOOD, **overrides}
    declared = set(ScreenerRow.model_fields)
    unknown = set(values) - declared
    assert not unknown, f"test assumes ScreenerRow fields that do not exist: {sorted(unknown)}"
    return ScreenerRow(**values)


def screen_one(row: ScreenerRow, **criteria_overrides) -> list[ScreenedCandidate]:
    return list(screen([row], ScoutCriteria(**criteria_overrides), exclude=frozenset()).shortlist)


def shortlist(rows, criteria, exclude):
    return list(screen(rows, criteria, exclude).shortlist)


# --- rule boundaries (floors and ceilings inclusive; fwd_pe > 0 strict;
#     value trap STRICTLY above max_eps_decline_pct)


def test_good_row_passes_every_rule():
    (candidate,) = screen_one(make_row())
    assert candidate.rank == 1
    assert list(candidate.reasons) == RULE_ORDER


@pytest.mark.parametrize(
    ("overrides", "passes"),
    [
        # forward P/E: 0 < fwd_pe <= max_forward_pe; ceiling inclusive
        ({"fwd_pe": 25.0}, True),
        ({"fwd_pe": 25.0000001}, False),
        ({"fwd_pe": 0.1}, True),
        ({"fwd_pe": 0.0}, False),
        ({"fwd_pe": -8.0}, False),  # negative fwd P/E = expected losses, never cheap
        # revenue growth: floor inclusive
        ({"revenue_growth_ttm_pct": 10.0}, True),
        ({"revenue_growth_ttm_pct": 9.999}, False),
        # margins: floor inclusive
        ({"gross_margin_pct": 40.0}, True),
        ({"gross_margin_pct": 39.999}, False),
        ({"operating_margin_pct": 12.0}, True),
        ({"operating_margin_pct": 11.999}, False),
        # ROE: floor inclusive
        ({"roe_pct": 15.0}, True),
        ({"roe_pct": 14.999}, False),
        # debt/equity: 0 <= d/e <= ceiling, both ends inclusive
        ({"debt_to_equity": 1.0}, True),
        ({"debt_to_equity": 1.0000001}, False),
        ({"debt_to_equity": 0.0}, True),
        ({"debt_to_equity": -0.01}, False),  # negative equity is not low leverage
        # value trap: strictly above -30; the boundary itself fails
        ({"eps_growth_ttm_pct": -29.999}, True),
        ({"eps_growth_ttm_pct": -30.0}, False),
        ({"eps_growth_ttm_pct": -30.001}, False),
    ],
)
def test_rule_boundaries(overrides, passes):
    assert bool(screen_one(make_row(**overrides))) is passes


@pytest.mark.parametrize(
    "metric",
    [
        "fwd_pe",
        "revenue_growth_ttm_pct",
        "gross_margin_pct",
        "operating_margin_pct",
        "roe_pct",
        "debt_to_equity",
        "eps_growth_ttm_pct",  # value-trap input: thin data is not a pass
    ],
)
def test_none_metric_fails_its_rule(metric):
    """Thin data is not a pass — a None metric fails, the row is dropped."""
    assert screen_one(make_row(**{metric: None})) == []


def test_one_failing_rule_drops_the_row_entirely():
    rows = [make_row(ticker="FAIL", fwd_pe=30.0), make_row(ticker="PASS")]
    candidates = shortlist(rows, ScoutCriteria(), exclude=frozenset())
    assert [c.row.ticker for c in candidates] == ["PASS"]


# --- value trap: growing revenue with collapsing earnings is a
#     margin-compression trap, not a bargain


def test_value_trap_kills_growing_revenue_with_collapsing_earnings():
    trap = make_row(revenue_growth_ttm_pct=25.0, eps_growth_ttm_pct=-45.0)
    assert screen_one(trap) == []


def test_mild_eps_decline_is_not_a_trap():
    """-30% guards against collapse; it is NOT a growth floor. A name with a
    modestly negative TTM EPS trend but strong forward economics stays in —
    TTM-EPS-growth requirements were the OLD strategy."""
    (candidate,) = screen_one(make_row(eps_growth_ttm_pct=-10.0))
    assert candidate.reasons["value_trap"] == "EPS trend -10.0% > -30%"


# --- reasons: humanized, one decimal for values, %g for thresholds,
#     fixed order, JSON-serializable


def test_reason_strings_are_humanized_verbatim():
    row = make_row(
        fwd_pe=20.4,
        revenue_growth_ttm_pct=70.7,
        gross_margin_pct=74.1,
        operating_margin_pct=65.7,
        roe_pct=114.3,
        debt_to_equity=0.06,
        eps_growth_ttm_pct=697.3,
    )
    (candidate,) = screen_one(row)
    assert candidate.reasons == {
        "forward_pe": "fwd P/E 20.4 ≤ 25",
        "revenue_growth": "rev growth +70.7% ≥ 10%",
        "gross_margin": "gross margin 74.1% ≥ 40%",
        "operating_margin": "op margin 65.7% ≥ 12%",
        "roe": "ROE 114.3% ≥ 15%",
        "debt_to_equity": "D/E 0.06 ≤ 1",
        "value_trap": "EPS trend +697.3% > -30%",
    }


def test_values_render_one_decimal_and_thresholds_compact():
    """No 0.0632668 noise: values get exactly one decimal (D/E two — it
    lives in the 0.0x range), thresholds render compactly via %g."""
    (candidate,) = screen_one(
        make_row(fwd_pe=20.0, revenue_growth_ttm_pct=12.345678), max_forward_pe=22.5
    )
    assert candidate.reasons["forward_pe"] == "fwd P/E 20.0 ≤ 22.5"
    assert candidate.reasons["revenue_growth"] == "rev growth +12.3% ≥ 10%"
    assert candidate.reasons["debt_to_equity"] == "D/E 0.50 ≤ 1"


def test_reasons_are_json_serializable_and_ordered():
    (candidate,) = screen_one(make_row())
    round_tripped = json.loads(json.dumps(candidate.reasons))
    assert list(round_tripped) == RULE_ORDER


# --- exclusion: watchlist members dropped before any rule runs


def test_exclusion_is_case_insensitive():
    rows = [make_row(ticker="NVDA"), make_row(ticker="msft"), make_row(ticker="AAPL")]
    candidates = shortlist(rows, ScoutCriteria(), exclude={"nvda", "MSFT"})
    assert [c.row.ticker for c in candidates] == ["AAPL"]


def test_exclusion_bridges_dotted_and_dashed_class_shares():
    """Review finding: a watched BRK-B (house dash symbology) must exclude
    TradingView's dotted BRK.B row — and the reverse spelling too. Without
    dot/dash canonicalization scout proposed names already held."""
    assert shortlist([make_row(ticker="BRK.B")], ScoutCriteria(), exclude={"BRK-B"}) == []
    assert shortlist([make_row(ticker="BRK-B")], ScoutCriteria(), exclude={"BRK.B"}) == []


def test_excluded_rows_do_not_consume_ranks_or_top_n_slots():
    rows = [
        make_row(ticker="HELD", fwd_pe=5.0),  # would rank first if not held
        make_row(ticker="AAA", fwd_pe=12.0),
        make_row(ticker="BBB", fwd_pe=18.0),
    ]
    candidates = shortlist(rows, ScoutCriteria(top_n=2), exclude={"held"})
    assert [(c.row.ticker, c.rank) for c in candidates] == [("AAA", 1), ("BBB", 2)]


# --- ranking: forward-PEG (fwd_pe / revenue growth) asc, market cap desc,
#     ticker alpha; deterministic


def test_ranking_forward_peg_ascending_with_tiebreaks():
    rows = [
        make_row(ticker="ZZZ", fwd_pe=10.0, revenue_growth_ttm_pct=20.0),  # 0.5
        make_row(ticker="BIG", fwd_pe=24.0, revenue_growth_ttm_pct=20.0, market_cap=9e10),  # 1.2
        make_row(ticker="SML", fwd_pe=24.0, revenue_growth_ttm_pct=20.0, market_cap=3e9),  # tie → cap desc
        make_row(ticker="BBB", fwd_pe=24.0, revenue_growth_ttm_pct=20.0, market_cap=9e10),  # full tie → alpha
        make_row(ticker="AAA", fwd_pe=21.0, revenue_growth_ttm_pct=15.0, market_cap=9e11),  # 1.4
    ]
    candidates = shortlist(rows, ScoutCriteria(max_per_sector=0), exclude=frozenset())
    assert [(c.row.ticker, c.rank) for c in candidates] == [
        ("ZZZ", 1),
        ("BBB", 2),
        ("BIG", 3),
        ("SML", 4),
        ("AAA", 5),
    ]


def test_ranking_is_growth_adjusted_not_naive_low_pe():
    """A higher multiple with much faster growth outranks a nominally
    cheaper slow grower — forward-PEG, the point of GARP."""
    rows = [
        make_row(ticker="SLOW", fwd_pe=12.0, revenue_growth_ttm_pct=10.0),  # 1.2
        make_row(ticker="FAST", fwd_pe=24.0, revenue_growth_ttm_pct=60.0),  # 0.4
    ]
    candidates = shortlist(rows, ScoutCriteria(), exclude=frozenset())
    assert [c.row.ticker for c in candidates] == ["FAST", "SLOW"]


def test_ranking_is_input_order_independent():
    rows = [
        make_row(ticker="ZZZ", fwd_pe=10.0),
        make_row(ticker="BBB", fwd_pe=18.0),
        make_row(ticker="AAA", fwd_pe=18.0),
    ]
    forward = shortlist(rows, ScoutCriteria(max_per_sector=0), exclude=frozenset())
    backward = shortlist(list(reversed(rows)), ScoutCriteria(max_per_sector=0), exclude=frozenset())
    assert forward == backward
    assert [c.row.ticker for c in forward] == ["ZZZ", "AAA", "BBB"]


def test_shrinking_passer_under_permissive_floor_ranks_last_not_first():
    """Defensive: every passer has positive revenue growth under the default
    floors, but a permissive config (negative floor) can admit a shrinker —
    naive division would hand it a NEGATIVE forward-PEG and first place. It
    pins to the bottom instead."""
    rows = [
        make_row(ticker="SHRK", fwd_pe=5.0, revenue_growth_ttm_pct=-5.0),
        make_row(ticker="ZERO", fwd_pe=1.0, revenue_growth_ttm_pct=0.0),
        make_row(ticker="GROW", fwd_pe=20.0, revenue_growth_ttm_pct=20.0),
    ]
    candidates = shortlist(rows, ScoutCriteria(min_revenue_growth_pct=-50.0, max_per_sector=0), exclude=frozenset())
    assert [c.row.ticker for c in candidates] == ["GROW", "SHRK", "ZERO"]


def test_top_n_caps_after_ranking():
    rows = [
        make_row(ticker=f"T{i:02d}", fwd_pe=10.0 + i / 2, revenue_growth_ttm_pct=20.0)
        for i in range(20)
    ]
    candidates = shortlist(rows, ScoutCriteria(top_n=3, max_per_sector=0), exclude=frozenset())
    assert [(c.row.ticker, c.rank) for c in candidates] == [
        ("T00", 1),
        ("T01", 2),
        ("T02", 3),
    ]


# --- load_scout_criteria: defaults, strictness


def test_missing_file_yields_defaults(tmp_path):
    criteria = load_scout_criteria(tmp_path / "scout.yaml")
    assert criteria == ScoutCriteria()
    assert criteria.max_forward_pe == 25.0
    assert criteria.min_revenue_growth_pct == 10.0
    assert criteria.min_roe_pct == 15.0
    assert criteria.max_eps_decline_pct == -30.0
    assert criteria.top_n == 15


def test_empty_file_yields_defaults(tmp_path):
    path = tmp_path / "scout.yaml"
    path.write_text("", encoding="utf-8")
    assert load_scout_criteria(path) == ScoutCriteria()


def test_present_file_overrides_only_named_keys(tmp_path):
    path = tmp_path / "scout.yaml"
    path.write_text("max_forward_pe: 22.0\ntop_n: 5\n", encoding="utf-8")
    criteria = load_scout_criteria(path)
    assert criteria.max_forward_pe == 22.0
    assert criteria.top_n == 5
    assert criteria.min_gross_margin_pct == 40.0  # untouched default


def test_typoed_yaml_key_fails_loudly(tmp_path):
    path = tmp_path / "scout.yaml"
    path.write_text("max_forward_pge: 22.0\n", encoding="utf-8")  # typo
    with pytest.raises(ValidationError):
        load_scout_criteria(path)


@pytest.mark.parametrize("old_key", ["max_peg", "min_eps_growth_pct"])
def test_old_ttm_garp_strategy_keys_fail_loudly(tmp_path, old_key):
    """A scout.yaml left over from the TTM-GARP strategy must error, not
    silently screen with new-strategy defaults in its place."""
    path = tmp_path / "scout.yaml"
    path.write_text(f"{old_key}: 1.5\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_scout_criteria(path)


def test_criteria_model_is_frozen():
    criteria = ScoutCriteria()
    with pytest.raises(ValidationError):
        criteria.max_forward_pe = 9.9


# --- sector cap + leaders (v1.3): concentration control and category coverage


def test_sector_cap_limits_concentration_and_fills_from_other_sectors():
    """4 cheap Technology names + 1 pricier Finance name, cap 3: the fourth
    tech name yields its slot to the finance one."""
    rows = [
        make_row(ticker="T1", fwd_pe=10.0, sector="Technology Services"),
        make_row(ticker="T2", fwd_pe=11.0, sector="Technology Services"),
        make_row(ticker="T3", fwd_pe=12.0, sector="Electronic Technology"),  # same bucket
        make_row(ticker="T4", fwd_pe=13.0, sector="Technology Services"),
        make_row(ticker="F1", fwd_pe=20.0, sector="Finance"),
    ]
    result = screen(rows, ScoutCriteria(top_n=4, max_per_sector=3), exclude=frozenset())
    assert [c.row.ticker for c in result.shortlist] == ["T1", "T2", "T3", "F1"]
    assert [c.sector for c in result.shortlist] == [
        "Technology", "Technology", "Technology", "Financial Services",
    ]
    assert result.shortlist[3].rank == 5  # global rank survives the cap


def test_sector_cap_zero_disables():
    rows = [make_row(ticker=f"T{i}", fwd_pe=10.0 + i, sector="Finance") for i in range(5)]
    result = screen(rows, ScoutCriteria(top_n=5, max_per_sector=0), exclude=frozenset())
    assert len(result.shortlist) == 5


def test_sector_leaders_cover_unrepresented_sectors_only():
    rows = [
        make_row(ticker="T1", fwd_pe=10.0, sector="Technology Services"),
        make_row(ticker="T2", fwd_pe=11.0, sector="Technology Services"),
        make_row(ticker="H1", fwd_pe=18.0, sector="Health Technology"),
        make_row(ticker="H2", fwd_pe=19.0, sector="Health Services"),  # same bucket as H1
        make_row(ticker="E1", fwd_pe=22.0, sector="Energy Minerals"),
    ]
    result = screen(rows, ScoutCriteria(top_n=2, max_per_sector=3), exclude=frozenset())
    assert [c.row.ticker for c in result.shortlist] == ["T1", "T2"]
    # Healthcare and Energy passed but missed the shortlist: one leader each,
    # the best of the bucket; Technology is represented, so no tech leader.
    assert [(c.sector, c.row.ticker) for c in result.sector_leaders] == [
        ("Healthcare", "H1"), ("Energy", "E1"),
    ]


def test_empty_sector_is_information_not_padding():
    """No quota filling: a sector with zero passers appears nowhere."""
    rows = [make_row(ticker="T1", fwd_pe=10.0, sector="Technology Services")]
    result = screen(rows, ScoutCriteria(), exclude=frozenset())
    sectors = {c.sector for c in result.shortlist} | {c.sector for c in result.sector_leaders}
    assert sectors == {"Technology"}
