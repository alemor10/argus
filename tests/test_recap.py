"""Sunday Edition contract tests: build_recap aggregates the persisted week
(events with standing-reminder roll-up, macro week-over-week, shortlist
churn) — pure read-side; the renderer and PDF are deterministic."""

from datetime import UTC, date, datetime, timedelta

import pytest

from argus.fields import Field, Source
from argus.models import (
    BellwetherEarning,
    GatedObservation,
    MacroSpec,
    PriceMove,
    RawObservation,
    ScoutCandidateRecord,
    ThesisDrift,
    Thresholds,
    TickerContext,
)
from argus.recap import build_recap, build_recap_pdf, render_recap
from argus.store import connect, migrate, writer

WEEK_ENDING = date(2026, 7, 19)
BEFORE = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)  # outside the window
MON = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
WED = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
SUN = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)

VIX_SPEC = MacroSpec(label="VIX")


def _accepted_price(ticker, value, fetched_at, *, field=Field.PRICE):
    return GatedObservation(
        obs=RawObservation(
            ticker=ticker, field=field, value_num=value,
            source=Source.YAHOO, fetched_at=fetched_at,
        ),
        verdict="accepted",
        is_primary=True,
    )


def _watch_run(con, at, *, vix=None, events=(), macro_spec=None):
    run_id = writer.begin_run(con, kind="watch", started_at=at, app_version="t")
    context = TickerContext(ticker="^VIX", macro=macro_spec or VIX_SPEC)
    gated = [_accepted_price("^VIX", vix, at)] if vix is not None else []
    writer.write_ticker_result(
        con, run_id=run_id, context=context, gated=gated, actions=[],
        source_health=[], status="ok",
    )
    if events:
        writer.record_events(
            con, run_id=run_id, ticker="^VIX", events=list(events), baseline_run_id=None
        )
    writer.finish_run(con, run_id=run_id, status="complete", finished_at=at)
    return run_id


def _scout_run(con, at, proposed):
    run_id = writer.begin_run(con, kind="scout", started_at=at, app_version="t")
    writer.finish_run(con, run_id=run_id, status="complete", finished_at=at)
    records = [
        ScoutCandidateRecord(
            ticker=ticker, rank=i + 1, status="proposed",
            screen_reasons={"fwd_pe": "ok"}, screener_metrics={},
        )
        for i, ticker in enumerate(proposed)
    ]
    writer.write_scout_candidates(con, run_id=run_id, records=records)
    return run_id


@pytest.fixture()
def con(tmp_path):
    con = connect(tmp_path / "argus.db")
    migrate(con)
    yield con
    con.close()


def _seed_week(con):
    _watch_run(con, BEFORE, vix=15.0)  # last week: the week-over-week baseline
    _scout_run(con, BEFORE, ["AAA", "BBB"])
    move = PriceMove(ticker="^VIX", old=15.0, new=25.4, pct=69.3, threshold=5.0, old_as_of=BEFORE)
    standing = ThesisDrift(
        ticker="^VIX", check="value >= 25", field=Field.PRICE, observed=25.4, newly=False
    )
    _watch_run(con, MON, vix=25.4, events=(move,))
    _watch_run(con, WED, vix=25.4, events=(standing,))  # re-fired reminder: rolled up
    _scout_run(con, SUN, ["BBB", "CCC"])


def test_recap_aggregates_the_week(con):
    _seed_week(con)
    recap = build_recap(con, week_ending=WEEK_ENDING)
    assert recap.watch_runs == 2  # MON + WED; BEFORE is outside the window
    # Events: the move fires with its day; the standing reminder rolls up.
    [event] = recap.events
    assert event.day == MON.date()
    assert event.event.kind == "price_move"
    assert recap.standing_suppressed == 1
    # Macro week over week: 25.4 now vs 15.0 before the window opened.
    [line] = recap.macro
    assert line.label == "VIX"
    assert line.current == 25.4
    assert line.week_ago == 15.0
    assert line.delta == pytest.approx(10.4)
    # Discovery churn vs the prior scout run.
    assert recap.entered == ("CCC",)
    assert recap.dropped == ("AAA",)
    assert {p.ticker for p in recap.proposals if p.status == "proposed"} == {"BBB", "CCC"}


def test_render_carries_every_section(con):
    _seed_week(con)
    recap = build_recap(
        con,
        week_ending=WEEK_ENDING,
        week_ahead=[
            BellwetherEarning(symbol="NVDA", report_date=date(2026, 7, 22), hour="amc",
                              eps_estimate=1.05)
        ],
        week_ahead_note="312 more companies report next week (unfiltered count).",
    )
    out = render_recap(recap)
    assert "# Argus Sunday Edition — week ending 2026-07-19" in out
    assert "- 2026-07-13 — Price 15.00 → 25.40 (+69.3%, threshold 5.0%) vs 2026-07-10" in out
    assert "_1 re-fired standing reminder(s) rolled up" in out
    assert "- VIX: 25.40 (Δ +10.40 over the week, from 15.00)" in out
    assert "NEW to the list: CCC." in out
    assert "Dropped off: AAA." in out
    assert "- NVDA — 2026-07-22 amc (est 1.05)" in out
    assert "print-time — not archived" in out
    assert "argus recap --week-ending 2026-07-19" in out


def test_quiet_week_is_stated_not_blank(con):
    _watch_run(con, MON, vix=15.0)
    recap = build_recap(con, week_ending=WEEK_ENDING)
    out = render_recap(recap)
    assert "No events fired this week — a quiet week is information." in out
    assert "No scout run this week." in out


def test_pdf_is_deterministic(con):
    _seed_week(con)
    recap = build_recap(con, week_ending=WEEK_ENDING)
    first, second = build_recap_pdf(recap), build_recap_pdf(recap)
    assert first.startswith(b"%PDF")
    assert first == second  # CreationDate suppressed — the regenerability contract
    assert b"CreationDate" not in first
