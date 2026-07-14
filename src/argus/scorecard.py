"""Scout self-scoring — "grade the grader", PURE.

Given scout's past proposals and the realized price history of each name (and
SPY), compute how every proposed name has actually done since it first
surfaced, versus the market over the same window. No prediction — realized
data only, and the market is the answer key (the engine never grades itself).

Honest by construction: EVERY name scout ever proposed is scored (no
survivorship — a name that dropped off the shortlist is still tracked from its
first proposal), total return uses adjusted closes (dividends + splits
included on both legs), and the marks are persisted immutably per scoring run
so the scorecard is a forward log, never retroactively revised.
"""

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from statistics import median

from argus.models import Scorecard, ScorecardCohort, ScorecardMark

PriceSeries = Sequence[tuple[date, float]]

# (label, low_weeks, high_weeks) — youngest cohort first.
_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("≤ 1 week", 0, 1),
    ("2–4 weeks", 2, 4),
    ("5–8 weeks", 5, 8),
    ("9–13 weeks", 9, 13),
    ("3+ months", 14, 10_000),
)


def compute_marks(
    proposals: Sequence[tuple[str, date]],
    histories: Mapping[str, PriceSeries | None],
    spy_history: PriceSeries | None,
    as_of: date,
) -> tuple[list[ScorecardMark], int]:
    """One mark per (ticker, first-proposed date). A name whose history — or
    SPY's — cannot be priced at both endpoints is counted as unpriceable and
    skipped, never silently folded into the aggregate as a zero."""
    spy_now = _latest(spy_history)
    marks: list[ScorecardMark] = []
    unpriceable = 0
    for ticker, first_date in proposals:
        series = histories.get(ticker)
        # Entry = the first tradeable close ON OR AFTER the proposal date —
        # what you could actually transact at. A weekend/holiday proposal
        # prices at the next session (not the prior close), so a name first
        # surfaced on a non-trading day is scored, not silently dropped.
        entry = _price_on_or_after(series, first_date)
        current = _latest(series)
        spy_entry = _price_on_or_after(spy_history, first_date)
        if not (entry and current and spy_entry and spy_now):
            unpriceable += 1
            continue
        marks.append(
            ScorecardMark(
                ticker=ticker,
                first_proposed_at=first_date,
                weeks_out=max((as_of - first_date).days // 7, 0),
                name_return=current / entry - 1.0,
                spy_return=spy_now / spy_entry - 1.0,
            )
        )
    return marks, unpriceable


def summarize(marks: Sequence[ScorecardMark], as_of: date, unpriceable: int) -> Scorecard:
    """Aggregate marks into age cohorts + an overall line — deterministic, so
    the digest reproduces from the persisted marks."""
    if not marks:
        return Scorecard(as_of=as_of, unpriceable=unpriceable)
    cohorts = []
    for label, lo, hi in _BUCKETS:
        group = [m for m in marks if lo <= m.weeks_out <= hi]
        if not group:
            continue
        cohorts.append(
            ScorecardCohort(
                label=label,
                n=len(group),
                median_return=median(m.name_return for m in group),
                median_spy=median(m.spy_return for m in group),
                median_alpha=median(m.alpha for m in group),
                beat_spy=sum(1 for m in group if m.alpha > 0),
            )
        )
    return Scorecard(
        as_of=as_of,
        cohorts=tuple(cohorts),
        overall_n=len(marks),
        overall_median_alpha=median(m.alpha for m in marks),
        overall_beat_spy=sum(1 for m in marks if m.alpha > 0),
        unpriceable=unpriceable,
    )


def _latest(series: PriceSeries | None) -> float | None:
    if not series:
        return None
    value = series[-1][1]
    return value if value and value > 0 else None


_MAX_ENTRY_GAP = timedelta(days=7)  # covers a long weekend + a holiday


def _price_on_or_after(series: PriceSeries | None, target: date) -> float | None:
    """The first close on or after `target`, within a week (series ascending
    by date). None when the target predates the history by more than a
    holiday gap — e.g. a name that had not begun trading — so a stale far-off
    bar is never passed off as an entry price."""
    if not series:
        return None
    for day, close in series:
        if day < target:
            continue
        if day > target + _MAX_ENTRY_GAP:
            return None
        return close if close and close > 0 else None
    return None
