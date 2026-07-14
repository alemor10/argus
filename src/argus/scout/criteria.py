"""Scout step 2 — PURE screening and ranking rules over screener rows.

Strategy: Quality-GARP, forward-looking. The first live screen ranked on
TTM-EPS-growth GARP and surfaced base-effect recovery cyclicals — miners at
"+697% EPS growth" off a collapsed prior year. Trailing EPS growth is where
the base effect lives, so the screen asks what is being paid for what comes
NEXT: forward P/E against revenue growth, with quality floors (ROE, margins,
leverage ceiling) doing the work naive-cheap screens skip. TTM EPS growth
survives only as a value-trap guard — growing revenue with collapsing
earnings is a margin-compression trap, not a bargain.

Screener values are only ever a candidate filter: nothing here is persisted
or reported as data — every number in a scout digest comes from the v1
fetch→gate stack, which re-verifies each survivor. That is also why a None
metric FAILS its rule: thin data is not a pass; scout proposes only clean
names, and the enrichment stage re-checks everything anyway.

The market-cap and average-volume floors live in ScoutCriteria because the
orchestrating layer passes them to the screener's scan() — they are applied
server-side and deliberately NOT re-applied here.

No IO, no clock. Ranking is fully deterministic: forward-PEG (fwd P/E per
point of revenue growth) ascending, market cap descending on ties, ticker
alphabetical last.
"""

from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from argus.scout.screener import ScreenerRow
from argus.scout.sectors import CANONICAL_SECTORS, OTHER, canonical_sector


class ScoutCriteria(BaseModel):
    """Screening thresholds, loaded from scout.yaml.

    Frozen + extra="forbid": a typo'd yaml key must error loudly, never
    silently screen with a default in its place (config is the fail-loudly
    boundary, same policy as Thresholds). The forbid also retires the old
    TTM-GARP strategy honestly — a leftover `max_peg`/`min_eps_growth_pct`
    key errors instead of silently screening with the new rules.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_market_cap: float = 2e9  # applied server-side by the screener
    min_avg_volume: float = 1_000_000  # applied server-side by the screener
    max_forward_pe: float = 25.0
    min_revenue_growth_pct: float = 10.0
    min_gross_margin_pct: float = 40.0
    min_operating_margin_pct: float = 12.0
    min_roe_pct: float = 15.0
    max_debt_to_equity: float = 1.0
    max_eps_decline_pct: float = -30.0  # value-trap guard: TTM EPS trend must stay above
    max_per_sector: int = 3  # shortlist concentration cap (0 disables) — a single-metric
    top_n: int = 15  #          ranking otherwise becomes one sector bet wearing 15 tickers


def load_scout_criteria(path: Path) -> ScoutCriteria:
    """Missing file → defaults (scout works out of the box). Present file →
    yaml.safe_load, validated strictly: unknown keys are a ValidationError,
    not a shrug."""
    if not path.exists():
        return ScoutCriteria()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ScoutCriteria.model_validate(raw)


class ScreenedCandidate(BaseModel):
    """One screener row that passed EVERY rule, with its global passer rank,
    canonical sector, and the per-rule pass reasons (actual values included)
    — these become the `screen_reasons` JSON in scout_candidates, labeled as
    screener claims."""

    model_config = ConfigDict(frozen=True)

    row: ScreenerRow
    rank: int  # 1-based GLOBAL rank among all passers (pre-cap, pre-top_n)
    sector: str  # canonical (sectors.CANONICAL_SECTORS or "Other")
    reasons: dict[str, str]  # rule -> human string; insertion order = rule order


class ScreenResult(BaseModel):
    """The screen's two outputs: the capped shortlist (goes on to enrichment
    and gating) and the sector leaders — the best passer from each sector
    that has NO representative in the shortlist, shown for category coverage
    without enrichment. An empty sector is information, never padded."""

    model_config = ConfigDict(frozen=True)

    shortlist: tuple[ScreenedCandidate, ...] = ()
    sector_leaders: tuple[ScreenedCandidate, ...] = ()


def screen(
    rows: Sequence[ScreenerRow],
    criteria: ScoutCriteria,
    exclude: AbstractSet[str],
) -> ScreenResult:
    """Apply the local rules to screener rows; return the ranked shortlist
    plus sector leaders.

    - `exclude` (watchlist tickers, compared case-insensitively on the bare
      symbol) is dropped before any rule runs — scout never proposes names
      already held.
    - A row passes only if EVERY rule passes; a None metric fails its rule.
    - Passers are ranked forward-PEG ascending (fwd P/E over revenue growth
      — cheap FOR ITS GROWTH first, never naive low-P/E), market cap
      descending on ties, then ticker alphabetical. `rank` is this GLOBAL
      position.
    - Shortlist selection walks the global ranking, skipping names whose
      canonical sector already holds `max_per_sector` slots (0 disables the
      cap), until `top_n` names are chosen.
    - Sector leaders: the single best passer of each sector with zero
      shortlist representation.
    """
    excluded = {_bare_symbol(ticker) for ticker in exclude}
    passers: list[tuple[ScreenerRow, dict[str, str]]] = []
    for row in rows:
        if _bare_symbol(row.ticker) in excluded:
            continue
        reasons = _pass_reasons(row, criteria)
        if reasons is not None:
            passers.append((row, reasons))
    passers.sort(key=lambda pair: _rank_key(pair[0]))
    ranked = [
        ScreenedCandidate(
            row=row, rank=rank, sector=canonical_sector(row.sector), reasons=reasons
        )
        for rank, (row, reasons) in enumerate(passers, start=1)
    ]

    shortlist: list[ScreenedCandidate] = []
    per_sector: dict[str, int] = {}
    for candidate in ranked:
        if len(shortlist) >= max(criteria.top_n, 0):
            break
        taken = per_sector.get(candidate.sector, 0)
        if criteria.max_per_sector > 0 and taken >= criteria.max_per_sector:
            continue
        per_sector[candidate.sector] = taken + 1
        shortlist.append(candidate)

    represented = {candidate.sector for candidate in shortlist}
    leaders: list[ScreenedCandidate] = []
    seen_sectors: set[str] = set()
    for candidate in ranked:
        if candidate.sector in represented or candidate.sector in seen_sectors:
            continue
        seen_sectors.add(candidate.sector)
        leaders.append(candidate)

    return ScreenResult(shortlist=tuple(shortlist), sector_leaders=tuple(leaders))


def _pass_reasons(row: ScreenerRow, criteria: ScoutCriteria) -> dict[str, str] | None:
    """All rules, in the fixed reporting order. Returns the reasons dict when
    every rule passes, None on the first failure — pass/fail is all-or-nothing,
    so partial reasons are never observable."""
    checks: tuple[tuple[str, str | None], ...] = (
        ("forward_pe", _forward_pe(row.fwd_pe, criteria.max_forward_pe)),
        (
            "revenue_growth",
            _pct_floor(
                "rev growth",
                row.revenue_growth_ttm_pct,
                criteria.min_revenue_growth_pct,
                signed=True,
            ),
        ),
        (
            "gross_margin",
            _pct_floor(
                "gross margin", row.gross_margin_pct, criteria.min_gross_margin_pct, signed=False
            ),
        ),
        (
            "operating_margin",
            _pct_floor(
                "op margin",
                row.operating_margin_pct,
                criteria.min_operating_margin_pct,
                signed=False,
            ),
        ),
        ("roe", _pct_floor("ROE", row.roe_pct, criteria.min_roe_pct, signed=False)),
        ("debt_to_equity", _leverage(row.debt_to_equity, criteria.max_debt_to_equity)),
        ("value_trap", _value_trap(row.eps_growth_ttm_pct, criteria.max_eps_decline_pct)),
    )
    reasons: dict[str, str] = {}
    for rule, reason in checks:
        if reason is None:
            return None
        reasons[rule] = reason
    return reasons


def _forward_pe(value: float | None, ceiling: float) -> str | None:
    """Present and 0 < fwd P/E <= ceiling. A zero or negative forward P/E
    means expected losses — never cheap, whatever the multiple says."""
    if value is None or not (0 < value <= ceiling):
        return None
    return f"fwd P/E {value:.1f} ≤ {_fmt(ceiling)}"


def _pct_floor(label: str, value: float | None, floor: float, *, signed: bool) -> str | None:
    """Present and value >= floor, rendered '<label> 74.1% ≥ 40%'. Growth
    lines render signed (+70.7% — direction is the story there); level
    metrics (margins, ROE) render bare."""
    if value is None or value < floor:
        return None
    rendered = f"{value:+.1f}" if signed else f"{value:.1f}"
    return f"{label} {rendered}% ≥ {_fmt(floor)}%"


def _leverage(value: float | None, ceiling: float) -> str | None:
    """Present and 0 <= D/E <= ceiling. Negative debt/equity means negative
    equity — a balance-sheet question mark, not low leverage. Two decimals,
    the one value not rendered at one: leverage lives in the 0.0x range,
    where one decimal would erase the number ('D/E 0.06', not 'D/E 0.1')."""
    if value is None or not (0 <= value <= ceiling):
        return None
    return f"D/E {value:.2f} ≤ {_fmt(ceiling)}"


def _value_trap(eps_growth: float | None, decline_floor: float) -> str | None:
    """Value-trap guard: TTM EPS trend STRICTLY above the decline ceiling
    (exactly at it fails). Growing revenue with collapsing earnings is a
    margin-compression trap; None fails — thin data is not a pass."""
    if eps_growth is None or eps_growth <= decline_floor:
        return None
    return f"EPS trend {eps_growth:+.1f}% > {_fmt(decline_floor)}%"


def _rank_key(row: ScreenerRow) -> tuple[float, float, str, str]:
    """Forward-PEG ascending, market cap descending, ticker alphabetical
    (bare symbol, then the full ticker string so ordering is total no matter
    what).

    The rules guarantee fwd_pe > 0 for passers, and revenue growth is
    positive under any sane floor — but a permissive config (negative
    growth floor) could admit a shrinking passer, and naive division would
    hand it a NEGATIVE forward-PEG and first place. Nonpositive growth pins
    to the bottom instead.
    """
    if row.fwd_pe is None or row.revenue_growth_ttm_pct is None:
        raise AssertionError("unreachable: rows without fwd P/E or revenue growth never pass")
    growth = row.revenue_growth_ttm_pct
    fwd_peg = row.fwd_pe / growth if growth > 0 else float("inf")
    market_cap = row.market_cap if row.market_cap is not None else float("-inf")
    return (fwd_peg, -market_cap, _bare_symbol(row.ticker), row.ticker)


def house_symbol(ticker: str) -> str:
    """Canonical house symbology: bare symbol, uppercase, dashes for class
    shares ('nasdaq:brk.b' → 'BRK-B'). THE one normalizer — exclusion
    matching, ranking, contexts, and the store must all share one spelling,
    or a watched BRK-B fails to exclude TradingView's BRK.B row."""
    return ticker.rsplit(":", 1)[-1].strip().upper().replace(".", "-")


_bare_symbol = house_symbol  # ranking/exclusion use the canonical form


def _fmt(value: float) -> str:
    """Compact, deterministic threshold rendering: 25.0 → '25', 22.5 → '22.5'."""
    return format(value, "g")
