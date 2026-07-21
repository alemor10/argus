"""Scout self-scoring: the pure fixed-horizon return/alpha math + aggregation,
and the store round-trip (persisted marks reproduce the digest scorecard).

Honesty invariants under test: every proposal is scored from its first
appearance (dropped names still tracked); a name is measured at each FIXED
horizon (4/13/26/52 weeks) it has matured past, and a horizon return is locked
once measured; names partition into matured / pending (priced but too young) /
unpriceable (no entry price), never a silent zero; and returns use both
endpoints of the same adjusted series. The min-sample gate withholds a
horizon's medians until enough names have matured.
"""

from datetime import date, timedelta

from argus import scorecard
from argus.models import ScorecardMark


def _daily(start: date, days: int, weekly_rate: float) -> list[tuple[date, float]]:
    """A daily price path from `start` for `days` calendar days: base 100,
    compounding `weekly_rate` per week so any horizon date lands on a real
    bar. One point per day → _price_on_or_after always finds an exact match."""
    return [
        (start + timedelta(days=d), 100.0 * (1 + weekly_rate) ** (d / 7.0))
        for d in range(days + 1)
    ]


AS_OF = date(2026, 7, 13)


class TestComputeMarks:
    def test_one_mark_per_matured_horizon_plus_entry_sentinel(self):
        # Proposed 30 weeks before AS_OF → 4/13/26 matured, 52 not.
        first = AS_OF - timedelta(weeks=30)
        series = _daily(first, days=30 * 7, weekly_rate=0.01)  # name +1%/wk
        spy = _daily(first, days=30 * 7, weekly_rate=0.005)  # SPY +0.5%/wk
        marks, unpriceable = scorecard.compute_marks([("AAA", first)], {"AAA": series}, spy, AS_OF)
        assert unpriceable == 0
        horizons = sorted(m.horizon_weeks for m in marks)
        assert horizons == [0, 4, 13, 26]  # entry sentinel + three matured horizons
        m4 = next(m for m in marks if m.horizon_weeks == 4)
        assert abs(m4.name_return - ((1.01) ** 4 - 1)) < 1e-6
        assert m4.alpha > 0  # beat SPY

    def test_horizon_return_is_locked_not_moving(self):
        # The 4-week mark is measured from the week-4 close, NOT from as_of —
        # so a later as_of leaves the 4-week number unchanged.
        first = AS_OF - timedelta(weeks=20)
        series = _daily(first, days=20 * 7, weekly_rate=0.02)
        spy = _daily(first, days=20 * 7, weekly_rate=0.0)
        m_now = next(
            m for m in scorecard.compute_marks([("A", first)], {"A": series}, spy, AS_OF)[0]
            if m.horizon_weeks == 4
        )
        later = AS_OF + timedelta(weeks=5)
        series2 = _daily(first, days=25 * 7, weekly_rate=0.02)
        spy2 = _daily(first, days=25 * 7, weekly_rate=0.0)
        m_later = next(
            m for m in scorecard.compute_marks([("A", first)], {"A": series2}, spy2, later)[0]
            if m.horizon_weeks == 4
        )
        assert abs(m_now.name_return - m_later.name_return) < 1e-9

    def test_too_young_name_gets_only_the_entry_sentinel(self):
        # Proposed 2 weeks ago → priced, but no horizon (4wk) has matured.
        first = AS_OF - timedelta(weeks=2)
        series = _daily(first, days=14, weekly_rate=0.01)
        marks, unpriceable = scorecard.compute_marks([("AAA", first)], {"AAA": series}, series, AS_OF)
        assert unpriceable == 0
        assert [m.horizon_weeks for m in marks] == [0]  # sentinel only

    def test_entry_is_first_tradeable_close_on_or_after(self):
        # A weekend proposal prices at the NEXT session (Monday), not the prior
        # Friday — what you could actually transact at.
        first = date(2026, 1, 3)  # Saturday
        # Monday close 52, then the 4-week horizon close 60.
        series = [(date(2026, 1, 5), 52.0), (date(2026, 1, 5) + timedelta(weeks=4), 60.0)]
        marks, _ = scorecard.compute_marks([("AAA", first)], {"AAA": series}, series, AS_OF)
        m4 = next(m for m in marks if m.horizon_weeks == 4)
        assert m4.name_return == (60.0 / 52.0 - 1.0)  # entry = Monday close

    def test_matured_horizon_with_a_price_gap_is_skipped_not_zeroed(self):
        # Delisted mid-window: entry + 4wk close exist, but the series ends
        # before the 13-week horizon → no 13wk mark (a gap, not a zero).
        first = AS_OF - timedelta(weeks=20)
        # bars at entry and ~week 4 only, then nothing.
        series = [(first, 100.0), (first + timedelta(weeks=4), 110.0)]
        spy = _daily(first, days=20 * 7, weekly_rate=0.0)
        marks, _ = scorecard.compute_marks([("AAA", first)], {"AAA": series}, spy, AS_OF)
        horizons = sorted(m.horizon_weeks for m in marks)
        assert horizons == [0, 4]  # 13/26 skipped for the price gap

    def test_unpriceable_name_is_excluded_not_zeroed(self):
        first = AS_OF - timedelta(weeks=6)
        good = _daily(first, days=6 * 7, weekly_rate=0.03)
        marks, unpriceable = scorecard.compute_marks(
            [("GOOD", first), ("DELISTED", first)],
            {"GOOD": good, "DELISTED": None},
            _daily(first, days=6 * 7, weekly_rate=0.0),
            AS_OF,
        )
        assert unpriceable == 1
        assert "DELISTED" not in {m.ticker for m in marks}
        assert "GOOD" in {m.ticker for m in marks}

    def test_missing_spy_makes_everything_unpriceable(self):
        first = AS_OF - timedelta(weeks=6)
        marks, unpriceable = scorecard.compute_marks(
            [("AAA", first)], {"AAA": _daily(first, days=6 * 7, weekly_rate=0.03)}, None, AS_OF
        )
        assert marks == [] and unpriceable == 1

    def test_proposal_predating_history_is_unpriceable(self):
        first = AS_OF - timedelta(weeks=20)
        # History starts well after the proposal (>1wk gap) → no honest entry.
        series = _daily(first + timedelta(weeks=6), days=14 * 7, weekly_rate=0.01)
        marks, unpriceable = scorecard.compute_marks([("AAA", first)], {"AAA": series}, series, AS_OF)
        assert marks == [] and unpriceable == 1


class TestSummarize:
    def _mark(self, ticker, horizon, name_ret, spy_ret):
        return ScorecardMark(
            ticker=ticker,
            first_proposed_at=AS_OF - timedelta(weeks=horizon),
            horizon_weeks=horizon,
            name_return=name_ret,
            spy_return=spy_ret,
        )

    def _sentinel(self, ticker):
        return ScorecardMark(
            ticker=ticker, first_proposed_at=AS_OF, horizon_weeks=0,
            name_return=0.0, spy_return=0.0,
        )

    def test_horizon_cohorts_and_gate(self):
        marks = [
            self._sentinel("A"), self._mark("A", 4, 0.02, 0.01),  # 4wk, beats
            self._sentinel("B"), self._mark("B", 4, 0.10, 0.04),  # 4wk, beats
            self._sentinel("C"), self._mark("C", 4, -0.05, 0.04),  # 4wk, lags
        ]
        card = scorecard.summarize(marks, AS_OF, unpriceable=1)
        by_h = {c.horizon_weeks: c for c in card.cohorts}
        four = by_h[4]
        assert four.n == 3 and four.enough is True  # MIN_SAMPLE == 3
        assert four.beat_spy == 2
        assert four.median_return == 0.02  # median(0.10, -0.05, 0.02)
        assert card.overall_n == 3  # A, B, C matured
        assert card.unpriceable == 1

    def test_min_sample_gate_withholds_but_counts(self):
        marks = [self._sentinel("A"), self._mark("A", 4, 0.05, 0.01)]  # n=1 < 3
        card = scorecard.summarize(marks, AS_OF, unpriceable=0)
        (four,) = card.cohorts
        assert four.n == 1 and four.enough is False
        assert card.overall_label == ""  # no headline until the gate clears

    def test_headline_is_longest_gated_horizon(self):
        marks = []
        for t in ("A", "B", "C"):
            marks += [self._sentinel(t), self._mark(t, 4, 0.05, 0.02), self._mark(t, 13, 0.09, 0.03)]
        card = scorecard.summarize(marks, AS_OF, unpriceable=0)
        assert card.overall_label == "13 weeks"  # longest with n>=3
        assert card.overall_horizon_n == 3

    def test_pending_counts_priced_but_unmatured_names(self):
        marks = [self._sentinel("YOUNG")]  # priced, no horizon
        card = scorecard.summarize(marks, AS_OF, unpriceable=2)
        assert card.overall_n == 0 and card.pending == 1 and card.unpriceable == 2

    def test_empty_marks_yields_empty_card(self):
        card = scorecard.summarize([], AS_OF, unpriceable=5)
        assert card.overall_n == 0 and card.cohorts == () and card.unpriceable == 5


def test_marks_round_trip_through_store_and_digest():
    """Persisted marks reproduce the digest scorecard — the forward-log
    guarantee: report --run N shows what was scored, never re-derived."""
    from argus.digest import render
    from argus.store import connect, migrate, queries, writer

    con = connect(":memory:")
    migrate(con)
    con.execute(
        "INSERT INTO runs (kind, started_at, app_version, status, finished_at) "
        "VALUES ('scout', ?, 't', 'complete', ?)",
        ("2026-07-13T15:00:00+00:00", "2026-07-13T15:00:00+00:00"),
    )
    # a prior proposal so first_proposals sees history before the run date
    con.execute(
        "INSERT INTO runs (run_id, kind, started_at, app_version, status, finished_at) "
        "VALUES (2, 'scout', ?, 't', 'complete', ?)",
        ("2026-07-13T15:00:00+00:00", "2026-07-13T15:00:00+00:00"),
    )
    con.execute(
        "INSERT INTO scout_candidates (run_id, ticker, rank, status, sector, screen_reasons, "
        "screener_metrics) VALUES (1, 'AAA', 1, 'proposed', 'Technology', '{}', '{}')"
    )
    first = date(2026, 6, 1)
    writer.write_scorecard_marks(
        con,
        run_id=2,
        marks=[
            ScorecardMark(ticker="AAA", first_proposed_at=first, horizon_weeks=0,
                          name_return=0.0, spy_return=0.0),
            ScorecardMark(ticker="AAA", first_proposed_at=first, horizon_weeks=4,
                          name_return=0.12, spy_return=0.03),
        ],
    )
    report = queries.run_report(con, 2)
    assert report.scorecard is not None
    assert report.scorecard.overall_n == 1
    four = next(c for c in report.scorecard.cohorts if c.horizon_weeks == 4)
    assert four.median_alpha == 0.09
    assert "Scorecard" in render(report)
    con.close()
