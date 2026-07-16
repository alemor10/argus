"""ETF holdings adapter + membership-diff tests: the SSGA xlsx parse over a
recorded fixture (never the network) and the pure set-diff that turns two
snapshots into a rebalance."""

from pathlib import Path

import pytest

from argus.etf import (
    HoldingsError,
    SsgaHoldingsSource,
    VanguardHoldingsSource,
    holdings_source_for,
    membership_diff,
)
from argus.models import EtfHolding

FIXTURES = Path(__file__).parent / "fixtures"
SPY = (FIXTURES / "ssga" / "SPY-holdings-2026-07-16.xlsx").read_bytes()


class TestParse:
    def test_recorded_spy_parses_to_constituents(self):
        holdings = SsgaHoldingsSource().parse(SPY, "SPY")
        assert len(holdings) == 504  # S&P 500 + a few share classes
        top = holdings[0]
        assert top.ticker == "NVDA"
        assert top.weight == pytest.approx(7.9, abs=0.1)
        assert top.name == "NVIDIA CORP"

    def test_share_classes_use_house_symbology(self):
        holdings = SsgaHoldingsSource().parse(SPY, "SPY")
        assert "BRK-B" in {h.ticker for h in holdings}  # SSGA's BRK.B → BRK-B

    def test_weights_are_a_full_book(self):
        holdings = SsgaHoldingsSource().parse(SPY, "SPY")
        assert sum(h.weight for h in holdings) == pytest.approx(100.0, abs=0.5)

    def test_html_shell_is_loud_not_empty(self):
        with pytest.raises(HoldingsError, match="unreadable|xlsx"):
            SsgaHoldingsSource().parse(b"<!DOCTYPE html><html>consent wall</html>", "SPY")

    def test_missing_ticker_header_raises(self):
        import io

        import openpyxl

        wb = openpyxl.Workbook()
        wb.active.append(["Fund", "Value"])
        wb.active.append(["SPY", 1])
        buf = io.BytesIO()
        wb.save(buf)
        with pytest.raises(HoldingsError, match="no Ticker header"):
            SsgaHoldingsSource().parse(buf.getvalue(), "SPY")


class TestVanguardParse:
    import json as _json

    PAYLOAD = _json.loads((FIXTURES / "vanguard" / "VYM-holdings-2026-07-16.json").read_text())

    def test_json_parses_to_constituents(self):
        holdings = VanguardHoldingsSource().parse(self.PAYLOAD, "VYM")
        assert len(holdings) == 40
        top = holdings[0]
        assert top.ticker == "AVGO"
        assert top.weight == pytest.approx(7.29, abs=0.01)  # a string in the feed, parsed
        assert top.name == "Broadcom Inc."

    def test_unrecognized_body_is_loud(self):
        with pytest.raises(HoldingsError, match="fund.entity|JSON object"):
            VanguardHoldingsSource().parse({"nope": 1}, "VYM")


class TestRouting:
    def test_known_funds_route_to_their_issuer(self):
        assert isinstance(holdings_source_for("SPY"), SsgaHoldingsSource)
        assert isinstance(holdings_source_for("VOO"), VanguardHoldingsSource)

    def test_unsupported_ticker_is_none(self):
        assert holdings_source_for("SCHD") is None  # Schwab blocks headless
        assert holdings_source_for("QQQ") is None


class TestMembershipDiff:
    def _h(self, *tickers):
        return [EtfHolding(ticker=t, weight=1.0) for t in tickers]

    def test_added_and_dropped_are_the_set_difference(self):
        added, dropped = membership_diff(self._h("A", "B", "C"), self._h("B", "C", "D"))
        assert added == ("D",)
        assert dropped == ("A",)

    def test_no_change_is_empty(self):
        added, dropped = membership_diff(self._h("A", "B"), self._h("B", "A"))
        assert added == () and dropped == ()

    def test_deterministic_alphabetical(self):
        added, _ = membership_diff(self._h("A"), self._h("A", "Z", "M", "B"))
        assert added == ("B", "M", "Z")


@pytest.mark.live
class TestLiveSmoke:
    def test_dia_has_thirty_constituents(self):
        holdings = SsgaHoldingsSource().fetch("DIA")
        assert len(holdings) == 30  # the Dow — a stable, checkable count
        assert all(h.ticker and h.weight > 0 for h in holdings)

    def test_vanguard_voo_is_a_full_book(self):
        holdings = VanguardHoldingsSource().fetch("VOO")
        assert len(holdings) > 400  # S&P 500
        assert sum(h.weight for h in holdings) == pytest.approx(100.0, abs=2.0)
