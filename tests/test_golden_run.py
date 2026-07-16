"""The flagship end-to-end test: two fabricated runs through the REAL engine
(gates, store, diff, events, render) against a tmp database, with stub
sources covering the pathology matrix:

  - NTDOY: healthy in run 1; run 2 serves the stale $35 target against a
    corroborated ~$11 price → relational gate quarantines the TARGET only,
    FieldQuarantined headlines, and the string "218" (the fake-upside number
    a naive pipeline would print) appears nowhere.
  - NVDA: +6.6% price move over a 5% threshold; consensus buy → hold; one NEW
    analyst downgrade among re-served old ones (dedup via first_seen_run_id);
    a re-served earnings result (dedup: reported on run 1 → suppressed as
    first-run baseline, and NEVER re-fires); earnings 4 days out; Finnhub
    down for NVDA in run 2 (cross-check skipped and disclosed).
  - NTDOY: a quarter REPORTED between the runs → earnings_reported fires
    beside the quarantine headline.
  - DEADCO: fine in run 1, every source dies in run 2 → ticker failed, run
    goes 'partial', digest still written.
  - ^VIX (macro): 15.0 → 25.4 between runs → MacroShift (+10.40 over the 3.0
    alert_move) AND a newly-crossed "value >= 25" line; NO PriceMove despite
    the 69% move (the equity machinery is off for macro contexts); renders
    in the Macro section, never the Watchlist.

Run 2's digest is byte-compared against tests/golden/digest_run2.md
(regenerate deliberately with `uv run pytest --update-golden`).
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from argus import engine
from argus.fields import Field, Source
from argus.gates import DEFAULT_PROFILE
from argus.models import (
    AnalystActionRecord,
    EarningsResultRecord,
    InsiderTransaction,
    MacroSpec,
    RawObservation,
    Thresholds,
    TickerContext,
)
from argus.sources.base import FetchResult, SourceError
from argus.store import connect, migrate
from argus.thesis import parse_thesis_check

GOLDEN = Path(__file__).parent / "golden" / "digest_run2.md"

RUN1_AT = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)
RUN2_AT = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)

_VIX_LINE = parse_thesis_check("price >= 25").model_copy(update={"raw": "value >= 25"})

CONTEXTS = [
    TickerContext(
        ticker="DEADCO",
        thesis="Goes dark in run 2.",
        thresholds=Thresholds(),
    ),
    TickerContext(
        ticker="NTDOY",
        thesis="Switch 2 cycle + IP monetization.",
        thresholds=Thresholds(),
    ),
    TickerContext(
        ticker="NVDA",
        thesis="Datacenter capex supercycle; CUDA moat.",
        thresholds=Thresholds(),  # default 5% price threshold; move is 6.6%
    ),
    TickerContext(
        ticker="^VIX",
        macro=MacroSpec(label="VIX", alert_move=3.0, alert_when=(_VIX_LINE,)),
    ),
]


def _obs(ticker, field, num=None, text=None, when=None, source=Source.YAHOO, observed=None):
    return RawObservation(
        ticker=ticker,
        field=field,
        value_num=num,
        value_text=text,
        value_date=when,
        source=source,
        fetched_at=RUN2_AT,  # overwritten per run by _StubSource
        observed_at=observed,
    )


class _StubSource:
    """Canned per-ticker payloads; raising a SourceError simulates an outage."""

    def __init__(self, source_id, payloads, fetched_at):
        self.source_id = source_id
        self._payloads = payloads
        self._fetched_at = fetched_at

    def covers(self, ticker: str) -> bool:
        return ticker in self._payloads

    def fetch(self, ticker: str) -> FetchResult:
        payload = self._payloads[ticker]
        if isinstance(payload, Exception):
            raise payload
        observations = tuple(
            o.model_copy(update={"fetched_at": self._fetched_at})
            for o in payload.get("observations", ())
        )
        actions = tuple(payload.get("actions", ()))
        earnings = tuple(payload.get("earnings", ()))
        insider = tuple(payload.get("insider", ()))
        return FetchResult(
            observations=observations, analyst_actions=actions, earnings_results=earnings,
            insider_transactions=insider,
        )


def _sources_run1():
    yahoo = {
        "DEADCO": {
            "observations": [
                _obs("DEADCO", Field.PRICE, num=50.0),
                _obs("DEADCO", Field.ANALYST_RATING, text="hold"),
            ]
        },
        "NTDOY": {
            "observations": [
                _obs("NTDOY", Field.PRICE, num=10.90),
                _obs("NTDOY", Field.ANALYST_TARGET_MEAN, num=11.00),
                _obs("NTDOY", Field.ANALYST_RATING, text="buy"),
            ]
        },
        "NVDA": {
            "observations": [
                _obs("NVDA", Field.PRICE, num=170.00),
                _obs("NVDA", Field.PE_FWD, num=29.8),
                _obs("NVDA", Field.ANALYST_RATING, text="buy"),
                _obs("NVDA", Field.ANALYST_TARGET_MEAN, num=200.0),
            ],
            "actions": [
                AnalystActionRecord(
                    ticker="NVDA",
                    action_date=date(2026, 6, 30),
                    firm="Old Firm",
                    action="up",
                    from_grade="Hold",
                    to_grade="Buy",
                    source=Source.YAHOO,
                    fetched_at=RUN1_AT,
                )
            ],
            "earnings": [
                # First-run history: stored as baseline, suppressed as news.
                EarningsResultRecord(
                    ticker="NVDA",
                    quarter_end=date(2026, 4, 26),
                    eps_actual=0.81,
                    eps_estimate=0.75,
                    source=Source.YAHOO,
                    fetched_at=RUN1_AT,
                )
            ],
        },
        "^VIX": {"observations": [_obs("^VIX", Field.PRICE, num=15.0)]},
    }
    finnhub = {
        "DEADCO": {"observations": [_obs("DEADCO", Field.PRICE, num=50.05, source=Source.FINNHUB)]},
        "NTDOY": {"observations": [_obs("NTDOY", Field.PRICE, num=10.92, source=Source.FINNHUB)]},
        "NVDA": {"observations": [_obs("NVDA", Field.PRICE, num=170.10, source=Source.FINNHUB)]},
    }
    return [
        _StubSource(Source.YAHOO, yahoo, RUN1_AT),
        _StubSource(Source.FINNHUB, finnhub, RUN1_AT),
    ]


def _sources_run2():
    yahoo = {
        "DEADCO": SourceError("HTTP 502 from upstream"),
        "NTDOY": {
            "observations": [
                _obs("NTDOY", Field.PRICE, num=10.97),
                _obs("NTDOY", Field.ANALYST_TARGET_MEAN, num=35.00),  # the stale pathology
                _obs("NTDOY", Field.ANALYST_RATING, text="buy"),
            ],
            "earnings": [
                # Reported between the runs: must fire as earnings_reported.
                EarningsResultRecord(
                    ticker="NTDOY",
                    quarter_end=date(2026, 5, 31),
                    eps_actual=0.12,
                    eps_estimate=0.10,
                    source=Source.YAHOO,
                    fetched_at=RUN2_AT,
                )
            ],
        },
        "NVDA": {
            "observations": [
                _obs("NVDA", Field.PRICE, num=181.25),  # +6.62% over run 1
                _obs("NVDA", Field.PE_FWD, num=31.2),
                _obs("NVDA", Field.ANALYST_RATING, text="hold"),  # consensus shift down
                _obs("NVDA", Field.ANALYST_TARGET_MEAN, num=205.0),
                _obs(
                    "NVDA",
                    Field.NEXT_EARNINGS_DATE,
                    when=RUN2_AT.date() + timedelta(days=4),  # earnings imminent
                ),
            ],
            "actions": [
                AnalystActionRecord(  # re-served from run 1: dedup must ignore it
                    ticker="NVDA",
                    action_date=date(2026, 6, 30),
                    firm="Old Firm",
                    action="up",
                    from_grade="Hold",
                    to_grade="Buy",
                    source=Source.YAHOO,
                    fetched_at=RUN2_AT,
                ),
                AnalystActionRecord(  # NEW this run: must surface as an event
                    ticker="NVDA",
                    action_date=date(2026, 7, 12),
                    firm="Morgan Stanley",
                    action="down",
                    from_grade="Overweight",
                    to_grade="Equal-Weight",
                    source=Source.YAHOO,
                    fetched_at=RUN2_AT,
                ),
            ],
            "earnings": [
                # Re-served from run 1: first_seen_run_id dedup must ignore it.
                EarningsResultRecord(
                    ticker="NVDA",
                    quarter_end=date(2026, 4, 26),
                    eps_actual=0.81,
                    eps_estimate=0.75,
                    source=Source.YAHOO,
                    fetched_at=RUN2_AT,
                )
            ],
            "insider": [
                # NEW this run: an open-market purchase → InsiderActivity.
                InsiderTransaction(
                    ticker="NVDA",
                    accession="0000-26-777",
                    filing_date=date(2026, 7, 12),
                    transaction_date=date(2026, 7, 11),
                    owner="Jane Director",
                    role="director",
                    shares=5000.0,
                    price=181.0,
                    source=Source.YAHOO,
                    fetched_at=RUN2_AT,
                )
            ],
        },
        # 15.0 → 25.4: MacroShift (+10.40 ≥ 3.0) AND the "value >= 25" line
        # newly crossed — but NO PriceMove, despite +69%.
        "^VIX": {"observations": [_obs("^VIX", Field.PRICE, num=25.4)]},
    }
    finnhub = {
        "DEADCO": SourceError("HTTP 502 from upstream"),
        "NTDOY": {"observations": [_obs("NTDOY", Field.PRICE, num=10.99, source=Source.FINNHUB)]},
        "NVDA": SourceError("read timeout"),  # cross-check skipped, disclosed
    }
    return [
        _StubSource(Source.YAHOO, yahoo, RUN2_AT),
        _StubSource(Source.FINNHUB, finnhub, RUN2_AT),
    ]


class _CaptureSink:
    def __init__(self):
        self.digests = {}

    def write(self, markdown: str, *, run_id: int, as_of: date):
        self.digests[run_id] = markdown
        return None


@pytest.fixture()
def con(tmp_path):
    con = connect(tmp_path / "argus.db")
    migrate(con)
    yield con
    con.close()


def _execute_both_runs(con):
    sink = _CaptureSink()
    outcome1 = engine.run(
        CONTEXTS,
        con=con,
        sources=_sources_run1(),
        profile=DEFAULT_PROFILE,
        sink=sink,
        as_of=RUN1_AT,
        today=RUN1_AT.date(),
        app_version="golden-test",
    )
    outcome2 = engine.run(
        CONTEXTS,
        con=con,
        sources=_sources_run2(),
        profile=DEFAULT_PROFILE,
        sink=sink,
        as_of=RUN2_AT,
        today=RUN2_AT.date(),
        app_version="golden-test",
    )
    return outcome1, outcome2, sink


def test_golden_run(con, update_golden):
    outcome1, outcome2, sink = _execute_both_runs(con)

    assert outcome1.status == "complete"
    assert outcome2.status == "partial"  # DEADCO died; the run still reports
    digest2 = sink.digests[outcome2.run_id]

    # The founding negative assertion: a naive pipeline computes 218% upside
    # from run 2's NTDOY payload. That number must be unprintable.
    assert "218" not in digest2

    if update_golden:
        GOLDEN.write_text(digest2, encoding="utf-8")
        pytest.skip("golden file regenerated")
    assert GOLDEN.exists(), "golden missing — run `uv run pytest --update-golden` once"
    assert digest2 == GOLDEN.read_text(encoding="utf-8")


def test_golden_run_events_in_store(con):
    _, outcome2, _ = _execute_both_runs(con)
    rows = con.execute(
        "SELECT ticker, kind FROM change_events WHERE run_id = ? ORDER BY event_id",
        (outcome2.run_id,),
    ).fetchall()
    by_ticker = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"], []).append(row["kind"])
    assert "price_move" in by_ticker["NVDA"]
    assert "consensus_shift" in by_ticker["NVDA"]
    assert "analyst_action" in by_ticker["NVDA"]
    assert "earnings_imminent" in by_ticker["NVDA"]
    assert "field_quarantined" in by_ticker["NTDOY"]
    assert "earnings_reported" in by_ticker["NTDOY"]  # reported between the runs
    assert "DEADCO" not in by_ticker  # failed ticker: no snapshot, no events

    # exactly ONE new analyst action despite the re-served old one
    actions = [k for k in by_ticker["NVDA"] if k == "analyst_action"]
    assert len(actions) == 1
    # NVDA's quarter was first seen on run 1 (baseline): the run-2 re-serve
    # must not fire — a stale quarter re-reported weekly would be noise.
    assert "earnings_reported" not in by_ticker["NVDA"]
    assert "insider_activity" in by_ticker["NVDA"]  # the open-market buy
    # The macro series alerts by ITS rules (line + shift), never the equity
    # machinery — +69% on VIX is not a PriceMove.
    assert by_ticker["^VIX"] == ["macro_line_crossed", "macro_shift"]

    # the quarantined target is in the store with its reason, price untouched
    quarantined = con.execute(
        "SELECT field, gate_reasons FROM observations WHERE run_id = ? AND verdict = 'quarantined'",
        (outcome2.run_id,),
    ).fetchall()
    assert [(r["field"]) for r in quarantined] == ["analyst_target_mean"]
    assert "target_price_ratio" in quarantined[0]["gate_reasons"]


def test_report_regeneration_is_bit_for_bit(con):
    from argus.digest import render
    from argus.store import queries

    _, outcome2, sink = _execute_both_runs(con)
    regenerated = render(queries.run_report(con, outcome2.run_id))
    assert regenerated == sink.digests[outcome2.run_id]
