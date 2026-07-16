"""Change detection. PURE — SQL supplies the inputs, this decides what is news.

Semantics (see ARCHITECTURE.md, Change detection):
  - PriceMove / TargetMove: |Δ%| between accepted baseline and accepted
    current ≥ threshold. If the field was quarantined or missing in the
    baseline snapshot, compare against `latest_accepted` instead — a change is
    reported late, never lost — and carry old_as_of so the digest prints the
    honest comparison window. Never computed against a quarantined endpoint:
    recovery emits FieldRecovered and establishes a new baseline rather than
    a fake move.
  - ConsensusShift: rating text moved along strong_buy > buy > hold >
    underperform > sell ("unclear" when either grade is off-scale — the
    shift is still reported, never suppressed).
  - AnalystAction: exactly the new_actions passed in (rows whose
    first_seen_run_id is the current run — set membership, no window math).
    Suppressed on a ticker's first-ever run: the feed's whole dated history
    is baseline then, not news.
  - EarningsReported: exactly the new_earnings passed in (same first-seen
    set membership, same first-run suppression — the source hands over its
    full reported history then, which is baseline, not news). The surprise
    is computed here from the stored estimate/actual facts, never taken
    from the source's own surprise figure.
  - EarningsImminent: state event; re-fires each run inside the window
    (suppression logic's failure mode is silence).
  - FieldQuarantined / FieldRecovered: verdict transitions per field.
"""

from collections.abc import Callable, Sequence
from datetime import date
from typing import Literal

from argus.fields import Field
from argus.models import (
    AnalystAction,
    AnalystActionRecord,
    ChangeEvent,
    ConsensusShift,
    EarningsImminent,
    EarningsReported,
    EarningsResultRecord,
    FieldQuarantined,
    FieldRecovered,
    FieldValue,
    PriceMove,
    Snapshot,
    TargetMove,
    ThesisDrift,
    TickerContext,
)
from argus.thesis import evaluate_thesis_checks

# The ordered rating scale, ascending: index = rank. Grades off this scale
# rank as None and the shift direction is "unclear" — the event still fires
# (suppressing a change because it is unrankable would be a silent drop).
_RATING_RANK: dict[str, int] = {
    grade: rank for rank, grade in enumerate(("sell", "underperform", "hold", "buy", "strong_buy"))
}


def detect(
    baseline: Snapshot | None,
    current: Snapshot,
    ctx: TickerContext,
    new_actions: Sequence[AnalystActionRecord],
    today: date,
    *,
    latest_accepted: Callable[[Field], FieldValue | None],
    new_earnings: Sequence[EarningsResultRecord] = (),
) -> list[ChangeEvent]:
    """Diff one ticker's snapshots into typed events.

    `latest_accepted` is the injected store lookup used only when the baseline
    snapshot lacks an accepted value for a field (quarantine/outage gap
    fallback). A `baseline` of None means first-ever run for this ticker:
    no diff events AND no analyst-action or earnings-reported events — the
    source's entire dated history is "first seen" on that run, and 14 years
    of rating actions (or four reported quarters) is baseline, not news (a
    real first live run produced 1,100 lines of it). The rows are still
    stored with first_seen_run_id, so from the next run on, only genuinely
    new items fire. On a first run only the state-style events fire —
    EarningsImminent, and ThesisDrift (a breach is a breach on day one,
    needing no history) — and the digest also lists the ticker under
    "baseline established".
    """
    events: list[ChangeEvent] = []

    # Thesis drift leads — the highest-signal event a monitor emits. A check
    # is BREACHED when the data crossed the human's stated line; `newly`
    # distinguishes a fresh breach from one continuing since last run (a
    # breach that only appears because the baseline could not verify the
    # check counts as newly breached — the reader is seeing it for the first
    # time). Fires every run while breached — suppression's failure mode is
    # silence, and a silently-drifting thesis is the worst thing to miss.
    if ctx.thesis_checks:
        breached_before = {
            r.check.raw
            for r in (
                evaluate_thesis_checks(ctx.thesis_checks, baseline)
                if baseline is not None
                else ()
            )
            if r.status == "breached"
        }
        for result in evaluate_thesis_checks(ctx.thesis_checks, current):
            if result.status != "breached":
                continue
            events.append(
                ThesisDrift(
                    ticker=current.ticker,
                    check=result.check.raw,
                    field=result.check.field,
                    observed=result.observed,
                    thesis=ctx.thesis,
                    newly=result.check.raw not in breached_before,
                )
            )

    # Diff and action events require a baseline run. Canonical order:
    # thesis_drift, price_move, target_move, consensus_shift, analyst_action,
    # earnings_reported, earnings_imminent, field_quarantined, field_recovered.
    if baseline is not None:
        for field, threshold, make in (
            (Field.PRICE, ctx.thresholds.price_move_pct, PriceMove),
            (Field.ANALYST_TARGET_MEAN, ctx.thresholds.target_move_pct, TargetMove),
        ):
            move = _numeric_move(field, threshold, make, baseline, current, latest_accepted)
            if move is not None:
                events.append(move)
        shift = _consensus_shift(baseline, current, latest_accepted)
        if shift is not None:
            events.append(shift)

        for record in sorted(new_actions, key=lambda r: (r.action_date, r.firm, r.to_grade)):
            events.append(
                AnalystAction(
                    ticker=record.ticker,
                    firm=record.firm,
                    action=record.action,
                    from_grade=record.from_grade,
                    to_grade=record.to_grade,
                    action_date=record.action_date,
                )
            )

        for result in sorted(new_earnings, key=lambda r: r.quarter_end):
            events.append(
                EarningsReported(
                    ticker=result.ticker,
                    quarter_end=result.quarter_end,
                    eps_actual=result.eps_actual,
                    eps_estimate=result.eps_estimate,
                    surprise_pct=_surprise_pct(result),
                )
            )

    earnings = current.values.get(Field.NEXT_EARNINGS_DATE)
    if earnings is not None:
        days_until = (earnings.value - today).days
        if 0 <= days_until <= ctx.thresholds.earnings_within_days:
            events.append(
                EarningsImminent(
                    ticker=current.ticker, earnings_date=earnings.value, days_until=days_until
                )
            )

    if baseline is not None:
        for field in sorted(current.quarantined):
            # "Went dark" is news whenever the field was EVER accepted before,
            # not only when it was accepted in the immediate baseline — an
            # outage gap (accepted → missing → quarantined) must not swallow
            # the headline, same reported-late-never-lost rule as the moves.
            # Already-quarantined-last-run is not a transition.
            if field in baseline.quarantined:
                continue
            if field in baseline.values or latest_accepted(field) is not None:
                events.append(
                    FieldQuarantined(
                        ticker=current.ticker, field=field, reasons=current.quarantined[field]
                    )
                )
        for field in sorted(baseline.quarantined):
            if field in current.values:
                events.append(FieldRecovered(ticker=current.ticker, field=field))

    return events


def _surprise_pct(result: EarningsResultRecord) -> float | None:
    """(actual − estimate) / |estimate| · 100, so a beat is positive whatever
    the estimate's sign (−0.50 actual against a −1.00 estimate is a +50%
    beat). Computed from the two stored facts, never taken from the source's
    own surprise figure (unit ambiguity is not worth importing). None when
    there is no estimate to be surprised against, or the estimate is zero —
    the division is undefined; the actual still reports."""
    if result.eps_estimate is None or result.eps_estimate == 0:
        return None
    return round((result.eps_actual - result.eps_estimate) / abs(result.eps_estimate) * 100, 1)


def _resolve_baseline(
    field: Field,
    baseline: Snapshot,
    latest_accepted: Callable[[Field], FieldValue | None],
) -> FieldValue | None:
    """The baseline snapshot's accepted value when it has one; otherwise the
    most recent accepted value from any earlier run (quarantine/outage gap
    fallback — reported late, never lost). Never a quarantined endpoint:
    recovery is FieldRecovered, not a fake move."""
    if field in baseline.values:
        return baseline.values[field]
    return latest_accepted(field)


def _numeric_move(
    field: Field,
    threshold: float,
    make: type[PriceMove] | type[TargetMove],
    baseline: Snapshot,
    current: Snapshot,
    latest_accepted: Callable[[Field], FieldValue | None],
) -> PriceMove | TargetMove | None:
    new = current.values.get(field)
    if new is None:
        return None  # quarantined/missing now is a verdict transition, not a move
    old = _resolve_baseline(field, baseline, latest_accepted)
    if old is None:
        return None  # first accepted observation anywhere — baseline established
    pct = round((new.value - old.value) / old.value * 100, 2)
    if abs(pct) < threshold:
        return None
    return make(
        ticker=current.ticker,
        old=old.value,
        new=new.value,
        pct=pct,
        threshold=threshold,
        old_as_of=old.fetched_at,
    )


def _consensus_shift(
    baseline: Snapshot,
    current: Snapshot,
    latest_accepted: Callable[[Field], FieldValue | None],
) -> ConsensusShift | None:
    new = current.values.get(Field.ANALYST_RATING)
    if new is None:
        return None
    old = _resolve_baseline(Field.ANALYST_RATING, baseline, latest_accepted)
    if old is None or old.value == new.value:
        return None
    return ConsensusShift(
        ticker=current.ticker,
        old=old.value,
        new=new.value,
        direction=_direction(old.value, new.value),
    )


def _direction(old: str, new: str) -> Literal["up", "down", "unclear"]:
    old_rank = _RATING_RANK.get(old)
    new_rank = _RATING_RANK.get(new)
    if old_rank is None or new_rank is None:
        return "unclear"
    return "up" if new_rank > old_rank else "down"
