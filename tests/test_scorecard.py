"""Scout self-scoring: the pure return/alpha math + aggregation, and the
store round-trip (persisted marks reproduce the digest scorecard).

Honesty invariants under test: every proposal is scored from its first
appearance (dropped names still tracked), unpriceable names are counted-and-excluded (never
a silent zero), and returns use both endpoints of the same adjusted series.
"""

from datetime import date, timedelta

from argus import scorecard
from argus.models import ScorecardMark


def _series(start: date, weekly_returns: list[float]) -> list[tuple[date, float]]:
    """A price path: base 100 on `start`, then one point per week applying the
    cumulative return."""
    pts = [(start, 100.0)]
    for i, r in enumerate(weekly_returns, start=1):
        pts.append((start + timedelta(weeks=i), 100.0 * (1 + r)))
    return pts


AS_OF = date(2026, 7, 13)


class TestComputeMarks:
    def test_return_and_alpha(self):
        first = date(2026, 6, 15)  # ~4 weeks before AS_OF
        marks, unpriceable = scorecard.compute_marks(
            [("AAA", first)],
            {"AAA": _series(first, [0.05, 0.05, 0.05, 0.05])},  # ~ +21.6% total
            _series(first, [0.01, 0.01, 0.01, 0.01]),  # SPY ~ +4.06%
            AS_OF,
        )
        assert unpriceable == 0
        (m,) = marks
        assert m.name_return > m.spy_return  # beat the market
        assert abs(m.alpha - (m.name_return - m.spy_return)) < 1e-9
        assert m.weeks_out == 4

    def test_entry_is_first_tradeable_close_on_or_after(self):
        # A weekend proposal prices at the NEXT session (Monday), not the
        # prior Friday — what you could actually transact at. Review finding:
        # the old on-or-before logic left weekend-first names permanently
        # unscored because history(start=Sat) has no Friday bar.
        first = date(2026, 6, 20)  # Saturday
        series = [(date(2026, 6, 22), 52.0), (date(2026, 7, 10), 60.0)]  # Monday onward
        marks, _ = scorecard.compute_marks([("AAA", first)], {"AAA": series}, series, AS_OF)
        assert marks[0].name_return == (60.0 / 52.0 - 1.0)  # entry = Monday close

    def test_far_off_first_bar_is_unpriceable_not_a_stale_entry(self):
        # A gap > a holiday week (e.g. not yet trading) → no honest entry.
        first = date(2026, 6, 1)
        series = [(date(2026, 6, 22), 50.0), (date(2026, 7, 10), 60.0)]  # 21-day gap
        marks, unpriceable = scorecard.compute_marks(
            [("AAA", first)], {"AAA": series}, series, AS_OF
        )
        assert marks == [] and unpriceable == 1

    def test_unpriceable_name_is_excluded_not_zeroed(self):
        first = date(2026, 6, 15)
        marks, unpriceable = scorecard.compute_marks(
            [("GOOD", first), ("DELISTED", first)],
            {"GOOD": _series(first, [0.1]), "DELISTED": None},
            _series(first, [0.02]),
            AS_OF,
        )
        assert unpriceable == 1
        assert [m.ticker for m in marks] == ["GOOD"]

    def test_missing_spy_makes_everything_unpriceable(self):
        first = date(2026, 6, 15)
        marks, unpriceable = scorecard.compute_marks(
            [("AAA", first)], {"AAA": _series(first, [0.1])}, None, AS_OF
        )
        assert marks == [] and unpriceable == 1

    def test_proposal_predating_history_is_unpriceable(self):
        # History starts after the proposal date → no entry price.
        first = date(2026, 5, 1)
        series = [(date(2026, 6, 1), 100.0), (date(2026, 7, 1), 110.0)]
        marks, unpriceable = scorecard.compute_marks([("AAA", first)], {"AAA": series}, series, AS_OF)
        assert marks == [] and unpriceable == 1


class TestSummarize:
    def _mark(self, ticker, weeks_out, name_ret, spy_ret):
        return ScorecardMark(
            ticker=ticker,
            first_proposed_at=AS_OF - timedelta(weeks=weeks_out),
            weeks_out=weeks_out,
            name_return=name_ret,
            spy_return=spy_ret,
        )

    def test_cohorts_and_overall(self):
        marks = [
            self._mark("A", 1, 0.02, 0.01),  # ≤1w, beats
            self._mark("B", 3, 0.10, 0.04),  # 2–4w, beats
            self._mark("C", 3, -0.05, 0.04),  # 2–4w, lags
        ]
        card = scorecard.summarize(marks, AS_OF, unpriceable=2)
        labels = {c.label: c for c in card.cohorts}
        assert labels["≤ 1 week"].n == 1 and labels["≤ 1 week"].beat_spy == 1
        assert labels["2–4 weeks"].n == 2 and labels["2–4 weeks"].beat_spy == 1
        assert labels["2–4 weeks"].median_return == 0.025  # median(0.10, -0.05)
        assert card.overall_n == 3
        assert card.overall_beat_spy == 2
        assert card.unpriceable == 2

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
    writer.write_scorecard_marks(
        con,
        run_id=2,
        marks=[
            ScorecardMark(
                ticker="AAA",
                first_proposed_at=date(2026, 6, 15),
                weeks_out=4,
                name_return=0.12,
                spy_return=0.03,
            )
        ],
    )
    report = queries.run_report(con, 2)
    assert report.scorecard is not None
    assert report.scorecard.overall_n == 1
    assert report.scorecard.cohorts[0].median_alpha == 0.09
    assert "Scorecard" in render(report)
    con.close()
