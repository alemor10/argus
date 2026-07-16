"""The market wire — the magazine's market-wide pages, from ONE scanner call.

The Argus Daily treats the digest as a magazine issue: beside YOUR desk
(watchlist events) and the macro dashboard, an investor wants yesterday's
movers, the sector pulse, notable earnings, and new 52-week extremes. All of
it comes from a single TradingView scan (the scout screener's endpoint with
magazine columns — `change` and `price_52_week_high/low` verified live
2026-07-16; the `high_52_week` variants return null, do not use) joined with
the Finnhub earnings calendar.

Wire content is CLAIMS-LABELED market context (single unofficial sources,
never gated, never observations, never a delivery trigger by itself) — the
same policy as scout's screener claims and the bellwether section it
replaces. Curation is mechanical and disclosed: cap floors, top-N, and
tolerance constants below — Argus never decides what is "important" by
judgment, only by rule.

Persistence: the whole wire persists as one JSON blob per run (market_wire
table) so `argus report --run N` reproduces the issue bit-for-bit.
"""

from collections.abc import Mapping, Sequence
from datetime import date
from statistics import median
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from argus.models import (
    BellwetherEarning,
    EarningsWireEntry,
    Extreme,
    FeatureCard,
    MarketWire,
    Mover,
    SectorPulse,
)
from argus.scout.screener import _SCAN_URL, _USER_AGENT
from argus.scout.sectors import canonical_sector

# Mechanical curation rules — disclosed in the section captions.
_UNIVERSE_CAP_FLOOR = 2e9  # server-side scan floor (the scout precedent)
MOVER_CAP_FLOOR = 1e10  # movers/extremes/earnings consider large caps only
MOVERS_SHOWN = 5  # each way
EXTREMES_SHOWN = 8  # each way
EXTREME_TOLERANCE = 0.005  # within 0.5% of the 52-week mark counts as "at" it
EARNINGS_REPORTED_SHOWN = 8  # by |surprise|, descending
EARNINGS_UPCOMING_SHOWN = 10  # by cap, descending

_COLUMNS: tuple[str, ...] = (
    "name",
    "description",
    "sector",
    "close",
    "change",  # percent, last session (verified live 2026-07-16)
    "market_cap_basic",
    "price_52_week_high",
    "price_52_week_low",
)
_IDX = {column: i for i, column in enumerate(_COLUMNS)}


class MarketWireError(RuntimeError):
    """The scan failed wholesale — the issue's market pages are absent and the
    digest says so (silence is a statement here too)."""


class MarketRow(BaseModel):
    """One universe row — screener claims only."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    company: str | None = None
    sector: str = "Other"  # canonical bucket
    close: float | None = None
    change_pct: float | None = None
    market_cap: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None


class MarketScanner:
    """One POST for the magazine columns — same endpoint, same eyes-open
    fragility contract, and the same loud-on-shape-change policy as the scout
    screener."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def scan(self) -> list[MarketRow]:
        try:
            payload = self._fetch_raw()
        except Exception:  # free feeds hiccup: one inline retry, no framework
            try:
                payload = self._fetch_raw()
            except Exception as exc:
                raise MarketWireError(f"tradingview: market scan failed: {exc}") from exc
        return self.parse(payload)

    def _fetch_raw(self) -> Any:
        """Network only. Recorded payloads in tests/fixtures/tradingview/."""
        body = {
            "filter": [
                {"left": "market_cap_basic", "operation": "greater", "right": _UNIVERSE_CAP_FLOOR},
                {"left": "type", "operation": "equal", "right": "stock"},
                {"left": "is_primary", "operation": "equal", "right": True},
                {"left": "subtype", "operation": "in_range", "right": ["common"]},
                {"left": "exchange", "operation": "in_range", "right": ["AMEX", "NASDAQ", "NYSE"]},
            ],
            "columns": list(_COLUMNS),
            "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
            "range": [0, 8000],
        }
        response = httpx.post(
            _SCAN_URL, json=body, headers={"User-Agent": _USER_AGENT}, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def parse(self, payload: Any) -> list[MarketRow]:
        """Pure. Unrecognizable body → loud MarketWireError; a row without a
        symbol is skipped; non-numeric metric slots become None."""
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise MarketWireError(
                f"tradingview: unexpected market-scan body shape: {type(payload).__name__}"
            )
        rows: list[MarketRow] = []
        for entry in payload["data"]:
            if not isinstance(entry, dict):
                continue
            d = entry.get("d")
            if not isinstance(d, list) or len(d) != len(_COLUMNS):
                continue
            symbol = d[_IDX["name"]]
            if not isinstance(symbol, str) or not symbol:
                continue
            rows.append(
                MarketRow(
                    symbol=symbol.upper().replace(".", "-"),  # house symbology
                    company=_text(d[_IDX["description"]]),
                    sector=canonical_sector(_text(d[_IDX["sector"]])),
                    close=_num(d[_IDX["close"]]),
                    change_pct=_num(d[_IDX["change"]]),
                    market_cap=_num(d[_IDX["market_cap_basic"]]),
                    high_52w=_num(d[_IDX["price_52_week_high"]]),
                    low_52w=_num(d[_IDX["price_52_week_low"]]),
                )
            )
        return rows


def build_wire(
    rows: Sequence[MarketRow],
    calendar: Sequence[BellwetherEarning],
    *,
    pins: frozenset[str] = frozenset(),
    today: date,
) -> MarketWire:
    """Pure curation of one issue's market pages. `pins` (the bellwether
    list) are always included in the earnings wire regardless of cap."""
    caps = {row.symbol: row.market_cap for row in rows}
    reported, upcoming, more = _earnings_wire(calendar, caps, pins=pins, today=today)
    gainers, losers = _movers(rows)
    highs, lows = _extremes(rows)
    return MarketWire(
        universe=len(rows),
        gainers=tuple(gainers),
        losers=tuple(losers),
        sectors=tuple(_sector_pulse(rows)),
        highs=tuple(highs),
        lows=tuple(lows),
        earnings_reported=tuple(reported),
        earnings_upcoming=tuple(upcoming),
        earnings_more_upcoming=more,
    )


def _movers(rows: Sequence[MarketRow]) -> tuple[list[Mover], list[Mover]]:
    eligible = [
        row
        for row in rows
        if row.change_pct is not None
        and row.close is not None
        and row.market_cap is not None
        and row.market_cap >= MOVER_CAP_FLOOR
    ]
    ordered = sorted(eligible, key=lambda r: (-r.change_pct, r.symbol))

    def mover(row: MarketRow) -> Mover:
        return Mover(
            symbol=row.symbol, company=row.company, sector=row.sector,
            close=row.close, change_pct=row.change_pct,
        )

    gainers = [mover(r) for r in ordered[:MOVERS_SHOWN] if r.change_pct > 0]
    losers = [mover(r) for r in ordered[::-1][:MOVERS_SHOWN] if r.change_pct < 0]
    return gainers, losers


def _sector_pulse(rows: Sequence[MarketRow]) -> list[SectorPulse]:
    by_sector: dict[str, list[float]] = {}
    for row in rows:
        if row.change_pct is not None:
            by_sector.setdefault(row.sector, []).append(row.change_pct)
    pulses = [
        SectorPulse(sector=sector, median_change_pct=median(changes), n=len(changes))
        for sector, changes in by_sector.items()
    ]
    return sorted(pulses, key=lambda p: (-p.median_change_pct, p.sector))


def _extremes(rows: Sequence[MarketRow]) -> tuple[list[Extreme], list[Extreme]]:
    highs: list[Extreme] = []
    lows: list[Extreme] = []
    for row in sorted(rows, key=lambda r: -(r.market_cap or 0)):
        if (
            row.close is None
            or row.market_cap is None
            or row.market_cap < MOVER_CAP_FLOOR
        ):
            continue
        if row.high_52w and row.close >= row.high_52w * (1 - EXTREME_TOLERANCE):
            if len(highs) < EXTREMES_SHOWN:
                highs.append(
                    Extreme(symbol=row.symbol, company=row.company, close=row.close, kind="high")
                )
        elif row.low_52w and row.close <= row.low_52w * (1 + EXTREME_TOLERANCE):
            if len(lows) < EXTREMES_SHOWN:
                lows.append(
                    Extreme(symbol=row.symbol, company=row.company, close=row.close, kind="low")
                )
        if len(highs) >= EXTREMES_SHOWN and len(lows) >= EXTREMES_SHOWN:
            break
    return highs, lows


def _earnings_wire(
    calendar: Sequence[BellwetherEarning],
    caps: Mapping[str, float | None],
    *,
    pins: frozenset[str],
    today: date,
) -> tuple[list[EarningsWireEntry], list[EarningsWireEntry], int]:
    """Reported = rows with an actual (surprise-ranked); upcoming = today and
    later, cap-ranked. A pinned symbol always qualifies; everything else needs
    the large-cap floor (the whole calendar is hundreds of microcaps a week)."""
    pinned = {p.upper() for p in pins}

    def qualifies(entry: BellwetherEarning) -> bool:
        if entry.symbol.upper() in pinned:
            return True
        cap = caps.get(entry.symbol.upper().replace(".", "-"))
        return cap is not None and cap >= MOVER_CAP_FLOOR

    def with_cap(entry: BellwetherEarning) -> EarningsWireEntry:
        return EarningsWireEntry(
            **entry.model_dump(),
            market_cap=caps.get(entry.symbol.upper().replace(".", "-")),
        )

    qualified = [with_cap(e) for e in calendar if qualifies(e)]
    # The feed can send duplicate rows for one report (two estimate vintages
    # were observed live for BNY 2026-07-15). One line per (symbol, date),
    # keeping the SMALLEST |surprise| — the conservative claim.
    reported = _dedupe_reported(
        [e for e in qualified if e.eps_actual is not None]
    )
    upcoming_raw = [e for e in qualified if e.eps_actual is None and e.report_date >= today]
    seen: set[tuple[str, date]] = set()
    upcoming = []
    for entry in upcoming_raw:
        key = (entry.symbol.upper(), entry.report_date)
        if key not in seen:
            seen.add(key)
            upcoming.append(entry)

    reported.sort(key=lambda e: (-_abs_surprise(e), e.symbol))
    upcoming.sort(key=lambda e: (-(e.market_cap or 0), e.report_date, e.symbol))
    more = max(len(upcoming) - EARNINGS_UPCOMING_SHOWN, 0)
    return reported[:EARNINGS_REPORTED_SHOWN], upcoming[:EARNINGS_UPCOMING_SHOWN], more


def select_features(wire: MarketWire) -> list[tuple[str, str]]:
    """The issue's reading material, picked by DISCLOSED mechanical rules —
    never judgment: yesterday's biggest large-cap gainer, biggest loser, and
    the largest company reporting today. Returns (symbol, why) pairs,
    deduped, at most three."""
    picks: list[tuple[str, str]] = []
    if wire.gainers:
        m = wire.gainers[0]
        picks.append(
            (m.symbol, f"Yesterday's biggest large-cap gainer: {m.change_pct:+.1f}% to {m.close:.2f}")
        )
    if wire.losers:
        m = wire.losers[0]
        picks.append(
            (m.symbol, f"Yesterday's biggest large-cap loser: {m.change_pct:+.1f}% to {m.close:.2f}")
        )
    reporters = [e for e in wire.earnings_upcoming if e.market_cap is not None]
    if reporters:
        top = max(reporters, key=lambda e: e.market_cap)
        when = top.report_date.isoformat() + (f" {top.hour}" if top.hour else "")
        why = f"Largest company reporting next: {when}"
        if top.eps_estimate is not None:
            why += f", street at {top.eps_estimate:.2f}"
        picks.append((top.symbol, why))
    seen: set[str] = set()
    unique = []
    for symbol, why in picks:
        if symbol.upper() not in seen:
            seen.add(symbol.upper())
            unique.append((symbol, why))
    return unique[:3]


def fetch_feature_card(symbol: str, why: str, rows_by_symbol: Mapping[str, MarketRow]) -> FeatureCard:
    """One yfinance info fetch → a claims-labeled card. Network-side (called
    from the wire step); any failure degrades to a card with the wire's own
    numbers — the issue never blocks on a profile."""
    row = rows_by_symbol.get(symbol.upper())
    card = FeatureCard(
        symbol=symbol,
        why=why,
        name=row.company if row else None,
        sector=row.sector if row else None,
        close=row.close if row else None,
        change_pct=row.change_pct if row else None,
        market_cap=row.market_cap if row else None,
    )
    try:
        import yfinance

        info = dict(yfinance.Ticker(symbol).info or {})
    except Exception:
        return card

    def text(key: str) -> str | None:
        raw = info.get(key)
        return raw if isinstance(raw, str) and raw.strip() else None

    employees = info.get("fullTimeEmployees")
    return card.model_copy(
        update={
            "name": text("longName") or text("shortName") or card.name,
            "sector": text("sector") or card.sector,
            "industry": text("industry"),
            "employees": employees if isinstance(employees, int) and employees > 0 else None,
            "summary": (text("longBusinessSummary") or "")[:900] or None,
            "fwd_pe": _num(info.get("forwardPE")),
        }
    )


def _abs_surprise(entry: EarningsWireEntry) -> float:
    if entry.eps_estimate in (None, 0) or entry.eps_actual is None:
        return 0.0
    return abs((entry.eps_actual - entry.eps_estimate) / abs(entry.eps_estimate))


def _dedupe_reported(entries: list[EarningsWireEntry]) -> list[EarningsWireEntry]:
    best: dict[tuple[str, date], EarningsWireEntry] = {}
    for entry in entries:
        key = (entry.symbol.upper(), entry.report_date)
        current = best.get(key)
        if current is None or _abs_surprise(entry) < _abs_surprise(current):
            best[key] = entry
    return list(best.values())


def _num(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw) if raw == raw and abs(raw) != float("inf") else None


def _text(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None
