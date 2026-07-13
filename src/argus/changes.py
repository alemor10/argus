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
    FieldQuarantined,
    FieldRecovered,
    FieldValue,
    PriceMove,
    Snapshot,
    TargetMove,
    TickerContext,
)

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
) -> list[ChangeEvent]:
    """Diff one ticker's snapshots into typed events.

    `latest_accepted` is the injected store lookup used only when the baseline
    snapshot lacks an accepted value for a field (quarantine/outage gap
    fallback). A `baseline` of None means first-ever run for this ticker:
    no diff events AND no analyst-action events — the source's entire dated
    history is "first seen" on that run, and 14 years of rating actions is
    baseline, not news (a real first live run produced 1,100 lines of it).
    The rows are still stored with first_seen_run_id, so from the next run
    on, only genuinely new actions fire. Only EarningsImminent (a state
    event) fires on a first run; the digest lists the ticker under
    "baseline established".
    """
    events: list[ChangeEvent] = []

    # Diff and action events require a baseline run. Canonical order:
    # price_move, target_move, consensus_shift, analyst_action,
    # earnings_imminent, field_quarantined, field_recovered.
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
