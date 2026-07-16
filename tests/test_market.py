"""Market-wire contract tests: the scan parse over the recorded fixture and
the pure, mechanical curation rules (cap floors, top-N, tolerance) — Argus
never decides importance by judgment, only by rule."""

import json
from datetime import date
from pathlib import Path

import pytest

from argus.market import (
    EXTREME_TOLERANCE,
    MOVER_CAP_FLOOR,
    MarketRow,
    MarketScanner,
    MarketWireError,
    build_wire,
)
from argus.models import BellwetherEarning, MarketWire

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 7, 16)


def _row(symbol, *, change=0.0, cap=2e10, close=100.0, high=None, low=None, sector="Technology"):
    return MarketRow(
        symbol=symbol, sector=sector, close=close, change_pct=change,
        market_cap=cap, high_52w=high, low_52w=low,
    )


class TestScanParse:
    def test_recorded_fixture_parses_with_house_symbology(self):
        payload = json.loads((FIXTURES / "tradingview/market-scan-2026-07-16.json").read_text())
        rows = MarketScanner().parse(payload)
        assert len(rows) == 80
        nvda = rows[0]
        assert nvda.symbol == "NVDA"
        assert nvda.sector == "Technology"  # canonical bucket, not TV vocabulary
        assert nvda.change_pct == pytest.approx(-2.62, abs=0.01)
        assert nvda.high_52w is not None and nvda.low_52w is not None

    def test_unexpected_body_shape_is_loud(self):
        with pytest.raises(MarketWireError, match="body shape"):
            MarketScanner().parse({"rows": []})

    def test_malformed_rows_are_skipped(self):
        payload = {"data": [{"d": ["OK", None, "Finance", 10.0, 1.0, 2e10, 12.0, 8.0]},
                            {"d": ["short row"]}, "not a dict", {"d": None}]}
        [row] = MarketScanner().parse(payload)
        assert row.symbol == "OK"


class TestMovers:
    def test_top_n_each_way_above_the_cap_floor(self):
        rows = [
            _row("UPBIG", change=6.0),
            _row("UPSMALL", change=9.0, cap=5e9),  # below the floor: never a mover
            _row("DOWNBIG", change=-7.0),
            _row("FLAT", change=0.0),
            *[_row(f"U{i}", change=1.0 + i / 10) for i in range(6)],
            *[_row(f"D{i}", change=-1.0 - i / 10) for i in range(6)],
        ]
        wire = build_wire(rows, (), today=TODAY)
        assert wire.gainers[0].symbol == "UPBIG"
        assert wire.losers[0].symbol == "DOWNBIG"
        assert len(wire.gainers) == 5 and len(wire.losers) == 5
        assert all(m.change_pct > 0 for m in wire.gainers)
        assert all(m.change_pct < 0 for m in wire.losers)
        assert "UPSMALL" not in {m.symbol for m in wire.gainers}

    def test_flat_days_produce_short_or_empty_lists_never_padding(self):
        wire = build_wire([_row("A", change=0.0), _row("B", change=0.0)], (), today=TODAY)
        assert wire.gainers == () and wire.losers == ()


class TestSectorPulse:
    def test_median_change_per_canonical_sector_over_the_whole_universe(self):
        rows = [
            _row("T1", change=1.0), _row("T2", change=3.0),
            _row("E1", change=-2.0, sector="Energy", cap=5e9),  # small caps still count here
        ]
        wire = build_wire(rows, (), today=TODAY)
        assert [(p.sector, p.median_change_pct, p.n) for p in wire.sectors] == [
            ("Technology", 2.0, 2),
            ("Energy", -2.0, 1),
        ]


class TestExtremes:
    def test_at_the_mark_within_tolerance_large_caps_only(self):
        rows = [
            _row("ATHIGH", close=99.6, high=100.0, low=50.0),  # within 0.5%
            _row("NEARMISS", close=99.0, high=100.0, low=50.0),  # 1% off: not at the mark
            _row("ATLOW", close=50.2, high=100.0, low=50.0),
            _row("SMALLHIGH", close=100.0, high=100.0, low=50.0, cap=5e9),
        ]
        wire = build_wire(rows, (), today=TODAY)
        assert [e.symbol for e in wire.highs] == ["ATHIGH"]
        assert [e.symbol for e in wire.lows] == ["ATLOW"]
        assert 0 < EXTREME_TOLERANCE < 0.01  # the disclosed rule

    def test_a_high_is_not_also_a_low(self):
        [row] = [_row("BOTH", close=100.0, high=100.0, low=99.8)]
        wire = build_wire([row], (), today=TODAY)
        assert [e.symbol for e in wire.highs] == ["BOTH"]
        assert wire.lows == ()


class TestEarningsWire:
    def _entry(self, symbol, *, actual=None, estimate=1.0, day=TODAY):
        return BellwetherEarning(
            symbol=symbol, report_date=day, eps_actual=actual, eps_estimate=estimate
        )

    def test_cap_floor_with_pins_always_qualifying(self):
        rows = [_row("BIGCO"), _row("TINY", cap=3e9)]
        calendar = [
            self._entry("BIGCO", actual=1.2),
            self._entry("TINY", actual=2.0),  # below floor, unpinned → out
            self._entry("PINNED", actual=0.9),  # not even in the scan → pinned in
        ]
        wire = build_wire(rows, calendar, pins=frozenset({"PINNED"}), today=TODAY)
        assert {e.symbol for e in wire.earnings_reported} == {"BIGCO", "PINNED"}

    def test_reported_ranked_by_surprise_upcoming_by_cap(self):
        rows = [_row("A", cap=5e10), _row("B", cap=9e10), _row("C", cap=2e10)]
        calendar = [
            self._entry("A", actual=1.5, estimate=1.0),  # +50%
            self._entry("B", actual=1.05, estimate=1.0),  # +5%
            self._entry("C", day=TODAY),  # upcoming
            self._entry("A", day=TODAY.replace(day=20)),  # upcoming, later
        ]
        wire = build_wire(rows, calendar, today=TODAY)
        assert [e.symbol for e in wire.earnings_reported] == ["A", "B"]
        assert [e.symbol for e in wire.earnings_upcoming] == ["A", "C"]  # cap-ranked

    def test_past_dated_rows_without_actuals_are_not_upcoming(self):
        rows = [_row("STALE")]
        calendar = [self._entry("STALE", day=date(2026, 7, 10))]  # past, never reported
        wire = build_wire(rows, calendar, today=TODAY)
        assert wire.earnings_reported == () and wire.earnings_upcoming == ()

    def test_overflow_count_is_disclosed(self):
        rows = [_row(f"S{i}", cap=2e10 + i) for i in range(14)]
        calendar = [self._entry(f"S{i}") for i in range(14)]
        wire = build_wire(rows, calendar, today=TODAY)
        assert len(wire.earnings_upcoming) == 10
        assert wire.earnings_more_upcoming == 4


def test_wire_round_trips_through_json():
    """The store persists the wire as one JSON blob — it must round-trip."""
    wire = build_wire(
        [_row("NVDA", change=4.2, high=100.0, close=99.9)],
        [BellwetherEarning(symbol="NVDA", report_date=TODAY, eps_estimate=1.05)],
        pins=frozenset({"NVDA"}),
        today=TODAY,
    )
    restored = MarketWire.model_validate_json(wire.model_dump_json())
    assert restored == wire
