"""Finnhub free tier — price cross-check only.

60 req/min with a free API key; ample at watchlist scale. Deliberately
narrow: its one job is corroborating (or contradicting) Yahoo's price so the
cross-source gate has a second leg.
"""

from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import AwareDatetime

from argus.fields import Field, Source
from argus.models import ParseFailure, RawObservation
from argus.sources.base import FetchResult, SourceError

_QUOTE_URL = "https://finnhub.io/api/v1/quote"


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

    def _failure(self, raw: Any, ticker: str, fetched_at: AwareDatetime) -> ParseFailure:
        return ParseFailure(
            ticker=ticker,
            field=Field.PRICE,
            raw=str(raw),
            source=self.source_id,
            fetched_at=fetched_at,
        )


def _epoch_to_utc(raw: Any) -> datetime | None:
    """Quote timestamps arrive as epoch seconds; t=0 accompanies c=0 for
    unknown symbols, so only a positive value counts as evidence."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
