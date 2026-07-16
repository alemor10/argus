"""FRED (St. Louis Fed) — economic series for the macro watch.

Keyless path: the public fredgraph.csv chart-data endpoint (verified live
2026-07-15: CPIAUCSL, UNRATE, PAYEMS, DFF all current) — unofficial-but-free,
accepted eyes-open the same way yfinance and the TV scanner are, with this
one module as the blast radius. The keyed official API (api.stlouisfed.org,
free registration) is the upgrade path if the chart endpoint changes.

Transforms (`yoy_pct`, `mom_change`) are computed in parse() from points of
the SAME officially published series — the EDGAR-ratio / FCF-margin
precedent: an adapter may derive a value from its own payload; it never
judges plausibility (gates do).

covers() is exactly the set of series macro.yaml configured — FRED is never
consulted for anything else (no wasted calls, no per-run health noise).
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Mapping

import httpx
from pydantic import AwareDatetime

from argus.fields import Field, Source
from argus.models import ParseFailure, RawObservation
from argus.sources.base import FetchResult, SourceError

_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

Transform = Literal["level", "yoy_pct", "mom_change"]

# yoy_pct pairs the latest point with the one closest to a year earlier;
# the window tolerates monthly/weekly/daily calendars without knowing the
# series' frequency (a quarterly series' nearest point is 9 or 12 months
# out — 12 wins the closest-match, 9 stays outside no window needed).
_YOY_TOLERANCE = timedelta(days=45)


class FredSource:
    source_id = Source.FRED

    def __init__(self, series: Mapping[str, Transform]) -> None:
        """`series`: FRED series id → transform, straight from macro.yaml."""
        self.series: dict[str, Transform] = dict(series)

    def covers(self, ticker: str) -> bool:
        return ticker in self.series

    def fetch(self, ticker: str) -> FetchResult:
        fetched_at = datetime.now(UTC)  # stamped once, before touching the wire
        try:
            payload = self._fetch_raw(ticker)
        except Exception:  # free feeds hiccup: one inline retry, no framework
            try:
                payload = self._fetch_raw(ticker)
            except Exception as exc:
                raise SourceError(f"fred: fetch failed for {ticker}: {exc}") from exc
        return self.parse(payload, ticker, fetched_at)

    def _fetch_raw(self, ticker: str) -> str:
        """Network only: the full series CSV (small — even 80 years of CPI is
        ~1,000 rows). Recorded payloads live in tests/fixtures/fred/."""
        response = httpx.get(
            _CSV_URL, params={"id": ticker}, timeout=30.0, follow_redirects=True
        )
        response.raise_for_status()
        return response.text

    def parse(self, payload: Any, ticker: str, fetched_at: AwareDatetime) -> FetchResult:
        """Pure. CSV text → ONE observation: the configured transform of the
        latest point, with observed_at = that point's period date (what
        MacroPrint keys on). FRED encodes missing observations as "." — those
        are absences, not failures; rows that cannot be read at all aggregate
        into one compact ParseFailure (the analyst-history precedent). A
        series too short for its transform yields no observation — absence,
        disclosed by the digest tri-state, never a fabricated number."""
        if not isinstance(payload, str) or not payload.strip():
            return FetchResult(
                parse_failures=(
                    _failure(ticker, f"non-CSV payload: {type(payload).__name__}", fetched_at),
                )
            )
        lines = payload.strip().splitlines()
        header, rows = lines[0], lines[1:]
        if "observation_date" not in header.lower() and "date" not in header.lower():
            return FetchResult(
                parse_failures=(_failure(ticker, f"unrecognized header {header!r}", fetched_at),)
            )
        points: list[tuple[date, float]] = []
        malformed = 0
        first_bad: str | None = None
        for row in rows:
            parts = row.split(",")
            if len(parts) != 2:
                malformed += 1
                first_bad = first_bad or row
                continue
            raw_date, raw_value = parts[0].strip(), parts[1].strip()
            if raw_value in (".", ""):
                # FRED's "no observation for this period" — documented as "."
                # but observed live as an empty cell too (CPIAUCSL 2025-10-01,
                # a data-gap month). Absence, not unreadable — a permanent
                # quarantine artifact would erode the section's credibility.
                continue
            try:
                points.append((date.fromisoformat(raw_date), float(raw_value)))
            except ValueError:
                malformed += 1
                first_bad = first_bad or row
        failures: list[ParseFailure] = []
        if malformed:
            failures.append(
                _failure(
                    ticker, f"{malformed} unreadable CSV row(s), e.g. {first_bad!r}", fetched_at
                )
            )
        value = _apply_transform(points, self.series.get(ticker, "level"))
        observations: tuple[RawObservation, ...] = ()
        if value is not None:
            level, period = value
            observations = (
                RawObservation(
                    ticker=ticker,
                    field=Field.ECON_VALUE,
                    value_num=level,
                    source=self.source_id,
                    fetched_at=fetched_at,
                    observed_at=datetime(period.year, period.month, period.day, tzinfo=UTC),
                ),
            )
        return FetchResult(observations=observations, parse_failures=tuple(failures))


def _apply_transform(
    points: list[tuple[date, float]], transform: Transform
) -> tuple[float, date] | None:
    """(value, period-of-latest-point), or None when the series is too short
    for the transform — an absence, never a guess."""
    if not points:
        return None
    points.sort()
    latest_date, latest_value = points[-1]
    if transform == "level":
        return latest_value, latest_date
    if transform == "mom_change":
        if len(points) < 2:
            return None
        return latest_value - points[-2][1], latest_date
    # yoy_pct: the point closest to one year before the latest, within
    # tolerance; a zero base makes the percent undefined → absent.
    target = latest_date - timedelta(days=365)
    candidates = [
        (abs(day - target), value)
        for day, value in points[:-1]
        if abs(day - target) <= _YOY_TOLERANCE
    ]
    if not candidates:
        return None
    _, base = min(candidates, key=lambda pair: pair[0])
    if base == 0:
        return None
    return (latest_value / base - 1.0) * 100, latest_date


def _failure(ticker: str, detail: str, fetched_at: AwareDatetime) -> ParseFailure:
    return ParseFailure(
        ticker=ticker,
        field=Field.ECON_VALUE,
        raw=detail,
        source=Source.FRED,
        fetched_at=fetched_at,
    )
