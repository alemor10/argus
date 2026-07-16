"""Change-detection tests. detect() is pure: snapshot pairs in, exact event
lists out — including the quarantine-gap fallback (a change is reported late,
never lost) and the canonical event ordering the digest and store rely on."""

from datetime import UTC, date, datetime, timedelta

from argus.changes import detect, has_new_information
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
    MacroLineCrossed,
    MacroPrint,
    MacroShift,
    MacroSpec,
    PriceMove,
    QuarantineHit,
    RunReport,
    Snapshot,
    TargetMove,
    ThesisDrift,
    Thresholds,
    TickerContext,
    TickerReport,
)
from argus.thesis import parse_thesis_check

NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)
YESTERDAY = datetime(2026, 7, 11, 14, 0, tzinfo=UTC)
LAST_WEEK = datetime(2026, 7, 5, 14, 0, tzinfo=UTC)
TWO_WEEKS_AGO = datetime(2026, 6, 28, 14, 0, tzinfo=UTC)
TODAY = date(2026, 7, 12)

STALE_HIT = QuarantineHit(code=QuarantineCode.STALE, detail="observed_at 6 days old")


def _fv(field, value, fetched_at=LAST_WEEK, source=Source.YAHOO, observed_at=None):
    return FieldValue(
        field=field, value=value, source=source, fetched_at=fetched_at, observed_at=observed_at
    )


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

    def test_reminder_already_seen_at_baseline_is_not_newly(self):
        """Daily cadence: the same date was inside the window yesterday — the
        re-fired reminder must not re-page under event-gated delivery."""
        d = TODAY + timedelta(days=3)
        baseline = _snap(
            1, {Field.NEXT_EARNINGS_DATE: _fv(Field.NEXT_EARNINGS_DATE, d)}, as_of=YESTERDAY
        )
        [event] = self._events(d, baseline=baseline)
        assert event.newly is False

    def test_entering_the_window_is_newly(self):
        """Known at baseline but OUTSIDE the window then — crossing in is news."""
        d = TODAY + timedelta(days=7)  # 14 days out as of LAST_WEEK
        baseline = _snap(
            1, {Field.NEXT_EARNINGS_DATE: _fv(Field.NEXT_EARNINGS_DATE, d)}, as_of=LAST_WEEK
        )
        [event] = self._events(d, baseline=baseline)
        assert event.newly is True

    def test_rescheduled_date_is_newly(self):
        old = TODAY + timedelta(days=2)
        new = TODAY + timedelta(days=5)
        baseline = _snap(
            1, {Field.NEXT_EARNINGS_DATE: _fv(Field.NEXT_EARNINGS_DATE, old)}, as_of=YESTERDAY
        )
        [event] = self._events(new, baseline=baseline)
        assert event.newly is True

    def test_gap_fallback_prevents_repage(self):
        """The baseline run missed the field (outage) but an older run had the
        same in-window date — the reader HAS seen it; not newly."""
        d = TODAY + timedelta(days=3)
        baseline = _snap(2, as_of=YESTERDAY)  # field absent this run
        older = _fv(Field.NEXT_EARNINGS_DATE, d, fetched_at=TWO_WEEKS_AGO)
        current = _snap(
            3, {Field.NEXT_EARNINGS_DATE: _fv(Field.NEXT_EARNINGS_DATE, d, fetched_at=NOW)}
        )
        [event] = _detect(
            baseline,
            current,
            latest_accepted=lambda f: older if f is Field.NEXT_EARNINGS_DATE else None,
        )
        assert event.newly is False


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


def _macro_ctx(ticker="^VIX", **spec_overrides):
    spec = dict(label="VIX")
    spec.update(spec_overrides)
    return TickerContext(ticker=ticker, macro=MacroSpec(**spec))


def _vix_snap(run_id, value, *, as_of=NOW, fetched_at=None):
    return _snap(
        run_id,
        {Field.PRICE: _fv(Field.PRICE, value, fetched_at=fetched_at or as_of)},
        ticker="^VIX",
        as_of=as_of,
    )


class TestMacroShift:
    def _detect_shift(self, old_value, new_value, **spec):
        ctx = _macro_ctx(alert_move=3.0, **spec)
        baseline = _vix_snap(1, old_value, as_of=LAST_WEEK)
        current = _vix_snap(2, new_value)
        return detect(baseline, current, ctx, (), TODAY, latest_accepted=_no_fallback)

    def test_at_or_above_threshold_fires_with_delta_and_window(self):
        events = self._detect_shift(15.0, 25.4)
        assert events == [
            MacroShift(
                ticker="^VIX", label="VIX", old=15.0, new=25.4, delta=10.4,
                unit="", decimals=2, threshold=3.0, old_as_of=LAST_WEEK,
            )
        ]

    def test_below_threshold_is_silent(self):
        assert self._detect_shift(15.0, 16.0) == []

    def test_delta_rounds_to_decimals_before_the_compare(self):
        """The printed number must justify the event (the PriceMove rule):
        a raw 0.149 move rounds to the 0.15 threshold and fires; a raw
        0.1449 rounds to 0.14 and stays silent."""
        ctx = _macro_ctx(ticker="^TNX", label="US 10Y yield", unit="%", alert_move=0.15)
        baseline = _snap(
            1, {Field.PRICE: _fv(Field.PRICE, 4.545)}, ticker="^TNX", as_of=LAST_WEEK
        )
        fires = _snap(2, {Field.PRICE: _fv(Field.PRICE, 4.694, fetched_at=NOW)}, ticker="^TNX")
        [event] = detect(baseline, fires, ctx, (), TODAY, latest_accepted=_no_fallback)
        assert event.delta == 0.15
        quiet = _snap(2, {Field.PRICE: _fv(Field.PRICE, 4.6899, fetched_at=NOW)}, ticker="^TNX")
        assert detect(baseline, quiet, ctx, (), TODAY, latest_accepted=_no_fallback) == []

    def test_gap_falls_back_to_latest_accepted(self):
        ctx = _macro_ctx(alert_move=3.0)
        baseline = _snap(2, ticker="^VIX", as_of=YESTERDAY)  # outage: field absent
        current = _vix_snap(3, 25.4)
        older = _fv(Field.PRICE, 15.0, fetched_at=TWO_WEEKS_AGO)
        [event] = detect(
            baseline, current, ctx, (), TODAY,
            latest_accepted=lambda f: older if f is Field.PRICE else None,
        )
        assert event.kind == "macro_shift"
        assert event.old_as_of == TWO_WEEKS_AGO  # reported late, never lost

    def test_first_run_establishes_baseline_silently(self):
        ctx = _macro_ctx(alert_move=3.0)
        assert detect(None, _vix_snap(1, 25.4), ctx, (), TODAY, latest_accepted=_no_fallback) == []

    def test_no_alert_move_never_shifts(self):
        ctx = _macro_ctx()  # alert_move None
        baseline = _vix_snap(1, 15.0, as_of=LAST_WEEK)
        current = _vix_snap(2, 45.0)
        assert detect(baseline, current, ctx, (), TODAY, latest_accepted=_no_fallback) == []

    def test_equity_machinery_is_off_for_macro_contexts(self):
        """A 69% move fires no PriceMove, an in-window earnings date no
        EarningsImminent — 5% of VIX is routine and nobody chose it."""
        ctx = _macro_ctx()
        baseline = _vix_snap(1, 15.0, as_of=LAST_WEEK)
        current = _snap(
            2,
            {
                Field.PRICE: _fv(Field.PRICE, 25.4, fetched_at=NOW),
                Field.NEXT_EARNINGS_DATE: _fv(
                    Field.NEXT_EARNINGS_DATE, TODAY + timedelta(days=3), fetched_at=NOW
                ),
            },
            ticker="^VIX",
        )
        assert detect(baseline, current, ctx, (), TODAY, latest_accepted=_no_fallback) == []

    def test_quarantined_now_is_a_verdict_transition_not_a_shift(self):
        ctx = _macro_ctx(alert_move=3.0)
        baseline = _vix_snap(1, 15.0, as_of=LAST_WEEK)
        current = _snap(2, quarantined={Field.PRICE: (STALE_HIT,)}, ticker="^VIX")
        events = detect(baseline, current, ctx, (), TODAY, latest_accepted=_never_called)
        assert events == [
            FieldQuarantined(ticker="^VIX", field=Field.PRICE, reasons=(STALE_HIT,))
        ]


class TestMacroLineCrossed:
    LINE = parse_thesis_check("price >= 25")

    def _ctx(self, *lines):
        return _macro_ctx(alert_when=tuple(lines or (self.LINE,)))

    def test_crossing_fires_newly(self):
        baseline = _vix_snap(1, 20.0, as_of=LAST_WEEK)
        current = _vix_snap(2, 25.4)
        events = detect(baseline, current, self._ctx(), (), TODAY, latest_accepted=_no_fallback)
        assert events == [
            MacroLineCrossed(
                ticker="^VIX", label="VIX", check="price >= 25", observed=25.4,
                unit="", decimals=2, newly=True,
            )
        ]

    def test_still_crossed_refires_but_not_newly(self):
        baseline = _vix_snap(1, 26.0, as_of=YESTERDAY)
        current = _vix_snap(2, 25.4)
        [event] = detect(baseline, current, self._ctx(), (), TODAY, latest_accepted=_no_fallback)
        assert event.newly is False  # suppression's failure mode is silence — refire, quietly

    def test_uncrossed_line_is_silent_the_inversion_regression(self):
        """THE inversion guard: thesis machinery fires when a condition stops
        holding — wired naively, a calm VIX (20 < 25) would page forever."""
        baseline = _vix_snap(1, 18.0, as_of=LAST_WEEK)
        current = _vix_snap(2, 20.0)
        assert detect(baseline, current, self._ctx(), (), TODAY, latest_accepted=_no_fallback) == []

    def test_first_run_crossing_fires(self):
        """A line crossed on day one needs no history (thesis-drift precedent)."""
        events = detect(None, _vix_snap(1, 25.4), self._ctx(), (), TODAY, latest_accepted=_no_fallback)
        assert [e.kind for e in events] == ["macro_line_crossed"]
        assert events[0].newly is True

    def test_quarantined_value_is_undeterminable_not_crossed(self):
        baseline = _vix_snap(1, 26.0, as_of=LAST_WEEK)
        current = _snap(2, quarantined={Field.PRICE: (STALE_HIT,)}, ticker="^VIX")
        events = detect(baseline, current, self._ctx(), (), TODAY, latest_accepted=_never_called)
        assert [e.kind for e in events] == ["field_quarantined"]


class TestMacroPrint:
    JUNE = datetime(2026, 6, 1, tzinfo=UTC)
    JULY = datetime(2026, 7, 1, tzinfo=UTC)

    def _cpi_ctx(self, **overrides):
        spec = dict(
            label="CPI inflation (YoY)", unit="%", decimals=1,
            source=Source.FRED, transform="yoy_pct", alert_on_release=True,
        )
        spec.update(overrides)
        return _macro_ctx(ticker="CPIAUCSL", **spec)

    def _cpi_snap(self, run_id, value, period, *, as_of=NOW):
        return _snap(
            run_id,
            {
                Field.ECON_VALUE: _fv(
                    Field.ECON_VALUE, value, source=Source.FRED,
                    fetched_at=as_of, observed_at=period,
                )
            },
            ticker="CPIAUCSL",
            as_of=as_of,
        )

    def test_new_period_fires_a_print(self):
        baseline = self._cpi_snap(1, 3.2, self.JUNE, as_of=LAST_WEEK)
        current = self._cpi_snap(2, 2.9, self.JULY)
        events = detect(baseline, current, self._cpi_ctx(), (), TODAY, latest_accepted=_no_fallback)
        assert events == [
            MacroPrint(
                ticker="CPIAUCSL", label="CPI inflation (YoY)", period=date(2026, 7, 1),
                value=2.9, prev_value=3.2, delta=-0.3, unit="%", decimals=1,
            )
        ]

    def test_same_period_refetch_is_silent(self):
        baseline = self._cpi_snap(1, 3.2, self.JUNE, as_of=YESTERDAY)
        current = self._cpi_snap(2, 3.2, self.JUNE)
        assert detect(baseline, current, self._cpi_ctx(), (), TODAY, latest_accepted=_no_fallback) == []

    def test_value_unchanged_new_period_still_fires(self):
        """The print is the news, not the delta — an unchanged unemployment
        rate on jobs day is still jobs day."""
        baseline = self._cpi_snap(1, 3.2, self.JUNE, as_of=LAST_WEEK)
        current = self._cpi_snap(2, 3.2, self.JULY)
        [event] = detect(baseline, current, self._cpi_ctx(), (), TODAY, latest_accepted=_no_fallback)
        assert event.kind == "macro_print"
        assert event.delta == 0.0

    def test_first_run_is_baseline_not_news(self):
        assert (
            detect(None, self._cpi_snap(1, 3.2, self.JUNE), self._cpi_ctx(), (), TODAY,
                   latest_accepted=_no_fallback)
            == []
        )

    def test_release_alerting_can_be_turned_off(self):
        baseline = self._cpi_snap(1, 3.2, self.JUNE, as_of=LAST_WEEK)
        current = self._cpi_snap(2, 2.9, self.JULY)
        ctx = self._cpi_ctx(alert_on_release=False)
        assert detect(baseline, current, ctx, (), TODAY, latest_accepted=_no_fallback) == []


class TestHasNewInformation:
    """The event-gated delivery decision — attribute-based: any event is news
    unless it marks itself a re-fired standing state (newly=False)."""

    def _report(self, *tickers):
        return RunReport(
            run_id=9, kind="watch", as_of=NOW, status="complete", tickers=tuple(tickers)
        )

    def _ticker(self, events=(), status="ok", ticker="NVDA"):
        return TickerReport(
            context=TickerContext(ticker=ticker), status=status, events=tuple(events)
        )

    def test_empty_run_is_quiet(self):
        assert has_new_information(self._report()) is False
        assert has_new_information(self._report(self._ticker())) is False

    def test_any_inherently_new_event_delivers(self):
        move = PriceMove(
            ticker="NVDA", old=100.0, new=110.0, pct=10.0, threshold=5.0, old_as_of=LAST_WEEK
        )
        assert has_new_information(self._report(self._ticker(events=(move,)))) is True

    def test_refired_standing_states_stay_quiet(self):
        standing = (
            ThesisDrift(
                ticker="NVDA", check="gross_margin >= 65%", field=Field.GROSS_MARGIN,
                observed=0.60, newly=False,
            ),
            EarningsImminent(
                ticker="NVDA", earnings_date=TODAY + timedelta(days=3), days_until=3, newly=False
            ),
        )
        assert has_new_information(self._report(self._ticker(events=standing))) is False

    def test_newly_breached_thesis_delivers(self):
        drift = ThesisDrift(
            ticker="NVDA", check="gross_margin >= 65%", field=Field.GROSS_MARGIN,
            observed=0.60, newly=True,
        )
        assert has_new_information(self._report(self._ticker(events=(drift,)))) is True

    def test_etf_rebalance_delivers(self):
        from argus.models import EtfRebalance

        report = self._report(self._ticker())
        report = report.model_copy(update={"etf_rebalances": (EtfRebalance(etf="SPY", added=("X",)),)})
        assert has_new_information(report) is True

    def test_failed_ticker_delivers(self):
        """A name going dark is news even with zero events."""
        assert has_new_information(self._report(self._ticker(status="failed"))) is True

    def test_one_eventful_ticker_among_quiet_ones_delivers(self):
        move = PriceMove(
            ticker="AAPL", old=100.0, new=110.0, pct=10.0, threshold=5.0, old_as_of=LAST_WEEK
        )
        report = self._report(self._ticker(), self._ticker(events=(move,), ticker="AAPL"))
        assert has_new_information(report) is True


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
