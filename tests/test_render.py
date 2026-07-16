"""render() contract tests: tri-state honesty, silence-is-a-statement, the
run-wide quarantine table, determinism — and the founding negative assertion
that a quarantined 35.00 target against a 10.97 price can never surface as
"218% upside". Fixtures are built directly from models.py types; no DB."""

from datetime import UTC, date, datetime

from argus.digest import render
from argus.fields import Field, QuarantineCode, Source
from argus.models import (
    EarningsImminent,
    EarningsReported,
    FieldQuarantined,
    FieldValue,
    PriceMove,
    QuarantineHit,
    QuarantinedObservation,
    RunReport,
    Snapshot,
    SourceHealth,
    TickerContext,
    TickerReport,
)

NOW = datetime(2026, 7, 12, 14, 3, tzinfo=UTC)
BASELINE_AT = datetime(2026, 6, 28, 13, 0, tzinfo=UTC)

TARGET_HIT = QuarantineHit(
    code=QuarantineCode.TARGET_PRICE_RATIO,
    detail="target 35.00 (yahoo) / price 10.97 (yahoo) = 3.19 outside [0.3, 3.0]",
)


def _fv(field, value, source=Source.YAHOO, corroborated_by=()):
    return FieldValue(
        field=field, value=value, source=source, fetched_at=NOW, corroborated_by=corroborated_by
    )


def _report(tickers, *, run_id=7, status="complete"):
    return RunReport(run_id=run_id, kind="watch", as_of=NOW, status=status, tickers=tuple(tickers))


def _ntdoy_report() -> RunReport:
    """The founding case, as a RunReport: price accepted and corroborated,
    the stale 35.00 target quarantined, EDGAR down for the ticker — so one
    report exercises all three tri-states plus quarantine and degradation."""
    snapshot = Snapshot(
        ticker="NTDOY",
        run_id=7,
        as_of=NOW,
        values={
            Field.PRICE: _fv(Field.PRICE, 10.97, corroborated_by=(Source.FINNHUB,)),
            Field.MARKET_CAP: _fv(Field.MARKET_CAP, 52_300_000_000.0),
            Field.ANALYST_RATING: _fv(Field.ANALYST_RATING, "buy"),
        },
        quarantined={Field.ANALYST_TARGET_MEAN: (TARGET_HIT,)},
    )
    ticker = TickerReport(
        context=TickerContext(ticker="NTDOY", thesis="Switch 2 cycle + IP monetization."),
        status="partial",
        snapshot=snapshot,
        events=(
            FieldQuarantined(ticker="NTDOY", field=Field.ANALYST_TARGET_MEAN, reasons=(TARGET_HIT,)),
        ),
        quarantines=(
            QuarantinedObservation(
                field=Field.ANALYST_TARGET_MEAN,
                source=Source.YAHOO,
                fetched_at=NOW,
                reasons=(TARGET_HIT,),
            ),
        ),
        sources=(
            SourceHealth(source=Source.YAHOO, status="ok"),
            SourceHealth(source=Source.EDGAR, status="error", error="HTTP 502"),
            SourceHealth(source=Source.FINNHUB, status="ok"),
        ),
        baseline_run_id=6,
        baseline_as_of=BASELINE_AT,
    )
    return _report([ticker], status="partial")


def _quiet_ticker(name="NVDA", **overrides) -> TickerReport:
    """A healthy ticker with zero events — the silence case."""
    kwargs = dict(
        context=TickerContext(ticker=name, thesis="Datacenter capex supercycle; CUDA moat."),
        status="ok",
        snapshot=Snapshot(
            ticker=name,
            run_id=7,
            as_of=NOW,
            values={Field.PRICE: _fv(Field.PRICE, 181.25)},
        ),
        sources=(
            SourceHealth(source=Source.YAHOO, status="ok"),
            SourceHealth(source=Source.FINNHUB, status="ok"),
        ),
        baseline_run_id=6,
        baseline_as_of=BASELINE_AT,
    )
    kwargs.update(overrides)
    return TickerReport(**kwargs)


class TestFoundingNegativeAssertion:
    def test_218_appears_nowhere(self):
        """price 10.97 accepted, target 35.00 quarantined → '218% upside' is
        uncomputable and unprintable. The reason this project exists."""
        out = render(_ntdoy_report())
        assert "218" not in out
        assert "10.97" in out  # the accepted price still reports normally
        assert "DATA QUARANTINED" in out


class TestTriState:
    def test_value_with_provenance_and_corroboration(self):
        out = render(_ntdoy_report())
        assert "- Price: 10.97 (yahoo, 2026-07-12 14:03Z) ✓finnhub" in out

    def test_market_cap_is_humanized(self):
        out = render(_ntdoy_report())
        assert "- Market cap: 52.3B (yahoo, 2026-07-12 14:03Z)" in out

    def test_quarantined_field_line(self):
        out = render(_ntdoy_report())
        assert (
            "- Analyst target (mean): ⚠ DATA QUARANTINED — "
            "target 35.00 (yahoo) / price 10.97 (yahoo) = 3.19 outside [0.3, 3.0]"
        ) in out

    def test_no_data_without_a_source_problem_reads_not_provided(self):
        # PE_TTM's only priority source (yahoo) is ok → the absence is the source's.
        out = render(_ntdoy_report())
        assert "- P/E (TTM): — no data (not provided)" in out

    def test_no_data_cause_derived_from_source_health(self):
        # GROSS_MARGIN's priority includes edgar, which errored for this ticker.
        out = render(_ntdoy_report())
        assert "- Gross margin: — no data (edgar: HTTP 502)" in out

    def test_watchlist_fields_follow_enum_order(self):
        out = render(_ntdoy_report())
        positions = [out.index(f"- {label}:") for label in ("Price", "Market cap", "Analyst rating")]
        assert positions == sorted(positions)


class TestChangesSection:
    def test_zero_events_run_wide_states_no_changes(self):
        out = render(_report([_quiet_ticker()]))
        assert "No changes since last run." in out

    def test_quarantine_transition_is_a_headline(self):
        out = render(_ntdoy_report())
        assert "⚠ Analyst target (mean) went dark — DATA QUARANTINED:" in out
        assert "No changes since last run." not in out

    def test_thesis_prints_under_the_ticker_heading(self):
        out = render(_ntdoy_report())
        assert "_Switch 2 cycle + IP monetization._" in out

    def test_numeric_move_prints_old_new_pct_threshold_and_window(self):
        mover = _quiet_ticker(
            events=(
                PriceMove(
                    ticker="NVDA", old=170.0, new=181.25, pct=6.6, threshold=5.0, old_as_of=BASELINE_AT
                ),
                EarningsImminent(ticker="NVDA", earnings_date=date(2026, 7, 17), days_until=4),
            ),
        )
        out = render(_report([mover]))
        assert "- Price 170.00 → 181.25 (+6.6%, threshold 5.0%) vs 2026-06-28" in out
        assert "- Earnings imminent: 2026-07-17 (in 4 days)" in out

    def test_earnings_reported_prints_actual_estimate_and_surprise(self):
        reporter = _quiet_ticker(
            events=(
                EarningsReported(
                    ticker="NVDA", quarter_end=date(2026, 6, 30),
                    eps_actual=1.05, eps_estimate=0.93, surprise_pct=12.9,
                ),
            ),
        )
        out = render(_report([reporter]))
        assert "- Earnings reported (quarter ended 2026-06-30): EPS 1.05 vs 0.93 est (+12.9%)" in out

    def test_earnings_reported_without_estimate_says_so(self):
        reporter = _quiet_ticker(
            events=(
                EarningsReported(
                    ticker="NVDA", quarter_end=date(2026, 6, 30), eps_actual=-0.42
                ),
            ),
        )
        out = render(_report([reporter]))
        assert "- Earnings reported (quarter ended 2026-06-30): EPS -0.42 (no street estimate)" in out

    def test_first_run_notes_baseline_established(self):
        out = render(_report([_quiet_ticker(baseline_run_id=None, baseline_as_of=None)]))
        assert "Baseline established this run" in out
        assert "NVDA" in out


class TestQuarantineTable:
    def test_lists_quarantine_coexisting_with_accepted_primary(self):
        """Snapshot.quarantined only carries fields that went fully dark; the
        table must also show a quarantined leg beside an accepted primary."""
        stale_hit = QuarantineHit(code=QuarantineCode.STALE, detail="quote 6 days old")
        ticker = _quiet_ticker(
            quarantines=(
                QuarantinedObservation(
                    field=Field.PRICE, source=Source.FINNHUB, fetched_at=NOW, reasons=(stale_hit,)
                ),
            ),
        )
        out = render(_report([ticker]))
        assert "## Data quarantined" in out
        assert "| NVDA | Price | finnhub | stale: quote 6 days old | 2026-07-12 14:03Z |" in out
        # ...and the field's accepted primary still renders as a value line.
        assert "- Price: 181.25 (yahoo, 2026-07-12 14:03Z)" in out

    def test_zero_quarantines_run_wide_renders_one_line_no_section(self):
        out = render(_report([_quiet_ticker()]))
        assert "No data quarantined this run." in out
        assert "## Data quarantined" not in out


class TestDataHealth:
    def test_down_cross_check_source_names_skipped_checks(self):
        out = render(_ntdoy_report())
        assert (
            "- edgar: 1 error (first: HTTP 502) — gross margin, operating margin, "
            "debt/equity cross-checks skipped (1 ticker)"
        ) in out
        assert "- yahoo: 1 ok" in out

    def test_failed_ticker_listed_with_error_and_flagged_in_changes_and_watchlist(self):
        dead = TickerReport(
            context=TickerContext(ticker="TCEHY", thesis="WeChat moat."),
            status="failed",
            error="HTTP 502 from yahoo",
        )
        out = render(_report([_quiet_ticker(), dead], status="partial"))
        assert "Fetch failures (no data this run): TCEHY (HTTP 502 from yahoo)." in out
        assert "Fetch failed — no data this run (HTTP 502 from yahoo)." in out
        assert "Failed tickers:" in out
        assert "- TCEHY: HTTP 502 from yahoo" in out


class TestHeaderAndFooter:
    def test_header_names_kind_run_date_and_status(self):
        out = render(_ntdoy_report())
        assert out.startswith("# Argus watch digest — run 7 — 2026-07-12")

    def test_partial_run_discloses_degradation_up_front(self):
        out = render(_ntdoy_report())
        assert "Status: PARTIAL" in out

    def test_complete_run_says_so(self):
        out = render(_report([_quiet_ticker()]))
        assert "Status: complete." in out

    def test_footer_is_self_identifying(self):
        out = render(_ntdoy_report())
        assert out.rstrip().endswith("Run 7 — regenerate with `argus report --run 7`.")


class TestDeterminismAndHygiene:
    def test_identical_report_renders_byte_identical(self):
        report = _ntdoy_report()
        assert render(report) == render(report)

    def test_no_trailing_whitespace_lines(self):
        out = render(_ntdoy_report())
        for line in out.splitlines():
            assert line == line.rstrip(), f"trailing whitespace: {line!r}"

    def test_ends_with_exactly_one_newline(self):
        out = render(_ntdoy_report())
        assert out.endswith("\n") and not out.endswith("\n\n")

    def test_tickers_render_alphabetically(self):
        out = render(_report([_quiet_ticker("NVDA"), _quiet_ticker("ASML")]))
        assert out.index("### ASML") < out.index("### NVDA")
