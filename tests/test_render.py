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
    MacroLineCrossed,
    MacroPrint,
    MacroShift,
    MacroSpec,
    PriceMove,
    QuarantineHit,
    QuarantinedObservation,
    RunReport,
    Snapshot,
    SourceHealth,
    TickerContext,
    TickerReport,
)
from argus.thesis import parse_thesis_check

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

    def test_insider_buy_line(self):
        from argus.models import InsiderActivity

        reporter = _quiet_ticker(events=(
            InsiderActivity(
                ticker="NVDA", owner="Jane Buyer", role="officer: CFO",
                shares=5000.0, price=42.5, transaction_date=date(2026, 6, 30),
            ),
        ))
        out = render(_report([reporter]))
        assert "- Insider buy: Jane Buyer (officer: CFO) bought 5,000 sh @ 42.50 (~212.5K) on 2026-06-30" in out

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


def _macro_ticker(
    symbol,
    label,
    *,
    value=None,
    baseline_value=None,
    spec=None,
    quarantined=None,
    events=(),
    source=Source.YAHOO,
    observed_at=None,
):
    spec = spec or MacroSpec(label=label, source=source)
    field = spec.value_field
    values = {}
    if value is not None:
        values[field] = FieldValue(
            field=field, value=value, source=source, fetched_at=NOW, observed_at=observed_at
        )
    baseline = None
    if baseline_value is not None:
        baseline = Snapshot(
            ticker=symbol,
            run_id=6,
            as_of=BASELINE_AT,
            values={
                field: FieldValue(
                    field=field, value=baseline_value, source=source, fetched_at=BASELINE_AT
                )
            },
        )
    return TickerReport(
        context=TickerContext(ticker=symbol, macro=spec),
        status="ok",
        snapshot=Snapshot(
            ticker=symbol, run_id=7, as_of=NOW, values=values, quarantined=quarantined or {}
        ),
        baseline=baseline,
        events=tuple(events),
        sources=(SourceHealth(source=source, status="ok"),),
        baseline_run_id=6 if baseline is not None else None,
        baseline_as_of=BASELINE_AT if baseline is not None else None,
    )


class TestMacroSection:
    def test_level_delta_provenance_and_label_sort(self):
        vix = _macro_ticker("^VIX", "VIX", value=25.4, baseline_value=15.0)
        tnx = _macro_ticker(
            "^TNX",
            "US 10Y yield",
            value=4.55,
            baseline_value=4.4,
            spec=MacroSpec(label="US 10Y yield", unit="%"),
        )
        out = render(_report([_quiet_ticker(), vix, tnx]))
        assert "## Macro" in out
        assert "- US 10Y yield: 4.55% (Δ +0.15 vs 2026-06-28) (yahoo, 2026-07-12 14:03Z)" in out
        assert "- VIX: 25.40 (Δ +10.40 vs 2026-06-28) (yahoo, 2026-07-12 14:03Z)" in out
        # Label sort: "US 10Y yield" before "VIX" despite ^TNX > ^VIX in ASCII.
        assert out.index("US 10Y yield:") < out.index("VIX: 25.40")
        # The macro series never appears in the Watchlist section.
        watchlist = out[out.index("## Watchlist"):]
        assert "^VIX" not in watchlist and "^TNX" not in watchlist

    def test_delta_suppressed_at_zero(self):
        vix = _macro_ticker("^VIX", "VIX", value=15.0, baseline_value=15.0)
        out = render(_report([vix]))
        assert "- VIX: 15.00 (yahoo, 2026-07-12 14:03Z)" in out
        assert "Δ" not in out.split("## Macro")[1].split("##")[0]

    def test_sanity_violation_flags_check_units(self):
        """The ×10 regime-change guard: 45.45 'yield' renders as implausible,
        never as a plain level."""
        tnx = _macro_ticker(
            "^TNX",
            "US 10Y yield",
            value=45.45,
            spec=MacroSpec(label="US 10Y yield", unit="%", sanity=(0.0, 25.0)),
        )
        out = render(_report([tnx]))
        assert "45.45%" in out
        assert "⚠ implausible (outside sanity [0, 25]) — check units" in out

    def test_spread_renders_iff_both_legs_accepted(self):
        tnx = _macro_ticker(
            "^TNX", "US 10Y yield", value=4.55, spec=MacroSpec(label="US 10Y yield", unit="%")
        )
        irx = _macro_ticker(
            "^IRX", "US 3M yield", value=3.69, spec=MacroSpec(label="US 3M yield", unit="%")
        )
        both = render(_report([tnx, irx]))
        assert "- 10Y − 3M spread: +0.86pp" in both
        alone = render(_report([tnx]))
        assert "spread" not in alone

    def test_econ_series_renders_period_and_prior_print_window(self):
        cpi = _macro_ticker(
            "CPIAUCSL",
            "CPI inflation (YoY)",
            value=2.9,
            baseline_value=3.2,
            source=Source.FRED,
            observed_at=datetime(2026, 6, 1, tzinfo=UTC),
            spec=MacroSpec(
                label="CPI inflation (YoY)", unit="%", decimals=1, source=Source.FRED,
                transform="yoy_pct", alert_on_release=True,
            ),
        )
        out = render(_report([cpi]))
        assert (
            "- CPI inflation (YoY): 2.9% (Δ -0.3 vs prior print) (fred, period 2026-06-01)"
            in out
        )

    def test_crossed_line_marks_the_standing_level(self):
        line = parse_thesis_check("price >= 25")
        vix = _macro_ticker(
            "^VIX", "VIX", value=25.4,
            spec=MacroSpec(label="VIX", alert_when=(line.model_copy(update={"raw": "value >= 25"}),)),
        )
        out = render(_report([vix]))
        assert "- VIX: 25.40 (yahoo, 2026-07-12 14:03Z) — ⚠ line crossed: value >= 25" in out

    def test_quarantined_macro_series_renders_the_verdict(self):
        stale = QuarantineHit(code=QuarantineCode.STALE, detail="quote 6 days old")
        vix = _macro_ticker("^VIX", "VIX", quarantined={Field.PRICE: (stale,)})
        out = render(_report([vix]))
        assert "- VIX: ⚠ DATA QUARANTINED — quote 6 days old" in out

    def test_no_macro_tickers_no_macro_section(self):
        out = render(_report([_quiet_ticker()]))
        assert "## Macro" not in out

    def test_macro_events_render_in_changes(self):
        events = (
            MacroLineCrossed(
                ticker="^VIX", label="VIX", check="value >= 25", observed=25.4,
                unit="", decimals=2, newly=True,
            ),
            MacroShift(
                ticker="^VIX", label="VIX", old=15.0, new=25.4, delta=10.4,
                unit="", decimals=2, threshold=3.0, old_as_of=BASELINE_AT,
            ),
            MacroPrint(
                ticker="^VIX", label="VIX", period=date(2026, 7, 1), value=25.4,
                prev_value=15.0, delta=10.4, unit="", decimals=2,
            ),
        )
        vix = _macro_ticker("^VIX", "VIX", value=25.4, baseline_value=15.0, events=events)
        out = render(_report([vix]))
        assert '- ⚠ LINE CROSSED — "value >= 25": VIX is at 25.40 (newly crossed)' in out
        assert "- VIX 15.00 → 25.40 (+10.40, alert ≥ 3) vs 2026-06-28" in out
        assert "- New print — VIX: 25.40 (period 2026-07-01), prior 15.00 (+10.40)" in out


class TestBellwetherSection:
    def _report_with(self, *bellwethers):
        from argus.models import BellwetherEarning  # local: only this class uses it

        return RunReport(
            run_id=7, kind="watch", as_of=NOW, status="complete",
            tickers=(_quiet_ticker(),), bellwethers=tuple(bellwethers),
        )

    def test_reported_and_upcoming_split_with_computed_surprise(self):
        from argus.models import BellwetherEarning

        out = render(
            self._report_with(
                BellwetherEarning(
                    symbol="MSFT", report_date=date(2026, 7, 10), hour="amc",
                    eps_estimate=3.05, eps_actual=3.11,
                ),
                BellwetherEarning(
                    symbol="NVDA", report_date=date(2026, 7, 15), hour="amc",
                    eps_estimate=1.05,
                ),
            )
        )
        assert "## Bellwether earnings (finnhub, unverified)" in out
        assert "- MSFT (2026-07-10): EPS 3.11 vs 3.05 est (+2.0%)" in out
        assert "- NVDA — 2026-07-15 amc (est 1.05)" in out
        assert "never a delivery trigger" in out

    def test_no_rows_no_section(self):
        out = render(self._report_with())
        assert "Bellwether" not in out


class TestEtfRebalanceSection:
    def test_added_and_dropped_render_when_changed(self):
        from argus.models import EtfRebalance

        report = RunReport(
            run_id=7, kind="watch", as_of=NOW, status="complete",
            tickers=(_quiet_ticker(),),
            etf_rebalances=(
                EtfRebalance(etf="SPY", added=("NEWCO",), dropped=("OLDCO", "GONE")),
                EtfRebalance(etf="XLK", added=("CHIPCO",)),
            ),
        )
        out = render(report)
        assert "## ETF rebalancing (ssga, unverified)" in out
        assert "- SPY added: NEWCO" in out
        assert "- SPY dropped: OLDCO, GONE" in out
        assert "- XLK added: CHIPCO" in out

    def test_no_rebalance_no_section(self):
        out = render(_report([_quiet_ticker()]))
        assert "ETF rebalancing" not in out


class TestRadarSection:
    def _radar(self):
        from argus.models import ScoutProposal

        return (
            ScoutProposal(ticker="NVDA", rank=3, status="proposed", sector="Technology",
                          screen_reasons={}, screener_metrics={}, streak=6),
            ScoutProposal(ticker="FSLR", rank=8, status="proposed", sector="Technology",
                          screen_reasons={}, screener_metrics={}, streak=6),
        )

    def test_strip_crossings_and_considering(self):
        from argus.models import Extreme, MarketWire, Mover

        wire = MarketWire(
            universe=100,
            losers=(Mover(symbol="NVDA", sector="Technology", close=203.0, change_pct=-6.1),),
            highs=(Extreme(symbol="FSLR", close=223.8, kind="high"),),
        )
        considering = _quiet_ticker(
            name="ONON",
            context=TickerContext(ticker="ONON", tier="consider"),
            snapshot=Snapshot(
                ticker="ONON", run_id=7, as_of=NOW,
                values={Field.PRICE: _fv(Field.PRICE, 37.46),
                        Field.PE_FWD: _fv(Field.PE_FWD, 17.5)},
            ),
        )
        report = RunReport(
            run_id=7, kind="watch", as_of=NOW, status="complete",
            tickers=(considering,), market=wire, radar=self._radar(),
        )
        out = render(report)
        assert "- #3 NVDA — Technology, streak 6w" in out
        assert "- ⚡ NVDA (shortlist, 6w) was a top-5 mover (-6.1%)" in out
        assert "- ⚡ FSLR (shortlist, 6w) hit a 52-week high" in out
        assert "- ONON: 37.46 · fwd P/E 17.5" in out
        assert "_Considering — promote with a thesis to graduate._" in out

    def test_no_radar_no_section(self):
        out = render(_report([_quiet_ticker()]))
        assert "## Radar" not in out


class TestMarketWireSections:
    def _wire_report(self):
        from argus.models import EarningsWireEntry, Extreme, MarketWire, Mover, SectorPulse

        wire = MarketWire(
            universe=1781,
            gainers=(Mover(symbol="ABT", company="Abbott Laboratories", sector="Healthcare",
                           close=99.15, change_pct=11.07),),
            losers=(Mover(symbol="MU", company="Micron", sector="Technology",
                          close=110.0, change_pct=-5.2),),
            sectors=(SectorPulse(sector="Healthcare", median_change_pct=1.2, n=140),
                     SectorPulse(sector="Technology", median_change_pct=-2.1, n=312)),
            highs=(Extreme(symbol="AAPL", company="Apple", close=327.5, kind="high"),),
            lows=(Extreme(symbol="NKE", company="Nike", close=48.2, kind="low"),),
            earnings_reported=(EarningsWireEntry(symbol="JPM", report_date=date(2026, 7, 14),
                                                 eps_estimate=5.91, eps_actual=6.14),),
            earnings_upcoming=(EarningsWireEntry(symbol="GOOGL", report_date=date(2026, 7, 22),
                                                 hour="amc", eps_estimate=2.97),),
            earnings_more_upcoming=3,
        )
        return RunReport(
            run_id=7, kind="watch", as_of=NOW, status="complete",
            tickers=(_quiet_ticker(),), market=wire,
        )

    def test_all_four_sections_render_with_disclosed_curation(self):
        out = render(self._wire_report())
        assert "- ABT +11.1% → 99.15 — Abbott Laboratories (Healthcare)" in out
        assert "- MU -5.2% → 110.00 — Micron (Technology)" in out
        assert "_Top 5 each way, last session, caps ≥ $10B (1781 names scanned)._" in out
        assert "- Technology: -2.1% median (312 names)" in out
        assert "- JPM (2026-07-14): EPS 6.14 vs 5.91 est (+3.9%)" in out
        assert "- GOOGL — 2026-07-22 amc (est 2.97)" in out
        assert "- … and 3 more large caps this week." in out
        assert "- AAPL 327.50 — Apple" in out
        assert "- NKE 48.20 — Nike" in out

    def test_quiet_pulse_has_no_wire_sections(self):
        out = render(_report([_quiet_ticker()]))
        assert "Market movers" not in out
        assert "Sector pulse" not in out


class TestFeaturedSection:
    def test_cards_render_with_disclosed_selection(self):
        from argus.models import FeatureCard, MarketWire

        wire = MarketWire(
            universe=100,
            features=(
                FeatureCard(
                    symbol="ABT", why="Yesterday's biggest large-cap gainer: +11.3% to 98.79",
                    name="Abbott Laboratories", sector="Healthcare", industry="Medical Devices",
                    employees=114000, market_cap=1.7e11, fwd_pe=22.4,
                    summary="Abbott Laboratories discovers, develops, and sells health care products.",
                ),
            ),
        )
        report = RunReport(
            run_id=7, kind="watch", as_of=NOW, status="complete",
            tickers=(_quiet_ticker(),), market=wire,
        )
        out = render(report)
        assert "### ABT — Abbott Laboratories" in out
        assert "_Yesterday's biggest large-cap gainer: +11.3% to 98.79._" in out
        assert "- Healthcare · Medical Devices · cap 170.0B · 114,000 employees" in out
        assert "- fwd P/E 22.4" in out
        assert "Abbott Laboratories discovers" in out
        assert "Selection is mechanical" in out


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
