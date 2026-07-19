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


def _row(ticker, fwd_pe=15.0, **overrides):
    kwargs = dict(
        ticker=ticker,
        exchange="NASDAQ",
        company=f"{ticker} Inc",
        sector="Technology Services",
        industry="Widgets",
        close=100.0,
        market_cap=5e9,
        pe_ttm=18.0,
        fwd_pe=fwd_pe,
        roe_pct=25.0,
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
                    ticker=ticker, field=Field.PE_FWD, value_num=14.8,
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


def _no_prices(ticker, start):
    """Default scorecard fetcher for scout tests: no history → nothing scored
    (the scorecard section renders its 'forward log starts now' line)."""
    return None


def _synthetic_prices(ticker, start):
    """Deterministic rising series so the golden scorecard populates: a name
    up ~10% from `start`, SPY up ~4%, no network."""
    from datetime import timedelta

    slope = 0.04 if ticker == "SPY" else 0.10
    days = [start + timedelta(days=i) for i in range(0, 40, 7)]
    base = 100.0
    return [(d, base * (1 + slope * i / (len(days) - 1))) for i, d in enumerate(days)]


def _scout(con, sink, as_of, rows=ROWS, error=None, price_fetcher=_no_prices):
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
        price_fetcher=price_fetcher,
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
        assert "forward or trailing P/E" in verdicts["THINCO"][1]
        assert verdicts["DEADCO"][0] == "excluded"
        assert "fetch failed" in verdicts["DEADCO"][1]

    def _scout_with_fwd_pe(self, con, verified_fwd_pe, claimed_fwd_pe):
        class _FwdPeSource(_StubSource):
            def fetch(self, ticker):
                result = super().fetch(ticker)
                observations = tuple(
                    o for o in result.observations if o.field is not Field.PE_FWD
                ) + (
                    RawObservation(
                        ticker=ticker, field=Field.PE_FWD, value_num=verified_fwd_pe,
                        source=Source.YAHOO, fetched_at=self._fetched_at,
                    ),
                )
                return FetchResult(observations=observations)

        sink = _CaptureSink()
        outcome = run_scout(
            con=con,
            screener=_StubScreener(rows=[_row("CLEANCO", fwd_pe=claimed_fwd_pe)]),
            criteria=ScoutCriteria(top_n=10),
            sources=[_FwdPeSource(RUN1_AT)],
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=RUN1_AT,
            today=RUN1_AT.date(),
            app_version="scout-test",
            exclude=set(),
            price_fetcher=_no_prices,
        )
        return con.execute(
            "SELECT status, exclusion_reason FROM scout_candidates WHERE run_id = ?",
            (outcome.run_id,),
        ).fetchone()

    def test_verified_fwd_pe_over_ceiling_excludes(self, con):
        """The INCY divergence class: screener claims fwd P/E 12, our gated
        value says 61.5 — the verified number wins and the digest shows both."""
        row = self._scout_with_fwd_pe(con, verified_fwd_pe=61.5, claimed_fwd_pe=12.0)
        assert row["status"] == "excluded"
        assert "verified fwd P/E 61.5" in row["exclusion_reason"]
        assert "12.0" in row["exclusion_reason"]  # the screener's claim, named

    def test_verified_negative_fwd_pe_also_excludes(self, con):
        """The mirror case: a verified negative forward P/E means expected
        losses — outside the screen's valuation window, never a bargain."""
        row = self._scout_with_fwd_pe(con, verified_fwd_pe=-8.2, claimed_fwd_pe=14.0)
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

    def test_verified_roe_below_floor_excludes(self, con):
        """The SSRM case: screener claimed ROE 17.3%, verified 12.4% — the
        verified number decides quality floors too."""

        class _LowRoeSource(_StubSource):
            def fetch(self, ticker):
                result = super().fetch(ticker)
                return FetchResult(
                    observations=result.observations
                    + (
                        RawObservation(
                            ticker=ticker, field=Field.ROE, value_num=0.124,
                            source=Source.YAHOO, fetched_at=self._fetched_at,
                        ),
                    )
                )

        sink = _CaptureSink()
        outcome = run_scout(
            con=con,
            screener=_StubScreener(rows=[_row("CLEANCO", roe_pct=17.3)]),
            criteria=ScoutCriteria(top_n=10),
            sources=[_LowRoeSource(RUN1_AT)],
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=RUN1_AT,
            today=RUN1_AT.date(),
            app_version="scout-test",
            exclude=set(),
            price_fetcher=_no_prices,
        )
        row = con.execute(
            "SELECT status, exclusion_reason FROM scout_candidates WHERE run_id = ?",
            (outcome.run_id,),
        ).fetchone()
        assert row["status"] == "excluded"
        assert "verified ROE 12.4%" in row["exclusion_reason"]
        assert "17.3%" in row["exclusion_reason"]

    def test_verified_shrinking_revenue_excludes(self, con):
        """Window-honest direction check: verified MRQ YoY revenue growth
        that is negative disqualifies whatever the screener's TTM said."""

        class _ShrinkingSource(_StubSource):
            def fetch(self, ticker):
                result = super().fetch(ticker)
                return FetchResult(
                    observations=result.observations
                    + (
                        RawObservation(
                            ticker=ticker, field=Field.REVENUE_GROWTH, value_num=-0.052,
                            source=Source.YAHOO, fetched_at=self._fetched_at,
                        ),
                    )
                )

        sink = _CaptureSink()
        outcome = run_scout(
            con=con,
            screener=_StubScreener(rows=[_row("CLEANCO")]),
            criteria=ScoutCriteria(top_n=10),
            sources=[_ShrinkingSource(RUN1_AT)],
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=RUN1_AT,
            today=RUN1_AT.date(),
            app_version="scout-test",
            exclude=set(),
            price_fetcher=_no_prices,
        )
        row = con.execute(
            "SELECT status, exclusion_reason FROM scout_candidates WHERE run_id = ?",
            (outcome.run_id,),
        ).fetchone()
        assert row["status"] == "excluded"
        assert "MRQ YoY" in row["exclusion_reason"]
        assert "-5.2%" in row["exclusion_reason"]

    def test_sector_leaders_persist_and_render(self, con):
        """A sector shut out of the shortlist gets one leader row: persisted
        with status='leader', never enriched, rendered in the digest strip."""
        rows = [
            _row("CLEANCO", fwd_pe=10.0),
            _row("FINCO", fwd_pe=20.0, sector="Finance", industry="Major Banks"),
        ]
        sink = _CaptureSink()
        outcome = run_scout(
            con=con,
            screener=_StubScreener(rows=rows),
            criteria=ScoutCriteria(top_n=1),
            sources=[_StubSource(RUN1_AT)],
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=RUN1_AT,
            today=RUN1_AT.date(),
            app_version="scout-test",
            exclude=set(),
            price_fetcher=_no_prices,
        )
        rows_db = con.execute(
            "SELECT ticker, status, sector FROM scout_candidates WHERE run_id = ? ORDER BY status",
            (outcome.run_id,),
        ).fetchall()
        verdicts = {r["ticker"]: (r["status"], r["sector"]) for r in rows_db}
        assert verdicts["CLEANCO"][0] == "proposed"
        assert verdicts["FINCO"] == ("leader", "Financial Services")
        fetched = con.execute(
            "SELECT COUNT(*) AS n FROM run_tickers WHERE run_id = ?", (outcome.run_id,)
        ).fetchone()
        assert fetched["n"] == 1  # the leader was never enriched
        digest = sink.digests[outcome.run_id]
        assert "Sector leaders beyond the shortlist" in digest
        assert "FINCO" in digest

    def test_all_unpriceable_scorecard_is_disclosed_not_hidden(self, con):
        """Review finding: a scoring run where eligible past proposals exist
        but none can be priced (fetch down) must SAY so, not read as 'nothing
        has matured'."""
        sink = _CaptureSink()
        _scout(con, sink, RUN1_AT)  # CLEANCO proposed run 1
        outcome = _scout(con, sink, RUN2_AT, price_fetcher=_no_prices)  # fetch down run 2
        digest = sink.digests[outcome.run_id]
        assert "Price data was unavailable" in digest
        assert "the forward log starts now" not in digest

    def test_peer_context_round_trips(self, con):
        """Same-industry peers + median from the SAME scan land on the
        proposal and render in the digest bullet."""
        rows = [
            _row("CLEANCO", fwd_pe=10.0),
            _row("PEERONE", fwd_pe=20.0, market_cap=9e9),
            _row("PEERTWO", fwd_pe=30.0, market_cap=8e9),
        ]
        sink = _CaptureSink()
        outcome = run_scout(
            con=con,
            screener=_StubScreener(rows=rows),
            criteria=ScoutCriteria(top_n=10, max_per_sector=0),
            sources=[_StubSource(RUN1_AT)],
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=RUN1_AT,
            today=RUN1_AT.date(),
            app_version="scout-test",
            exclude=set(),
            price_fetcher=_no_prices,
        )
        report = queries.run_report(con, outcome.run_id)
        clean = next(p for p in report.scout if p.ticker == "CLEANCO")
        assert clean.peer_context is not None
        assert clean.peer_context["industry"] == "Widgets"
        assert clean.peer_context["n"] == 3
        assert clean.peer_context["median_fwd_pe"] == 20.0
        peer_tickers = {peer["ticker"] for peer in clean.peer_context["peers"]}
        assert peer_tickers == {"PEERONE", "PEERTWO"}
        assert "vs industry median fwd P/E 20" in sink.digests[outcome.run_id]

    def test_render_survives_adversarial_peer_context_json(self, con):
        """Review findings: a string median (JSON round-trips are unvalidated)
        crashed render(); NaN fwd_pe on a leader printed 'fwd P/E nan'; both
        must degrade, never kill the digest or report --run N."""
        from argus.digest import render
        from argus.models import ScoutCandidateRecord
        from argus.store import writer

        con.execute(
            "INSERT INTO runs (kind, started_at, app_version, status, finished_at) "
            "VALUES ('scout', ?, 't', 'complete', ?)",
            (RUN1_AT.isoformat(), RUN1_AT.isoformat()),
        )
        writer.write_scout_candidates(
            con,
            run_id=1,
            records=[
                ScoutCandidateRecord(
                    ticker="ODDCO", rank=1, status="proposed", sector="Technology",
                    screen_reasons={"forward_pe": "fwd P/E 10.0 ≤ 25"},
                    screener_metrics={},
                    peer_context={"industry": "Widgets", "median_fwd_pe": "28.4", "n": None},
                ),
                ScoutCandidateRecord(
                    ticker="NANCO", rank=2, status="leader", sector="Energy",
                    screen_reasons={}, screener_metrics={"fwd_pe": float("nan")},
                ),
            ],
        )
        digest = render(queries.run_report(con, 1))  # must not raise
        assert "ODDCO" in digest
        assert "median fwd P/E" not in digest  # unformattable claim → omitted
        assert "nan" not in digest
        assert "(#2 overall)" in digest  # leader degrades to rank-only

    def test_leaders_render_even_when_nothing_is_proposed(self, con):
        """Review finding: the no-proposals early return silently swallowed
        the leaders strip while the PDF showed it — artifacts must agree."""
        rows = [
            _row("BADCO", fwd_pe=10.0),  # will fail enrichment (no margins source)
            _row("FINCO", fwd_pe=20.0, sector="Finance", industry="Major Banks"),
        ]

        class _ThinSource(_StubSource):
            def fetch(self, ticker):  # price only → core fields unverifiable
                result = super().fetch(ticker)
                price_only = tuple(o for o in result.observations if o.field is Field.PRICE)
                return FetchResult(observations=price_only)

        sink = _CaptureSink()
        outcome = run_scout(
            con=con,
            screener=_StubScreener(rows=rows),
            criteria=ScoutCriteria(top_n=1),
            sources=[_ThinSource(RUN1_AT)],
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=RUN1_AT,
            today=RUN1_AT.date(),
            app_version="scout-test",
            exclude=set(),
            price_fetcher=_no_prices,
        )
        digest = sink.digests[outcome.run_id]
        assert "No candidates passed" in digest
        assert "Sector leaders beyond the shortlist" in digest
        assert "FINCO" in digest

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

    def test_rank_history_tracks_proposed_ranks_chronologically(self, con):
        """rank_history carries the ticker's screen rank across recent proposed
        scout runs, oldest→newest, powering the PDF rank sparkline. A name never
        proposed contributes no points (gaps are honest, never faked)."""
        sink = _CaptureSink()
        _scout(con, sink, RUN1_AT)  # CLEANCO proposed at rank 1
        second = _scout(con, sink, RUN2_AT)  # proposed again at rank 1
        assert queries.scout_rank_history(con, "CLEANCO", second.run_id) == (1, 1)
        assert queries.scout_rank_history(con, "THINCO", second.run_id) == ()  # never proposed
        report = queries.run_report(con, second.run_id)
        cleanco = next(p for p in report.scout if p.ticker == "CLEANCO")
        assert cleanco.rank_history == (1, 1)

    def test_new_this_week_callout_lists_fresh_names_then_says_held(self, con):
        """A first-time name (streak 1) is called out by ticker; once it is a
        returning name the callout reports the shortlist held — the honest
        version of 'nothing new cleared the screen'."""
        sink = _CaptureSink()
        first = _scout(con, sink, RUN1_AT)
        assert "**New this week:** CLEANCO" in sink.digests[first.run_id]
        second = _scout(con, sink, RUN2_AT)
        assert "No new names cleared the screen this week" in sink.digests[second.run_id]

    def test_golden_scout_digest(self, con, update_golden):
        sink = _CaptureSink()
        _scout(con, sink, RUN1_AT, price_fetcher=_synthetic_prices)
        outcome = _scout(con, sink, RUN2_AT, price_fetcher=_synthetic_prices)
        digest = sink.digests[outcome.run_id]
        assert "CLEANCO" in digest and "2w" in digest  # streak visible
        assert "promote" in digest  # the human-decides hint
        assert "Scorecard" in digest  # CLEANCO (proposed run 1) scored on run 2
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


class TestCompanyProfiles:
    def test_profile_round_trips_from_fetch_to_report(self, con):
        """A profile captured at enrichment lands in company_profiles and
        rides TickerReport.profile into the digest inputs."""
        from argus.models import CompanyProfile
        from argus.store import writer

        class _ProfileSource(_StubSource):
            def fetch(self, ticker):
                result = super().fetch(ticker)
                return FetchResult(
                    observations=result.observations,
                    profile=CompanyProfile(
                        ticker=ticker,
                        name="Cleanco Incorporated",
                        sector="Technology",
                        industry="Software",
                        employees=1200,
                        summary="Cleanco sells verified cleanliness.",
                        source=Source.YAHOO,
                        fetched_at=self._fetched_at,
                    ),
                )

        sink = _CaptureSink()
        outcome = run_scout(
            con=con,
            screener=_StubScreener(rows=[_row("CLEANCO")]),
            criteria=ScoutCriteria(top_n=10),
            sources=[_ProfileSource(RUN1_AT)],
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=RUN1_AT,
            today=RUN1_AT.date(),
            app_version="scout-test",
            exclude=set(),
            price_fetcher=_no_prices,
        )
        stored = queries.company_profile(con, "CLEANCO")
        assert stored is not None and stored.sector == "Technology"
        report = queries.run_report(con, outcome.run_id)
        assert report.tickers[0].profile is not None
        assert report.tickers[0].profile.name == "Cleanco Incorporated"
        digest = sink.digests[outcome.run_id]
        assert "(Technology · Software)" in digest  # business context in the md too


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
    result = screen(ROWS, ScoutCriteria(top_n=2), exclude=set())
    assert [c.row.ticker for c in result.shortlist][:1] == ["CLEANCO"]  # cheapest fwd-PEG first
    assert result.shortlist[0].rank == 1
    assert result.shortlist[0].reasons  # populated, JSON-able
