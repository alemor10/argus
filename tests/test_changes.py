"""Change-detection tests. detect() is pure: snapshot pairs in, exact event
lists out — including the quarantine-gap fallback (a change is reported late,
never lost) and the canonical event ordering the digest and store rely on."""

from datetime import UTC, date, datetime, timedelta

from argus.changes import detect
from argus.fields import Field, QuarantineCode, Source
from argus.models import (
    AnalystAction,
    AnalystActionRecord,
    ConsensusShift,
    EarningsImminent,
    EarningsReported,
    EarningsResultRecord,
    FieldQuarantined,
    FieldRecovered,
    FieldValue,
    PriceMove,
    QuarantineHit,
    Snapshot,
    TargetMove,
    Thresholds,
    TickerContext,
)

NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
LAST_WEEK = datetime(2026, 7, 5, 14, 0, tzinfo=UTC)
TWO_WEEKS_AGO = datetime(2026, 6, 28, 14, 0, tzinfo=UTC)
TODAY = date(2026, 7, 12)

STALE_HIT = QuarantineHit(code=QuarantineCode.STALE, detail="observed_at 6 days old")


def _fv(field, value, fetched_at=LAST_WEEK, source=Source.YAHOO):
    return FieldValue(field=field, value=value, source=source, fetched_at=fetched_at)


def _snap(run_id, values=None, quarantined=None, ticker="NVDA", as_of=NOW):
    return Snapshot(
        ticker=ticker, run_id=run_id, as_of=as_of, values=values or {}, quarantined=quarantined or {}
    )


def _ctx(**threshold_overrides):
    return TickerContext(ticker="NVDA", thresholds=Thresholds(**threshold_overrides))


def _no_fallback(field):
    return None


def _never_called(field):
    raise AssertionError(f"latest_accepted must not be consulted (asked for {field})")


def _detect(baseline, current, *, ctx=None, new_actions=(), new_earnings=(), today=TODAY,
            latest_accepted=_no_fallback):
    return detect(baseline, current, ctx or _ctx(), new_actions, today,
                  latest_accepted=latest_accepted, new_earnings=new_earnings)


class TestPriceMove:
    def test_six_percent_move_over_five_percent_threshold_fires(self):
        baseline = _snap(1, {Field.PRICE: _fv(Field.PRICE, 100.0)})
        current = _snap(2, {Field.PRICE: _fv(Field.PRICE, 106.0, fetched_at=NOW)})
        events = _detect(baseline, current, latest_accepted=_never_called)
        assert events == [
            PriceMove(ticker="NVDA", old=100.0, new=106.0, pct=6.0, threshold=5.0, old_as_of=LAST_WEEK)
        ]

    def test_below_threshold_is_silent(self):
        baseline = _snap(1, {Field.PRICE: _fv(Field.PRICE, 100.0)})
        current = _snap(2, {Field.PRICE: _fv(Field.PRICE, 104.0, fetched_at=NOW)})
        assert _detect(baseline, current) == []

    def test_exactly_at_threshold_fires(self):
        baseline = _snap(1, {Field.PRICE: _fv(Field.PRICE, 100.0)})
        current = _snap(2, {Field.PRICE: _fv(Field.PRICE, 105.0, fetched_at=NOW)})
        [event] = _detect(baseline, current)
        assert event.pct == 5.0

    def test_downward_move_fires_with_negative_pct(self):
        baseline = _snap(1, {Field.PRICE: _fv(Field.PRICE, 100.0)})
        current = _snap(2, {Field.PRICE: _fv(Field.PRICE, 94.0, fetched_at=NOW)})
        [event] = _detect(baseline, current)
        assert event.pct == -6.0

    def test_pct_is_rounded_to_two_decimals_before_the_threshold_check(self):
        """4.996% rounds to 5.00 — the rounded pct is what is compared (and
        what the digest prints; the printed number must justify the event)."""
        baseline = _snap(1, {Field.PRICE: _fv(Field.PRICE, 100.0)})
        current = _snap(2, {Field.PRICE: _fv(Field.PRICE, 104.996, fetched_at=NOW)})
        [event] = _detect(baseline, current)
        assert event.pct == 5.0

    def test_per_ticker_override_raises_the_bar(self):
        ctx = _ctx(price_move_pct=8.0)  # volatile name, raised bar
        baseline = _snap(1, {Field.PRICE: _fv(Field.PRICE, 100.0)})
        quiet = _snap(2, {Field.PRICE: _fv(Field.PRICE, 106.0, fetched_at=NOW)})
        assert _detect(baseline, quiet, ctx=ctx) == []
        loud = _snap(2, {Field.PRICE: _fv(Field.PRICE, 109.0, fetched_at=NOW)})
        [event] = _detect(baseline, loud, ctx=ctx)
        assert event.pct == 9.0
        assert event.threshold == 8.0

    def test_first_accepted_observation_establishes_baseline_silently(self):
        """Baseline run exists but never had an accepted price and neither did
        any earlier run → no move to report, the new value IS the baseline."""
        baseline = _snap(1)  # price absent entirely
        current = _snap(2, {Field.PRICE: _fv(Field.PRICE, 106.0, fetched_at=NOW)})
        assert _detect(baseline, current) == []

    def test_quarantined_current_value_is_not_a_move(self):
        baseline = _snap(1, {Field.PRICE: _fv(Field.PRICE, 100.0)})
        current = _snap(2, quarantined={Field.PRICE: (STALE_HIT,)})
        events = _detect(baseline, current, latest_accepted=_never_called)
        assert events == [FieldQuarantined(ticker="NVDA", field=Field.PRICE, reasons=(STALE_HIT,))]


class TestQuarantineGapFallback:
    """The decision-log call: both endpoints passed the gates, so suppressing
    the comparison would be a silent failure. Honesty comes from printing the
    (older) window, not hiding the event."""

    def test_outage_gap_falls_back_to_latest_accepted(self):
        # Baseline run never got a price (source down) — the accepted value
        # from an older run supplies the comparison endpoint.
        baseline = _snap(2)
        current = _snap(3, {Field.PRICE: _fv(Field.PRICE, 112.0, fetched_at=NOW)})
        older = _fv(Field.PRICE, 100.0, fetched_at=TWO_WEEKS_AGO)
        events = _detect(
            baseline, current, latest_accepted=lambda f: older if f is Field.PRICE else None
        )
        assert events == [
            PriceMove(
                ticker="NVDA", old=100.0, new=112.0, pct=12.0, threshold=5.0, old_as_of=TWO_WEEKS_AGO
            )
        ]
        # The window is the OLDER run's timestamp — reported late, never lost.
        assert events[0].old_as_of == TWO_WEEKS_AGO

    def test_recovery_emits_recovered_plus_move_vs_latest_accepted(self):
        """Price was quarantined in the baseline run; it recovered now. The
        move is computed against the last ACCEPTED value (older run), never
        against the quarantined number — which is absent from Snapshot.values
        by construction, so a fake move is unrepresentable."""
        baseline = _snap(2, quarantined={Field.PRICE: (STALE_HIT,)})
        current = _snap(3, {Field.PRICE: _fv(Field.PRICE, 112.0, fetched_at=NOW)})
        older = _fv(Field.PRICE, 100.0, fetched_at=TWO_WEEKS_AGO)
        events = _detect(
            baseline, current, latest_accepted=lambda f: older if f is Field.PRICE else None
        )
        assert events == [
            PriceMove(
                ticker="NVDA", old=100.0, new=112.0, pct=12.0, threshold=5.0, old_as_of=TWO_WEEKS_AGO
            ),
            FieldRecovered(ticker="NVDA", field=Field.PRICE),
        ]

    def test_recovery_without_accepted_history_is_just_recovered(self):
        baseline = _snap(2, quarantined={Field.PRICE: (STALE_HIT,)})
        current = _snap(3, {Field.PRICE: _fv(Field.PRICE, 112.0, fetched_at=NOW)})
        assert _detect(baseline, current) == [FieldRecovered(ticker="NVDA", field=Field.PRICE)]


class TestTargetMove:
    def test_target_move_uses_its_own_threshold(self):
        baseline = _snap(1, {Field.ANALYST_TARGET_MEAN: _fv(Field.ANALYST_TARGET_MEAN, 100.0)})
        quiet = _snap(2, {Field.ANALYST_TARGET_MEAN: _fv(Field.ANALYST_TARGET_MEAN, 109.0, fetched_at=NOW)})
        assert _detect(baseline, quiet) == []  # 9% < the 10% target threshold
        loud = _snap(2, {Field.ANALYST_TARGET_MEAN: _fv(Field.ANALYST_TARGET_MEAN, 112.0, fetched_at=NOW)})
        assert _detect(baseline, loud) == [
            TargetMove(ticker="NVDA", old=100.0, new=112.0, pct=12.0, threshold=10.0, old_as_of=LAST_WEEK)
        ]


class TestConsensusShift:
    def _shift(self, old, new):
        baseline = _snap(1, {Field.ANALYST_RATING: _fv(Field.ANALYST_RATING, old)})
        current = _snap(2, {Field.ANALYST_RATING: _fv(Field.ANALYST_RATING, new, fetched_at=NOW)})
        return _detect(baseline, current)

    def test_upgrade_along_the_scale(self):
        assert self._shift("hold", "buy") == [
            ConsensusShift(ticker="NVDA", old="hold", new="buy", direction="up")
        ]

    def test_downgrade_along_the_scale(self):
        [event] = self._shift("buy", "underperform")
        assert event.direction == "down"
        [event] = self._shift("strong_buy", "sell")
        assert event.direction == "down"

    def test_off_scale_grade_is_unclear_but_never_suppressed(self):
        [event] = self._shift("moderate buy", "hold")
        assert event == ConsensusShift(ticker="NVDA", old="moderate buy", new="hold", direction="unclear")

    def test_unchanged_rating_is_silent(self):
        assert self._shift("buy", "buy") == []

    def test_rating_gap_falls_back_to_latest_accepted(self):
        baseline = _snap(2, quarantined={Field.ANALYST_RATING: (STALE_HIT,)})
        current = _snap(3, {Field.ANALYST_RATING: _fv(Field.ANALYST_RATING, "hold", fetched_at=NOW)})
        older = _fv(Field.ANALYST_RATING, "buy", fetched_at=TWO_WEEKS_AGO)
        events = _detect(
            baseline, current, latest_accepted=lambda f: older if f is Field.ANALYST_RATING else None
        )
        assert events == [
            ConsensusShift(ticker="NVDA", old="buy", new="hold", direction="down"),
            FieldRecovered(ticker="NVDA", field=Field.ANALYST_RATING),
        ]


class TestAnalystActions:
    RECORD = AnalystActionRecord(
        ticker="NVDA",
        action_date=date(2026, 7, 10),
        firm="Morgan Stanley",
        action="down",
        from_grade="Overweight",
        to_grade="Equal-Weight",
        source=Source.YAHOO,
        fetched_at=NOW,
    )

    def test_each_new_action_maps_field_for_field(self):
        events = _detect(_snap(1), _snap(2), new_actions=[self.RECORD])
        assert events == [
            AnalystAction(
                ticker="NVDA",
                firm="Morgan Stanley",
                action="down",
                from_grade="Overweight",
                to_grade="Equal-Weight",
                action_date=date(2026, 7, 10),
            )
        ]

    def test_no_new_actions_no_events(self):
        assert _detect(_snap(1), _snap(2), new_actions=[]) == []


class TestEarningsReported:
    def _record(self, actual, estimate, quarter=date(2026, 6, 30)):
        return EarningsResultRecord(
            ticker="NVDA", quarter_end=quarter, eps_actual=actual, eps_estimate=estimate,
            source=Source.YAHOO, fetched_at=NOW,
        )

    def test_each_new_result_maps_field_for_field_with_computed_surprise(self):
        events = _detect(_snap(1), _snap(2), new_earnings=[self._record(1.05, 0.93)])
        assert events == [
            EarningsReported(
                ticker="NVDA", quarter_end=date(2026, 6, 30),
                eps_actual=1.05, eps_estimate=0.93, surprise_pct=12.9,
            )
        ]

    def test_miss_is_a_negative_surprise(self):
        [event] = _detect(_snap(1), _snap(2), new_earnings=[self._record(0.80, 1.00)])
        assert event.surprise_pct == -20.0

    def test_beating_a_negative_estimate_is_a_positive_surprise(self):
        """Loss smaller than feared: −0.50 against a −1.00 estimate is a beat,
        and |estimate| in the denominator keeps its sign positive."""
        [event] = _detect(_snap(1), _snap(2), new_earnings=[self._record(-0.50, -1.00)])
        assert event.surprise_pct == 50.0

    def test_no_estimate_reports_the_actual_without_a_surprise(self):
        [event] = _detect(_snap(1), _snap(2), new_earnings=[self._record(1.05, None)])
        assert event.eps_actual == 1.05
        assert event.eps_estimate is None
        assert event.surprise_pct is None

    def test_zero_estimate_reports_without_a_surprise(self):
        """The division is undefined at zero — the actual still reports."""
        [event] = _detect(_snap(1), _snap(2), new_earnings=[self._record(0.10, 0.0)])
        assert event.eps_estimate == 0.0
        assert event.surprise_pct is None

    def test_results_sort_by_quarter_end(self):
        newer = self._record(1.05, 0.93, quarter=date(2026, 6, 30))
        older = self._record(0.98, 1.00, quarter=date(2026, 3, 31))
        events = _detect(_snap(1), _snap(2), new_earnings=[newer, older])
        assert [e.quarter_end for e in events] == [date(2026, 3, 31), date(2026, 6, 30)]

    def test_no_new_results_no_events(self):
        assert _detect(_snap(1), _snap(2), new_earnings=[]) == []


class TestEarningsImminent:
    def _events(self, earnings_date, ctx=None, baseline=_snap(1)):
        current = _snap(
            2, {Field.NEXT_EARNINGS_DATE: _fv(Field.NEXT_EARNINGS_DATE, earnings_date, fetched_at=NOW)}
        )
        return _detect(baseline, current, ctx=ctx)

    def test_earnings_today_fires_with_zero_days(self):
        assert self._events(TODAY) == [
            EarningsImminent(ticker="NVDA", earnings_date=TODAY, days_until=0)
        ]

    def test_exactly_at_window_edge_fires(self):
        d = TODAY + timedelta(days=7)
        assert self._events(d) == [EarningsImminent(ticker="NVDA", earnings_date=d, days_until=7)]

    def test_one_day_past_the_window_is_silent(self):
        assert self._events(TODAY + timedelta(days=8)) == []

    def test_past_earnings_date_is_silent(self):
        assert self._events(TODAY - timedelta(days=1)) == []

    def test_per_ticker_window_override(self):
        ctx = _ctx(earnings_within_days=2)
        assert self._events(TODAY + timedelta(days=2), ctx=ctx) != []
        assert self._events(TODAY + timedelta(days=3), ctx=ctx) == []

    def test_refires_inside_the_window_even_on_first_run(self):
        """State event, not a diff — fires with baseline=None too."""
        d = TODAY + timedelta(days=4)
        assert self._events(d, baseline=None) == [
            EarningsImminent(ticker="NVDA", earnings_date=d, days_until=4)
        ]


class TestFieldQuarantined:
    def test_accepted_to_quarantined_transition_carries_reasons(self):
        baseline = _snap(1, {Field.ANALYST_TARGET_MEAN: _fv(Field.ANALYST_TARGET_MEAN, 35.0)})
        hits = (QuarantineHit(code=QuarantineCode.TARGET_PRICE_RATIO, detail="3.19 outside [0.3, 3.0]"),)
        current = _snap(2, quarantined={Field.ANALYST_TARGET_MEAN: hits})
        events = _detect(baseline, current, latest_accepted=_never_called)
        assert events == [
            FieldQuarantined(ticker="NVDA", field=Field.ANALYST_TARGET_MEAN, reasons=hits)
        ]

    def test_still_quarantined_is_not_a_transition(self):
        baseline = _snap(1, quarantined={Field.PRICE: (STALE_HIT,)})
        current = _snap(2, quarantined={Field.PRICE: (STALE_HIT,)})
        assert _detect(baseline, current) == []

    def test_never_accepted_then_quarantined_is_not_a_transition(self):
        baseline = _snap(1)  # field absent: no source offered it
        current = _snap(2, quarantined={Field.PRICE: (STALE_HIT,)})
        assert _detect(baseline, current) == []

    def test_went_dark_fires_across_an_outage_gap(self):
        """accepted → missing-for-a-run → quarantined must still headline:
        latest_accepted proves the signal existed, so its loss is news —
        the same reported-late-never-lost rule the numeric moves follow."""
        baseline = _snap(2)  # outage run: field absent
        current = _snap(3, quarantined={Field.PRICE: (STALE_HIT,)})
        older = _fv(Field.PRICE, 100.0)
        events = _detect(
            baseline, current, latest_accepted=lambda f: older if f is Field.PRICE else None
        )
        assert [e.kind for e in events] == ["field_quarantined"]

    def test_still_quarantined_does_not_refire(self):
        baseline = _snap(1, quarantined={Field.PRICE: (STALE_HIT,)})
        current = _snap(2, quarantined={Field.PRICE: (STALE_HIT,)})
        older = _fv(Field.PRICE, 100.0)
        events = _detect(
            baseline, current, latest_accepted=lambda f: older if f is Field.PRICE else None
        )
        assert events == []


class TestFirstRun:
    """baseline=None: no diff events AND no analyst-action or earnings-
    reported events — the source's entire dated history is baseline then, not
    news (a real first live run produced 1,100 lines of 2012-era actions;
    the earnings feed likewise hands over its reported quarters). Only state
    events (EarningsImminent) fire, and latest_accepted is never consulted."""

    FIRST_RUN_EARNINGS = EarningsResultRecord(
        ticker="NVDA", quarter_end=date(2026, 4, 26), eps_actual=0.81, eps_estimate=0.75,
        source=Source.YAHOO, fetched_at=NOW,
    )

    def test_only_state_events_fire(self):
        current = _snap(
            1,
            {
                Field.PRICE: _fv(Field.PRICE, 106.0, fetched_at=NOW),
                Field.ANALYST_RATING: _fv(Field.ANALYST_RATING, "buy", fetched_at=NOW),
                Field.NEXT_EARNINGS_DATE: _fv(
                    Field.NEXT_EARNINGS_DATE, TODAY + timedelta(days=3), fetched_at=NOW
                ),
            },
            quarantined={Field.ANALYST_TARGET_MEAN: (STALE_HIT,)},
        )
        events = _detect(
            None,
            current,
            new_actions=[TestAnalystActions.RECORD],
            new_earnings=[self.FIRST_RUN_EARNINGS],
            latest_accepted=_never_called,
        )
        assert [e.kind for e in events] == ["earnings_imminent"]

    def test_second_run_emits_only_genuinely_new_actions(self):
        """The history stored on run 1 is dedup'd by first_seen_run_id, so a
        baseline-bearing run emits exactly the new_actions handed to it."""
        baseline = _snap(1, {Field.PRICE: _fv(Field.PRICE, 100.0)})
        current = _snap(2, {Field.PRICE: _fv(Field.PRICE, 101.0, fetched_at=NOW)})
        events = _detect(baseline, current, new_actions=[TestAnalystActions.RECORD])
        assert [e.kind for e in events] == ["analyst_action"]

    def test_second_run_emits_only_genuinely_new_earnings(self):
        baseline = _snap(1, {Field.PRICE: _fv(Field.PRICE, 100.0)})
        current = _snap(2, {Field.PRICE: _fv(Field.PRICE, 101.0, fetched_at=NOW)})
        events = _detect(baseline, current, new_earnings=[self.FIRST_RUN_EARNINGS])
        assert [e.kind for e in events] == ["earnings_reported"]


class TestCanonicalOrdering:
    def test_all_kinds_emerge_in_canonical_order(self):
        baseline = _snap(
            1,
            {
                Field.PRICE: _fv(Field.PRICE, 100.0),
                Field.ANALYST_TARGET_MEAN: _fv(Field.ANALYST_TARGET_MEAN, 200.0),
                Field.ANALYST_RATING: _fv(Field.ANALYST_RATING, "buy"),
                Field.PE_TTM: _fv(Field.PE_TTM, 30.0),
                Field.DEBT_TO_EQUITY: _fv(Field.DEBT_TO_EQUITY, 0.4),
            },
            quarantined={Field.GROSS_MARGIN: (STALE_HIT,)},
        )
        current = _snap(
            2,
            {
                Field.PRICE: _fv(Field.PRICE, 110.0, fetched_at=NOW),
                Field.ANALYST_TARGET_MEAN: _fv(Field.ANALYST_TARGET_MEAN, 240.0, fetched_at=NOW),
                Field.ANALYST_RATING: _fv(Field.ANALYST_RATING, "hold", fetched_at=NOW),
                Field.NEXT_EARNINGS_DATE: _fv(
                    Field.NEXT_EARNINGS_DATE, TODAY + timedelta(days=3), fetched_at=NOW
                ),
                Field.GROSS_MARGIN: _fv(Field.GROSS_MARGIN, 0.62, fetched_at=NOW),
            },
            quarantined={Field.PE_TTM: (STALE_HIT,), Field.DEBT_TO_EQUITY: (STALE_HIT,)},
        )
        actions = [
            AnalystActionRecord(
                ticker="NVDA", action_date=date(2026, 7, 11), firm="Morgan Stanley",
                action="down", to_grade="Equal-Weight", source=Source.YAHOO, fetched_at=NOW,
            ),
            AnalystActionRecord(
                ticker="NVDA", action_date=date(2026, 7, 10), firm="Citi",
                action="up", to_grade="Buy", source=Source.YAHOO, fetched_at=NOW,
            ),
            AnalystActionRecord(
                ticker="NVDA", action_date=date(2026, 7, 10), firm="Barclays",
                action="init", to_grade="Overweight", source=Source.YAHOO, fetched_at=NOW,
            ),
        ]
        earnings = [
            EarningsResultRecord(
                ticker="NVDA", quarter_end=date(2026, 6, 30), eps_actual=1.05,
                eps_estimate=0.93, source=Source.YAHOO, fetched_at=NOW,
            ),
        ]
        events = _detect(baseline, current, new_actions=actions, new_earnings=earnings)
        assert [e.kind for e in events] == [
            "price_move",
            "target_move",
            "consensus_shift",
            "analyst_action",
            "analyst_action",
            "analyst_action",
            "earnings_reported",
            "earnings_imminent",
            "field_quarantined",
            "field_quarantined",
            "field_recovered",
        ]
        # Actions sub-sorted by action_date then firm.
        assert [(e.action_date, e.firm) for e in events[3:6]] == [
            (date(2026, 7, 10), "Barclays"),
            (date(2026, 7, 10), "Citi"),
            (date(2026, 7, 11), "Morgan Stanley"),
        ]
        # Quarantine transitions sub-sorted by field.
        assert [e.field for e in events[8:10]] == [Field.DEBT_TO_EQUITY, Field.PE_TTM]
        assert events[10] == FieldRecovered(ticker="NVDA", field=Field.GROSS_MARGIN)
