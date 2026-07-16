"""Well-known ETF rebalancing — constituent-change detection.

When a name is added to (or dropped from) a major index ETF, index funds are
forced to buy (or sell) it — knowing that a well-known ETF is rebalancing is
signal, and it works whether or not you hold the ETF. Argus watches a
configured set of ETFs, snapshots their membership, and reports the diff:
who entered, who left.

Data sources (verified live 2026-07-16), one adapter per issuer behind the
HoldingsSource protocol; `holdings_source_for` routes each ETF by a known
map:
  - SSGA / State Street SPDR — one uniform daily-holdings xlsx per fund
    (SPY, DIA, the eleven sector SPDRs, SDY, MDY, style/size funds).
  - Vanguard — the investor.vanguard.com portfolio-holding JSON per fund
    (VOO, VTI, VYM, VUG, …), full membership by ticker.
Both accepted eyes-open the same way yfinance and the TV scanner are —
unofficial, one-module blast radius behind this file — and both give tickers
directly, so the CUSIP→ticker join that makes N-PORT painful is skipped.
(Schwab blocks headless requests, so SCHD is not routable; SDY/VYM are the
dividend-strategy stand-ins.) Holdings are CLAIMS: never gated, never
observations, never a delivery trigger by themselves; only the membership
diff (an event) is.

Storage is a change-log, not a daily dump: a holdings snapshot is persisted
only when membership actually changes (the analyst-actions "first-seen"
philosophy). Most days nothing is stored and nothing is reported — silence
is a statement here too.
"""

import io
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import httpx

from argus.models import EtfHolding

_URL = (
    "https://www.ssga.com/us/en/intermediary/etfs/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-{etf}.xlsx"
)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class HoldingsError(RuntimeError):
    """The holdings feed failed wholesale (network, HTTP error, or an xlsx
    shape this module does not recognize). The digest reports the outage —
    silence is a statement — so this must raise, never guess at a changed
    contract."""


@runtime_checkable
class HoldingsSource(Protocol):
    """The ETF-holdings seam. iShares / Vanguard / SEC N-PORT slot in behind
    this later."""

    def fetch(self, etf: str) -> list[EtfHolding]: ...


class SsgaHoldingsSource:
    """One GET of an SSGA SPDR daily-holdings xlsx per ETF."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def fetch(self, etf: str) -> list[EtfHolding]:
        try:
            content = self._fetch_raw(etf)
        except Exception:  # free feeds hiccup: one inline retry, no framework
            try:
                content = self._fetch_raw(etf)
            except Exception as exc:
                raise HoldingsError(f"ssga: holdings fetch failed for {etf}: {exc}") from exc
        return self.parse(content, etf)

    def _fetch_raw(self, etf: str) -> bytes:
        """Network only. Recorded payloads in tests/fixtures/ssga/."""
        response = httpx.get(
            _URL.format(etf=etf.lower()),
            headers={"User-Agent": _USER_AGENT},
            timeout=self.timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "spreadsheet" not in content_type and "officedocument" not in content_type:
            # An HTML consent/404 shell instead of the xlsx — a changed
            # contract, not empty data. Fail loud.
            raise HoldingsError(f"ssga: {etf} returned {content_type or 'unknown'}, not an xlsx")
        return response.content

    def parse(self, content: bytes, etf: str) -> list[EtfHolding]:
        """Pure over the xlsx bytes. The sheet carries a fund-metadata block,
        then a header row (Name, Ticker, …, Weight, …), then constituents.
        A row without a real ticker (cash lines carry '-') is skipped; a
        sheet with no header or zero constituents raises — an empty holdings
        list would read as 'the fund holds nothing', a false statement."""
        import openpyxl

        try:
            workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
        except Exception as exc:
            raise HoldingsError(f"ssga: {etf} xlsx unreadable: {exc}") from exc
        rows = list(workbook.active.iter_rows(values_only=True))
        header_idx = next(
            (i for i, r in enumerate(rows) if r and any(_clean(c) == "Ticker" for c in r)),
            None,
        )
        if header_idx is None:
            raise HoldingsError(f"ssga: {etf} xlsx has no Ticker header — changed shape")
        header = [_clean(c) for c in rows[header_idx]]
        try:
            t_col = header.index("Ticker")
            w_col = header.index("Weight")
        except ValueError as exc:
            raise HoldingsError(f"ssga: {etf} xlsx missing Ticker/Weight columns") from exc
        n_col = header.index("Name") if "Name" in header else None

        holdings: list[EtfHolding] = []
        for row in rows[header_idx + 1 :]:
            if not row or t_col >= len(row):
                continue
            ticker = _normalize(row[t_col])
            if ticker is None:
                continue  # cash / derivative / footer line
            holdings.append(
                EtfHolding(
                    ticker=ticker,
                    weight=_num(row[w_col]) if w_col < len(row) else 0.0,
                    name=_clean(row[n_col]) if n_col is not None and n_col < len(row) else None,
                )
            )
        if not holdings:
            raise HoldingsError(f"ssga: {etf} xlsx parsed to zero constituents")
        return holdings


class VanguardHoldingsSource:
    """One GET of a Vanguard fund's portfolio-holding JSON per ETF."""

    _URL = (
        "https://investor.vanguard.com/investment-products/etfs/profile/api/"
        "{etf}/portfolio-holding/stock"
    )

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def fetch(self, etf: str) -> list[EtfHolding]:
        try:
            payload = self._fetch_raw(etf)
        except Exception:
            try:
                payload = self._fetch_raw(etf)
            except Exception as exc:
                raise HoldingsError(f"vanguard: holdings fetch failed for {etf}: {exc}") from exc
        return self.parse(payload, etf)

    def _fetch_raw(self, etf: str) -> object:
        response = httpx.get(
            self._URL.format(etf=etf.upper()),
            params={"start": 1, "count": 5000},  # one page for any fund
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=self.timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.json()

    def parse(self, payload: object, etf: str) -> list[EtfHolding]:
        """Pure. fund.entity[] carries one row per holding with a ticker and
        percentWeight (a string). Rows without a real ticker (cash, futures)
        are skipped; an unrecognized body or zero constituents raises."""
        if not isinstance(payload, dict):
            raise HoldingsError(f"vanguard: {etf} body is not JSON object")
        entities = payload.get("fund", {}).get("entity")
        if not isinstance(entities, list):
            raise HoldingsError(f"vanguard: {etf} body has no fund.entity list — changed shape")
        holdings: list[EtfHolding] = []
        for row in entities:
            if not isinstance(row, dict):
                continue
            ticker = _normalize(row.get("ticker"))
            if ticker is None:
                continue
            holdings.append(
                EtfHolding(
                    ticker=ticker,
                    weight=_num(row.get("percentWeight")),
                    name=_clean(row.get("longName")) or None,
                )
            )
        if not holdings:
            raise HoldingsError(f"vanguard: {etf} parsed to zero constituents")
        return holdings


# Which issuer feed serves each ETF. Extend as issuers are added; an ETF not
# here is skipped with a disclosed note (Argus never guesses a feed).
_SSGA_ETFS = frozenset(
    "SPY DIA XLK XLF XLE XLV XLI XLY XLP XLU XLB XLRE XLC "
    "SDY MDY SPLG SPYG SPYV SPSM XBI KRE XHB XRT XME XOP".split()
)
_VANGUARD_ETFS = frozenset(
    "VOO VTI VUG VTV VYM VIG VGT VNQ VO VB VBR VEA VWO VXUS VT BND".split()
)


def holdings_source_for(etf: str) -> HoldingsSource | None:
    """The issuer adapter that serves this ETF, or None when Argus has no
    feed for it (disclosed by the caller, never guessed)."""
    symbol = etf.strip().upper()
    if symbol in _SSGA_ETFS:
        return SsgaHoldingsSource()
    if symbol in _VANGUARD_ETFS:
        return VanguardHoldingsSource()
    return None


def membership_diff(
    prev: Sequence[EtfHolding], curr: Sequence[EtfHolding]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(added, dropped) constituent tickers between two membership snapshots —
    a pure set diff, alphabetical for determinism."""
    prev_set = {h.ticker for h in prev}
    curr_set = {h.ticker for h in curr}
    return tuple(sorted(curr_set - prev_set)), tuple(sorted(prev_set - curr_set))


def _clean(raw: object) -> str:
    return str(raw).strip() if raw is not None else ""


def _normalize(raw: object) -> str | None:
    """House symbology (dashes for share classes), or None for a non-ticker
    cell (cash lines carry '-', footers carry prose)."""
    text = _clean(raw).upper().replace(".", "-")
    if not text or text == "-" or " " in text or len(text) > 12:
        return None
    return text


def _num(raw: object) -> float:
    """Weights arrive as floats (SSGA xlsx) or strings (Vanguard JSON)."""
    if isinstance(raw, bool):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value == value and abs(value) != float("inf") else 0.0  # NaN/inf guard
