"""Yahoo Finance via yfinance — the primary source for every field.

Unofficial API: breakage is priced in, which is why this file is the blast
radius. Covered ALL Phase-0 spike tickers including OTC ADRs (NTDOY/TCEHY/
NSRGY), BRK-B, and ETFs; `upgrades_downgrades` provides the dated per-firm
rating-change history that feeds analyst_actions.
"""

import re
from datetime import UTC, date, datetime
from typing import Any

from pydantic import AwareDatetime

from argus.fields import Field, Source
from argus.models import AnalystActionRecord, CompanyProfile, ParseFailure, RawObservation
from argus.sources.base import FetchResult, SourceError

# info-key → (Field, divisor). yfinance already reports margins as fractions
# (pass through) but debtToEquity as a percent (÷100 to the stored ratio).
# PRICE is handled separately: it has a fallback key and carries observed_at.
_NUM_FIELDS: tuple[tuple[str, Field, float], ...] = (
    ("marketCap", Field.MARKET_CAP, 1.0),
    ("totalRevenue", Field.REVENUE, 1.0),
    ("revenueGrowth", Field.REVENUE_GROWTH, 1.0),  # fraction, like the margins
    ("trailingPE", Field.PE_TTM, 1.0),
    ("forwardPE", Field.PE_FWD, 1.0),
    ("trailingPegRatio", Field.PEG, 1.0),
    ("grossMargins", Field.GROSS_MARGIN, 1.0),
    ("operatingMargins", Field.OPERATING_MARGIN, 1.0),
    ("returnOnEquity", Field.ROE, 1.0),  # fraction, like the margins
    ("debtToEquity", Field.DEBT_TO_EQUITY, 100.0),
    ("totalCash", Field.TOTAL_CASH, 1.0),
    ("totalDebt", Field.TOTAL_DEBT, 1.0),
    ("enterpriseToEbitda", Field.EV_EBITDA, 1.0),
    ("dividendYield", Field.DIVIDEND_YIELD, 100.0),  # yfinance reports percent (verified live)
    ("beta", Field.BETA, 1.0),
    ("targetMeanPrice", Field.ANALYST_TARGET_MEAN, 1.0),
    ("numberOfAnalystOpinions", Field.ANALYST_COUNT, 1.0),
)

# _fetch_raw stringifies calendar values (str() over the raw dict) so payloads
# round-trip through JSON fixtures — the earnings date therefore arrives as
# "[datetime.date(2026, 8, 6)]" and must be recovered from that repr.
_DATE_REPR = re.compile(r"datetime\.date\((\d{4}),\s*(\d{1,2}),\s*(\d{1,2})\)")


class _UnreadableValue(ValueError):
    """Present but not parseable — becomes a ParseFailure, never an absence."""


class YahooSource:
    source_id = Source.YAHOO

    def covers(self, ticker: str) -> bool:
        return True  # broadest coverage of the three; the spike found no gaps

    def fetch(self, ticker: str) -> FetchResult:
        fetched_at = datetime.now(UTC)  # stamped once, before touching the wire
        try:
            payload = self._fetch_raw(ticker)
        except Exception:  # free feeds hiccup: one inline retry, no framework
            try:
                payload = self._fetch_raw(ticker)
            except Exception as exc:
                raise SourceError(f"yahoo: fetch failed for {ticker}: {exc}") from exc
        return self.parse(payload, ticker, fetched_at)

    def _fetch_raw(self, ticker: str) -> dict[str, Any]:
        """Network only: yfinance Ticker info + calendar + upgrades_downgrades.
        Recorded payloads live in tests/fixtures/yahoo/."""
        import yfinance  # lazy: keep module import free of network side effects

        t = yfinance.Ticker(ticker)
        info = dict(t.info or {})
        frame = t.upgrades_downgrades
        if frame is None or frame.empty:
            records = None
        else:
            records = frame.reset_index().to_dict("records")
            for record in records:  # Timestamp index → ISO string (JSON-safe)
                grade_date = record.get("GradeDate")
                if hasattr(grade_date, "isoformat"):
                    record["GradeDate"] = grade_date.isoformat()
        calendar = {str(k): str(v) for k, v in (t.calendar or {}).items()}
        return {"info": info, "upgrades_downgrades": records, "calendar": calendar}

    def parse(self, payload: Any, ticker: str, fetched_at: AwareDatetime) -> FetchResult:
        """Pure. Maps yfinance keys to Fields with unit normalization:
        margins stay fractions, debtToEquity percent → ratio, quote
        timestamps → observed_at, upgrades_downgrades rows →
        AnalystActionRecord. Values present but unreadable become
        ParseFailure, never silent absences; implausible values pass
        through untouched — gates judge, adapters only normalize."""
        info = _subdict(payload, "info")
        calendar = _subdict(payload, "calendar")

        observations: list[RawObservation] = []
        failures: list[ParseFailure] = []
        actions: list[AnalystActionRecord] = []

        def emit_num(
            field: Field, raw: Any, divisor: float = 1.0, observed_at: datetime | None = None
        ) -> None:
            if isinstance(raw, bool):  # float(True) == 1.0 would launder garbage
                failures.append(_failure(field, raw, ticker, fetched_at))
                return
            try:
                value = float(raw)
            except (TypeError, ValueError):
                failures.append(_failure(field, raw, ticker, fetched_at))
                return
            observations.append(
                RawObservation(
                    ticker=ticker,
                    field=field,
                    value_num=value / divisor,
                    source=self.source_id,
                    fetched_at=fetched_at,
                    observed_at=observed_at,
                )
            )

        price_raw = info.get("currentPrice")
        if price_raw is None:
            price_raw = info.get("regularMarketPrice")
        if price_raw is not None:
            emit_num(Field.PRICE, price_raw, observed_at=_epoch_to_utc(info.get("regularMarketTime")))

        for key, field, divisor in _NUM_FIELDS:
            raw = info.get(key)
            if raw is not None:
                emit_num(field, raw, divisor)

        # FCF margin is derived (freeCashflow / totalRevenue) — the same
        # ratio-from-tags precedent as the EDGAR adapter. freeCashflow has no
        # field of its own, so a present-but-garbled value must become a
        # ParseFailure HERE (hard rule 2) — the revenue leg gets its failure
        # via Field.REVENUE's normal path.
        fcf_raw = info.get("freeCashflow")
        revenue_raw = info.get("totalRevenue")
        fcf_clean = isinstance(fcf_raw, (int, float)) and not isinstance(fcf_raw, bool)
        if fcf_raw is not None and not fcf_clean:
            failures.append(_failure(Field.FCF_MARGIN, fcf_raw, ticker, fetched_at))
        elif (
            fcf_clean
            and isinstance(revenue_raw, (int, float))
            and not isinstance(revenue_raw, bool)
            and revenue_raw > 0
        ):
            emit_num(Field.FCF_MARGIN, fcf_raw / revenue_raw)

        rating = info.get("recommendationKey")
        if isinstance(rating, str) and rating:
            observations.append(
                RawObservation(
                    ticker=ticker,
                    field=Field.ANALYST_RATING,
                    value_text=rating,
                    source=self.source_id,
                    fetched_at=fetched_at,
                )
            )
        elif rating is not None and not isinstance(rating, str):
            failures.append(_failure(Field.ANALYST_RATING, rating, ticker, fetched_at))

        raw_earnings = calendar.get("Earnings Date")
        if raw_earnings is not None:
            try:
                earnings = _first_earnings_date(raw_earnings)
            except _UnreadableValue:
                failures.append(_failure(Field.NEXT_EARNINGS_DATE, raw_earnings, ticker, fetched_at))
            else:
                if earnings is not None:
                    observations.append(
                        RawObservation(
                            ticker=ticker,
                            field=Field.NEXT_EARNINGS_DATE,
                            value_date=earnings,
                            source=self.source_id,
                            fetched_at=fetched_at,
                        )
                    )

        records = payload.get("upgrades_downgrades") if isinstance(payload, dict) else None
        if isinstance(records, (list, tuple)):
            malformed: list[Any] = []
            for record in records:
                if _is_out_of_scope_row(record):
                    # Not malformed — a shape we deliberately don't model:
                    # price-target-only changes, and historical (2015-16 era)
                    # rows without a destination grade, which our action
                    # model cannot even key. Quarantining those forever, on
                    # every run, erodes the quarantine section's credibility
                    # (observed live: 4 of 5 noise rows).
                    continue
                parsed = _parse_action(record, ticker, self.source_id, fetched_at)
                if parsed is None:
                    malformed.append(record)
                else:
                    actions.append(parsed)
            if malformed:
                # Records the source sent but we could not read are evidence,
                # not an absence (hard rule 2) — surfaced on ANALYST_RATING,
                # the field the history belongs to. Aggregated into ONE
                # compact failure: a count plus a terse fingerprint informs;
                # a raw dict dump (or hundreds of rows) trains the reader to
                # skim the quarantine section.
                failures.append(
                    _failure(
                        Field.ANALYST_RATING,
                        f"{len(malformed)} unreadable analyst-history row(s), "
                        f"e.g. {_fingerprint(malformed[0])}",
                        ticker,
                        fetched_at,
                    )
                )

        return FetchResult(
            observations=tuple(observations),
            parse_failures=tuple(failures),
            analyst_actions=tuple(actions),
            profile=_profile(info, ticker, self.source_id, fetched_at),
        )


def _profile(
    info: dict[str, Any], ticker: str, source: Source, fetched_at: AwareDatetime
) -> CompanyProfile | None:
    """Descriptive identity from the info payload — rendered verbatim in
    reports, never gated (no plausibility bounds exist for prose)."""

    def clean(raw: Any) -> str | None:
        return raw if isinstance(raw, str) and raw.strip() else None

    name = clean(info.get("longName")) or clean(info.get("shortName"))
    sector = clean(info.get("sector"))
    industry = clean(info.get("industry"))
    summary = clean(info.get("longBusinessSummary"))
    employees_raw = info.get("fullTimeEmployees")
    employees = (
        employees_raw
        if isinstance(employees_raw, int) and not isinstance(employees_raw, bool) and employees_raw > 0
        else None
    )
    if not any((name, sector, industry, summary)):
        return None
    return CompanyProfile(
        ticker=ticker,
        name=name,
        sector=sector,
        industry=industry,
        employees=employees,
        summary=summary[:2000] if summary else None,
        source=source,
        fetched_at=fetched_at,
    )


def fetch_annual_revenue(ticker: str, years: int = 5) -> list[tuple[int, float]] | None:
    """Annual revenue points for the PDF's revenue chart. UNGATED display
    data, same policy as fetch_history — never enters the store."""
    try:
        import yfinance as yf

        statement = yf.Ticker(ticker).income_stmt
        row = statement.loc["Total Revenue"]
        points = sorted(
            (timestamp.year, float(value))
            for timestamp, value in row.items()
            if value == value  # NaN guard
        )
        return points[-years:] or None
    except Exception:
        return None


def _failure(field: Field, raw: Any, ticker: str, fetched_at: AwareDatetime) -> ParseFailure:
    return ParseFailure(
        ticker=ticker, field=field, raw=str(raw), source=Source.YAHOO, fetched_at=fetched_at
    )


def _subdict(payload: Any, key: str) -> dict[str, Any]:
    sub = payload.get(key) if isinstance(payload, dict) else None
    return sub if isinstance(sub, dict) else {}


def _epoch_to_utc(raw: Any) -> datetime | None:
    """Quote timestamps arrive as epoch seconds. A missing or unreadable one
    just means no observed_at — the staleness gate skips without evidence."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _first_earnings_date(raw: Any) -> date | None:
    """First entry of the calendar's Earnings Date, in any of the shapes the
    wire (or a recorded fixture of it) produces: a date, a list of dates, an
    ISO string, or the stringified list repr. Raises _UnreadableValue when
    something is there but cannot be read."""
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if text in ("", "None", "[]"):
            return None  # the source reported "nothing scheduled"
        match = _DATE_REPR.search(text)
        if match:
            try:
                return date(int(match[1]), int(match[2]), int(match[3]))
            except ValueError as exc:
                raise _UnreadableValue(text) from exc
        try:
            return datetime.fromisoformat(text).date()  # accepts date-only too
        except ValueError as exc:
            raise _UnreadableValue(text) from exc
    raise _UnreadableValue(str(raw))


def _fingerprint(record: Any) -> str:
    """A terse, human-scannable identity for an unreadable history row —
    year + firm — instead of a raw dict dump in the quarantine table."""
    if not isinstance(record, dict):
        return f"a {type(record).__name__} row"
    raw_date = record.get("GradeDate")
    year = str(raw_date)[:4] if raw_date else "undated"
    firm = record.get("Firm")
    firm_text = firm if isinstance(firm, str) and firm else "unattributed"
    return f"{year} row from {firm_text}"


def _is_out_of_scope_row(record: Any) -> bool:
    """yfinance mixes shapes into upgrades_downgrades that are not grade
    actions: price-target-change rows, and old rows with no destination
    grade at all. Our AnalystActionRecord requires to_grade (it is part of
    the natural key) — a row without one is a different event type, not a
    broken instance of ours. Non-dict garbage stays malformed."""
    if not isinstance(record, dict):
        return False
    to_grade = record.get("ToGrade")
    return not isinstance(to_grade, str) or not to_grade


def _parse_action(
    record: Any, ticker: str, source: Source, fetched_at: AwareDatetime
) -> AnalystActionRecord | None:
    """One upgrades_downgrades row → AnalystActionRecord; None when the record
    is missing a required key (the caller turns None into a ParseFailure)."""
    if not isinstance(record, dict):
        return None
    action_date = _grade_date(record.get("GradeDate"))
    firm = record.get("Firm")
    action = record.get("Action")
    to_grade = record.get("ToGrade")
    if (
        action_date is None
        or not (isinstance(firm, str) and firm)
        or not (isinstance(action, str) and action)
        or not (isinstance(to_grade, str) and to_grade)
    ):
        return None
    from_grade = record.get("FromGrade")
    return AnalystActionRecord(
        ticker=ticker,
        action_date=action_date,
        firm=firm,
        action=action,
        from_grade=from_grade if isinstance(from_grade, str) and from_grade else None,
        to_grade=to_grade,
        source=source,
        fetched_at=fetched_at,
    )


def _grade_date(raw: Any) -> date | None:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            return None
    return None


def fetch_history(ticker: str, period: str = "1y") -> list[tuple[date, float]] | None:
    """Daily closes for the PDF report's charts. UNGATED display data — it
    never enters the store, and the PDF captions it as raw. None on any
    failure: a chart is optional, the digest is not."""
    try:
        import yfinance as yf

        frame = yf.Ticker(ticker).history(period=period, auto_adjust=True)
        return _closes(frame)
    except Exception:
        return None


def fetch_price_series(ticker: str, start: date) -> list[tuple[date, float]] | None:
    """Adjusted daily closes from `start` to now — the scout scorecard's price
    input. UNGATED (realized market data, never stored as observations);
    adjusted so total return includes dividends and splits on both endpoints.
    None on any failure — a name that can't be priced is counted as
    unpriceable, never folded into the aggregate as a zero."""
    try:
        import yfinance as yf

        frame = yf.Ticker(ticker).history(start=start.isoformat(), auto_adjust=True)
        return _closes(frame)
    except Exception:
        return None


def _closes(frame: Any) -> list[tuple[date, float]] | None:
    closes = frame["Close"]
    points = [
        (index.date(), float(value))
        for index, value in closes.items()
        if value == value  # NaN guard
    ]
    return points or None
