"""The adapter seam. Free feeds break — a dead or replaced source must be a
one-module fix, so every external fetch goes through this Protocol.

Each adapter splits into a thin `_fetch_raw()` (network only) and a pure
`parse(payload, ticker, fetched_at)` tested against recorded fixtures.
Adapters stamp `fetched_at` themselves and pass source-reported data
timestamps through as `observed_at`. Adapters never assign verdicts —
RawObservation has no verdict field, and only gates.py can construct
GatedObservation.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from argus.fields import Source
from argus.models import AnalystActionRecord, ParseFailure, RawObservation


class SourceError(RuntimeError):
    """A source failed wholesale (network, auth, breaking API change). The
    engine records it as a run_sources error row; other sources and tickers
    are unaffected."""


class FetchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    observations: tuple[RawObservation, ...] = ()
    parse_failures: tuple[ParseFailure, ...] = ()  # sent-but-unreadable → UNPARSEABLE quarantine
    analyst_actions: tuple[AnalystActionRecord, ...] = ()


@runtime_checkable
class DataSource(Protocol):
    source_id: Source

    def covers(self, ticker: str) -> bool:
        """Whether this source carries this ticker at all — checked BEFORE
        fetching, so 'not applicable' is never conflated with 'error'."""
        ...

    def fetch(self, ticker: str) -> FetchResult:
        """Fetch and parse everything this source offers for one ticker.
        Raises SourceError on wholesale failure; per-field problems come back
        as parse_failures, never as dropped values."""
        ...
