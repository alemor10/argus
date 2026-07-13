"""build_pdf contract tests: bytes are a real PDF, one page per proposed
candidate (or per watch ticker) plus the summary, the 25-page cap, honest
degradation on missing/None history and malformed snapshot values, and byte
determinism (CreationDate is suppressed via PdfPages metadata, so identical
inputs must yield identical bytes). Fixtures are built directly from
models.py types, following tests/test_render.py — no DB, no network."""

from datetime import UTC, date, datetime, timedelta

from argus.fields import Field, QuarantineCode, Source
from argus.models import (
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
from argus.report_pdf import build_pdf

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


def _proposal(ticker, rank, *, streak=1, status="proposed", reason=None):
    return ScoutProposal(
        ticker=ticker,
        rank=rank,
        status=status,
        exclusion_reason=reason,
        screen_reasons={
            "fwd_pe": "fwd_pe 18.4 <= 25",
            "revenue_growth": "revenue_growth 12.5 >= 8",
            "roe": "roe 24 >= 15",
            "debt_to_equity": "debt_to_equity 0.42 <= 1.5",
        },
        screener_metrics={"fwd_pe": 18.4, "revenue_growth_pct": 12.5, "roe_pct": 24.0},
        streak=streak,
    )


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


def _synthetic_history(n=52, base=100.0):
    start = date(2025, 7, 14)
    return [
        (start + timedelta(weeks=i), base + 3.0 * ((i % 9) - 4) + 0.5 * i) for i in range(n)
    ]


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


class TestDeterminism:
    def test_same_inputs_yield_identical_bytes(self):
        # CreationDate is the one nondeterministic thing matplotlib embeds;
        # build_pdf suppresses it via PdfPages metadata, so equality is exact.
        report = _two_proposal_report()
        history = {"AAA": _synthetic_history(), "BBB": None}
        assert build_pdf(report, history) == build_pdf(report, history)

    def test_no_creation_date_embedded(self):
        # The only timestamps in the file are report.as_of and the snapshots'
        # provenance stamps — never the wall clock.
        pdf = build_pdf(_two_proposal_report(), {})
        assert b"CreationDate" not in pdf
