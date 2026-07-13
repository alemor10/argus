"""TradingViewScreener over the recorded fixture — never the network.

parse() is exercised against the real recorded /america/scan response only,
plus synthetic malformed rows. The screener is the accepted-fragile kind: an
unexpected body shape must raise ScreenerError (loud), while an individual
row without a usable identity is skipped and counted in last_skipped (never
silent). Screener metrics are claims, not observations — an unreadable metric
becomes None, and nothing here ever reaches the store.

A @pytest.mark.live smoke test exists but is excluded by default (pytest
addopts) — CI must never depend on free feeds.
"""

import json
import math
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from argus.scout.screener import Screener, ScreenerError, ScreenerRow, TradingViewScreener

FIXTURES = Path(__file__).parent / "fixtures" / "tradingview"

# Mirrors the adapter's requested column order — the fixture's `d` arrays are
# real wire rows in exactly this shape.
_POS = {
    "name": 0,
    "description": 1,
    "sector": 2,
    "close": 3,
    "market_cap": 4,
    "pe": 5,
    "peg": 6,
    "eps_growth": 7,
    "revenue_growth": 8,
    "gross_margin": 9,
    "operating_margin": 10,
    "debt_to_equity": 11,
    "avg_volume": 12,
    "fwd_pe": 13,
    "roe": 14,
    "fcf_margin": 15,
}

GARBAGE_BODIES = (
    None,
    [],
    "not a dict",
    42,
    {},
    {"totalCount": 10},
    {"data": None},
    {"data": "not a list"},
    {"data": {"s": "NYSE:TEST"}},
)


def entry(s="NYSE:TEST", **overrides):
    """A synthetic-but-wire-shaped scanner row; overrides address `d` slots
    by the column names in _POS."""
    d = [
        "TEST", "Test Corp", "Finance", 10.0, 5_000_000_000.0, 15.0, 1.2,
        8.0, 5.0, 40.0, 20.0, 0.5, 1_000_000.0, 12.5, 18.0, 15.0,
    ]
    for key, value in overrides.items():
        d[_POS[key]] = value
    return {"s": s, "d": d}


def payload(*entries):
    return {"totalCount": len(entries), "data": list(entries)}


def by_ticker(rows):
    return {row.ticker: row for row in rows}


@pytest.fixture(scope="module")
def fixture_payload():
    return json.loads((FIXTURES / "scan-america-2026-07-13.json").read_text())


@pytest.fixture()
def rows(fixture_payload):
    return TradingViewScreener().parse(fixture_payload)


# --- parse() over the recorded response --------------------------------------


class TestParseFixture:
    def test_all_forty_rows_parse_with_none_skipped(self, fixture_payload):
        screener = TradingViewScreener()
        parsed = screener.parse(fixture_payload)
        assert len(parsed) == 40
        assert screener.last_skipped == 0

    def test_nvda_full_column_mapping(self, rows):
        nvda = by_ticker(rows)["NVDA"]
        assert nvda.exchange == "NASDAQ"
        assert nvda.company == "NVIDIA Corporation"
        assert nvda.sector == "Electronic Technology"
        assert nvda.close == pytest.approx(203.53)
        assert nvda.market_cap == pytest.approx(4925426156753.604)
        assert nvda.pe_ttm == pytest.approx(31.169407945113175)
        assert nvda.peg_ttm == pytest.approx(0.2928149773102382)
        assert nvda.eps_growth_ttm_pct == pytest.approx(110.33338701884364)
        assert nvda.revenue_growth_ttm_pct == pytest.approx(70.68376931623068)
        assert nvda.gross_margin_pct == pytest.approx(74.1454331711974)
        assert nvda.operating_margin_pct == pytest.approx(64.0200243795638)
        assert nvda.debt_to_equity == pytest.approx(0.0655534751424742)
        assert nvda.avg_volume_30d == pytest.approx(159516837.10000005)
        assert nvda.fwd_pe == pytest.approx(20.44827259165906)
        assert nvda.roe_pct == pytest.approx(114.288066963343)
        assert nvda.fcf_margin_pct == pytest.approx(46.97444879699871)

    def test_ticker_is_the_bare_symbol_not_the_company_name(self, rows):
        """TV's "name" column is the SYMBOL; the company name is
        "description". Confusing the two would make every downstream fetch
        query Yahoo for "NVIDIA Corporation"."""
        for row in rows:
            assert " " not in row.ticker
        assert by_ticker(rows)["AAPL"].company == "Apple Inc."

    def test_exchange_comes_from_the_s_prefix(self, rows):
        tickers = by_ticker(rows)
        assert tickers["NVDA"].exchange == "NASDAQ"
        assert tickers["JPM"].exchange == "NYSE"
        assert {row.exchange for row in rows} <= {"NASDAQ", "NYSE", "AMEX"}

    def test_percent_fields_pass_through_as_percent_numbers(self, rows):
        """74.15 means 74.15% — no rescaling to fractions here. (The monitor's
        Yahoo adapter stores fractions, but screener values never meet gate
        bounds: they are claims, compared only against scout.yaml criteria
        written in the same percent unit TV reports.)"""
        nvda = by_ticker(rows)["NVDA"]
        assert nvda.gross_margin_pct > 1.01  # a fraction could never say 74
        assert nvda.eps_growth_ttm_pct == pytest.approx(110.333, abs=0.001)
        assert nvda.roe_pct > 1.01  # 114.29 means 114.29%, not 1.14x
        assert nvda.fcf_margin_pct > 1.01  # 46.97 means 46.97%

    def test_null_metrics_become_none_fields(self, rows):
        """Real nulls from the recorded response: banks report no gross
        margin, INTC had negative TTM earnings (no P/E, no PEG)."""
        tickers = by_ticker(rows)
        assert tickers["INTC"].pe_ttm is None
        assert tickers["INTC"].peg_ttm is None
        assert tickers["INTC"].close is not None  # neighbors unharmed
        assert tickers["TSLA"].peg_ttm is None
        assert tickers["TSLA"].pe_ttm is not None
        assert tickers["JPM"].gross_margin_pct is None
        assert tickers["JPM"].operating_margin_pct is not None
        assert tickers["SNDK"].eps_growth_ttm_pct is None

    def test_null_quality_garp_metrics_become_none_fields(self, rows):
        """Real nulls in the three appended columns: pre-profit RVMD has no
        forward-P/E estimate and no FCF margin; ABBV's negative book equity
        nulls its return_on_equity (alongside its null debt_to_equity)."""
        tickers = by_ticker(rows)
        assert tickers["RVMD"].fwd_pe is None
        assert tickers["RVMD"].fcf_margin_pct is None
        assert tickers["RVMD"].roe_pct is not None  # neighbors unharmed
        assert tickers["ABBV"].roe_pct is None
        assert tickers["ABBV"].fwd_pe is not None
        assert tickers["ABBV"].fcf_margin_pct is not None

    def test_rows_are_frozen(self, rows):
        with pytest.raises(ValidationError):
            rows[0].ticker = "HACKED"


# --- parse() failure modes ----------------------------------------------------


class TestParseFailureModes:
    @pytest.mark.parametrize("junk", GARBAGE_BODIES)
    def test_unexpected_body_shape_raises_screener_error(self, junk):
        """The accepted-fragile contract: a changed endpoint must be LOUD.
        An empty candidate list from a reshaped body would read as "nothing
        matched this week" — the silent-failure class this project kills."""
        with pytest.raises(ScreenerError, match="unexpected body shape"):
            TradingViewScreener().parse(junk)

    def test_empty_data_is_a_valid_empty_scan(self):
        screener = TradingViewScreener()
        assert screener.parse({"totalCount": 0, "data": []}) == []
        assert screener.last_skipped == 0

    @pytest.mark.parametrize(
        "bad",
        [
            "not a dict at all",
            {"s": "NYSE:TEST"},  # no d array
            {"s": "NYSE:TEST", "d": "not a list"},
            {"s": "NYSE:TEST", "d": ["TEST", "too short"]},
            entry(s=None),  # no exchange evidence
            entry(s="TESTNOCOLON"),
            entry(s=":TEST"),  # empty exchange prefix
            entry(name=None),  # no symbol
            entry(name=""),
            entry(name=42),
        ],
    )
    def test_row_without_usable_identity_is_skipped_and_counted(self, bad):
        screener = TradingViewScreener()
        parsed = screener.parse(payload(bad, entry(name="GOOD")))
        assert [row.ticker for row in parsed] == ["GOOD"]  # neighbor lands
        assert screener.last_skipped == 1

    def test_wrong_length_d_is_skipped_even_when_symbol_slot_reads(self):
        """One slot off in either direction means the column contract shifted
        for that row — every index after the shift would be silently wrong.
        Exactly 16 (the 13 originals + the three Quality-GARP columns) parses."""
        sixteen = entry()
        assert len(sixteen["d"]) == 16
        seventeen = {"s": "NYSE:TEST", "d": sixteen["d"] + [99.9]}
        fifteen = {"s": "NYSE:TEST", "d": sixteen["d"][:-1]}
        thirteen = {"s": "NYSE:TEST", "d": sixteen["d"][:13]}  # the pre-1.2 shape
        screener = TradingViewScreener()
        assert screener.parse(payload(seventeen, fifteen, thirteen)) == []
        assert screener.last_skipped == 3
        assert len(screener.parse(payload(sixteen))) == 1
        assert screener.last_skipped == 0

    def test_last_skipped_resets_on_every_parse(self):
        screener = TradingViewScreener()
        screener.parse(payload(entry(s="no-colon"), entry()))
        assert screener.last_skipped == 1
        screener.parse(payload(entry()))
        assert screener.last_skipped == 0

    @pytest.mark.parametrize(
        ("override", "attr"),
        [
            ({"close": "12.34"}, "close"),  # a stringified number is a guess
            ({"market_cap": True}, "market_cap"),  # float(True) launders garbage
            ({"pe": {"v": 1}}, "pe_ttm"),
            ({"peg": float("nan")}, "peg_ttm"),  # json.loads admits NaN
            ({"debt_to_equity": float("inf")}, "debt_to_equity"),
            ({"fwd_pe": "20.4"}, "fwd_pe"),
            ({"roe": True}, "roe_pct"),
            ({"fcf_margin": float("nan")}, "fcf_margin_pct"),
        ],
    )
    def test_unreadable_metric_becomes_none_not_a_crash(self, override, attr):
        [row] = TradingViewScreener().parse(payload(entry(**override)))
        assert getattr(row, attr) is None
        assert row.avg_volume_30d == pytest.approx(1_000_000.0)  # neighbors unharmed

    def test_integer_metrics_coerce_to_float(self):
        [row] = TradingViewScreener().parse(payload(entry(close=10)))
        assert row.close == pytest.approx(10.0)
        assert isinstance(row.close, float)

    def test_non_string_description_and_sector_become_none(self):
        [row] = TradingViewScreener().parse(payload(entry(description=None, sector=42)))
        assert row.company is None
        assert row.sector is None


# --- scan(): the one POST, the ctor timeout, the loud failures ----------------


class _FakeResponse:
    def __init__(self, body, error=None):
        self._body = body
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return self._body


class TestScan:
    def test_scan_posts_once_with_the_pinned_request_shape(self, monkeypatch):
        calls = []

        def fake_post(url, *, json, headers, timeout):
            calls.append((url, json, headers, timeout))
            return _FakeResponse(payload(entry()))

        monkeypatch.setattr(httpx, "post", fake_post)
        rows = TradingViewScreener(timeout=7.5).scan(
            min_market_cap=2e9, min_avg_volume=5e5
        )
        assert [row.ticker for row in rows] == ["TEST"]
        [(url, body, headers, timeout)] = calls  # exactly one POST
        assert url == "https://scanner.tradingview.com/america/scan"
        assert timeout == 7.5  # the ctor's timeout reaches the wire
        assert headers["User-Agent"].startswith("Mozilla/5.0")  # browser-ish
        assert {"left": "market_cap_basic", "operation": "greater", "right": 2e9} in body["filter"]
        assert {"left": "average_volume_30d_calc", "operation": "greater", "right": 5e5} in body["filter"]
        assert {"left": "type", "operation": "equal", "right": "stock"} in body["filter"]
        assert body["sort"] == {"sortBy": "market_cap_basic", "sortOrder": "desc"}
        assert body["range"] == [0, 8000]
        assert body["columns"][0] == "name"
        assert len(body["columns"]) == 16
        # The Quality-GARP columns ride at the END — the fixture's d arrays
        # are index-mapped, so column order IS the wire contract.
        assert body["columns"][13:] == [
            "price_earnings_fwd",
            "return_on_equity",
            "free_cash_flow_margin_ttm",
        ]
        # The similarly-named identifier that exists but returns null must
        # never sneak in: it would silently None every fwd_pe.
        assert "price_earnings_forward" not in body["columns"]

    def test_wholesale_failure_retries_once_then_raises_screener_error(self, monkeypatch):
        calls = []

        def dead_post(url, **kwargs):
            calls.append(url)
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", dead_post)
        with pytest.raises(ScreenerError, match="tradingview: scan failed"):
            TradingViewScreener().scan(min_market_cap=1.0, min_avg_volume=1.0)
        assert len(calls) == 2  # one inline retry, no framework

    def test_transient_failure_recovers_on_the_retry(self, monkeypatch):
        attempts = []

        def flaky_post(url, **kwargs):
            attempts.append(url)
            if len(attempts) == 1:
                raise httpx.ReadTimeout("slow feed")
            return _FakeResponse(payload(entry()))

        monkeypatch.setattr(httpx, "post", flaky_post)
        rows = TradingViewScreener().scan(min_market_cap=1.0, min_avg_volume=1.0)
        assert len(rows) == 1
        assert len(attempts) == 2

    def test_non_200_raises_screener_error(self, monkeypatch):
        error = httpx.HTTPStatusError("HTTP 503", request=None, response=None)
        monkeypatch.setattr(
            httpx, "post", lambda url, **kwargs: _FakeResponse(None, error=error)
        )
        with pytest.raises(ScreenerError, match="tradingview: scan failed"):
            TradingViewScreener().scan(min_market_cap=1.0, min_avg_volume=1.0)

    def test_reshaped_body_through_scan_is_loud_not_retried(self, monkeypatch):
        calls = []

        def reshaped_post(url, **kwargs):
            calls.append(url)
            return _FakeResponse({"error": "endpoint moved"})

        monkeypatch.setattr(httpx, "post", reshaped_post)
        with pytest.raises(ScreenerError, match="unexpected body shape"):
            TradingViewScreener().scan(min_market_cap=1.0, min_avg_volume=1.0)
        assert len(calls) == 1  # shape breakage is not a hiccup; no retry

    def test_default_timeout_is_thirty_seconds(self):
        assert TradingViewScreener().timeout == pytest.approx(30.0)

    def test_satisfies_the_screener_protocol(self):
        assert isinstance(TradingViewScreener(), Screener)


# --- ScreenerRow ---------------------------------------------------------------


class TestScreenerRow:
    def test_metrics_default_to_none(self):
        row = ScreenerRow(ticker="NVDA", exchange="NASDAQ")
        assert row.company is None
        assert row.pe_ttm is None
        assert row.avg_volume_30d is None
        assert row.fwd_pe is None
        assert row.roe_pct is None
        assert row.fcf_margin_pct is None

    def test_frozen(self):
        row = ScreenerRow(ticker="NVDA", exchange="NASDAQ")
        with pytest.raises(ValidationError):
            row.close = 1.0


# --- Live smoke (excluded by default via pytest addopts) -----------------------


@pytest.mark.live
class TestLiveScan:
    def test_scan_live(self):
        screener = TradingViewScreener()
        rows = screener.scan(min_market_cap=10_000_000_000, min_avg_volume=1_000_000)
        assert len(rows) > 100  # hundreds of US large caps clear these floors
        assert screener.last_skipped == 0
        assert all(row.ticker and row.exchange for row in rows)
        assert {row.exchange for row in rows} <= {"AMEX", "NASDAQ", "NYSE"}
        first = rows[0]  # sorted market_cap desc, filtered server-side
        assert first.market_cap is not None and first.market_cap > 10_000_000_000
        assert first.close is not None and math.isfinite(first.close)
