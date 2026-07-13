"""SEC EDGAR companyfacts — the official fundamentals cross-check.

Free and authoritative for US filers, including 20-F foreign filers (ASML).
NOT available for OTC ADRs or ETFs — covers() must say so up front, so the
digest reports "not applicable", never "error". Symbology uses dashes
(BRK-B). Requests require a User-Agent header carrying a contact email.

Deliberately not a taxonomy engine: three ratios from five us-gaap tags,
best-effort. A missing tag means that field is absent — EDGAR is a
cross-check, and absence is disclosed elsewhere in the pipeline.
"""

from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import AwareDatetime

from argus import __version__
from argus.fields import Field, Source
from argus.models import RawObservation
from argus.sources.base import FetchResult, SourceError

_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

_ANNUAL_FORMS = frozenset({"10-K", "20-F"})
# A fiscal year is ~365 days (52/53-week calendars drift a little); the window
# keeps quarterly periods that also appear inside annual filings out of the
# margin math.
_ANNUAL_DAYS = (300, 400)

# Merged as a union, later tags overriding on the same period end: legacy
# filings sit under Revenues, post-ASC-606 filers (Apple among them) report
# under the contract tag — taking the first non-empty tag would compute
# margins from a years-stale revenue series.
_REVENUE_TAGS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")

# Yahoo's debtToEquity is TOTAL DEBT / equity — comparing Liabilities/equity
# against it is a different ratio (~2x on healthy names) and would false-
# quarantine D/E on every EDGAR-covered ticker. Total debt, best effort:
# the combined tag when filed; else LongTermDebt (which includes current
# maturities) or its noncurrent+current parts, plus short-term borrowings.
# The 25% cross-source tolerance absorbs the definitional noise that remains.
_DEBT_COMBINED_TAG = "DebtLongtermAndShorttermCombinedAmount"
_DEBT_SHORT_TAGS = ("ShortTermBorrowings", "CommercialPaper")


class EdgarSource:
    source_id = Source.EDGAR

    def __init__(self, contact_email: str) -> None:
        self.contact_email = contact_email  # SEC requires it in the User-Agent
        self._cik_by_ticker: dict[str, str] | None = None  # lazy; one fetch per process

    def covers(self, ticker: str) -> bool:
        """True only for tickers with a resolvable CIK that file with the SEC
        (excludes OTC ADRs and ETFs). Backed by the SEC ticker→CIK mapping."""
        return _normalize(ticker) in self._cik_map()

    def fetch(self, ticker: str) -> FetchResult:
        fetched_at = datetime.now(UTC)  # stamped once, before touching the wire
        try:
            payload = self._fetch_raw(ticker)
        except Exception:  # free feeds hiccup: one inline retry, no framework
            try:
                payload = self._fetch_raw(ticker)
            except Exception as exc:
                raise SourceError(f"edgar: fetch failed for {ticker}: {exc}") from exc
        return self.parse(payload, ticker, fetched_at)

    def _fetch_raw(self, ticker: str) -> Any:
        """Network only: companyfacts JSON for the resolved CIK.
        Recorded payloads live in tests/fixtures/edgar/."""
        cik = self._cik_map().get(_normalize(ticker))
        if cik is None:
            raise SourceError(f"edgar: no CIK for {ticker} — covers() should have excluded it")
        return self._get_json(_COMPANYFACTS_URL.format(cik=cik))

    def parse(self, payload: Any, ticker: str, fetched_at: AwareDatetime) -> FetchResult:
        """Pure. Best-effort fundamentals cross-check from us-gaap annual
        (10-K/20-F) facts: GROSS_MARGIN = GrossProfit / revenue and
        OPERATING_MARGIN = OperatingIncomeLoss / the same revenue series for
        the latest matching fiscal year; DEBT_TO_EQUITY = total debt /
        StockholdersEquity at the latest common instant (matching Yahoo's
        ratio definition). observed_at is the period end as aware-UTC
        midnight. Any tag missing → field absent."""
        gaap = _us_gaap(payload)
        revenue: dict[date, float] = {}
        for tag in _REVENUE_TAGS:  # union; later (ASC-606) tags win same-end conflicts
            revenue.update(_annual_durations(gaap.get(tag)))
        gross = _annual_durations(gaap.get("GrossProfit"))
        operating = _annual_durations(gaap.get("OperatingIncomeLoss"))
        debt = _total_debt(gaap)
        equity = _annual_instants(gaap.get("StockholdersEquity"))

        observations: list[RawObservation] = []
        for field, numerators, denominators in (
            (Field.GROSS_MARGIN, gross, revenue),
            (Field.OPERATING_MARGIN, operating, revenue),
            (Field.DEBT_TO_EQUITY, debt, equity),
        ):
            obs = self._ratio(field, numerators, denominators, ticker, fetched_at)
            if obs is not None:
                observations.append(obs)
        return FetchResult(observations=tuple(observations))

    def _ratio(
        self,
        field: Field,
        numerators: dict[date, float],
        denominators: dict[date, float],
        ticker: str,
        fetched_at: AwareDatetime,
    ) -> RawObservation | None:
        """Latest period end where numerator and denominator align (and the
        denominator is nonzero — a ratio over zero is not computable, and
        that absence is disclosed by the digest's tri-state, not here)."""
        common = [end for end in numerators.keys() & denominators.keys() if denominators[end] != 0]
        if not common:
            return None
        end = max(common)
        return RawObservation(
            ticker=ticker,
            field=field,
            value_num=numerators[end] / denominators[end],
            source=self.source_id,
            fetched_at=fetched_at,
            observed_at=datetime(end.year, end.month, end.day, tzinfo=UTC),
        )

    def _cik_map(self) -> dict[str, str]:
        if self._cik_by_ticker is None:
            try:
                payload = self._get_json(_COMPANY_TICKERS_URL)
            except Exception as exc:
                raise SourceError(f"edgar: ticker→CIK mapping fetch failed: {exc}") from exc
            self._cik_by_ticker = _build_cik_map(payload)
        return self._cik_by_ticker

    def _get_json(self, url: str) -> Any:
        response = httpx.get(
            url,
            headers={"User-Agent": f"argus/{__version__} {self.contact_email}"},
            timeout=10.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.json()


def _normalize(ticker: str) -> str:
    """SEC symbology uses dashes for share classes (BRK-B, as does the rest of
    Argus); accept the dotted form too."""
    return ticker.strip().upper().replace(".", "-")


def _build_cik_map(payload: Any) -> dict[str, str]:
    """company_tickers.json ({"0": {"cik_str": ..., "ticker": ...}, ...}) →
    {normalized ticker: zero-padded 10-digit CIK}."""
    mapping: dict[str, str] = {}
    if not isinstance(payload, dict):
        return mapping
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        ticker, cik = entry.get("ticker"), entry.get("cik_str")
        if isinstance(ticker, str) and ticker and isinstance(cik, int):
            mapping[_normalize(ticker)] = f"{cik:010d}"
    return mapping


def _us_gaap(payload: Any) -> dict[str, Any]:
    facts = payload.get("facts") if isinstance(payload, dict) else None
    gaap = facts.get("us-gaap") if isinstance(facts, dict) else None
    return gaap if isinstance(gaap, dict) else {}


def _annual_durations(fact: Any) -> dict[date, float]:
    """Annual (10-K/20-F) duration facts as {period end: value}. Entries are
    filed-date ordered in companyfacts, so a later filing for the same period
    end (a restatement) overwrites the earlier value."""
    out: dict[date, float] = {}
    lo, hi = _ANNUAL_DAYS
    for entry in _entries(fact):
        if entry.get("form") not in _ANNUAL_FORMS:
            continue
        end = _iso_date(entry.get("end"))
        start = _iso_date(entry.get("start"))
        val = entry.get("val")
        if end is None or start is None or not _is_number(val):
            continue
        if not lo <= (end - start).days <= hi:
            continue  # quarterly period reported inside an annual filing
        out[end] = float(val)
    return out


def _total_debt(gaap: dict[str, Any]) -> dict[date, float]:
    """Total debt per period end, matching Yahoo's debtToEquity numerator.
    Prefer the explicitly-combined tag; else long-term debt (the LongTermDebt
    tag includes current maturities; otherwise sum its noncurrent + current
    parts) plus short-term borrowings where filed at the same instant."""
    combined = _annual_instants(gaap.get(_DEBT_COMBINED_TAG))
    if combined:
        return combined
    long_term = _annual_instants(gaap.get("LongTermDebt"))
    if not long_term:
        long_term = _sum_instants(
            _annual_instants(gaap.get("LongTermDebtNoncurrent")),
            _annual_instants(gaap.get("LongTermDebtCurrent")),
        )
    if not long_term:
        return {}
    short = {}
    for tag in _DEBT_SHORT_TAGS:
        short = _sum_instants(short, _annual_instants(gaap.get(tag)))
    return {end: value + short.get(end, 0.0) for end, value in long_term.items()}


def _sum_instants(a: dict[date, float], b: dict[date, float]) -> dict[date, float]:
    """Pointwise sum; a date present in only one input keeps that value."""
    return {end: a.get(end, 0.0) + b.get(end, 0.0) for end in a.keys() | b.keys()}


def _annual_instants(fact: Any) -> dict[date, float]:
    """Balance-sheet (instant) facts from annual filings as {end: value};
    instants carry no `start` key."""
    out: dict[date, float] = {}
    for entry in _entries(fact):
        if entry.get("form") not in _ANNUAL_FORMS or "start" in entry:
            continue
        end = _iso_date(entry.get("end"))
        val = entry.get("val")
        if end is None or not _is_number(val):
            continue
        out[end] = float(val)
    return out


def _entries(fact: Any) -> list[dict[str, Any]]:
    """One tag's fact entries. Prefer USD; otherwise the first unit key sorted
    — a filer reports its monetary tags in one currency and every field here
    is a ratio, so the unit cancels between numerator and denominator."""
    if not isinstance(fact, dict):
        return []
    units = fact.get("units")
    if not isinstance(units, dict) or not units:
        return []
    key = "USD" if "USD" in units else sorted(units)[0]
    entries = units.get(key)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _iso_date(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _is_number(raw: Any) -> bool:
    return isinstance(raw, (int, float)) and not isinstance(raw, bool)
