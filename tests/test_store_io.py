"""Round-trip contracts for the store's write and read sides, against real
SQLite on tmp_path (never mocked). Rows go in through writer and come back
typed through queries — the insurance policy for a future paid-feed store.
test_store.py covers the DDL guarantees; this file covers the Python seam."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from argus.fields import Field, QuarantineCode, Source
from argus.models import (
    AnalystActionRecord,
    FieldQuarantined,
    GatedObservation,
    ParseFailure,
    PriceMove,
    QuarantineHit,
    RawObservation,
    SourceHealth,
    Thresholds,
    TickerContext,
)
from argus.store import connect, migrate, queries, writer

T0 = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)  # a week-ago baseline run
T1 = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)  # the current run

NTDOY_HIT = QuarantineHit(
    code=QuarantineCode.TARGET_PRICE_RATIO,
    detail="target 35.00 (yahoo) / price 10.97 (yahoo) = 3.19 outside [0.3, 3.0]",
)


@pytest.fixture()
def con(tmp_path):
    con = connect(tmp_path / "argus.db")
    migrate(con)
    yield con
    con.close()


def _raw(field, *, ticker="NVDA", source=Source.YAHOO, num=None, text=None, day=None,
         fetched_at=T1, observed_at=None):
    return RawObservation(
        ticker=ticker, field=field, value_num=num, value_text=text, value_date=day,
        source=source, fetched_at=fetched_at, observed_at=observed_at,
    )


def _accepted(field, *, ticker="NVDA", source=Source.YAHOO, num=None, text=None, day=None,
              corroborated=(), primary=True, fetched_at=T1, observed_at=None):
    return GatedObservation(
        obs=_raw(field, ticker=ticker, source=source, num=num, text=text, day=day,
                 fetched_at=fetched_at, observed_at=observed_at),
        verdict="accepted", corroborated_by=corroborated, is_primary=primary,
    )


def _quarantined(field, hits, *, ticker="NVDA", source=Source.YAHOO, num=None, text=None,
                 day=None, fetched_at=T1):
    return GatedObservation(
        obs=_raw(field, ticker=ticker, source=source, num=num, text=text, day=day,
                 fetched_at=fetched_at),
        verdict="quarantined", reasons=hits,
    )


def _begin(con, *, started_at=T1, kind="watch"):
    return writer.begin_run(con, kind=kind, started_at=started_at, app_version="test")


def _write(con, run_id, *, ticker="NVDA", gated=(), actions=(), health=(), status="ok",
           error=None, thesis=None, thresholds=Thresholds()):
    writer.write_ticker_result(
        con, run_id=run_id,
        context=TickerContext(ticker=ticker, thesis=thesis, thresholds=thresholds),
        gated=gated, actions=actions, source_health=health, status=status, error=error,
    )


# --- run lifecycle -----------------------------------------------------------


def test_begin_run_inserts_running_row(con):
    run_id = _begin(con, started_at=T0)
    row = con.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["kind"] == "watch"
    assert row["status"] == "running"
    assert row["started_at"] == T0.isoformat()
    assert row["finished_at"] is None
    assert row["app_version"] == "test"


def test_begin_run_rejects_naive_datetime(con):
    with pytest.raises(ValueError, match="naive"):
        writer.begin_run(
            con, kind="watch", started_at=datetime(2026, 7, 13, 14, 0), app_version="test"
        )


def test_finish_run_stamps_status_and_time(con):
    run_id = _begin(con, started_at=T0)
    finished_at = T0 + timedelta(minutes=4)
    writer.finish_run(con, run_id=run_id, status="partial", finished_at=finished_at)
    row = con.execute("SELECT status, finished_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["status"] == "partial"
    assert row["finished_at"] == finished_at.isoformat()


def test_sweep_flips_only_stale_running_runs(con):
    finished = _begin(con, started_at=T0)
    writer.finish_run(con, run_id=finished, status="complete", finished_at=T0 + timedelta(minutes=5))
    stale = _begin(con, started_at=T0)  # crashed: still 'running' 8h later
    fresh = _begin(con, started_at=T0 + timedelta(hours=7))  # genuinely in progress

    now = T0 + timedelta(hours=8)
    assert writer.sweep_stale_runs(con, now=now) == [stale]  # ids, so the CLI can offer recovery

    status = {r["run_id"]: r["status"] for r in con.execute("SELECT run_id, status FROM runs")}
    assert status == {finished: "complete", stale: "failed", fresh: "running"}
    swept = con.execute("SELECT finished_at FROM runs WHERE run_id = ?", (stale,)).fetchone()
    assert swept["finished_at"] == now.isoformat()


# --- write_ticker_result round trips -----------------------------------------


def test_write_ticker_result_round_trips_all_row_shapes(con):
    run_id = _begin(con)
    gated = [
        _accepted(Field.PRICE, num=181.25, corroborated=(Source.FINNHUB,),
                  observed_at=T1 - timedelta(minutes=3)),
        _accepted(Field.PRICE, num=181.30, source=Source.FINNHUB,
                  corroborated=(Source.YAHOO,), primary=False),
        _quarantined(Field.ANALYST_TARGET_MEAN, (NTDOY_HIT,), num=35.0),
        GatedObservation(
            obs=ParseFailure(ticker="NVDA", field=Field.PE_TTM, raw="NaN%",
                             source=Source.YAHOO, fetched_at=T1),
            verdict="quarantined",
            reasons=(QuarantineHit(code=QuarantineCode.UNPARSEABLE,
                                   detail="could not parse 'NaN%' as num"),),
        ),
    ]
    action = AnalystActionRecord(
        ticker="NVDA", action_date=date(2026, 7, 10), firm="Morgan Stanley", action="up",
        from_grade="Equal-Weight", to_grade="Overweight", source=Source.YAHOO, fetched_at=T1,
    )
    health = [
        SourceHealth(source=Source.YAHOO, status="ok", latency_ms=812),
        SourceHealth(source=Source.EDGAR, status="not_applicable", error="not an EDGAR filer"),
    ]
    _write(con, run_id, gated=gated, actions=[action], health=health,
           thesis="Datacenter capex supercycle.", thresholds=Thresholds(price_move_pct=8.0))

    rows = {(r["field"], r["source"]): r
            for r in con.execute("SELECT * FROM observations WHERE run_id = ?", (run_id,))}
    assert len(rows) == 4

    primary = rows[("price", "yahoo")]
    assert primary["value_num"] == 181.25
    assert primary["verdict"] == "accepted"
    assert primary["is_primary"] == 1
    assert primary["gate_reasons"] is None  # NULL iff accepted
    assert primary["fetched_at"] == T1.isoformat()
    assert primary["observed_at"] == (T1 - timedelta(minutes=3)).isoformat()
    assert rows[("price", "finnhub")]["is_primary"] == 0

    bad_target = rows[("analyst_target_mean", "yahoo")]
    assert bad_target["verdict"] == "quarantined"
    assert bad_target["value_num"] == 35.0
    assert json.loads(bad_target["gate_reasons"]) == [
        {"code": "target_price_ratio", "detail": NTDOY_HIT.detail}
    ]

    # ParseFailure: raw wire text lands in value_text although pe_ttm is num-kind
    failure = rows[("pe_ttm", "yahoo")]
    assert failure["value_text"] == "NaN%"
    assert failure["value_num"] is None and failure["value_date"] is None
    assert failure["observed_at"] is None
    assert failure["verdict"] == "quarantined"
    assert [h["code"] for h in json.loads(failure["gate_reasons"])] == ["unparseable"]

    rt = con.execute("SELECT * FROM run_tickers WHERE run_id = ?", (run_id,)).fetchone()
    assert rt["status"] == "ok" and rt["error"] is None
    assert rt["thesis"] == "Datacenter capex supercycle."
    assert Thresholds.model_validate_json(rt["thresholds"]) == Thresholds(price_move_pct=8.0)

    rs = {r["source"]: r for r in con.execute("SELECT * FROM run_sources WHERE run_id = ?", (run_id,))}
    assert rs["yahoo"]["status"] == "ok" and rs["yahoo"]["latency_ms"] == 812
    assert rs["edgar"]["status"] == "not_applicable" and rs["edgar"]["error"] == "not an EDGAR filer"

    assert queries.new_analyst_actions(con, run_id, "NVDA") == [action]


def test_corroborated_by_round_trips_sorted(con):
    run_id = _begin(con)
    _write(con, run_id, gated=[
        _accepted(Field.PRICE, num=10.97, corroborated=(Source.FINNHUB, Source.EDGAR)),
        _accepted(Field.MARKET_CAP, num=52.4e9),  # uncorroborated → NULL, hydrates to ()
    ])

    stored = {r["field"]: r["corroborated_by"]
              for r in con.execute("SELECT field, corroborated_by FROM observations")}
    assert json.loads(stored["price"]) == ["edgar", "finnhub"]  # sorted on the wire
    assert stored["market_cap"] is None

    snap = queries.snapshot(con, run_id, "NVDA")
    assert snap.values[Field.PRICE].corroborated_by == (Source.EDGAR, Source.FINNHUB)
    assert snap.values[Field.MARKET_CAP].corroborated_by == ()


# --- snapshot -----------------------------------------------------------------


def test_snapshot_tri_state(con):
    run_id = _begin(con)
    edgar_target_hit = QuarantineHit(
        code=QuarantineCode.OUT_OF_BOUNDS, detail="target -1.00 outside (0.0001, 10000000)"
    )
    margin_hit = QuarantineHit(
        code=QuarantineCode.STALE, detail="observed 2026-06-01, older than max_age"
    )
    _write(con, run_id, gated=[
        _accepted(Field.PRICE, num=10.97),
        # fully dark: every source's target quarantined
        _quarantined(Field.ANALYST_TARGET_MEAN, (NTDOY_HIT,), num=35.0, source=Source.YAHOO),
        _quarantined(Field.ANALYST_TARGET_MEAN, (edgar_target_hit,), num=-1.0, source=Source.EDGAR),
        # accepted primary WITH a quarantined sibling from another source
        _accepted(Field.GROSS_MARGIN, num=0.62),
        _quarantined(Field.GROSS_MARGIN, (margin_hit,), num=0.31, source=Source.EDGAR),
    ])

    snap = queries.snapshot(con, run_id, "NVDA")
    assert snap is not None
    assert snap.ticker == "NVDA" and snap.run_id == run_id
    assert snap.as_of == T1  # the run's started_at, parsed back aware

    # state 1: usable signal
    assert set(snap.values) == {Field.PRICE, Field.GROSS_MARGIN}
    assert snap.values[Field.PRICE].value == 10.97
    assert snap.values[Field.PRICE].source is Source.YAHOO
    assert snap.values[Field.PRICE].fetched_at == T1
    # state 2: fully dark — hits merged across sources, in source order
    assert snap.quarantined == {Field.ANALYST_TARGET_MEAN: (edgar_target_hit, NTDOY_HIT)}
    # a field with an accepted primary is NOT dark, whatever its siblings did
    assert Field.GROSS_MARGIN not in snap.quarantined
    # state 3: offered by nobody — absent from both dicts
    assert Field.PE_TTM not in snap.values and Field.PE_TTM not in snap.quarantined


def test_snapshot_none_when_ticker_not_in_run(con):
    run_id = _begin(con)
    _write(con, run_id, gated=[_accepted(Field.PRICE, num=1.0)])
    assert queries.snapshot(con, run_id, "AMD") is None  # never fetched ≠ fetched-and-empty


def test_date_field_round_trips_as_date(con):
    run_id = _begin(con)
    _write(con, run_id, gated=[_accepted(Field.NEXT_EARNINGS_DATE, day=date(2026, 8, 20))])

    stored = con.execute("SELECT value_date FROM observations").fetchone()[0]
    assert stored == "2026-08-20"  # date.isoformat on the wire

    value = queries.snapshot(con, run_id, "NVDA").values[Field.NEXT_EARNINGS_DATE].value
    assert value == date(2026, 8, 20)
    assert type(value) is date  # FieldValue coerced the TEXT — no latent str downstream


# --- baseline selection -------------------------------------------------------


def test_baseline_run_skips_failed_tickers_and_non_watch_runs(con):
    r1 = _begin(con, started_at=T0 - timedelta(days=21))
    _write(con, r1, status="partial")  # partial is baseline-eligible
    writer.finish_run(con, run_id=r1, status="partial", finished_at=T0 - timedelta(days=21))

    r2 = _begin(con, started_at=T0 - timedelta(days=14))
    _write(con, r2, status="failed", error="yfinance: HTTP 500")
    writer.finish_run(con, run_id=r2, status="partial", finished_at=T0 - timedelta(days=14))

    r3 = _begin(con, started_at=T0 - timedelta(days=7), kind="scout")
    _write(con, r3, status="ok")
    writer.finish_run(con, run_id=r3, status="complete", finished_at=T0 - timedelta(days=7))

    r4 = _begin(con, started_at=T0)
    assert queries.baseline_run(con, "NVDA", r4) == r1  # skips failed r2 and scout r3
    assert queries.baseline_run(con, "NVDA", r1) is None  # nothing before the first run
    assert queries.baseline_run(con, "AMD", r4) is None  # never-seen ticker

    # a crashed run's committed tickers stay baseline-eligible after the sweep
    _write(con, r4, status="ok")
    assert writer.sweep_stale_runs(con, now=T1) == [r4]
    r5 = _begin(con, started_at=T1)
    assert queries.baseline_run(con, "NVDA", r5) == r4


def test_latest_accepted_skips_quarantine_gaps_and_scout_runs(con):
    r1 = _begin(con, started_at=T0 - timedelta(days=14))
    _write(con, r1, gated=[_accepted(Field.PRICE, num=100.0, fetched_at=T0 - timedelta(days=14))])
    writer.finish_run(con, run_id=r1, status="complete", finished_at=T0 - timedelta(days=14))

    r2 = _begin(con, started_at=T0 - timedelta(days=7))  # price quarantined: no primary row
    hit = QuarantineHit(code=QuarantineCode.OUT_OF_BOUNDS, detail="price -3.00 not positive")
    _write(con, r2, gated=[_quarantined(Field.PRICE, (hit,), num=-3.0)])
    writer.finish_run(con, run_id=r2, status="complete", finished_at=T0 - timedelta(days=7))

    r3 = _begin(con, started_at=T0, kind="scout")  # newer, accepted — but not a watch run
    _write(con, r3, gated=[_accepted(Field.PRICE, num=999.0, fetched_at=T0)])
    writer.finish_run(con, run_id=r3, status="complete", finished_at=T0)

    r4 = _begin(con, started_at=T1)
    value = queries.latest_accepted(con, "NVDA", Field.PRICE, r4)
    assert value is not None
    assert value.value == 100.0  # the r1 value, across the r2 gap, past scout r3
    assert value.source is Source.YAHOO
    assert value.fetched_at == T0 - timedelta(days=14)  # old_as_of stays honest
    assert queries.latest_accepted(con, "NVDA", Field.PRICE, r1) is None


# --- analyst actions ----------------------------------------------------------


def test_analyst_action_dedup_across_runs_preserves_first_seen(con):
    downgrade = AnalystActionRecord(
        ticker="NVDA", action_date=date(2026, 7, 3), firm="UBS", action="down",
        from_grade="Buy", to_grade="Neutral", source=Source.YAHOO,
        fetched_at=T0,
    )
    r1 = _begin(con, started_at=T0)
    _write(con, r1, actions=[downgrade])
    writer.finish_run(con, run_id=r1, status="complete", finished_at=T0)

    # next week the source re-serves the same action alongside two new ones
    r2 = _begin(con, started_at=T1)
    refetched = downgrade.model_copy(update={"fetched_at": T1})
    baird = AnalystActionRecord(
        ticker="NVDA", action_date=date(2026, 7, 10), firm="Baird", action="up",
        to_grade="Outperform", source=Source.YAHOO, fetched_at=T1,
    )
    argus_research = AnalystActionRecord(
        ticker="NVDA", action_date=date(2026, 7, 10), firm="Argus Research", action="init",
        to_grade="Buy", source=Source.YAHOO, fetched_at=T1,
    )
    _write(con, r2, actions=[refetched, baird, argus_research])

    # the refetch was ignored: the stored row keeps run-1 provenance intact
    assert queries.new_analyst_actions(con, r1, "NVDA") == [downgrade]
    # only the genuinely new actions belong to r2, ordered by (action_date, firm)
    assert queries.new_analyst_actions(con, r2, "NVDA") == [argus_research, baird]


# --- events + run_report ------------------------------------------------------


def test_record_events_run_report_round_trip(con):
    r1 = _begin(con, started_at=T0)
    _write(con, r1, gated=[_accepted(Field.PRICE, num=100.0, fetched_at=T0)])
    writer.finish_run(con, run_id=r1, status="complete", finished_at=T0 + timedelta(minutes=1))

    r2 = _begin(con, started_at=T1)
    _write(con, r2, ticker="NVDA",
           gated=[
               _accepted(Field.PRICE, num=112.0),
               _quarantined(Field.ANALYST_TARGET_MEAN, (NTDOY_HIT,), num=35.0),
           ],
           health=[
               SourceHealth(source=Source.YAHOO, status="ok", latency_ms=640),
               SourceHealth(source=Source.FINNHUB, status="error", error="HTTP 502"),
           ],
           thesis="Datacenter capex supercycle.", thresholds=Thresholds(price_move_pct=8.0))
    _write(con, r2, ticker="AMD", status="failed", error="yfinance: HTTP 500")
    events = (
        PriceMove(ticker="NVDA", old=100.0, new=112.0, pct=12.0, threshold=8.0, old_as_of=T0),
        FieldQuarantined(ticker="NVDA", field=Field.ANALYST_TARGET_MEAN, reasons=(NTDOY_HIT,)),
    )
    writer.record_events(con, run_id=r2, ticker="NVDA", events=events, baseline_run_id=r1)
    writer.finish_run(con, run_id=r2, status="partial", finished_at=T1 + timedelta(minutes=2))

    report = queries.run_report(con, r2)
    assert report.run_id == r2 and report.kind == "watch" and report.status == "partial"
    assert report.as_of == T1
    assert [t.context.ticker for t in report.tickers] == ["AMD", "NVDA"]  # alphabetical

    amd, nvda = report.tickers
    assert amd.status == "failed" and amd.error == "yfinance: HTTP 500"
    assert amd.baseline_run_id is None and amd.baseline_as_of is None
    assert amd.events == () and amd.quarantines == ()
    assert amd.snapshot is not None and amd.snapshot.values == {}  # in the run, no data

    # context as of the run, from run_tickers — never the live watchlist
    assert nvda.context.thesis == "Datacenter capex supercycle."
    assert nvda.context.thresholds == Thresholds(price_move_pct=8.0)
    assert nvda.status == "ok" and nvda.error is None
    # persisted events rehydrate bit-for-bit, typed, in event_id order
    assert nvda.events == events
    assert isinstance(nvda.events[0], PriceMove) and nvda.events[0].old_as_of == T0
    assert isinstance(nvda.events[1], FieldQuarantined)
    assert nvda.baseline_run_id == r1 and nvda.baseline_as_of == T0
    assert nvda.snapshot.values[Field.PRICE].value == 112.0
    # EVERY quarantined obs reaches quarantines — even beside an accepted primary
    assert [(q.field, q.source) for q in nvda.quarantines] == [
        (Field.ANALYST_TARGET_MEAN, Source.YAHOO)
    ]
    assert nvda.quarantines[0].reasons == (NTDOY_HIT,)
    assert [(s.source, s.status, s.error) for s in nvda.sources] == [
        (Source.FINNHUB, "error", "HTTP 502"),
        (Source.YAHOO, "ok", None),
    ]
    assert nvda.sources[1].latency_ms == 640


def test_run_report_on_running_run_raises(con):
    run_id = _begin(con)
    with pytest.raises(ValueError, match="running"):
        queries.run_report(con, run_id)


def test_quarantine_report_lists_all_quarantined_rows_ordered(con):
    run_id = _begin(con)
    hit = QuarantineHit(code=QuarantineCode.OUT_OF_BOUNDS, detail="nope")
    _write(con, run_id, ticker="NVDA", gated=[
        _accepted(Field.PRICE, num=10.97),  # accepted rows never appear
        _quarantined(Field.PE_TTM, (hit,), num=99999.0, source=Source.YAHOO),
        _quarantined(Field.PE_TTM, (hit,), num=88888.0, source=Source.EDGAR),
    ])
    _write(con, run_id, ticker="AMD", gated=[
        _quarantined(Field.PRICE, (hit,), num=-1.0, ticker="AMD"),
    ])
    rows = queries.quarantine_report(con, run_id)
    assert [(r["ticker"], r["field"], r["source"]) for r in rows] == [
        ("AMD", "price", "yahoo"),
        ("NVDA", "pe_ttm", "edgar"),
        ("NVDA", "pe_ttm", "yahoo"),
    ]
    assert all(json.loads(r["gate_reasons"]) for r in rows)
