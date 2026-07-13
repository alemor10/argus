"""TradingView scanner — scout's candidate-universe feed.

Unofficial endpoint, accepted with eyes open the same way yfinance is: it can
change or vanish without notice, so the blast radius is this one module behind
the `Screener` protocol (EODHD/Finviz slot in later). Because it is the
accepted-fragile kind, it fails LOUDLY: a non-200 response or an unexpected
body shape raises ScreenerError — this adapter never guesses at a changed
contract.

Screener values are ONLY a candidate filter. They are never persisted as
observations and never appear as data in a digest — every reported number
comes from the v1 fetch→gate stack (see ARCHITECTURE.md, Scout). That is why
rows here are plain `ScreenerRow`, not `RawObservation`: they carry no
provenance because they never enter the store.

Endpoint contract (verified live 2026-07-13; recorded response in
tests/fixtures/tradingview/):

- One POST to /america/scan; the body names filters, columns, sort, range.
  `range: [0, 8000]` covers any plausible filtered universe in a single
  request — 1,517 rows came back at a $2B-cap / 500k-volume floor.
- Each result row is `{"s": "EXCHANGE:SYMBOL", "d": [...]}` with `d` ordered
  exactly as the requested columns. The `name` column is the bare SYMBOL
  (company name lives under `description`); exchange comes from the `s`
  prefix.
- Percent-kind columns report percent numbers (gross_margin_ttm 74.15 means
  74.15%) and pass through unchanged.
- Missing metrics arrive as JSON null and become None fields.
"""

import math
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict

_SCAN_URL = "https://scanner.tradingview.com/america/scan"

# The scanner backs tradingview.com's own screener page; a library-default
# User-Agent is the kind of request that gets blocked, so send a browser-ish
# one.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Requested columns, in order — parse() maps row values by index, so this
# tuple IS the wire contract. Every identifier verified live 2026-07-13.
_COLUMNS: tuple[str, ...] = (
    "name",  # bare symbol ("NVDA") — NOT the company name
    "description",  # company name
    "sector",
    "close",
    "market_cap_basic",
    "price_earnings_ttm",
    "price_earnings_growth_ttm",  # PEG
    "earnings_per_share_diluted_yoy_growth_ttm",  # percent
    "total_revenue_yoy_growth_ttm",  # percent
    "gross_margin_ttm",  # percent
    "operating_margin_ttm",  # percent
    "debt_to_equity",
    "average_volume_30d_calc",
)
_IDX = {column: i for i, column in enumerate(_COLUMNS)}


class ScreenerError(RuntimeError):
    """The screener failed wholesale (network, HTTP error, or a body shape
    this module does not recognize). Scout reports the outage in its digest —
    silence is a statement there too — so this must raise, never degrade."""


class ScreenerRow(BaseModel):
    """One screener result: a candidate identity plus the screener's CLAIMS
    about it. Claims feed scout's local criteria and the digest's
    labeled-as-screener-claims column only — never the observations table."""

    model_config = ConfigDict(frozen=True)

    ticker: str  # bare symbol, e.g. "NVDA"
    exchange: str  # "NASDAQ" | "NYSE" | "AMEX"
    company: str | None = None
    sector: str | None = None
    close: float | None = None
    market_cap: float | None = None
    pe_ttm: float | None = None
    peg_ttm: float | None = None
    eps_growth_ttm_pct: float | None = None  # percent, as TV reports
    revenue_growth_ttm_pct: float | None = None  # percent
    gross_margin_pct: float | None = None  # percent
    operating_margin_pct: float | None = None  # percent
    debt_to_equity: float | None = None
    avg_volume_30d: float | None = None


@runtime_checkable
class Screener(Protocol):
    """The candidate-universe seam. A paid feed (EODHD, Finviz export) is a
    one-module swap behind this."""

    def scan(self, *, min_market_cap: float, min_avg_volume: float) -> list[ScreenerRow]: ...


class TradingViewScreener:
    """One POST against the /america/scan endpoint.

    `last_skipped` (public) is set by every parse() call: the number of
    result rows dropped for lacking a usable symbol/exchange identity. Scout
    surfaces it in the digest's data-health section, so skipped rows are
    counted, never silent.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.last_skipped: int = 0

    def scan(self, *, min_market_cap: float, min_avg_volume: float) -> list[ScreenerRow]:
        try:
            payload = self._fetch_raw(min_market_cap, min_avg_volume)
        except Exception:  # free feeds hiccup: one inline retry, no framework
            try:
                payload = self._fetch_raw(min_market_cap, min_avg_volume)
            except Exception as exc:
                raise ScreenerError(f"tradingview: scan failed: {exc}") from exc
        return self.parse(payload)

    def _fetch_raw(self, min_market_cap: float, min_avg_volume: float) -> Any:
        """Network only. Recorded response in tests/fixtures/tradingview/."""
        response = httpx.post(
            _SCAN_URL,
            json=_request_body(min_market_cap, min_avg_volume),
            headers={"User-Agent": _USER_AGENT},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def parse(self, payload: Any) -> list[ScreenerRow]:
        """Pure over the payload (its one effect is setting self.last_skipped).

        A body that is not `{"data": [row, ...]}` raises ScreenerError — an
        endpoint that changed shape must be loud, not empty. A row without a
        usable symbol/exchange identity is skipped and counted in
        `last_skipped`. Metric slots: numbers pass through; JSON null and
        anything non-numeric (including NaN, which json.loads admits) become
        None — an unreadable screener claim is no claim, and the fetch→gate
        stack re-derives every number for candidates that survive.
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ScreenerError(f"tradingview: unexpected body shape: {_describe(payload)}")
        rows: list[ScreenerRow] = []
        skipped = 0
        for entry in payload["data"]:
            row = _parse_row(entry)
            if row is None:
                skipped += 1
            else:
                rows.append(row)
        self.last_skipped = skipped
        return rows


def _request_body(min_market_cap: float, min_avg_volume: float) -> dict[str, Any]:
    """Server-side pre-filter. Every filter verified live 2026-07-13 at a
    $2B-cap / 500k-volume floor:

    - type "stock": commons + preferreds; excludes funds, ETFs, DRs.
    - is_primary: 1,630 → 1,517 rows — one listing per company (kept
      NASDAQ:GOOG, dropped the GOOGL sibling) and drops non-primary
      preferred lines.
    - subtype ["common"]: 1,630 → 1,624 standalone — drops preferreds that
      survive type "stock" (NYSE:ORCL/PD, NYSE:BA/PA). "foreign-issuer"
      matched 0 rows under type "stock", so it is not in the allowlist.
    - exchange allowlist: 1,630 → 1,622 standalone — drops OTC rows, which
      at these floors were exactly the junk scout must not chase
      (conservatorship names FNMA/FMCC, thin foreign ordinaries NOKBF/ERIXF).
    """
    return {
        "filter": [
            {"left": "market_cap_basic", "operation": "greater", "right": min_market_cap},
            {"left": "average_volume_30d_calc", "operation": "greater", "right": min_avg_volume},
            {"left": "type", "operation": "equal", "right": "stock"},
            {"left": "is_primary", "operation": "equal", "right": True},
            {"left": "subtype", "operation": "in_range", "right": ["common"]},
            {"left": "exchange", "operation": "in_range", "right": ["AMEX", "NASDAQ", "NYSE"]},
        ],
        "columns": list(_COLUMNS),
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, 8000],
    }


def _parse_row(entry: Any) -> ScreenerRow | None:
    """One scanner row → ScreenerRow, or None when it lacks the identity that
    makes a candidate actionable: a non-empty `name` (bare symbol) and an
    exchange prefix on `s`. A `d` array that is not exactly our column count
    also skips — the index mapping cannot be trusted for that row."""
    if not isinstance(entry, dict):
        return None
    d = entry.get("d")
    if not isinstance(d, list) or len(d) != len(_COLUMNS):
        return None
    ticker = d[_IDX["name"]]
    exchange = _exchange_prefix(entry.get("s"))
    if not isinstance(ticker, str) or not ticker or exchange is None:
        return None
    return ScreenerRow(
        ticker=ticker,
        exchange=exchange,
        company=_text(d[_IDX["description"]]),
        sector=_text(d[_IDX["sector"]]),
        close=_num(d[_IDX["close"]]),
        market_cap=_num(d[_IDX["market_cap_basic"]]),
        pe_ttm=_num(d[_IDX["price_earnings_ttm"]]),
        peg_ttm=_num(d[_IDX["price_earnings_growth_ttm"]]),
        eps_growth_ttm_pct=_num(d[_IDX["earnings_per_share_diluted_yoy_growth_ttm"]]),
        revenue_growth_ttm_pct=_num(d[_IDX["total_revenue_yoy_growth_ttm"]]),
        gross_margin_pct=_num(d[_IDX["gross_margin_ttm"]]),
        operating_margin_pct=_num(d[_IDX["operating_margin_ttm"]]),
        debt_to_equity=_num(d[_IDX["debt_to_equity"]]),
        avg_volume_30d=_num(d[_IDX["average_volume_30d_calc"]]),
    )


def _exchange_prefix(s: Any) -> str | None:
    """"NASDAQ:NVDA" → "NASDAQ"; anything without a named prefix is None."""
    if not isinstance(s, str) or ":" not in s:
        return None
    return s.split(":", 1)[0] or None


def _num(raw: Any) -> float | None:
    """int/float → float; everything else → None. bool is excluded
    (float(True) == 1.0 would launder garbage) and so are NaN/inf."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def _text(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _describe(payload: Any) -> str:
    if isinstance(payload, dict):
        return f"dict with keys {sorted(map(str, payload))[:10]}"
    return type(payload).__name__
