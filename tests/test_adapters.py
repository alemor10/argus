"""Adapter parse()/covers() over recorded fixtures — never the network.

Each adapter is a thin _fetch_raw() plus a pure parse(); everything here
drives parse() with checked-in payloads, including the pathological real
NTDOY capture. Adapters only normalize units and types — implausible values
(the $35 target against a $10.97 price) MUST pass through as plain
RawObservations for gates.py to judge; an adapter that pre-judges would hide
the founding case from the quarantine machinery.

A @pytest.mark.live smoke test per adapter exists but is excluded by default
(pytest addopts) — CI must never depend on free feeds.
"""

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from argus.fields import Field, Source
from argus.sources import EdgarSource, FinnhubSource, FredSource, YahooSource

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED_AT = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)

GARBAGE_PAYLOADS = (
    None,
    [],
    "not a dict",
    42,
    {"info": "not a dict", "calendar": 3, "upgrades_downgrades": "not a list",
     "earnings_history": "not a list", "facts": "not a dict", "c": {}},
)


def load(relative: str):
    return json.loads((FIXTURES / relative).read_text())


def by_field(result):
    """field → RawObservation; asserts one observation per field on the way."""
    out = {}
    for obs in result.observations:
        assert obs.field not in out, f"duplicate observation for {obs.field}"
        out[obs.field] = obs
    return out


# --- Yahoo ------------------------------------------------------------------


@pytest.fixture()
def ntdoy_payload():
    return load("yahoo/NTDOY-2026-07-12.json")


@pytest.fixture()
def ntdoy_result(ntdoy_payload):
    return YahooSource().parse(ntdoy_payload, "NTDOY", FETCHED_AT)


class TestYahooParse:
    def test_pathological_ntdoy_passes_through_unjudged(self, ntdoy_result):
        """The founding case: the adapter must NOT quarantine — it has no
        verdict to give. Both legs reach gates.py as plain observations."""
        values = by_field(ntdoy_result)
        assert values[Field.PRICE].value_num == pytest.approx(10.97)
        assert values[Field.ANALYST_TARGET_MEAN].value_num == pytest.approx(35.0)
        assert ntdoy_result.parse_failures == ()

    def test_ntdoy_full_field_mapping(self, ntdoy_result):
        values = by_field(ntdoy_result)
        assert values[Field.MARKET_CAP].value_num == pytest.approx(50586124288)
        assert values[Field.PE_TTM].value_num == pytest.approx(19.589287)
        assert values[Field.PE_FWD].value_num == pytest.approx(5.1261683)
        # PEG comes from trailingPegRatio (3.5766), never pegRatio (3.58)
        assert values[Field.PEG].value_num == pytest.approx(3.5766)
        # margins are already fractions — pass through, no rescaling
        assert values[Field.GROSS_MARGIN].value_num == pytest.approx(0.39297)
        assert values[Field.OPERATING_MARGIN].value_num == pytest.approx(0.14668)
        assert values[Field.ANALYST_RATING].value_text == "none"
        assert values[Field.ANALYST_COUNT].value_num == pytest.approx(1.0)
        # calendar's stringified "[datetime.date(2026, 8, 6)]" is recovered
        assert values[Field.NEXT_EARNINGS_DATE].value_date == date(2026, 8, 6)
        for obs in ntdoy_result.observations:
            assert obs.ticker == "NTDOY"
            assert obs.source is Source.YAHOO
            assert obs.fetched_at == FETCHED_AT

    def test_price_observed_at_from_regular_market_time_epoch(self, ntdoy_result):
        values = by_field(ntdoy_result)
        assert values[Field.PRICE].observed_at == datetime.fromtimestamp(1783713600, tz=UTC)
        # only the quote carries a source-reported timestamp
        assert values[Field.PE_TTM].observed_at is None

    def test_absent_keys_are_simply_absent(self, ntdoy_result):
        """NTDOY's info has no debtToEquity key: no observation, and no
        ParseFailure either — absence is not the same as unreadable."""
        assert Field.DEBT_TO_EQUITY not in by_field(ntdoy_result)
        assert not any(f.field is Field.DEBT_TO_EQUITY for f in ntdoy_result.parse_failures)

    def test_debt_to_equity_percent_normalized_to_ratio(self):
        result = YahooSource().parse({"info": {"debtToEquity": 41.5}}, "TEST", FETCHED_AT)
        [obs] = result.observations
        assert obs.field is Field.DEBT_TO_EQUITY
        assert obs.value_num == pytest.approx(0.415)  # yfinance reports percent

    def test_current_price_falls_back_to_regular_market_price(self):
        payload = {"info": {"regularMarketPrice": 12.5, "regularMarketTime": 1783713600}}
        values = by_field(YahooSource().parse(payload, "TEST", FETCHED_AT))
        assert values[Field.PRICE].value_num == pytest.approx(12.5)
        assert values[Field.PRICE].observed_at == datetime.fromtimestamp(1783713600, tz=UTC)

    def test_malformed_value_becomes_parse_failure_not_exception(self):
        payload = {"info": {"trailingPE": "N/A garbled", "currentPrice": 10.0}}
        result = YahooSource().parse(payload, "TEST", FETCHED_AT)
        values = by_field(result)
        assert Field.PE_TTM not in values  # not silently passed through…
        [failure] = result.parse_failures  # …but not silently dropped either
        assert failure.field is Field.PE_TTM
        assert failure.raw == "N/A garbled"
        assert failure.source is Source.YAHOO
        assert values[Field.PRICE].value_num == pytest.approx(10.0)  # neighbors unharmed

    def test_unreadable_earnings_date_becomes_parse_failure(self):
        payload = {"calendar": {"Earnings Date": "sometime next quarter"}}
        result = YahooSource().parse(payload, "TEST", FETCHED_AT)
        assert result.observations == ()
        [failure] = result.parse_failures
        assert failure.field is Field.NEXT_EARNINGS_DATE
        assert failure.raw == "sometime next quarter"

    def test_upgrades_downgrades_become_analyst_action_records(self, ntdoy_result):
        assert len(ntdoy_result.analyst_actions) == 6
        first = ntdoy_result.analyst_actions[0]
        assert first.action_date == date(2021, 7, 7)  # GradeDate datetime → date
        assert first.firm == "Jefferies"
        assert first.action == "down"
        assert first.from_grade == "Buy"
        assert first.to_grade == "Hold"
        assert first.source is Source.YAHOO
        # the init row carries FromGrade "" → None, not empty string
        [init] = [a for a in ntdoy_result.analyst_actions if a.action == "init"]
        assert init.from_grade is None
        assert init.to_grade == "Underperform"

    def test_malformed_action_record_becomes_parse_failure(self):
        payload = {
            "info": {},
            "upgrades_downgrades": [
                {"Firm": "NoDate Broker", "ToGrade": "Buy", "Action": "up"},  # no GradeDate
                {"GradeDate": "2026-07-01T10:00:00", "Firm": "OK Broker",
                 "ToGrade": "Hold", "FromGrade": "Buy", "Action": "down"},
            ],
        }
        result = YahooSource().parse(payload, "TEST", FETCHED_AT)
        [action] = result.analyst_actions  # the good record still lands
        assert action.firm == "OK Broker"
        [failure] = result.parse_failures  # the bad one is evidence, not an absence
        assert failure.field is Field.ANALYST_RATING
        assert "NoDate Broker" in failure.raw

    def test_earnings_history_rows_with_actuals_become_records(self):
        payload = {
            "info": {},
            "earnings_history": [
                {"quarter": "2026-03-31T00:00:00", "epsActual": 0.98, "epsEstimate": 1.00,
                 "epsDifference": -0.02, "surprisePercent": -0.02},
                {"quarter": "2026-06-30T00:00:00", "epsActual": 1.05, "epsEstimate": 0.93,
                 "epsDifference": 0.12, "surprisePercent": 0.129},
            ],
        }
        result = YahooSource().parse(payload, "TEST", FETCHED_AT)
        assert result.parse_failures == ()
        [q1, q2] = result.earnings_results
        assert (q1.quarter_end, q1.eps_actual, q1.eps_estimate) == (date(2026, 3, 31), 0.98, 1.00)
        assert (q2.quarter_end, q2.eps_actual, q2.eps_estimate) == (date(2026, 6, 30), 1.05, 0.93)
        for record in (q1, q2):
            assert record.ticker == "TEST"
            assert record.source is Source.YAHOO
            assert record.fetched_at == FETCHED_AT

    def test_unreported_quarter_is_skipped_not_quarantined(self):
        """A just-announced quarter can arrive with its actual not yet filled
        (NaN) — not yet a result and not malformed. It lands once the actual
        appears; quarantining it every run would erode the section."""
        payload = {
            "info": {},
            "earnings_history": [
                {"quarter": "2026-06-30T00:00:00", "epsActual": float("nan"), "epsEstimate": 0.93},
                {"quarter": "2026-09-30T00:00:00", "epsEstimate": 1.10},  # actual absent entirely
            ],
        }
        result = YahooSource().parse(payload, "TEST", FETCHED_AT)
        assert result.earnings_results == ()
        assert result.parse_failures == ()

    def test_missing_estimate_maps_to_none_not_a_failure(self):
        """No street coverage is a fact, distinct from garbage: the actual
        still records, with estimate None."""
        payload = {
            "info": {},
            "earnings_history": [
                {"quarter": "2026-06-30T00:00:00", "epsActual": 1.05, "epsEstimate": float("nan")},
            ],
        }
        [record] = YahooSource().parse(payload, "TEST", FETCHED_AT).earnings_results
        assert record.eps_actual == 1.05
        assert record.eps_estimate is None

    def test_malformed_earnings_rows_become_one_aggregated_parse_failure(self):
        payload = {
            "info": {},
            "earnings_history": [
                {"quarter": "garbled", "epsActual": 1.05},  # unreadable quarter
                {"quarter": "2026-03-31T00:00:00", "epsActual": True},  # bool laundering guard
                {"quarter": "2026-06-30T00:00:00", "epsActual": 1.05, "epsEstimate": "N/A"},
                {"quarter": "2026-09-30T00:00:00", "epsActual": 0.50, "epsEstimate": 0.40},  # good
            ],
        }
        result = YahooSource().parse(payload, "TEST", FETCHED_AT)
        [record] = result.earnings_results  # the good row still lands
        assert record.quarter_end == date(2026, 9, 30)
        [failure] = result.parse_failures  # the bad ones are evidence, aggregated
        assert failure.field is Field.NEXT_EARNINGS_DATE
        assert "3 unreadable earnings-history row(s)" in failure.raw

    @pytest.mark.parametrize("junk", GARBAGE_PAYLOADS)
    def test_parse_never_raises_on_garbage(self, junk):
        result = YahooSource().parse(junk, "TEST", FETCHED_AT)
        assert result.observations == ()
        assert result.analyst_actions == ()
        assert result.earnings_results == ()

    def test_covers_everything(self):
        assert YahooSource().covers("NTDOY")
        assert YahooSource().covers("VOO")


class TestYahooPriceFallback:
    """`_fetch_raw` backfills the price from the chart endpoint (fast_info)
    when quoteSummary (`.info`) sheds it — the real box failure where index/
    futures/crypto macro symbols (^TNX, GC=F, BTC-USD) 404'd under load and
    their levels vanished from the digest."""

    @staticmethod
    def _install(monkeypatch, *, info, fast_price):
        import sys
        import types

        class _FastInfo:
            def get(self, key, default=None):
                return fast_price if key == "lastPrice" else default

        class _Ticker:
            def __init__(self, symbol):
                self.info = dict(info)
                self.fast_info = _FastInfo()
                self.upgrades_downgrades = None
                self.earnings_history = None
                self.calendar = {}

        module = types.ModuleType("yfinance")
        module.Ticker = _Ticker
        monkeypatch.setitem(sys.modules, "yfinance", module)

    def test_fast_info_backfills_when_info_has_no_price(self, monkeypatch):
        self._install(monkeypatch, info={"quoteType": "INDEX"}, fast_price=4.547)
        result = YahooSource().fetch("^TNX")
        [price] = [o for o in result.observations if o.field is Field.PRICE]
        assert price.value_num == pytest.approx(4.547)
        assert price.observed_at is None  # chart endpoint carries no timestamp
        assert result.parse_failures == ()

    def test_info_price_wins_and_fast_info_is_not_consulted(self, monkeypatch):
        # regularMarketTime present → observed_at set; fast_price is a sentinel
        # that must never surface if the primary path already has a price.
        self._install(
            monkeypatch,
            info={"regularMarketPrice": 100.0, "regularMarketTime": 1_760_000_000},
            fast_price=999.0,
        )
        result = YahooSource().fetch("AAPL")
        [price] = [o for o in result.observations if o.field is Field.PRICE]
        assert price.value_num == pytest.approx(100.0)
        assert price.observed_at is not None

    def test_no_price_anywhere_stays_absent_not_zero(self, monkeypatch):
        # Both paths dry: the field is simply absent (an honest gap), never a
        # fabricated 0 — absence of data stays distinguishable from a signal.
        self._install(monkeypatch, info={"quoteType": "INDEX"}, fast_price=None)
        result = YahooSource().fetch("^VIX")
        assert not [o for o in result.observations if o.field is Field.PRICE]


# --- Finnhub ----------------------------------------------------------------


class TestFinnhubParse:
    def test_quote_fixture_maps_price_with_observed_at(self):
        payload = load("finnhub/NVDA-quote.json")
        result = FinnhubSource(api_key="test-key").parse(payload, "NVDA", FETCHED_AT)
        [obs] = result.observations
        assert obs.field is Field.PRICE
        assert obs.value_num == pytest.approx(164.92)
        assert obs.observed_at == datetime.fromtimestamp(1783713600, tz=UTC)
        assert obs.source is Source.FINNHUB
        assert obs.fetched_at == FETCHED_AT
        assert result.parse_failures == ()

    def test_zero_price_is_absent_not_a_zero_observation(self):
        """Finnhub's convention for unknown symbols: c=0, t=0. That is 'no
        data', and it must be absent — a 0.0 price observation would be a
        fabricated value."""
        payload = {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}
        result = FinnhubSource(api_key="k").parse(payload, "NOSUCH", FETCHED_AT)
        assert result.observations == ()
        assert result.parse_failures == ()

    def test_missing_price_key_is_absent(self):
        result = FinnhubSource(api_key="k").parse({}, "TEST", FETCHED_AT)
        assert result.observations == ()
        assert result.parse_failures == ()

    def test_unreadable_price_becomes_parse_failure(self):
        result = FinnhubSource(api_key="k").parse({"c": "garbled", "t": 1783713600}, "TEST", FETCHED_AT)
        assert result.observations == ()
        [failure] = result.parse_failures
        assert failure.field is Field.PRICE
        assert failure.raw == "garbled"
        assert failure.source is Source.FINNHUB

    def test_nonpositive_timestamp_means_no_observed_at(self):
        result = FinnhubSource(api_key="k").parse({"c": 10.5, "t": 0}, "TEST", FETCHED_AT)
        [obs] = result.observations
        assert obs.observed_at is None  # staleness gate then skips: no evidence

    @pytest.mark.parametrize("junk", GARBAGE_PAYLOADS[:4])
    def test_parse_never_raises_on_garbage(self, junk):
        result = FinnhubSource(api_key="k").parse(junk, "TEST", FETCHED_AT)
        assert result.observations == ()


# --- EDGAR ------------------------------------------------------------------


@pytest.fixture()
def edgar_source(monkeypatch):
    """An EdgarSource whose HTTP layer serves the synthetic ticker→CIK
    mapping — covers() runs its real path (fetch, build, cache) offline."""
    mapping_payload = load("edgar/company-tickers-synthetic.json")
    monkeypatch.setattr(EdgarSource, "_get_json", lambda self, url: mapping_payload)
    return EdgarSource(contact_email="argus-test@example.com")


class TestEdgarCovers:
    def test_mapped_tickers_are_covered(self, edgar_source):
        assert edgar_source.covers("AAPL")
        assert edgar_source.covers("ACME")
        assert edgar_source.covers("BRK-B")

    def test_symbology_is_normalized_to_sec_dashes(self, edgar_source):
        assert edgar_source.covers("brk.b")  # dotted, lowercased → BRK-B
        assert edgar_source.covers(" aapl ")

    def test_unmapped_otc_adr_and_etf_are_not_covered(self, edgar_source):
        assert not edgar_source.covers("NTDOY")  # OTC ADR: no EDGAR filings
        assert not edgar_source.covers("VOO")  # ETF: no companyfacts

    def test_mapping_is_fetched_once_and_cached(self, monkeypatch):
        mapping_payload = load("edgar/company-tickers-synthetic.json")
        calls = []
        monkeypatch.setattr(
            EdgarSource, "_get_json", lambda self, url: calls.append(url) or mapping_payload
        )
        source = EdgarSource(contact_email="argus-test@example.com")
        assert source.covers("AAPL")
        assert not source.covers("NTDOY")
        assert len(calls) == 1


class TestEdgarParse:
    @pytest.fixture()
    def acme_result(self):
        payload = load("edgar/companyfacts-ACME-synthetic.json")
        return EdgarSource(contact_email="argus-test@example.com").parse(
            payload, "ACME", FETCHED_AT
        )

    def test_ratio_math_over_latest_annual_period(self, acme_result):
        values = by_field(acme_result)
        assert values[Field.GROSS_MARGIN].value_num == pytest.approx(0.40)  # 450M / 1125M
        assert values[Field.OPERATING_MARGIN].value_num == pytest.approx(0.20)  # 225M / 1125M
        assert values[Field.DEBT_TO_EQUITY].value_num == pytest.approx(1.20)  # 660M / 550M
        assert len(acme_result.observations) == 3
        assert acme_result.parse_failures == ()

    def test_observed_at_is_period_end_utc_midnight(self, acme_result):
        for obs in acme_result.observations:
            assert obs.observed_at == datetime(2024, 12, 31, tzinfo=UTC)
            assert obs.source is Source.EDGAR

    def test_quarterly_entries_never_leak_into_annual_math(self, acme_result):
        """The fixture's 10-Q Q1-2025 rows have a LATER period end and a 0.50
        gross margin — if form/duration filtering broke, they would win."""
        values = by_field(acme_result)
        assert values[Field.GROSS_MARGIN].value_num != pytest.approx(0.50)
        assert values[Field.GROSS_MARGIN].observed_at == datetime(2024, 12, 31, tzinfo=UTC)

    def test_revenue_falls_back_to_contract_revenue_tag(self):
        """Post-ASC-606 filers (Apple) report under the long tag, not Revenues."""
        payload = {
            "facts": {
                "us-gaap": {
                    "GrossProfit": {"units": {"USD": [
                        {"start": "2025-01-01", "end": "2025-12-31", "val": 40, "form": "10-K"},
                    ]}},
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                        {"start": "2025-01-01", "end": "2025-12-31", "val": 100, "form": "10-K"},
                    ]}},
                }
            }
        }
        result = EdgarSource(contact_email="x@example.com").parse(payload, "TEST", FETCHED_AT)
        [obs] = result.observations
        assert obs.field is Field.GROSS_MARGIN
        assert obs.value_num == pytest.approx(0.40)

    def test_missing_tag_means_absent_field(self):
        """EDGAR is a cross-check: no StockholdersEquity → no DEBT_TO_EQUITY,
        and nothing else — absence is disclosed by the digest tri-state."""
        payload = {
            "facts": {
                "us-gaap": {
                    "Liabilities": {"units": {"USD": [
                        {"end": "2025-12-31", "val": 600, "form": "10-K"},
                    ]}},
                }
            }
        }
        result = EdgarSource(contact_email="x@example.com").parse(payload, "TEST", FETCHED_AT)
        assert result.observations == ()

    def test_zero_denominator_yields_absent_not_crash(self):
        payload = {
            "facts": {
                "us-gaap": {
                    "Liabilities": {"units": {"USD": [
                        {"end": "2025-12-31", "val": 600, "form": "10-K"},
                    ]}},
                    "StockholdersEquity": {"units": {"USD": [
                        {"end": "2025-12-31", "val": 0, "form": "10-K"},
                    ]}},
                }
            }
        }
        result = EdgarSource(contact_email="x@example.com").parse(payload, "TEST", FETCHED_AT)
        assert result.observations == ()

    @pytest.mark.parametrize("junk", GARBAGE_PAYLOADS)
    def test_parse_never_raises_on_garbage(self, junk):
        result = EdgarSource(contact_email="x@example.com").parse(junk, "TEST", FETCHED_AT)
        assert result.observations == ()


class TestFinnhubEarningsCalendar:
    def test_live_fixture_parses_to_claims_rows(self):
        from argus.sources.finnhub import parse_earnings_calendar

        rows = parse_earnings_calendar(load("finnhub/earnings-calendar-2026-07-15.json"))
        assert len(rows) == 6
        acu = next(r for r in rows if r.symbol == "ACU")
        assert acu.report_date == date(2026, 7, 17)
        assert acu.eps_estimate == pytest.approx(0.5858)
        assert acu.eps_actual is None  # not yet reported → renders as upcoming
        alv = next(r for r in rows if r.symbol == "ALV")
        assert alv.hour == "bmo"

    def test_malformed_rows_are_skipped_numbers_cleaned(self):
        from argus.sources.finnhub import parse_earnings_calendar

        payload = {
            "earningsCalendar": [
                {"symbol": "OK", "date": "2026-07-16", "hour": "amc",
                 "epsEstimate": 1.0, "epsActual": True},  # bool laundering guard
                {"symbol": "", "date": "2026-07-16"},  # no identity
                {"symbol": "BAD", "date": "not-a-date"},
                "not a dict",
            ]
        }
        [row] = parse_earnings_calendar(payload)
        assert row.symbol == "OK"
        assert row.eps_actual is None

    @pytest.mark.parametrize("junk", GARBAGE_PAYLOADS[:4])
    def test_parse_never_raises_on_garbage(self, junk):
        from argus.sources.finnhub import parse_earnings_calendar

        assert parse_earnings_calendar(junk) == []


# --- FRED --------------------------------------------------------------------


class TestFredParse:
    @pytest.fixture()
    def cpi_csv(self):
        return (FIXTURES / "fred" / "CPIAUCSL-2026-07-15.csv").read_text()

    def test_level_takes_the_latest_point_with_its_period(self, cpi_csv):
        source = FredSource({"CPIAUCSL": "level"})
        [obs] = source.parse(cpi_csv, "CPIAUCSL", FETCHED_AT).observations
        assert obs.field is Field.ECON_VALUE
        assert obs.value_num == pytest.approx(332.568)
        assert obs.observed_at == datetime(2026, 6, 1, tzinfo=UTC)  # the period, not fetch time
        assert obs.source is Source.FRED
        assert obs.fetched_at == FETCHED_AT

    def test_yoy_pct_computes_from_the_published_series(self, cpi_csv):
        """June 2026 (332.568) over June 2025 (321.435) → +3.46% — derived
        from two points of the same official series (EDGAR-ratio precedent)."""
        source = FredSource({"CPIAUCSL": "yoy_pct"})
        [obs] = source.parse(cpi_csv, "CPIAUCSL", FETCHED_AT).observations
        assert obs.value_num == pytest.approx(3.4635, abs=0.01)
        assert obs.observed_at == datetime(2026, 6, 1, tzinfo=UTC)

    def test_mom_change_is_latest_minus_previous(self, cpi_csv):
        source = FredSource({"CPIAUCSL": "mom_change"})
        [obs] = source.parse(cpi_csv, "CPIAUCSL", FETCHED_AT).observations
        assert obs.value_num == pytest.approx(332.568 - 333.979)

    def test_missing_observations_are_absences_not_failures(self):
        """FRED encodes a missing period as "." — and, observed LIVE on
        CPIAUCSL (2025-10-01), as an empty cell. Both are absences; treating
        the empty cell as unreadable would quarantine the same artifact on
        every run forever."""
        payload = (
            "observation_date,DFF\n"
            "2026-07-11,.\n"
            "2026-07-12,\n"  # the live empty-cell variant
            "2026-07-13,3.62\n"
            "2026-07-14,3.63\n"
        )
        result = FredSource({"DFF": "level"}).parse(payload, "DFF", FETCHED_AT)
        [obs] = result.observations
        assert obs.value_num == pytest.approx(3.63)
        assert result.parse_failures == ()

    def test_unreadable_rows_aggregate_into_one_failure(self):
        payload = (
            "observation_date,UNRATE\n"
            "2026-05-01,4.3\n"
            "garbled row without a comma\n"
            "2026-06-01,not-a-number\n"
            "2026-06-01,4.2\n"
        )
        result = FredSource({"UNRATE": "level"}).parse(payload, "UNRATE", FETCHED_AT)
        [obs] = result.observations  # the good rows still land
        assert obs.value_num == pytest.approx(4.2)
        [failure] = result.parse_failures
        assert failure.field is Field.ECON_VALUE
        assert "2 unreadable CSV row(s)" in failure.raw

    def test_series_too_short_for_its_transform_is_absent(self):
        payload = "observation_date,NEW\n2026-06-01,100.0\n"
        for transform in ("yoy_pct", "mom_change"):
            result = FredSource({"NEW": transform}).parse(payload, "NEW", FETCHED_AT)
            assert result.observations == ()
            assert result.parse_failures == ()  # absence, not unreadable data

    def test_yoy_needs_a_point_near_one_year_back(self):
        """A gap where last-year should be → absent, never a wrong-base pct."""
        payload = (
            "observation_date,GAPPY\n"
            "2024-06-01,90.0\n"  # two years back — outside the tolerance
            "2026-06-01,110.0\n"
        )
        result = FredSource({"GAPPY": "yoy_pct"}).parse(payload, "GAPPY", FETCHED_AT)
        assert result.observations == ()

    def test_zero_base_yoy_is_absent_not_infinite(self):
        payload = "observation_date,Z\n2025-06-01,0.0\n2026-06-01,5.0\n"
        assert FredSource({"Z": "yoy_pct"}).parse(payload, "Z", FETCHED_AT).observations == ()

    def test_covers_only_configured_series(self):
        source = FredSource({"CPIAUCSL": "yoy_pct"})
        assert source.covers("CPIAUCSL")
        assert not source.covers("UNRATE")
        assert not source.covers("NVDA")  # never consulted for equities

    @pytest.mark.parametrize("junk", (*GARBAGE_PAYLOADS[:4], "not,a\nreal csv"))
    def test_parse_never_raises_on_garbage(self, junk):
        result = FredSource({"X": "level"}).parse(junk, "X", FETCHED_AT)
        assert result.observations == ()
        assert len(result.parse_failures) == 1  # the whole payload is evidence


class TestForm4Parse:
    from datetime import date as _date

    FA = FETCHED_AT
    GRANT = (FIXTURES / "edgar" / "form4-CF-grant-2026-05-28.xml").read_text()

    _P_BUY = (
        "<ownershipDocument><reportingOwner><reportingOwnerId>"
        "<rptOwnerName>Jane Buyer</rptOwnerName></reportingOwnerId>"
        "<reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>0</isOfficer>"
        "</reportingOwnerRelationship></reportingOwner><nonDerivativeTable>"
        "<nonDerivativeTransaction><transactionDate><value>2026-07-10</value></transactionDate>"
        "<transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>"
        "<transactionAmounts><transactionShares><value>5000</value></transactionShares>"
        "<transactionPricePerShare><value>42.50</value></transactionPricePerShare>"
        "<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>"
        "</transactionAmounts></nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"
    )

    def _parse(self, xml, ticker="NVDA"):
        from argus.sources.edgar import parse_form4

        return parse_form4(xml, ticker, "0000-00-1", date(2026, 7, 10), Source.EDGAR, self.FA)

    def test_real_grant_yields_no_buys(self):
        # CF's recorded Form 4 was a code-A grant by the CFO — not a buy.
        assert self._parse(self.GRANT, "CF") == []

    def test_open_market_purchase_is_captured(self):
        [buy] = self._parse(self._P_BUY.format(code="P"))
        assert buy.owner == "Jane Buyer"
        assert buy.role == "director"
        assert buy.shares == 5000.0
        assert buy.price == pytest.approx(42.5)
        assert buy.transaction_date == date(2026, 7, 10)
        assert buy.source is Source.EDGAR

    @pytest.mark.parametrize("code", ["S", "A", "M", "G"])
    def test_non_purchase_codes_are_filtered(self, code):
        assert self._parse(self._P_BUY.format(code=code)) == []

    def test_malformed_xml_never_raises(self):
        assert self._parse("<ownershipDocument><broken") == []

    def test_recent_form4s_windows_and_caps(self):
        from datetime import date as d
        from datetime import timedelta

        from argus.sources.edgar import _FORM4_CAP, _recent_form4s

        today = datetime.now(UTC).date()
        recent = {"form": [], "accessionNumber": [], "filingDate": [], "primaryDocument": []}
        # one old Form 4 (excluded), then many recent (capped)
        recent["form"].append("4"); recent["accessionNumber"].append("old")
        recent["filingDate"].append((today - timedelta(days=400)).isoformat())
        recent["primaryDocument"].append("x/old.xml")
        for i in range(_FORM4_CAP + 5):
            recent["form"].append("4"); recent["accessionNumber"].append(f"a{i}")
            recent["filingDate"].append(today.isoformat())
            recent["primaryDocument"].append(f"xslF345X06/f{i}.xml")
        recent["form"].append("10-K"); recent["accessionNumber"].append("k")
        recent["filingDate"].append(today.isoformat()); recent["primaryDocument"].append("k.htm")
        got = _recent_form4s({"filings": {"recent": recent}})
        assert len(got) == _FORM4_CAP  # capped
        assert all(acc != "old" and acc != "k" for acc, _d, _doc in got)  # windowed + form-filtered


# --- Live smoke (excluded by default via pytest addopts) ---------------------


@pytest.mark.live
class TestLiveSmoke:
    def test_yahoo_live(self):
        result = YahooSource().fetch("AAPL")
        assert any(o.field is Field.PRICE for o in result.observations)
        # AAPL always has reported quarters; every record carries an actual.
        assert result.earnings_results
        assert all(r.eps_actual is not None for r in result.earnings_results)

    def test_finnhub_live(self):
        api_key = os.environ.get("FINNHUB_API_KEY")
        if not api_key:
            pytest.skip("FINNHUB_API_KEY not set")
        result = FinnhubSource(api_key=api_key).fetch("AAPL")
        assert any(o.field is Field.PRICE for o in result.observations)

    def test_edgar_live(self):
        contact = os.environ.get("ARGUS_CONTACT_EMAIL", "invocation.dev@gmail.com")
        source = EdgarSource(contact_email=contact)
        assert source.covers("AAPL")
        assert not source.covers("NTDOY")  # OTC ADR: must be not-applicable
        result = source.fetch("AAPL")
        assert result.observations  # margins and/or debt-to-equity

    def test_fred_live(self):
        result = FredSource({"UNRATE": "level"}).fetch("UNRATE")
        [obs] = result.observations
        assert obs.field is Field.ECON_VALUE
        assert 0 < obs.value_num < 30  # an unemployment rate, not an index level
        assert obs.observed_at is not None
