"""Finnhub free tier — price cross-check, plus the market earnings calendar.

60 req/min with a free API key; ample at watchlist scale. Deliberately
narrow: its per-ticker job is corroborating (or contradicting) Yahoo's price
so the cross-source gate has a second leg. The calendar method is a separate
seam feeding the bellwether context section — CLAIMS-labeled display data
(one unofficial source), never gated, never observations.
"""

import math
from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import AwareDatetime

from argus.fields import Field, Source
from argus.models import BellwetherEarning, ParseFailure, RawObservation
from argus.sources.base import FetchResult, SourceError

_QUOTE_URL = "https://finnhub.io/api/v1/quote"
_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/earnings"


class FinnhubSource:
    source_id = Source.FINNHUB

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def covers(self, ticker: str) -> bool:
        return True  # quote endpoint covered all spike tickers

    def fetch(self, ticker: str) -> FetchResult:
        fetched_at = datetime.now(UTC)  # stamped once, before touching the wire
        try:
            payload = self._fetch_raw(ticker)
        except Exception:  # free feeds hiccup: one inline retry, no framework
            try:
                payload = self._fetch_raw(ticker)
            except Exception as exc:
                raise SourceError(f"finnhub: fetch failed for {ticker}: {exc}") from exc
        return self.parse(payload, ticker, fetched_at)

    def _fetch_raw(self, ticker: str) -> Any:
        """Network only: /quote. Recorded payloads in tests/fixtures/finnhub/."""
        response = httpx.get(
            _QUOTE_URL, params={"symbol": ticker, "token": self.api_key}, timeout=10.0
        )
        response.raise_for_status()
        return response.json()

    def parse(self, payload: Any, ticker: str, fetched_at: AwareDatetime) -> FetchResult:
        """Pure. Maps current price (`c`) → Field.PRICE with the quote's own
        timestamp (`t`) as observed_at — the staleness gate's evidence.
        Finnhub reports c=0 for symbols it does not know: that is "no data",
        not a zero price, so it maps to absent (never a zero observation)."""
        if not isinstance(payload, dict):
            return FetchResult()
        raw_price = payload.get("c")
        if raw_price is None:
            return FetchResult()
        if isinstance(raw_price, bool):  # float(True) == 1.0 would launder garbage
            return FetchResult(parse_failures=(self._failure(raw_price, ticker, fetched_at),))
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            return FetchResult(parse_failures=(self._failure(raw_price, ticker, fetched_at),))
        if price == 0:
            return FetchResult()  # Finnhub's unknown-symbol convention
        observation = RawObservation(
            ticker=ticker,
            field=Field.PRICE,
            value_num=price,
            source=self.source_id,
            fetched_at=fetched_at,
            observed_at=_epoch_to_utc(payload.get("t")),
        )
        return FetchResult(observations=(observation,))

    def earnings_calendar(self, frm: date, to: date) -> list[BellwetherEarning]:
        """The whole market's earnings window in one GET (verified free-tier
        2026-07-15) — the caller filters to its bellwether list. Raises
        SourceError on wholesale failure, same policy as fetch()."""
        try:
            payload = self._fetch_calendar(frm, to)
        except Exception:  # free feeds hiccup: one inline retry, no framework
            try:
                payload = self._fetch_calendar(frm, to)
            except Exception as exc:
                raise SourceError(f"finnhub: earnings calendar failed: {exc}") from exc
        return parse_earnings_calendar(payload)

    def _fetch_calendar(self, frm: date, to: date) -> Any:
        """Network only. Recorded payloads in tests/fixtures/finnhub/."""
        response = httpx.get(
            _CALENDAR_URL,
            params={"from": frm.isoformat(), "to": to.isoformat(), "token": self.api_key},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()

    def _failure(self, raw: Any, ticker: str, fetched_at: AwareDatetime) -> ParseFailure:
        return ParseFailure(
            ticker=ticker,
            field=Field.PRICE,
            raw=str(raw),
            source=self.source_id,
            fetched_at=fetched_at,
        )


def parse_earnings_calendar(payload: Any) -> list[BellwetherEarning]:
    """Pure. Tolerant per row — this is claims-labeled context, not gated
    data: a row without a readable symbol/date is skipped, numbers that are
    not clean finite floats become None."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("earningsCalendar")
    if not isinstance(rows, list):
        return []
    out: list[BellwetherEarning] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        raw_date = row.get("date")
        if not isinstance(symbol, str) or not symbol or not isinstance(raw_date, str):
            continue
        try:
            report_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        hour = row.get("hour")
        out.append(
            BellwetherEarning(
                symbol=symbol,
                report_date=report_date,
                hour=hour if isinstance(hour, str) else "",
                eps_estimate=_clean_number(row.get("epsEstimate")),
                eps_actual=_clean_number(row.get("epsActual")),
                revenue_estimate=_clean_number(row.get("revenueEstimate")),
                revenue_actual=_clean_number(row.get("revenueActual")),
            )
        )
    return out


def _clean_number(raw: Any) -> float | None:
    """int/float → finite float; bool/NaN/inf/anything else → None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def _epoch_to_utc(raw: Any) -> datetime | None:
    """Quote timestamps arrive as epoch seconds; t=0 accompanies c=0 for
    unknown symbols, so only a positive value counts as evidence."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
