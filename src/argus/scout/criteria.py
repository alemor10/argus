"""Scout step 2 — PURE screening and ranking rules over screener rows.

Screener values are only ever a candidate filter: nothing here is persisted
or reported as data — every number in a scout digest comes from the v1
fetch→gate stack, which re-verifies each survivor. That is also why a None
metric FAILS its rule: thin data is not a pass; scout proposes only clean
names, and the enrichment stage re-checks everything anyway.

The market-cap and average-volume floors live in ScoutCriteria because the
orchestrating layer passes them to the screener's scan() — they are applied
server-side and deliberately NOT re-applied here.

No IO, no clock. Ranking is fully deterministic: PEG ascending, market cap
descending on ties, ticker alphabetical last.
"""

from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from argus.scout.screener import ScreenerRow


class ScoutCriteria(BaseModel):
    """Screening thresholds, loaded from scout.yaml.

    Frozen + extra="forbid": a typo'd yaml key must error loudly, never
    silently screen with a default in its place (config is the fail-loudly
    boundary, same policy as Thresholds).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_market_cap: float = 2e9  # applied server-side by the screener
    min_avg_volume: float = 1_000_000  # applied server-side by the screener
    max_peg: float = 1.5
    min_gross_margin_pct: float = 30.0
    min_operating_margin_pct: float = 8.0
    min_eps_growth_pct: float = 10.0
    min_revenue_growth_pct: float = 5.0
    max_debt_to_equity: float = 1.5
    top_n: int = 15


def load_scout_criteria(path: Path) -> ScoutCriteria:
    """Missing file → defaults (scout works out of the box). Present file →
    yaml.safe_load, validated strictly: unknown keys are a ValidationError,
    not a shrug."""
    if not path.exists():
        return ScoutCriteria()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ScoutCriteria.model_validate(raw)


class ScreenedCandidate(BaseModel):
    """One screener row that passed EVERY rule, with its rank and the
    per-rule pass reasons (actual values included) — these become the
    `screen_reasons` JSON in scout_candidates, labeled as screener claims."""

    model_config = ConfigDict(frozen=True)

    row: ScreenerRow
    rank: int  # 1-based, assigned after sorting
    reasons: dict[str, str]  # rule -> human string; insertion order = rule order


def screen(
    rows: Sequence[ScreenerRow],
    criteria: ScoutCriteria,
    exclude: AbstractSet[str],
) -> list[ScreenedCandidate]:
    """Apply the local rules to screener rows; return the ranked shortlist.

    - `exclude` (watchlist tickers, compared case-insensitively on the bare
      symbol) is dropped before any rule runs — scout never proposes names
      already held.
    - A row passes only if EVERY rule passes; a None metric fails its rule.
    - Passers are ranked PEG ascending (growth-adjusted cheapness first),
      market cap descending on ties, then ticker alphabetical, and the list
      is capped to criteria.top_n.
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
    return [
        ScreenedCandidate(row=row, rank=rank, reasons=reasons)
        for rank, (row, reasons) in enumerate(passers[: max(criteria.top_n, 0)], start=1)
    ]


def _pass_reasons(row: ScreenerRow, criteria: ScoutCriteria) -> dict[str, str] | None:
    """All rules, in the fixed reporting order. Returns the reasons dict when
    every rule passes, None on the first failure — pass/fail is all-or-nothing,
    so partial reasons are never observable."""
    checks: tuple[tuple[str, str | None], ...] = (
        ("peg", _peg(row.peg_ttm, criteria.max_peg)),
        (
            "gross_margin",
            _floor("gross_margin", row.gross_margin_pct, criteria.min_gross_margin_pct),
        ),
        (
            "operating_margin",
            _floor(
                "operating_margin", row.operating_margin_pct, criteria.min_operating_margin_pct
            ),
        ),
        ("eps_growth", _floor("eps_growth", row.eps_growth_ttm_pct, criteria.min_eps_growth_pct)),
        (
            "revenue_growth",
            _floor("revenue_growth", row.revenue_growth_ttm_pct, criteria.min_revenue_growth_pct),
        ),
        ("debt_to_equity", _leverage(row.debt_to_equity, criteria.max_debt_to_equity)),
        ("value_trap", _value_trap(row.eps_growth_ttm_pct, row.revenue_growth_ttm_pct)),
    )
    reasons: dict[str, str] = {}
    for rule, reason in checks:
        if reason is None:
            return None
        reasons[rule] = reason
    return reasons


def _peg(value: float | None, ceiling: float) -> str | None:
    """Present and 0 < peg <= ceiling. A zero or negative PEG is meaningless
    (negative growth or negative earnings behind it), never a bargain."""
    if value is None or not (0 < value <= ceiling):
        return None
    return f"peg {_fmt(value)} <= {_fmt(ceiling)}"


def _floor(rule: str, value: float | None, floor: float) -> str | None:
    if value is None or value < floor:
        return None
    return f"{rule} {_fmt(value)} >= {_fmt(floor)}"


def _leverage(value: float | None, ceiling: float) -> str | None:
    """Present and 0 <= d/e <= ceiling. Negative debt/equity means negative
    equity — a balance-sheet question mark, not low leverage."""
    if value is None or not (0 <= value <= ceiling):
        return None
    return f"debt_to_equity {_fmt(value)} <= {_fmt(ceiling)}"


def _value_trap(eps_growth: float | None, revenue_growth: float | None) -> str | None:
    """Value-trap exclusion: both growth lines strictly positive, regardless
    of the configured floors — cheap + shrinking is not cheap."""
    if eps_growth is None or revenue_growth is None:
        return None
    if eps_growth <= 0 or revenue_growth <= 0:
        return None
    return f"eps_growth {_fmt(eps_growth)} > 0 and revenue_growth {_fmt(revenue_growth)} > 0"


def _rank_key(row: ScreenerRow) -> tuple[float, float, str, str]:
    """PEG ascending, market cap descending, ticker alphabetical (bare symbol,
    then the full ticker string so ordering is total no matter what)."""
    if row.peg_ttm is None:  # the peg rule guarantees presence for passers
        raise AssertionError("unreachable: rows without a PEG never pass screening")
    market_cap = row.market_cap if row.market_cap is not None else float("-inf")
    return (row.peg_ttm, -market_cap, _bare_symbol(row.ticker), row.ticker)


def _bare_symbol(ticker: str) -> str:
    """'nasdaq:NVDA' → 'NVDA': exclusion (and alphabetical ordering) compare
    bare symbols case-insensitively, whatever prefix a screener uses."""
    return ticker.rsplit(":", 1)[-1].strip().upper()


def _fmt(value: float) -> str:
    """Compact, deterministic number rendering: 0.82 → '0.82', 30.0 → '30'."""
    return format(value, "g")
