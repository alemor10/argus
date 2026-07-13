"""Scout screening rules: rule-by-rule boundaries, None-fails-rule, the
value-trap exclusion, watchlist exclusion, deterministic ranking, top_n cap,
and strict scout.yaml loading."""

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
    "peg",
    "gross_margin",
    "operating_margin",
    "eps_growth",
    "revenue_growth",
    "debt_to_equity",
    "value_trap",
]

# Values that pass every default rule with room to spare.
_GOOD = {
    "ticker": "GOOD",
    "exchange": "NASDAQ",
    "market_cap": 5e10,
    "avg_volume_30d": 5e6,
    "peg_ttm": 1.0,
    "gross_margin_pct": 45.0,
    "operating_margin_pct": 20.0,
    "eps_growth_ttm_pct": 15.0,
    "revenue_growth_ttm_pct": 10.0,
    "debt_to_equity": 0.5,
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
    return screen([row], ScoutCriteria(**criteria_overrides), exclude=frozenset())


# --- rule boundaries (floors and ceilings are inclusive; peg > 0 is strict)


def test_good_row_passes_every_rule():
    (candidate,) = screen_one(make_row())
    assert candidate.rank == 1
    assert list(candidate.reasons) == RULE_ORDER


@pytest.mark.parametrize(
    ("overrides", "passes"),
    [
        # peg: 0 < peg <= max_peg; ceiling inclusive, zero and negative fail
        ({"peg_ttm": 1.5}, True),
        ({"peg_ttm": 1.5000001}, False),
        ({"peg_ttm": 0.01}, True),
        ({"peg_ttm": 0.0}, False),
        ({"peg_ttm": -0.5}, False),  # negative PEG is meaningless, never a bargain
        # margins: floor inclusive
        ({"gross_margin_pct": 30.0}, True),
        ({"gross_margin_pct": 29.999}, False),
        ({"operating_margin_pct": 8.0}, True),
        ({"operating_margin_pct": 7.999}, False),
        # growth: floor inclusive
        ({"eps_growth_ttm_pct": 10.0}, True),
        ({"eps_growth_ttm_pct": 9.999}, False),
        ({"revenue_growth_ttm_pct": 5.0}, True),
        ({"revenue_growth_ttm_pct": 4.999}, False),
        # debt/equity: 0 <= d/e <= ceiling, both ends inclusive
        ({"debt_to_equity": 1.5}, True),
        ({"debt_to_equity": 1.5000001}, False),
        ({"debt_to_equity": 0.0}, True),
        ({"debt_to_equity": -0.01}, False),  # negative equity is not low leverage
    ],
)
def test_rule_boundaries(overrides, passes):
    assert bool(screen_one(make_row(**overrides))) is passes


@pytest.mark.parametrize(
    "metric",
    [
        "peg_ttm",
        "gross_margin_pct",
        "operating_margin_pct",
        "eps_growth_ttm_pct",
        "revenue_growth_ttm_pct",
        "debt_to_equity",
    ],
)
def test_none_metric_fails_its_rule(metric):
    """Thin data is not a pass — a None metric fails, the row is dropped."""
    assert screen_one(make_row(**{metric: None})) == []


def test_one_failing_rule_drops_the_row_entirely():
    rows = [make_row(ticker="FAIL", peg_ttm=2.0), make_row(ticker="PASS")]
    candidates = screen(rows, ScoutCriteria(), exclude=frozenset())
    assert [c.row.ticker for c in candidates] == ["PASS"]


# --- value trap: growth must be strictly positive regardless of the floors


def test_value_trap_catches_cheap_and_shrinking_despite_lenient_floors():
    """A config with negative growth floors lets a shrinking name pass the
    floor rules; the value-trap rule still kills it — cheap + shrinking is
    not cheap."""
    shrinking = make_row(peg_ttm=0.4, eps_growth_ttm_pct=-5.0, revenue_growth_ttm_pct=-2.0)
    assert screen_one(shrinking, min_eps_growth_pct=-50.0, min_revenue_growth_pct=-50.0) == []


@pytest.mark.parametrize("metric", ["eps_growth_ttm_pct", "revenue_growth_ttm_pct"])
def test_value_trap_zero_growth_fails_even_when_floor_is_zero(metric):
    row = make_row(**{metric: 0.0})
    assert screen_one(row, min_eps_growth_pct=0.0, min_revenue_growth_pct=0.0) == []


def test_value_trap_reason_present_on_passers():
    (candidate,) = screen_one(make_row())
    assert candidate.reasons["value_trap"] == "eps_growth 15 > 0 and revenue_growth 10 > 0"


# --- reasons: actual values, fixed order, JSON-serializable


def test_reasons_carry_actual_values():
    (candidate,) = screen_one(make_row(peg_ttm=0.82))
    assert candidate.reasons["peg"] == "peg 0.82 <= 1.5"
    assert candidate.reasons["gross_margin"] == "gross_margin 45 >= 30"
    assert candidate.reasons["debt_to_equity"] == "debt_to_equity 0.5 <= 1.5"


def test_reasons_are_json_serializable_and_ordered():
    (candidate,) = screen_one(make_row())
    round_tripped = json.loads(json.dumps(candidate.reasons))
    assert list(round_tripped) == RULE_ORDER


# --- exclusion: watchlist members dropped before any rule runs


def test_exclusion_is_case_insensitive():
    rows = [make_row(ticker="NVDA"), make_row(ticker="msft"), make_row(ticker="AAPL")]
    candidates = screen(rows, ScoutCriteria(), exclude={"nvda", "MSFT"})
    assert [c.row.ticker for c in candidates] == ["AAPL"]


def test_exclusion_bridges_dotted_and_dashed_class_shares():
    """Review finding: a watched BRK-B (house dash symbology) must exclude
    TradingView's dotted BRK.B row — and the reverse spelling too. Without
    dot/dash canonicalization scout proposed names already held."""
    assert screen([make_row(ticker="BRK.B")], ScoutCriteria(), exclude={"BRK-B"}) == []
    assert screen([make_row(ticker="BRK-B")], ScoutCriteria(), exclude={"BRK.B"}) == []


def test_excluded_rows_do_not_consume_ranks_or_top_n_slots():
    rows = [
        make_row(ticker="HELD", peg_ttm=0.1),  # would rank first if not held
        make_row(ticker="AAA", peg_ttm=0.5),
        make_row(ticker="BBB", peg_ttm=0.9),
    ]
    candidates = screen(rows, ScoutCriteria(top_n=2), exclude={"held"})
    assert [(c.row.ticker, c.rank) for c in candidates] == [("AAA", 1), ("BBB", 2)]


# --- ranking: PEG asc, market cap desc, ticker alpha; deterministic


def test_ranking_peg_ascending_with_tiebreaks():
    rows = [
        make_row(ticker="ZZZ", peg_ttm=0.9, market_cap=1e10),
        make_row(ticker="BIG", peg_ttm=1.2, market_cap=9e10),
        make_row(ticker="SML", peg_ttm=1.2, market_cap=3e9),  # peg tie → cap desc
        make_row(ticker="BBB", peg_ttm=1.2, market_cap=9e10),  # full tie → alpha
        make_row(ticker="AAA", peg_ttm=1.4, market_cap=9e11),
    ]
    candidates = screen(rows, ScoutCriteria(), exclude=frozenset())
    assert [(c.row.ticker, c.rank) for c in candidates] == [
        ("ZZZ", 1),
        ("BBB", 2),
        ("BIG", 3),
        ("SML", 4),
        ("AAA", 5),
    ]


def test_ranking_is_input_order_independent():
    rows = [
        make_row(ticker="ZZZ", peg_ttm=0.9),
        make_row(ticker="BBB", peg_ttm=1.2),
        make_row(ticker="AAA", peg_ttm=1.2),
    ]
    forward = screen(rows, ScoutCriteria(), exclude=frozenset())
    backward = screen(list(reversed(rows)), ScoutCriteria(), exclude=frozenset())
    assert forward == backward
    assert [c.row.ticker for c in forward] == ["ZZZ", "AAA", "BBB"]


def test_top_n_caps_after_ranking():
    rows = [make_row(ticker=f"T{i:02d}", peg_ttm=0.5 + i / 100) for i in range(20)]
    candidates = screen(rows, ScoutCriteria(top_n=3), exclude=frozenset())
    assert [(c.row.ticker, c.rank) for c in candidates] == [
        ("T00", 1),
        ("T01", 2),
        ("T02", 3),
    ]


# --- load_scout_criteria: defaults, strictness


def test_missing_file_yields_defaults(tmp_path):
    criteria = load_scout_criteria(tmp_path / "scout.yaml")
    assert criteria == ScoutCriteria()
    assert criteria.max_peg == 1.5
    assert criteria.top_n == 15


def test_empty_file_yields_defaults(tmp_path):
    path = tmp_path / "scout.yaml"
    path.write_text("", encoding="utf-8")
    assert load_scout_criteria(path) == ScoutCriteria()


def test_present_file_overrides_only_named_keys(tmp_path):
    path = tmp_path / "scout.yaml"
    path.write_text("max_peg: 1.2\ntop_n: 5\n", encoding="utf-8")
    criteria = load_scout_criteria(path)
    assert criteria.max_peg == 1.2
    assert criteria.top_n == 5
    assert criteria.min_gross_margin_pct == 30.0  # untouched default


def test_typoed_yaml_key_fails_loudly(tmp_path):
    path = tmp_path / "scout.yaml"
    path.write_text("max_pge: 1.2\n", encoding="utf-8")  # typo
    with pytest.raises(ValidationError):
        load_scout_criteria(path)


def test_criteria_model_is_frozen():
    criteria = ScoutCriteria()
    with pytest.raises(ValidationError):
        criteria.max_peg = 9.9
