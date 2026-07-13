"""Scout integration: candidate persistence + streaks, post-enrichment
eligibility, the scout digest end-to-end (golden), the screener-outage
disclosure, and the promote command's guarded watchlist append."""

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus import engine
from argus.cli import app
from argus.config import build_contexts, load_watch_config
from argus.fields import Field, Source
from argus.gates import DEFAULT_PROFILE
from argus.models import RawObservation, ScoutCandidateRecord
from argus.scout.criteria import ScoutCriteria, screen
from argus.scout.run import run_scout
from argus.scout.screener import ScreenerError, ScreenerRow
from argus.sources.base import FetchResult, SourceError
from argus.store import connect, migrate, queries, writer

GOLDEN = Path(__file__).parent / "golden" / "scout_run2.md"
RUN1_AT = datetime(2026, 7, 6, 15, 0, tzinfo=UTC)
RUN2_AT = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)


def _row(ticker, peg=0.9, **overrides):
    kwargs = dict(
        ticker=ticker,
        exchange="NASDAQ",
        company=f"{ticker} Inc",
        sector="Technology Services",
        close=100.0,
        market_cap=5e9,
        pe_ttm=18.0,
        peg_ttm=peg,
        eps_growth_ttm_pct=25.0,
        revenue_growth_ttm_pct=15.0,
        gross_margin_pct=55.0,
        operating_margin_pct=20.0,
        debt_to_equity=0.4,
        avg_volume_30d=2e6,
    )
    kwargs.update(overrides)
    return ScreenerRow(**kwargs)


class _StubScreener:
    def __init__(self, rows=None, error=None):
        self._rows, self._error = rows or [], error

    def scan(self, *, min_market_cap, min_avg_volume):
        if self._error is not None:
            raise self._error
        return self._rows


class _StubSource:
    """CLEANCO enriches fully; THINCO returns price only (no P/E, no PEG);
    DEADCO's fetch dies."""

    source_id = Source.YAHOO

    def __init__(self, fetched_at):
        self._fetched_at = fetched_at

    def covers(self, ticker):
        return True

    def fetch(self, ticker):
        if ticker == "DEADCO":
            raise SourceError("HTTP 502 from upstream")
        observations = [
            RawObservation(
                ticker=ticker, field=Field.PRICE, value_num=100.0,
                source=Source.YAHOO, fetched_at=self._fetched_at,
            )
        ]
        if ticker == "CLEANCO":
            observations += [
                RawObservation(
                    ticker=ticker, field=Field.PE_TTM, value_num=18.2,
                    source=Source.YAHOO, fetched_at=self._fetched_at,
                ),
                RawObservation(
                    ticker=ticker, field=Field.PEG, value_num=0.92,
                    source=Source.YAHOO, fetched_at=self._fetched_at,
                ),
                RawObservation(
                    ticker=ticker, field=Field.GROSS_MARGIN, value_num=0.55,
                    source=Source.YAHOO, fetched_at=self._fetched_at,
                ),
            ]
        return FetchResult(observations=tuple(observations))


class _CaptureSink:
    def __init__(self):
        self.digests = {}

    def write(self, markdown, *, run_id, as_of):
        self.digests[run_id] = markdown
        return None


ROWS = [_row("CLEANCO", peg=0.9), _row("THINCO", peg=1.1), _row("DEADCO", peg=1.3)]


@pytest.fixture()
def con(tmp_path):
    con = connect(tmp_path / "argus.db")
    migrate(con)
    yield con
    con.close()


def _scout(con, sink, as_of, rows=ROWS, error=None):
    return run_scout(
        con=con,
        screener=_StubScreener(rows=rows, error=error),
        criteria=ScoutCriteria(top_n=10),
        sources=[_StubSource(as_of)],
        profile=DEFAULT_PROFILE,
        sink=sink,
        as_of=as_of,
        today=as_of.date(),
        app_version="scout-test",
        exclude=set(),
    )


class TestScoutRun:
    def test_eligibility_verdicts(self, con):
        sink = _CaptureSink()
        outcome = _scout(con, sink, RUN1_AT)
        assert outcome.status == "partial"  # DEADCO's fetch died
        rows = con.execute(
            "SELECT ticker, status, exclusion_reason FROM scout_candidates "
            "WHERE run_id = ? ORDER BY ticker",
            (outcome.run_id,),
        ).fetchall()
        verdicts = {r["ticker"]: (r["status"], r["exclusion_reason"]) for r in rows}
        assert verdicts["CLEANCO"] == ("proposed", None)
        assert verdicts["THINCO"][0] == "excluded"
        assert "P/E or PEG" in verdicts["THINCO"][1]
        assert verdicts["DEADCO"][0] == "excluded"
        assert "fetch failed" in verdicts["DEADCO"][1]

    def test_verified_peg_over_ceiling_excludes(self, con):
        """The first live run's INCY case: screener claims PEG 0.008, our
        gated value says 11.99 — the verified number wins and the digest
        shows both."""

        class _RichPegSource(_StubSource):
            def fetch(self, ticker):
                result = super().fetch(ticker)
                observations = tuple(
                    o for o in result.observations if o.field is not Field.PEG
                ) + (
                    RawObservation(
                        ticker=ticker, field=Field.PEG, value_num=11.99,
                        source=Source.YAHOO, fetched_at=self._fetched_at,
                    ),
                )
                return FetchResult(observations=observations)

        sink = _CaptureSink()
        outcome = run_scout(
            con=con,
            screener=_StubScreener(rows=[_row("CLEANCO", peg=0.008)]),
            criteria=ScoutCriteria(top_n=10),
            sources=[_RichPegSource(RUN1_AT)],
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=RUN1_AT,
            today=RUN1_AT.date(),
            app_version="scout-test",
            exclude=set(),
        )
        row = con.execute(
            "SELECT status, exclusion_reason FROM scout_candidates WHERE run_id = ?",
            (outcome.run_id,),
        ).fetchone()
        assert row["status"] == "excluded"
        assert "verified PEG 11.99" in row["exclusion_reason"]
        assert "0.008" in row["exclusion_reason"]  # the screener's claim, named

    def test_verified_negative_peg_also_excludes(self, con):
        """Review finding: the ceiling-only check let a verified PEG of -3.4
        (negative earnings growth) through — 'zero or negative is meaningless,
        never a bargain' applies to verified values too."""

        class _NegativePegSource(_StubSource):
            def fetch(self, ticker):
                result = super().fetch(ticker)
                observations = tuple(
                    o for o in result.observations if o.field is not Field.PEG
                ) + (
                    RawObservation(
                        ticker=ticker, field=Field.PEG, value_num=-3.4,
                        source=Source.YAHOO, fetched_at=self._fetched_at,
                    ),
                )
                return FetchResult(observations=observations)

        sink = _CaptureSink()
        outcome = run_scout(
            con=con,
            screener=_StubScreener(rows=[_row("CLEANCO", peg=0.9)]),
            criteria=ScoutCriteria(top_n=10),
            sources=[_NegativePegSource(RUN1_AT)],
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=RUN1_AT,
            today=RUN1_AT.date(),
            app_version="scout-test",
            exclude=set(),
        )
        row = con.execute(
            "SELECT status, exclusion_reason FROM scout_candidates WHERE run_id = ?",
            (outcome.run_id,),
        ).fetchone()
        assert row["status"] == "excluded"
        assert "zero or negative" in row["exclusion_reason"]

    def test_duplicate_house_symbols_dedupe_instead_of_crashing(self, con):
        """Review finding: DUP.A and DUP-A both normalize to DUP-A; without
        dedupe the second insert violated the store's per-run keys and killed
        the whole run."""
        sink = _CaptureSink()
        outcome = _scout(
            con, sink, RUN1_AT, rows=[_row("DUP.A", peg=0.5), _row("DUP-A", peg=0.7)]
        )
        assert outcome.status in ("complete", "partial")
        rows = con.execute(
            "SELECT ticker FROM scout_candidates WHERE run_id = ?", (outcome.run_id,)
        ).fetchall()
        assert [r["ticker"] for r in rows] == ["DUP-A"]  # once, best rank kept

    def test_all_enrichment_failed_still_digests(self, con):
        """Review finding: screener fine + every fetch dead produced NO digest
        even though a full page of exclusion verdicts existed."""
        sink = _CaptureSink()
        outcome = _scout(con, sink, RUN1_AT, rows=[_row("DEADCO")])
        assert outcome.status == "failed"
        digest = sink.digests[outcome.run_id]
        assert "DEADCO" in digest and "fetch failed" in digest

    def test_outage_run_does_not_break_streaks(self, con):
        """Review finding: a screener-outage week reset every streak to 'new'
        — an outage is not a verdict."""
        sink = _CaptureSink()
        _scout(con, sink, RUN1_AT)  # CLEANCO proposed
        _scout(con, sink, datetime(2026, 7, 10, 15, 0, tzinfo=UTC), error=ScreenerError("503"))
        third = _scout(con, sink, RUN2_AT)  # CLEANCO proposed again
        assert queries.scout_streak(con, "CLEANCO", third.run_id) == 2

    def test_streak_counts_consecutive_proposed_runs(self, con):
        sink = _CaptureSink()
        first = _scout(con, sink, RUN1_AT)
        second = _scout(con, sink, RUN2_AT)
        assert queries.scout_streak(con, "CLEANCO", second.run_id) == 2
        assert queries.scout_streak(con, "THINCO", second.run_id) == 0  # excluded both runs
        report = queries.run_report(con, second.run_id)
        proposed = [p for p in report.scout if p.status == "proposed"]
        assert [p.ticker for p in proposed] == ["CLEANCO"]
        assert proposed[0].streak == 2
        assert first.run_id != second.run_id

    def test_golden_scout_digest(self, con, update_golden):
        sink = _CaptureSink()
        _scout(con, sink, RUN1_AT)
        outcome = _scout(con, sink, RUN2_AT)
        digest = sink.digests[outcome.run_id]
        assert "CLEANCO" in digest and "2w" in digest  # streak visible
        assert "promote" in digest  # the human-decides hint
        if update_golden:
            GOLDEN.write_text(digest, encoding="utf-8")
            pytest.skip("golden regenerated")
        assert GOLDEN.exists(), "run `uv run pytest --update-golden` once"
        assert digest == GOLDEN.read_text(encoding="utf-8")

    def test_screener_outage_produces_a_digest_that_says_so(self, con):
        sink = _CaptureSink()
        outcome = _scout(con, sink, RUN1_AT, error=ScreenerError("HTTP 503"))
        assert outcome.status == "failed"
        digest = sink.digests[outcome.run_id]
        assert "screener unavailable" in digest
        assert "HTTP 503" in digest
        # an outage digest must NOT read like "nothing passed the screen"
        assert "No candidates passed" not in digest

    def test_screener_metrics_never_reach_observations(self, con):
        sink = _CaptureSink()
        outcome = _scout(con, sink, RUN1_AT)
        sources = {
            r["source"]
            for r in con.execute(
                "SELECT DISTINCT source FROM observations WHERE run_id = ?",
                (outcome.run_id,),
            )
        }
        assert "tradingview" not in sources  # claims stay in scout_candidates


class TestScoutStore:
    def test_excluded_requires_reason_in_db(self, con):
        con.execute(
            "INSERT INTO runs (kind, started_at, app_version) VALUES ('scout', ?, 't')",
            (RUN1_AT.isoformat(),),
        )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                """INSERT INTO scout_candidates
                     (run_id, ticker, rank, status, exclusion_reason, screen_reasons, screener_metrics)
                   VALUES (1, 'X', 1, 'excluded', NULL, '{}', '{}')"""
            )

    def test_record_model_enforces_reason_iff_excluded(self):
        with pytest.raises(ValueError):
            ScoutCandidateRecord(
                ticker="X", rank=1, status="proposed", exclusion_reason="nope",
                screen_reasons={}, screener_metrics={},
            )


runner = CliRunner()


class TestPromote:
    def _init(self, tmp_path, monkeypatch):
        for var in ("ARGUS_EMAIL_TO", "ARGUS_SMTP_USER", "ARGUS_SMTP_PASSWORD", "ARGUS_DISCORD_WEBHOOK"):
            monkeypatch.delenv(var, raising=False)
        runner.invoke(app, ["init", "--root", str(tmp_path)])

    def test_promote_onto_scaffold_and_again(self, tmp_path, monkeypatch):
        self._init(tmp_path, monkeypatch)
        first = runner.invoke(
            app, ["promote", "cleanco", "--thesis", "Vertical SaaS moat.", "--root", str(tmp_path)]
        )
        assert first.exit_code == 0
        second = runner.invoke(
            app,
            ["promote", "OTHERCO", "--thesis", 'Margins "inflect" this year.', "--root", str(tmp_path)],
        )
        assert second.exit_code == 0
        contexts = build_contexts(load_watch_config(tmp_path / "watchlist.yaml"))
        assert [c.ticker for c in contexts] == ["CLEANCO", "OTHERCO"]
        assert contexts[0].thesis == "Vertical SaaS moat."
        assert contexts[1].thesis == 'Margins "inflect" this year.'  # quoting survives

    def test_duplicate_refused(self, tmp_path, monkeypatch):
        self._init(tmp_path, monkeypatch)
        runner.invoke(app, ["promote", "X", "--thesis", "t", "--root", str(tmp_path)])
        again = runner.invoke(app, ["promote", "x", "--thesis", "t2", "--root", str(tmp_path)])
        assert again.exit_code == 1
        assert "already" in again.output

    def test_thesis_with_backslashes_newlines_unicode_round_trips(self, tmp_path, monkeypatch):
        """Review finding: json.dumps output fed to re.sub as a TEMPLATE
        collapsed backslashes and crashed on non-ASCII — the user's words
        must come back exactly."""
        self._init(tmp_path, monkeypatch)
        thesis = 'Ставка on C:\\ drives and "\\n" literals — moat™'
        result = runner.invoke(
            app, ["promote", "WEIRD", "--thesis", thesis, "--root", str(tmp_path)]
        )
        assert result.exit_code == 0
        contexts = build_contexts(load_watch_config(tmp_path / "watchlist.yaml"))
        assert contexts[0].thesis == thesis

    def test_garbage_ticker_and_empty_thesis_refused(self, tmp_path, monkeypatch):
        self._init(tmp_path, monkeypatch)
        bad = runner.invoke(app, ["promote", "not a ticker!", "--thesis", "t", "--root", str(tmp_path)])
        assert bad.exit_code == 1
        empty = runner.invoke(app, ["promote", "OK", "--thesis", "   ", "--root", str(tmp_path)])
        assert empty.exit_code == 1
        assert "decision" in empty.output


def test_screen_smoke_end_to_end():
    """The pinned interfaces meet: screener rows → criteria.screen → ranked."""
    candidates = screen(ROWS, ScoutCriteria(top_n=2), exclude=set())
    assert [c.row.ticker for c in candidates][:1] == ["CLEANCO"]  # lowest PEG first
    assert candidates[0].rank == 1
    assert candidates[0].reasons  # populated, JSON-able
