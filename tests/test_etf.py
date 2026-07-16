"""ETF holdings adapter + membership-diff tests: the SSGA xlsx parse over a
recorded fixture (never the network) and the pure set-diff that turns two
snapshots into a rebalance."""

from pathlib import Path

import pytest

from argus.etf import (
    HoldingsError,
    NportHoldingsSource,
    SsgaHoldingsSource,
    VanguardHoldingsSource,
    holdings_source_for,
    is_nport_etf,
    membership_diff,
)
from argus.models import EtfHolding

FIXTURES = Path(__file__).parent / "fixtures"
SPY = (FIXTURES / "ssga" / "SPY-holdings-2026-07-16.xlsx").read_bytes()
NPORT_SCHD = (FIXTURES / "edgar" / "nport-SCHD-sample.xml").read_text()


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


class TestNportParse:
    def test_recorded_nport_parses_to_holdings(self):
        holdings = NportHoldingsSource("x@y.z").parse(NPORT_SCHD, "SCHD")
        # 3 real securities + the net-assets line (placeholder cusip, kept by name)
        assert len(holdings) == 4
        top = holdings[0]
        assert top.cusip == "17275R102"
        assert top.name == "Cisco Systems Inc"
        assert top.ticker is None  # N-PORT carries no ticker
        assert top.weight == pytest.approx(3.576, abs=0.001)

    def test_holding_keys_on_cusip_labels_on_name(self):
        cisco = NportHoldingsSource("x@y.z").parse(NPORT_SCHD, "SCHD")[0]
        assert cisco.key == "17275R102"  # stable identity for the diff
        assert cisco.label == "Cisco Systems Inc"  # how the rebalance reads

    def test_placeholder_cusip_falls_back_to_name(self):
        net = NportHoldingsSource("x@y.z").parse(NPORT_SCHD, "SCHD")[-1]
        assert net.cusip is None  # "N/A" is not a usable key
        assert net.key == "Net Other Assets (Liabilities)"

    def test_zero_holdings_is_loud_not_empty(self):
        empty = "<edgarSubmission xmlns='http://www.sec.gov/edgar/nport'></edgarSubmission>"
        with pytest.raises(HoldingsError, match="zero holdings"):
            NportHoldingsSource("x@y.z").parse(empty, "SCHD")

    def test_unparseable_xml_is_loud(self):
        with pytest.raises(HoldingsError, match="unparseable"):
            NportHoldingsSource("x@y.z").parse("not xml <<<", "SCHD")


class TestRouting:
    def test_known_funds_route_to_their_issuer(self):
        assert isinstance(holdings_source_for("SPY"), SsgaHoldingsSource)
        assert isinstance(holdings_source_for("VOO"), VanguardHoldingsSource)

    def test_schwab_and_ishares_route_to_nport_with_contact(self):
        assert isinstance(holdings_source_for("SCHD", "x@y.z"), NportHoldingsSource)
        assert isinstance(holdings_source_for("IWM", "x@y.z"), NportHoldingsSource)
        assert is_nport_etf("SCHD") and is_nport_etf("iwm")

    def test_nport_fund_without_contact_is_none(self):
        # SEC refuses anonymous requests, so no email means no feed.
        assert holdings_source_for("SCHD") is None

    def test_unsupported_ticker_is_none(self):
        assert holdings_source_for("QQQ", "x@y.z") is None  # Invesco, no feed
        assert not is_nport_etf("QQQ")


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

    def test_cusip_keyed_holdings_diff_by_cusip_report_by_name(self):
        # N-PORT names have no ticker: identity is the CUSIP, display the name.
        prev = [
            EtfHolding(cusip="111", name="Alpha Corp"),
            EtfHolding(cusip="222", name="Beta Inc"),
        ]
        curr = [
            EtfHolding(cusip="222", name="Beta Inc"),
            EtfHolding(cusip="333", name="Gamma Ltd"),
        ]
        added, dropped = membership_diff(prev, curr)
        assert added == ("Gamma Ltd",)  # labelled by company, keyed by cusip
        assert dropped == ("Alpha Corp",)

    def test_same_cusip_renamed_is_not_a_change(self):
        # A name restatement must not read as a drop+add — identity is the cusip.
        prev = [EtfHolding(cusip="111", name="Meta Platforms")]
        curr = [EtfHolding(cusip="111", name="Meta Platforms Inc")]
        assert membership_diff(prev, curr) == ((), ())


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

    def test_nport_schd_resolves_and_parses(self):
        # SEC path: ticker → series → latest NPORT-P → ~100 dividend names.
        holdings = NportHoldingsSource("invocation.dev@gmail.com").fetch("SCHD")
        assert len(holdings) > 90  # Dow Jones US Dividend 100
        assert all(h.key for h in holdings)  # every holding is diffable
        assert all(h.ticker is None and h.name for h in holdings)  # named, no ticker
