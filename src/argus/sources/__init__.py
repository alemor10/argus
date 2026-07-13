"""Source adapters. Registration is a hand-written tuple, not entry-points —
adding a source (EODHD, a yfinance replacement) is one new module plus one
line here, plus priority entries in fields.SPECS."""

from argus.sources.base import DataSource, FetchResult, SourceError
from argus.sources.edgar import EdgarSource
from argus.sources.finnhub import FinnhubSource
from argus.sources.yahoo import YahooSource

ALL_SOURCE_TYPES: tuple[type, ...] = (YahooSource, EdgarSource, FinnhubSource)

__all__ = [
    "ALL_SOURCE_TYPES",
    "DataSource",
    "EdgarSource",
    "FetchResult",
    "FinnhubSource",
    "SourceError",
    "YahooSource",
]
