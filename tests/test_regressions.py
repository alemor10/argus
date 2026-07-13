"""Regressions from the post-implementation adversarial review — each test is
a confirmed production failure that the fixes must keep impossible."""

from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from argus import changes, engine
from argus.fields import Field, QuarantineCode, Source
from argus.gates import DEFAULT_PROFILE, run_gates
from argus.models import (
    GatedObservation,
    QuarantineHit,
    RawObservation,
    Thresholds,
    TickerContext,
)
from argus.sources.base import FetchResult
from argus.sources.yahoo import YahooSource
from argus.store import connect, migrate, writer

NOW = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)
CTX = TickerContext(ticker="NVDA", thesis=None, thresholds=Thresholds())


@pytest.fixture()
def con(tmp_path):
    con = connect(tmp_path / "argus.db")
    migrate(con)
    yield con
    con.close()


class _Sink:
    def __init__(self):
        self.written = []

    def write(self, markdown, *, run_id, as_of):
        self.written.append(run_id)
        return None


class _Stub:
    source_id = Source.YAHOO

    def __init__(self, result: FetchResult):
        self._result = result
        self.fetched: list[str] = []

    def covers(self, ticker):
        return True

    def fetch(self, ticker):
        self.fetched.append(ticker)
        return FetchResult(
            observations=tuple(
                o.model_copy(update={"ticker": ticker}) for o in self._result.observations
            ),
            parse_failures=self._result.parse_failures,
            analyst_actions=self._result.analyst_actions,
        )


def _run(con, contexts, sources, sink=None):
    return engine.run(
        contexts,
        con=con,
        sources=sources,
        profile=DEFAULT_PROFILE,
        sink=sink or _Sink(),
        as_of=NOW,
        today=NOW.date(),
        app_version="regression-test",
    )


def test_one_malformed_analyst_row_cannot_destroy_the_ticker(con):
    """Review finding: an accepted recommendationKey plus a NaN-firm
    upgrades_downgrades row produced colliding (run, ticker, field, source)
    observations → IntegrityError → the ENTIRE ticker rolled back. The
    partial unique index (accepted rows only) plus the yahoo aggregation
    must keep every good value AND the visible UNPARSEABLE quarantine."""
    payload = {
        "info": {"currentPrice": 181.25, "recommendationKey": "buy"},
        "upgrades_downgrades": [
            {"GradeDate": None, "Firm": None, "ToGrade": None, "Action": None},  # NaN row
            {
                "GradeDate": "2026-07-12T00:00:00",
                "Firm": "Morgan Stanley",
                "ToGrade": "Equal-Weight",
                "FromGrade": "Overweight",
                "Action": "down",
            },
        ],
        "calendar": {},
    }
    result = YahooSource().parse(payload, "NVDA", NOW)
    assert len(result.parse_failures) == 1  # aggregated, not one per bad row
    assert "1 malformed" in result.parse_failures[0].raw

    outcome = _run(con, [CTX], [_Stub(result)])
    assert outcome.status == "complete"
    rows = con.execute(
        "SELECT field, verdict FROM observations WHERE ticker = 'NVDA' ORDER BY field, verdict"
    ).fetchall()
    verdicts = {(r["field"], r["verdict"]) for r in rows}
    assert ("analyst_rating", "accepted") in verdicts  # the good value survived
    assert ("analyst_rating", "quarantined") in verdicts  # the failure is visible
    assert ("price", "accepted") in verdicts
    actions = con.execute("SELECT firm FROM analyst_actions").fetchall()
    assert [r["firm"] for r in actions] == ["Morgan Stanley"]  # valid action kept


def test_nan_value_persists_as_quarantined_text_not_integrity_error(con):
    """Review finding: sqlite3 binds NaN as NULL → exactly-one-value CHECK →
    the whole ticker discarded. The writer must preserve the (always
    NON_FINITE-quarantined) value as text instead."""
    obs = RawObservation(
        ticker="NVDA", field=Field.PE_FWD, value_num=float("nan"), source=Source.YAHOO, fetched_at=NOW
    )
    gated = run_gates(DEFAULT_PROFILE, [obs], [], NOW)
    assert gated[0].verdict == "quarantined"
    assert gated[0].reasons[0].code == QuarantineCode.NON_FINITE

    con.execute(
        "INSERT INTO runs (kind, started_at, app_version) VALUES ('watch', ?, 't')",
        (NOW.isoformat(),),
    )
    writer.write_ticker_result(
        con, run_id=1, context=CTX, gated=gated, actions=[], source_health=[], status="ok"
    )
    row = con.execute("SELECT value_num, value_text, verdict FROM observations").fetchone()
    assert row["value_num"] is None
    assert row["value_text"] == "nan"
    assert row["verdict"] == "quarantined"


def test_diff_phase_crash_degrades_the_ticker_not_the_run(con):
    """Review finding: an exception AFTER the ticker's data committed made the
    guard re-INSERT its run_tickers row → IntegrityError escaped engine.run,
    the remaining tickers were never fetched, no digest was produced, and the
    runs row stayed 'running'. Post-commit failures must degrade to partial
    and keep going."""
    price = RawObservation(
        ticker="AAA", field=Field.PRICE, value_num=50.0, source=Source.YAHOO, fetched_at=NOW
    )
    result = FetchResult(observations=(price,))
    stub = _Stub(result)
    contexts = [
        TickerContext(ticker="AAA", thresholds=Thresholds()),
        TickerContext(ticker="BBB", thresholds=Thresholds()),
    ]

    real_detect = changes.detect

    def explode_for_aaa(baseline, current, ctx, new_actions, today, *, latest_accepted):
        if ctx.ticker == "AAA":
            raise RuntimeError("diff-phase bug")
        return real_detect(baseline, current, ctx, new_actions, today, latest_accepted=latest_accepted)

    sink = _Sink()
    with patch.object(engine.changes, "detect", side_effect=explode_for_aaa):
        outcome = _run(con, contexts, [stub], sink)

    assert stub.fetched == ["AAA", "BBB"]  # BBB was still fetched
    assert outcome.status == "partial"  # degradation disclosed, run not killed
    assert sink.written == [outcome.run_id]  # a digest was still produced
    statuses = {
        r["ticker"]: r["status"]
        for r in con.execute("SELECT ticker, status FROM run_tickers").fetchall()
    }
    assert statuses == {"AAA": "ok", "BBB": "ok"}  # AAA's committed data intact, no re-insert
    run_status = con.execute("SELECT status FROM runs WHERE run_id = ?", (outcome.run_id,)).fetchone()
    assert run_status["status"] == "partial"  # finish_run executed


def test_gated_observation_from_parse_failure_and_accepted_same_source_round_trip(con):
    """An accepted value and an UNPARSEABLE ParseFailure on the SAME
    (field, source) must both persist — the poison class behind the review's
    highest-severity finding."""
    from argus.models import ParseFailure

    con.execute(
        "INSERT INTO runs (kind, started_at, app_version) VALUES ('watch', ?, 't')",
        (NOW.isoformat(),),
    )
    accepted = GatedObservation(
        obs=RawObservation(
            ticker="NVDA",
            field=Field.ANALYST_RATING,
            value_text="buy",
            source=Source.YAHOO,
            fetched_at=NOW,
        ),
        verdict="accepted",
        is_primary=True,
    )
    failed = GatedObservation(
        obs=ParseFailure(
            ticker="NVDA", field=Field.ANALYST_RATING, raw="garbled", source=Source.YAHOO, fetched_at=NOW
        ),
        verdict="quarantined",
        reasons=(QuarantineHit(code=QuarantineCode.UNPARSEABLE, detail="raw: garbled"),),
    )
    writer.write_ticker_result(
        con, run_id=1, context=CTX, gated=[accepted, failed], actions=[], source_health=[], status="ok"
    )
    rows = con.execute("SELECT verdict FROM observations ORDER BY verdict").fetchall()
    assert [r["verdict"] for r in rows] == ["accepted", "quarantined"]
