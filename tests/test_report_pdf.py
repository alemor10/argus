"""build_pdf contract tests: bytes are a real PDF, one page per proposed
candidate (or per watch ticker) plus the summary, the 25-page cap, honest
degradation on missing/None history and malformed snapshot values, and byte
determinism (CreationDate is suppressed via PdfPages metadata, so identical
inputs must yield identical bytes). The business block, the why-it-surfaced
narrative (a fixed factual template — asserted by exact match, so no wording
can drift into opinion), the sector-grouped summary (grouping, leaders strip,
data-health rollups), the industry-peers line, and the revenue panel with its
fiscal-year caption are covered here too; text that ends up compressed inside
PDF streams is asserted at the pure-helper seam instead. Fixtures are built
directly from models.py types, following tests/test_render.py — no DB, no
network."""

from datetime import UTC, date, datetime, timedelta

import matplotlib.pyplot as plt

from argus.fields import Field, QuarantineCode, Source
from argus.models import (
    CompanyProfile,
    EarningsImminent,
    FieldValue,
    PriceMove,
    QuarantineHit,
    RunReport,
    ScoutProposal,
    Snapshot,
    SourceHealth,
    TickerContext,
    TickerReport,
)
from argus.report_pdf import (
    _FISCAL_YEAR_CAPTION,
    _fmt_value,
    _health_lines,
    _identity_line,
    _leader_line,
    _peer_line,
    _revenue_chart,
    _sector_groups,
    _why_surfaced,
    build_pdf,
)

NOW = datetime(2026, 7, 12, 14, 3, tzinfo=UTC)
BASELINE_AT = datetime(2026, 6, 28, 13, 0, tzinfo=UTC)

TARGET_HIT = QuarantineHit(
    code=QuarantineCode.TARGET_PRICE_RATIO,
    detail="target 35.00 (yahoo) / price 10.97 (yahoo) = 3.19 outside [0.3, 3.0]",
)


def _page_count(pdf: bytes) -> int:
    """Count page objects straight off the bytes: object dictionaries are
    uncompressed in matplotlib's PDF output. '/Type /Page' also prefixes the
    one '/Type /Pages' tree node, hence the subtraction."""
    return pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages")


def _fv(field, value, source=Source.YAHOO, corroborated_by=()):
    return FieldValue(
        field=field, value=value, source=source, fetched_at=NOW, corroborated_by=corroborated_by
    )


def _snapshot(ticker, values, quarantined=None, run_id=12):
    return Snapshot(
        ticker=ticker, run_id=run_id, as_of=NOW, values=values, quarantined=quarantined or {}
    )


def _ticker_report(ticker, values, *, quarantined=None, **overrides):
    kwargs = dict(
        context=TickerContext(ticker=ticker),
        status="ok",
        snapshot=_snapshot(ticker, values, quarantined),
        sources=(
            SourceHealth(source=Source.YAHOO, status="ok"),
            SourceHealth(source=Source.FINNHUB, status="ok"),
        ),
    )
    kwargs.update(overrides)
    return TickerReport(**kwargs)


def _garp_values(price=181.25):
    """A clean Quality-GARP candidate's verified snapshot values."""
    return {
        Field.PRICE: _fv(Field.PRICE, price, corroborated_by=(Source.FINNHUB,)),
        Field.MARKET_CAP: _fv(Field.MARKET_CAP, 52_300_000_000.0),
        Field.PE_FWD: _fv(Field.PE_FWD, 18.4),
        Field.GROSS_MARGIN: _fv(Field.GROSS_MARGIN, 0.62),
        Field.OPERATING_MARGIN: _fv(Field.OPERATING_MARGIN, 0.31),
        Field.ROE: _fv(Field.ROE, 0.24),
        Field.DEBT_TO_EQUITY: _fv(Field.DEBT_TO_EQUITY, 0.42),
    }


def _proposal(
    ticker,
    rank,
    *,
    streak=1,
    status="proposed",
    reason=None,
    sector="Other",
    peer_context=None,
    metrics=None,
):
    return ScoutProposal(
        ticker=ticker,
        rank=rank,
        status=status,
        sector=sector,
        exclusion_reason=reason,
        screen_reasons={
            "fwd_pe": "fwd_pe 18.4 <= 25",
            "revenue_growth": "revenue_growth 12.5 >= 8",
            "roe": "roe 24 >= 15",
            "debt_to_equity": "debt_to_equity 0.42 <= 1.5",
        },
        screener_metrics=(
            metrics
            if metrics is not None
            else {"fwd_pe": 18.4, "revenue_growth_pct": 12.5, "roe_pct": 24.0}
        ),
        peer_context=peer_context,
        streak=streak,
    )


_PEER_CONTEXT = {
    "industry": "Semiconductors",
    "n": 12,
    "median_fwd_pe": 28.4,
    "peers": [
        {"ticker": "AVGO", "fwd_pe": 32.1},
        {"ticker": "AMD", "fwd_pe": 29.9},
        {"ticker": "INTC", "fwd_pe": None},
    ],
}


def _scout_report(proposals, tickers, *, status="complete", notes=None):
    return RunReport(
        run_id=12,
        kind="scout",
        as_of=NOW,
        status=status,
        notes=notes,
        tickers=tuple(tickers),
        scout=tuple(proposals),
    )


def _two_proposal_report() -> RunReport:
    tickers = [
        _ticker_report("AAA", _garp_values(181.25)),
        _ticker_report("BBB", _garp_values(64.10)),
    ]
    proposals = [
        _proposal("AAA", 1, streak=3),
        _proposal("BBB", 2),
        _proposal("CCC", 3, status="excluded", reason="fwd P/E quarantined: cross-source disagreement"),
    ]
    return _scout_report(proposals, tickers)


def _rich_values(price=181.25):
    """A snapshot exercising every v1.3 panel field, six new ones included."""
    values = _garp_values(price)
    values.update(
        {
            Field.REVENUE_GROWTH: _fv(Field.REVENUE_GROWTH, 0.125),
            Field.FCF_MARGIN: _fv(Field.FCF_MARGIN, 0.284),
            Field.TOTAL_CASH: _fv(Field.TOTAL_CASH, 96_100_000_000.0),
            Field.TOTAL_DEBT: _fv(Field.TOTAL_DEBT, 12_400_000_000.0),
            Field.EV_EBITDA: _fv(Field.EV_EBITDA, 14.2),
            Field.DIVIDEND_YIELD: _fv(Field.DIVIDEND_YIELD, 0.012),
            Field.BETA: _fv(Field.BETA, 1.13),
        }
    )
    return values


def _multi_sector_report(*, notes=None) -> RunReport:
    """Three sectors on the shortlist, one leader beyond it, one exclusion,
    one failed enrichment ticker, one finnhub error — the full v1.3 summary
    page in one fixture."""
    tickers = [
        _ticker_report("AAA", _rich_values(181.25), profile=_profile("AAA")),
        _ticker_report(
            "BBB",
            _garp_values(64.10),
            profile=_profile(
                "BBB", name="Beta Bioworks plc", sector="Healthcare",
                industry="Biotechnology", employees=410,
            ),
            sources=(
                SourceHealth(source=Source.YAHOO, status="ok"),
                SourceHealth(source=Source.FINNHUB, status="error", error="HTTP 502"),
            ),
        ),
        _ticker_report("CCC", _garp_values(23.05)),
        TickerReport(
            context=TickerContext(ticker="DDD"),
            status="failed",
            error="HTTP 429 from yahoo",
        ),
    ]
    proposals = [
        _proposal("AAA", 1, streak=3, sector="Technology", peer_context=_PEER_CONTEXT),
        _proposal("BBB", 2, sector="Healthcare"),
        _proposal("CCC", 4, sector="Technology"),
        _proposal("EEE", 3, status="excluded", reason="price quarantined: non_finite"),
        _proposal(
            "FFF", 5, status="leader", sector="Energy", metrics={"fwd_pe": 9.8}
        ),
    ]
    return _scout_report(proposals, tickers, notes=notes)


def _synthetic_history(n=52, base=100.0):
    start = date(2025, 7, 14)
    return [
        (start + timedelta(weeks=i), base + 3.0 * ((i % 9) - 4) + 0.5 * i) for i in range(n)
    ]


def _synthetic_revenue():
    return [
        (2022, 21_300_000_000.0),
        (2023, 26_970_000_000.0),
        (2024, 39_100_000_000.0),
        (2025, 52_300_000_000.0),
    ]


_LONG_SUMMARY = (
    "Axon Analytics, Inc. provides an observability and data-quality platform for "
    "streaming financial market data. The company offers ingestion adapters, "
    "cross-source reconciliation, anomaly quarantine, and provenance tracking, and "
    "serves brokerages, asset managers, and independent research desks. "
) * 4  # long enough that the business block must truncate it with '…'


def _profile(ticker="AAA", **overrides):
    kwargs = dict(
        ticker=ticker,
        name="Axon Analytics, Inc.",
        sector="Technology",
        industry="Software — Infrastructure",
        employees=36_000,
        summary=_LONG_SUMMARY,
        source=Source.YAHOO,
        fetched_at=NOW,
    )
    kwargs.update(overrides)
    return CompanyProfile(**kwargs)


def _watch_report() -> RunReport:
    quiet = _ticker_report(
        "NVDA",
        _garp_values(181.25),
        context=TickerContext(ticker="NVDA", thesis="Datacenter capex supercycle; CUDA moat."),
        events=(
            PriceMove(
                ticker="NVDA", old=170.0, new=181.25, pct=6.6, threshold=5.0, old_as_of=BASELINE_AT
            ),
            EarningsImminent(ticker="NVDA", earnings_date=date(2026, 7, 17), days_until=5),
        ),
    )
    ntdoy = _ticker_report(
        "NTDOY",
        {Field.PRICE: _fv(Field.PRICE, 10.97, corroborated_by=(Source.FINNHUB,))},
        quarantined={Field.ANALYST_TARGET_MEAN: (TARGET_HIT,)},
        context=TickerContext(ticker="NTDOY", thesis="Switch 2 cycle + IP monetization."),
        status="partial",
    )
    dead = TickerReport(
        context=TickerContext(ticker="TCEHY", thesis="WeChat moat."),
        status="failed",
        error="HTTP 502 from yahoo",
    )
    return RunReport(
        run_id=9,
        kind="watch",
        as_of=NOW,
        status="partial",
        tickers=(quiet, ntdoy, dead),
    )


class TestScoutPdf:
    def test_bytes_are_a_pdf_and_substantial(self):
        pdf = build_pdf(
            _two_proposal_report(),
            {"AAA": _synthetic_history(), "BBB": _synthetic_history(base=50.0)},
        )
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 2048

    def test_one_summary_page_plus_one_per_proposed_candidate(self):
        # Two proposed + one excluded → 3 pages: the excluded name is listed
        # on the summary page but earns no detail page.
        pdf = build_pdf(
            _two_proposal_report(),
            {"AAA": _synthetic_history(), "BBB": _synthetic_history(base=50.0)},
        )
        assert _page_count(pdf) == 3

    def test_none_history_renders_unavailable_note_page(self):
        # None means "could not be fetched" — the detail page still renders,
        # with a visible note in the chart's place.
        pdf = build_pdf(_two_proposal_report(), {"AAA": None, "BBB": None})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 3

    def test_empty_history_mapping_and_empty_point_lists(self):
        for history in ({}, {"AAA": [], "BBB": []}):
            pdf = build_pdf(_two_proposal_report(), history)
            assert pdf.startswith(b"%PDF")
            assert _page_count(pdf) == 3

    def test_zero_proposals_still_yields_a_valid_one_page_pdf(self):
        pdf = build_pdf(_scout_report([], []), {})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 1

    def test_failed_run_with_notes_is_one_honest_page(self):
        report = _scout_report(
            [], [], status="failed", notes="screener unavailable: HTTP 503 from tradingview"
        )
        pdf = build_pdf(report, {})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 1

    def test_detail_pages_capped_at_25(self):
        proposals = [_proposal(f"T{i:02d}", i, streak=2) for i in range(1, 31)]
        pdf = build_pdf(_scout_report(proposals, []), {})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 26  # summary + 25, not 31

    def test_proposal_without_enrichment_snapshot_renders_dashes_not_crash(self):
        # report.tickers carries no entry for the proposed name: every metric
        # is '—' and the page still renders.
        pdf = build_pdf(_scout_report([_proposal("GHOST", 1)], []), {})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 2


class TestWatchPdf:
    def test_watch_kind_renders_one_page_per_ticker(self):
        pdf = build_pdf(_watch_report(), {"NVDA": _synthetic_history(base=150.0)})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 4  # summary + NVDA + NTDOY + TCEHY (failed still gets a page)

    def test_failed_ticker_and_quarantined_field_never_crash(self):
        # NTDOY's quarantined target and TCEHY's None snapshot are the
        # malformed/absent paths — both must degrade to disclosure, not raise.
        pdf = build_pdf(_watch_report(), {})
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 2048


class TestMalformedValues:
    def test_non_finite_numbers_render_as_dash_not_crash(self):
        values = {
            Field.PRICE: _fv(Field.PRICE, float("nan")),
            Field.MARKET_CAP: _fv(Field.MARKET_CAP, float("inf")),
            Field.ROE: _fv(Field.ROE, 0.18),
        }
        report = _scout_report([_proposal("AAA", 1)], [_ticker_report("AAA", values)])
        pdf = build_pdf(report, {"AAA": _synthetic_history()})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 2

    def test_malformed_history_points_are_skipped_not_fatal(self):
        # Contract says (date, float) tuples, but display data is untrusted:
        # non-finite closes must not poison the chart.
        history = {
            "AAA": [
                (date(2026, 1, 5), 100.0),
                (date(2026, 1, 12), float("nan")),
                (date(2026, 1, 19), float("inf")),
                (date(2026, 1, 26), 104.5),
            ]
        }
        report = _scout_report([_proposal("AAA", 1)], [_ticker_report("AAA", _garp_values())])
        pdf = build_pdf(report, history)
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 2


class TestBusinessBlock:
    def _report_with_profiles(self):
        tickers = [
            _ticker_report("AAA", _garp_values(181.25), profile=_profile("AAA")),
            _ticker_report(
                "BBB",
                _garp_values(64.10),
                profile=_profile(
                    "BBB", name="Beta Bioworks plc", sector="Healthcare",
                    industry="Biotechnology", employees=410,
                ),
            ),
        ]
        return _scout_report([_proposal("AAA", 1, streak=3), _proposal("BBB", 2)], tickers)

    def test_profile_pages_render_with_business_column_on_summary(self):
        pdf = build_pdf(
            self._report_with_profiles(),
            {"AAA": _synthetic_history(), "BBB": _synthetic_history(base=50.0)},
            {"AAA": _synthetic_revenue(), "BBB": _synthetic_revenue()},
        )
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 3

    def test_profile_none_renders_unavailable_line_not_crash(self):
        # _two_proposal_report carries no profiles at all — every detail page
        # takes the 'business profile unavailable' path.
        pdf = build_pdf(_two_proposal_report(), {})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 3

    def test_empty_shell_profile_is_treated_as_unavailable(self):
        shell = _profile(
            "AAA", name=None, sector=None, industry=None, employees=None, summary=None
        )
        tickers = [_ticker_report("AAA", _garp_values(), profile=shell)]
        pdf = build_pdf(_scout_report([_proposal("AAA", 1)], tickers), {})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 2

    def test_watch_pages_render_profiles_too(self):
        report = _watch_report()
        with_profile = report.tickers[0].model_copy(update={"profile": _profile("NVDA")})
        report = report.model_copy(update={"tickers": (with_profile,) + report.tickers[1:]})
        pdf = build_pdf(report, {"NVDA": _synthetic_history(base=150.0)}, {"NVDA": _synthetic_revenue()})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 4

    def test_identity_line_humanizes_employees(self):
        assert _identity_line(_profile()) == (
            "Technology · Software — Infrastructure · ~36,000 employees"
        )

    def test_identity_line_skips_missing_parts(self):
        assert _identity_line(_profile(employees=None)) == (
            "Technology · Software — Infrastructure"
        )
        assert _identity_line(
            _profile(sector=None, industry=None)
        ) == "~36,000 employees"
        assert _identity_line(
            _profile(sector=None, industry=None, employees=None)
        ) is None


class TestWhySurfaced:
    """The narrative is a fixed template over report data — asserted by exact
    match, so any drift toward adjectives/outlook/recommendation fails here."""

    def _quality_garp_proposal(self):
        return ScoutProposal(
            ticker="QGRP",
            rank=4,
            status="proposed",
            exclusion_reason=None,
            screen_reasons={
                "forward_pe": "fwd P/E 15.9 ≤ 25",
                "revenue_growth": "rev growth +70.7% ≥ 8%",
                "gross_margin": "gross margin 74.1% ≥ 40%",
                "operating_margin": "op margin 31.2% ≥ 12%",
                "roe": "ROE 114.3% ≥ 15%",
                "debt_to_equity": "D/E 0.42 ≤ 1.5",
                "value_trap": "EPS trend +12.0% > -20%",
            },
            screener_metrics={"fwd_pe": 15.9},
            streak=2,
        )

    def test_paragraph_is_the_exact_template_output(self):
        snapshot = _snapshot(
            "QGRP",
            {
                Field.PE_FWD: _fv(Field.PE_FWD, 15.9),
                Field.GROSS_MARGIN: _fv(Field.GROSS_MARGIN, 0.741),
                Field.ROE: _fv(Field.ROE, 1.143),
            },
        )
        assert _why_surfaced(self._quality_garp_proposal(), snapshot) == (
            "Surfaced at #4 (2nd consecutive week): "
            "trades at 15.9× forward earnings (verified), "
            "with the screener reporting rev growth +70.7% ≥ 8%, "
            "on verified gross margins of 74.1% and ROE of 114.3%. "
            "Passed all 7 screen rules. "
            'Argus proposes; the thesis is yours — argus promote QGRP --thesis "..."'
        )

    def test_verified_revenue_growth_preferred_over_screener_claim(self):
        snapshot = _snapshot(
            "QGRP", {Field.REVENUE_GROWTH: _fv(Field.REVENUE_GROWTH, 0.707)}
        )
        text = _why_surfaced(self._quality_garp_proposal(), snapshot)
        assert "with verified revenue growth of +70.7%" in text
        assert "screener reporting" not in text

    def test_no_data_degrades_to_rank_and_streak_only(self):
        bare = ScoutProposal(
            ticker="GHOST", rank=1, status="proposed", exclusion_reason=None,
            screen_reasons={}, screener_metrics={}, streak=1,
        )
        assert _why_surfaced(bare, None) == (
            "Surfaced at #1 (new this week). "
            'Argus proposes; the thesis is yours — argus promote GHOST --thesis "..."'
        )

    def test_paragraph_always_ends_with_the_fixed_promote_line(self):
        text = _why_surfaced(_proposal("AAA", 1), None)
        assert text.endswith('argus promote AAA --thesis "..."')


class TestRevenuePanel:
    def test_three_arg_call_with_series_renders(self):
        pdf = build_pdf(
            _two_proposal_report(),
            {"AAA": _synthetic_history(), "BBB": _synthetic_history(base=50.0)},
            {"AAA": _synthetic_revenue(), "BBB": _synthetic_revenue()},
        )
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 3

    def test_none_or_missing_series_renders_unavailable_note(self):
        # None mapping (legacy two-arg behavior), empty mapping, and explicit
        # per-ticker None/empty must all degrade to the visible note.
        for series in (None, {}, {"AAA": None, "BBB": []}):
            pdf = build_pdf(_two_proposal_report(), {}, series)
            assert pdf.startswith(b"%PDF")
            assert _page_count(pdf) == 3

    def test_malformed_revenue_points_are_skipped_not_fatal(self):
        series = {
            "AAA": [
                (2023, float("nan")),
                ("2024x", 1.0),
                (True, 5.0),
                (2024, 1_000_000_000.0),
                (2025,),
            ],
            "BBB": [("junk",), (None, None)],
        }
        pdf = build_pdf(_two_proposal_report(), {}, series)
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 3


class TestRevenueFormatting:
    def test_revenue_humanized_like_market_cap(self):
        assert _fmt_value(Field.REVENUE, 52_300_000_000.0) == "52.3B"
        assert _fmt_value(Field.REVENUE, 1_230_000_000_000.0) == "1.23T"

    def test_revenue_growth_fraction_rendered_as_percent(self):
        assert _fmt_value(Field.REVENUE_GROWTH, 0.707) == "70.7%"
        assert _fmt_value(Field.REVENUE_GROWTH, -0.033) == "-3.3%"

    def test_malformed_revenue_values_render_dash(self):
        assert _fmt_value(Field.REVENUE, float("nan")) == "—"
        assert _fmt_value(Field.REVENUE_GROWTH, float("inf")) == "—"


class TestNewFieldFormatting:
    """The six v1.3 fields follow the digest's conventions exactly: fraction
    fields read as percents, dollar magnitudes humanize, ratios print 2dp."""

    def test_fraction_fields_render_as_percents(self):
        assert _fmt_value(Field.DIVIDEND_YIELD, 0.012) == "1.2%"
        assert _fmt_value(Field.FCF_MARGIN, 0.284) == "28.4%"

    def test_dollar_fields_humanize_like_market_cap(self):
        assert _fmt_value(Field.TOTAL_CASH, 96_100_000_000.0) == "96.1B"
        assert _fmt_value(Field.TOTAL_DEBT, 12_400_000_000.0) == "12.4B"

    def test_ratio_fields_render_two_decimals(self):
        assert _fmt_value(Field.EV_EBITDA, 14.2) == "14.20"
        assert _fmt_value(Field.BETA, 1.13) == "1.13"

    def test_detail_page_renders_all_21_panel_fields(self):
        report = _scout_report(
            [_proposal("AAA", 1)], [_ticker_report("AAA", _rich_values())]
        )
        pdf = build_pdf(report, {"AAA": _synthetic_history()}, {"AAA": _synthetic_revenue()})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 2


class TestGroupedSummary:
    def test_sector_groups_order_by_best_rank_then_name(self):
        proposed = [
            _proposal("AAA", 1, sector="Technology"),
            _proposal("BBB", 2, sector="Healthcare"),
            _proposal("CCC", 4, sector="Technology"),
            _proposal("DDD", 6, sector="Energy"),
        ]
        groups = [(s, [p.ticker for p in g]) for s, g in _sector_groups(proposed)]
        assert groups == [
            ("Technology", ["AAA", "CCC"]),
            ("Healthcare", ["BBB"]),
            ("Energy", ["DDD"]),
        ]

    def test_missing_sector_falls_back_to_other(self):
        [(sector, group)] = _sector_groups([_proposal("AAA", 1, sector="")])
        assert sector == "Other"
        assert [p.ticker for p in group] == ["AAA"]

    def test_multi_sector_report_renders_grouped_summary(self):
        pdf = build_pdf(
            _multi_sector_report(),
            {"AAA": _synthetic_history(), "BBB": _synthetic_history(base=50.0)},
            {"AAA": _synthetic_revenue()},
        )
        assert pdf.startswith(b"%PDF")
        # 3 proposed detail pages + summary; the leader and the exclusion
        # appear on the summary only.
        assert _page_count(pdf) == 4


class TestLeadersStrip:
    def test_leader_line_is_the_exact_template(self):
        leader = _proposal(
            "FFF", 5, status="leader", sector="Energy", metrics={"fwd_pe": 9.8}
        )
        assert _leader_line(leader) == "Energy — FFF (claimed fwd P/E 9.8, #5 overall)"

    def test_leader_line_without_claimed_fwd_pe_degrades_to_rank(self):
        leader = _proposal("FFF", 5, status="leader", sector="Energy", metrics={})
        assert _leader_line(leader) == "Energy — FFF (#5 overall)"
        junk = _proposal(
            "GGG", 7, status="leader", sector="Utilities",
            metrics={"fwd_pe": float("nan")},
        )
        assert _leader_line(junk) == "Utilities — GGG (#7 overall)"

    def test_leaders_get_no_detail_pages(self):
        # Two proposed + one leader + one excluded → 3 pages: the leader is a
        # summary-strip line only (never enriched, nothing verified to show).
        proposals = [
            _proposal("AAA", 1, sector="Technology"),
            _proposal("BBB", 2, sector="Healthcare"),
            _proposal("CCC", 3, status="excluded", reason="price quarantined: non_finite"),
            _proposal("FFF", 4, status="leader", sector="Energy", metrics={"fwd_pe": 9.8}),
        ]
        tickers = [
            _ticker_report("AAA", _garp_values()),
            _ticker_report("BBB", _garp_values(64.10)),
        ]
        pdf = build_pdf(_scout_report(proposals, tickers), {})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 3


class TestDataHealthBlock:
    def test_rollups_first_error_skipped_checks_and_note(self):
        report = _multi_sector_report(notes="screener degraded: 2 retries")
        lines = [text for text, _tone in _health_lines(report)]
        assert lines == [
            "yahoo: 3 ok",
            "edgar: not configured — its cross-checks never ran",
            "finnhub: 2 ok, 1 error (first: HTTP 502) — price cross-checks skipped (1 ticker)",
            "Failed tickers:",
            "  DDD: HTTP 429 from yahoo",
            "Run note: screener degraded: 2 retries",
        ]

    def test_no_sources_recorded_is_stated_not_blank(self):
        report = _scout_report([], [], status="failed", notes="screener unavailable: HTTP 503")
        lines = [text for text, _tone in _health_lines(report)]
        assert lines == [
            "No source health recorded.",
            "Run note: screener unavailable: HTTP 503",
        ]

    def test_health_block_renders_on_summary_page(self):
        pdf = build_pdf(_multi_sector_report(notes="screener degraded: 2 retries"), {})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 4


class TestPeersLine:
    """The peers line is a fixed template over peer_context (screener claims)
    plus the one gate-verified number — exact-matched at the helper seam, the
    same discipline as the why-surfaced narrative."""

    def test_line_is_the_exact_template(self):
        proposal = _proposal("AAA", 1, peer_context=_PEER_CONTEXT)
        snapshot = _snapshot("AAA", {Field.PE_FWD: _fv(Field.PE_FWD, 18.4)})
        assert _peer_line(proposal, snapshot) == (
            "Semiconductors (n=12) — median fwd P/E 28.4 · "
            "AVGO 32.1, AMD 29.9, INTC — · AAA (verified) 18.4"
        )

    def test_unverified_own_fwd_pe_is_a_dash_not_a_claim(self):
        proposal = _proposal("AAA", 1, peer_context=_PEER_CONTEXT)
        assert _peer_line(proposal, None) == (
            "Semiconductors (n=12) — median fwd P/E 28.4 · "
            "AVGO 32.1, AMD 29.9, INTC — · AAA (verified) —"
        )

    def test_none_or_empty_context_is_omitted_silently(self):
        assert _peer_line(_proposal("AAA", 1), None) is None
        assert _peer_line(_proposal("AAA", 1, peer_context={}), None) is None

    def test_malformed_context_degrades_field_by_field(self):
        context = {
            "industry": None,
            "n": "twelve",
            "median_fwd_pe": float("inf"),
            "peers": [{"ticker": ""}, "junk", {"ticker": "AVGO", "fwd_pe": "n/a"}],
        }
        proposal = _proposal("AAA", 1, peer_context=context)
        assert _peer_line(proposal, None) == (
            "industry unknown — median fwd P/E — · AVGO — · AAA (verified) —"
        )

    def test_peers_line_renders_on_detail_page(self):
        report = _scout_report(
            [_proposal("AAA", 1, peer_context=_PEER_CONTEXT)],
            [_ticker_report("AAA", _rich_values())],
        )
        pdf = build_pdf(report, {"AAA": _synthetic_history()})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 2


class TestFiscalYearCaption:
    def test_rendered_revenue_chart_carries_the_caption(self):
        fig = plt.figure(figsize=(8.5, 11.0))
        try:
            _revenue_chart(fig, (0.665, 0.435, 0.275, 0.165), _synthetic_revenue())
            assert _FISCAL_YEAR_CAPTION == "fiscal years — TTM revenue in the panel may differ"
            assert fig.axes[0].get_xlabel() == _FISCAL_YEAR_CAPTION
        finally:
            plt.close(fig)

    def test_unavailable_panel_carries_no_caption(self):
        fig = plt.figure(figsize=(8.5, 11.0))
        try:
            _revenue_chart(fig, (0.665, 0.435, 0.275, 0.165), None)
            assert fig.axes == []  # the unavailable note is a figure patch, not a chart
        finally:
            plt.close(fig)


class TestDeterminism:
    def test_same_inputs_yield_identical_bytes(self):
        # CreationDate is the one nondeterministic thing matplotlib embeds;
        # build_pdf suppresses it via PdfPages metadata, so equality is exact.
        report = _two_proposal_report()
        history = {"AAA": _synthetic_history(), "BBB": None}
        assert build_pdf(report, history) == build_pdf(report, history)

    def test_same_inputs_with_profiles_and_revenue_yield_identical_bytes(self):
        tickers = [
            _ticker_report("AAA", _garp_values(), profile=_profile("AAA")),
            _ticker_report("BBB", _garp_values(64.10)),
        ]
        report = _scout_report([_proposal("AAA", 1, streak=3), _proposal("BBB", 2)], tickers)
        history = {"AAA": _synthetic_history(), "BBB": None}
        revenue = {"AAA": _synthetic_revenue(), "BBB": None}
        assert build_pdf(report, history, revenue) == build_pdf(report, history, revenue)

    def test_v13_summary_and_peers_features_yield_identical_bytes(self):
        # Sector bands, the leaders strip, the data-health block, the peers
        # line, and the fiscal-year caption must all be as deterministic as
        # everything that came before them.
        report = _multi_sector_report(notes="screener degraded: 2 retries")
        history = {"AAA": _synthetic_history(), "BBB": _synthetic_history(base=50.0), "CCC": None}
        revenue = {"AAA": _synthetic_revenue(), "BBB": None}
        assert build_pdf(report, history, revenue) == build_pdf(report, history, revenue)

    def test_no_creation_date_embedded(self):
        # The only timestamps in the file are report.as_of and the snapshots'
        # provenance stamps — never the wall clock.
        pdf = build_pdf(_two_proposal_report(), {})
        assert b"CreationDate" not in pdf

    def test_legacy_two_arg_call_still_works(self):
        # The revenue_series parameter is additive: pre-existing callers pass
        # (report, history) and every revenue panel reads 'unavailable'.
        pdf = build_pdf(_two_proposal_report(), {"AAA": _synthetic_history()})
        assert pdf.startswith(b"%PDF")
        assert _page_count(pdf) == 3
