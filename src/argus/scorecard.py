"""Scout self-scoring — "grade the grader", PURE.

Given scout's past proposals and the realized price history of each name (and
SPY), compute how every proposed name has actually done at each of a few FIXED
horizons — 4, 13, 26, and 52 weeks after it first surfaced — versus the market
over the identical window. No prediction: realized data only, and the market is
the answer key (the engine never grades itself).

Fixed horizons, not "return since first proposed", because a moving as-of makes
every past number quietly re-price each run — a name looks better or worse for
reasons that have nothing to do with the call. A horizon return is LOCKED once
measured: the 4-week return of a name proposed in May never changes again. That
is the honest forward log.

Honest by construction: EVERY name scout ever proposed is eligible; a name that
dropped off the shortlist is still tracked from its first proposal. Returns use
adjusted closes (dividends + splits on both legs). Names are partitioned into
three disjoint, disclosed buckets — matured (produced ≥1 horizon mark),
pending (priced at entry but too young for even the 4-week mark), and
unpriceable (no entry price at all this run) — so "no signal yet" is never
confused with "data was missing". Marks are persisted immutably per scoring run,
so the scorecard is a forward log, never retroactively revised.
"""

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from statistics import median

from argus.models import Scorecard, ScorecardCohort, ScorecardMark

PriceSeries = Sequence[tuple[date, float]]

# The fixed measurement horizons, in weeks — youngest first. A name contributes
# one mark per horizon it has MATURED past (and that both it and SPY can be
# priced at). 4 ≈ a month, 13 ≈ a quarter, 26 ≈ a half, 52 ≈ a year.
HORIZONS: tuple[int, ...] = (4, 13, 26, 52)

# Min-sample gate: a horizon's medians are withheld until at least this many
# names have matured to it — one or two names is an anecdote, not a track
# record. `n` is always shown regardless, so coverage stays honest.
MIN_SAMPLE = 3

_ENTRY_SENTINEL = 0  # ScorecardMark.horizon_weeks value for the "priced at entry" row


def compute_marks(
    proposals: Sequence[tuple[str, date]],
    histories: Mapping[str, PriceSeries | None],
    spy_history: PriceSeries | None,
    as_of: date,
) -> tuple[list[ScorecardMark], int]:
    """Marks for one scoring run + the unpriceable count.

    For each proposal, the entry price is the first tradeable close ON OR AFTER
    the proposal date (a weekend/holiday proposal prices at the next session —
    what you could actually transact at). A name whose entry — or SPY's — cannot
    be priced is UNPRICEABLE and produces no marks. Otherwise it emits an entry
    sentinel (horizon_weeks=0) plus one mark for every horizon that has both
    (a) matured — its target date is on or before `as_of` — and (b) a close for
    the name AND SPY near that date. A matured horizon with a gap (e.g. a name
    delisted mid-window) is skipped, not zeroed.
    """
    spy_entry_cache: dict[date, float | None] = {}
    marks: list[ScorecardMark] = []
    unpriceable = 0
    for ticker, first_date in proposals:
        series = histories.get(ticker)
        entry = _price_on_or_after(series, first_date)
        if first_date not in spy_entry_cache:
            spy_entry_cache[first_date] = _price_on_or_after(spy_history, first_date)
        spy_entry = spy_entry_cache[first_date]
        if not (entry and spy_entry):
            unpriceable += 1
            continue
        marks.append(
            ScorecardMark(
                ticker=ticker,
                first_proposed_at=first_date,
                horizon_weeks=_ENTRY_SENTINEL,
                name_return=0.0,
                spy_return=0.0,
            )
        )
        for horizon in HORIZONS:
            target = first_date + timedelta(weeks=horizon)
            if target > as_of:
                break  # HORIZONS ascending — nothing longer has matured either
            name_at = _price_on_or_after(series, target)
            spy_at = _price_on_or_after(spy_history, target)
            if not (name_at and spy_at):
                continue  # matured but the close is missing — a gap, not a zero
            marks.append(
                ScorecardMark(
                    ticker=ticker,
                    first_proposed_at=first_date,
                    horizon_weeks=horizon,
                    name_return=name_at / entry - 1.0,
                    spy_return=spy_at / spy_entry - 1.0,
                )
            )
    return marks, unpriceable


def summarize(marks: Sequence[ScorecardMark], as_of: date, unpriceable: int) -> Scorecard:
    """Aggregate marks into the fixed-horizon cohorts + an overall roll-up —
    deterministic, so the digest reproduces from the persisted marks. `marks`
    may include entry sentinels (horizon_weeks=0); they count a name as priced
    without grading it."""
    priced = {m.ticker for m in marks}
    graded = [m for m in marks if m.horizon_weeks > 0]
    matured_names = {m.ticker for m in graded}
    pending = len(priced) - len(matured_names)
    if not marks:
        return Scorecard(as_of=as_of, unpriceable=unpriceable)

    cohorts: list[ScorecardCohort] = []
    for horizon in HORIZONS:
        group = [m for m in graded if m.horizon_weeks == horizon]
        if not group:
            continue
        cohorts.append(
            ScorecardCohort(
                label=f"{horizon} weeks",
                horizon_weeks=horizon,
                n=len(group),
                median_return=median(m.name_return for m in group),
                median_spy=median(m.spy_return for m in group),
                median_alpha=median(m.alpha for m in group),
                beat_spy=sum(1 for m in group if m.alpha > 0),
                enough=len(group) >= MIN_SAMPLE,
            )
        )

    # The headline is the LONGEST horizon that clears the min-sample gate — the
    # most-seasoned honest read. None clearing it → no headline (overall_label "").
    headline = next((c for c in reversed(cohorts) if c.enough), None)
    return Scorecard(
        as_of=as_of,
        cohorts=tuple(cohorts),
        overall_n=len(matured_names),
        overall_label=headline.label if headline else "",
        overall_median_alpha=headline.median_alpha if headline else 0.0,
        overall_beat_spy=headline.beat_spy if headline else 0,
        overall_horizon_n=headline.n if headline else 0,
        pending=pending,
        unpriceable=unpriceable,
        marks=tuple(graded),
    )


_MAX_ENTRY_GAP = timedelta(days=7)  # covers a long weekend + a holiday


def _price_on_or_after(series: PriceSeries | None, target: date) -> float | None:
    """The first close on or after `target`, within a week (series ascending
    by date). None when the target predates the history by more than a
    holiday gap — e.g. a name that had not begun trading — so a stale far-off
    bar is never passed off as a price, and None when the target is beyond the
    last bar (an unmatured or delisted-past horizon)."""
    if not series:
        return None
    for day, close in series:
        if day < target:
            continue
        if day > target + _MAX_ENTRY_GAP:
            return None
        return close if close and close > 0 else None
    return None
