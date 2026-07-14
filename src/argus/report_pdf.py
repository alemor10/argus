"""PDF report rendering (pure): RunReport + price/revenue history → bytes.

The PDF is the digest's visual companion. Page 1 summarizes the run with the
same tri-state honesty as the markdown — a value the gates did not accept is
'—', never a blank and never a guess. For scout runs the proposals table is
grouped by canonical sector, followed by the sector-leaders strip (screener
claims, never enriched, no detail pages), the exclusions, and a Data health
block mirroring the markdown's — the summary is the whole run on one page.
Then one page per proposed scout candidate (or per watched ticker) opens
with what the business IS (the CompanyProfile, rendered verbatim), states —
for scout pages — why the screen surfaced the name (a purely factual
restatement of rank, streak, screener pass-reasons, and gate-verified
values; Argus never writes the thesis) plus the industry-peer context line
(labeled screener claims, with the candidate's own verified forward P/E
beside them), and pairs a trailing-year price chart plus an annual-revenue
bar chart with a panel of gate-verified metrics, each with its provenance
stamp.

The charts are the ONE place ungated data appears anywhere in Argus: price
history arrives from the caller as plain (date, close) tuples and revenue
history as (fiscal_year, dollars) tuples — labeled ungated display data,
never persisted. Every chart page therefore carries a footer saying exactly
that — the reader must always know which numbers earned trust and which are
decoration.

PURE by contract: no network, no clock. The only timestamps embedded are
report.as_of and the snapshots' own provenance stamps; the PDF CreationDate
is suppressed so identical inputs yield identical bytes (regenerability is a
tested property here, same as the markdown digest).
"""

import io
import math
import textwrap
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

import matplotlib

matplotlib.use("Agg")  # before pyplot: headless-safe, never a GUI backend

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from argus.fields import SPECS, Field, Source
from argus.models import (
    CompanyProfile,
    RunReport,
    ScoutProposal,
    Snapshot,
    ThesisCheckResult,
    TickerReport,
)
from argus.thesis import evaluate_thesis_checks

# ticker → chronological (date, close) points for ~1y, or None when history
# could not be fetched. Plain tuples — NOT pandas — supplied by the caller
# and labeled as UNGATED display data (see module docstring).
History = Mapping[str, Sequence[tuple[date, float]] | None]

# ticker → chronological (fiscal_year, revenue_dollars) points, ~4 annual
# values, or None when unavailable. UNGATED display data like `history`.
RevenueSeries = Mapping[str, Sequence[tuple[int, float]] | None]

_MAX_DETAIL_PAGES = 25  # a shortlist longer than this is a screening bug, not a report

_PAGE = (8.5, 11.0)  # letter portrait: charts wide enough, tables tall enough

# Light-surface ink & chart chrome (a PDF is a print-like medium — one look,
# deliberately). Series color is a validated categorical slot; text always
# wears ink, never the series color.
_INK = "#0b0b0b"
_SECONDARY = "#52514e"
_MUTED = "#898781"
_GRID = "#e1e0d9"
_BASELINE = "#c3c2b7"
_SERIES = "#2a78d6"
_CRITICAL = "#d03b3b"  # quarantine lines: status color + explicit label, never color alone
_PANEL = "#f9f9f7"

_UNGATED_FOOTER = (
    "Price & revenue charts: raw Yahoo data, ungated — the metrics panel is gate-verified."
)

# Under the revenue bars: they are fiscal-year totals, while the panel's
# Revenue (TTM) is a trailing window — the two legitimately differ, and the
# reader must not mistake that for a data-quality bug.
_FISCAL_YEAR_CAPTION = "fiscal years — TTM revenue in the panel may differ"

# The fixed hand-off line on every scout detail page: Argus surfaces facts,
# the human writes the thesis (hard constraint — no self-generated theses).
_PROMOTE_LINE = 'Argus proposes; the thesis is yours — argus promote {ticker} --thesis "..."'

# Human-facing field names — mirrors the markdown digest's labels exactly
# (the two artifacts must never name the same field differently). A Field
# without a label falls back to the enum value, never a KeyError in the
# report path.
_FIELD_LABELS: dict[Field, str] = {
    Field.PRICE: "Price",
    Field.MARKET_CAP: "Market cap",
    Field.REVENUE: "Revenue (TTM)",
    Field.REVENUE_GROWTH: "Revenue growth (MRQ YoY)",
    Field.PE_TTM: "P/E (TTM)",
    Field.PE_FWD: "Fwd P/E",
    Field.PEG: "PEG",
    Field.GROSS_MARGIN: "Gross margin",
    Field.OPERATING_MARGIN: "Operating margin",
    Field.FCF_MARGIN: "FCF margin",
    Field.ROE: "ROE",
    Field.DEBT_TO_EQUITY: "Debt/equity",
    Field.TOTAL_CASH: "Total cash",
    Field.TOTAL_DEBT: "Total debt",
    Field.EV_EBITDA: "EV/EBITDA",
    Field.DIVIDEND_YIELD: "Dividend yield",
    Field.BETA: "Beta",
    Field.NEXT_EARNINGS_DATE: "Next earnings",
    Field.ANALYST_RATING: "Analyst rating",
    Field.ANALYST_TARGET_MEAN: "Analyst target (mean)",
    Field.ANALYST_COUNT: "Analyst count",
}

# Margins, ROE, revenue growth, and dividend yield are stored as fractions,
# read as percents (same convention as the markdown digest).
_PERCENT_FIELDS = frozenset(
    {
        Field.GROSS_MARGIN,
        Field.OPERATING_MARGIN,
        Field.FCF_MARGIN,
        Field.ROE,
        Field.REVENUE_GROWTH,
        Field.DIVIDEND_YIELD,
    }
)

# Dollar magnitudes humanized like market cap (52.3B, never raw).
_HUMANIZED_FIELDS = frozenset(
    {Field.MARKET_CAP, Field.REVENUE, Field.TOTAL_CASH, Field.TOTAL_DEBT}
)


def build_pdf(
    report: RunReport, history: History, revenue_series: RevenueSeries | None = None
) -> bytes:
    """RunReport + price/revenue history → PDF bytes. Pure; deterministic.

    Page 1 is the run summary (proposals + exclusions for scout, the
    watchlist table + event counts for watch). Then one detail page per
    proposed candidate / watched ticker — business profile, why-it-surfaced
    narrative (scout), price + revenue charts, verified-metrics panel —
    capped at _MAX_DETAIL_PAGES with the cap disclosed on page 1.
    revenue_series is optional and additive: the two-argument call renders
    every revenue panel as 'unavailable'. Malformed or absent values render
    as '—' — the report path never crashes on bad data, it discloses it.
    """
    subjects = _detail_subjects(report)
    shown = subjects[:_MAX_DETAIL_PAGES]
    metadata = {
        # No wall-clock anywhere: the title carries report.as_of, and
        # CreationDate is suppressed so identical inputs → identical bytes.
        "Title": f"Argus {report.kind} digest — run {report.run_id} — {report.as_of.date().isoformat()}",
        "Creator": "argus",
        "Producer": "argus",
        "CreationDate": None,
    }
    revenue = revenue_series or {}
    buffer = io.BytesIO()
    with PdfPages(buffer, metadata=metadata) as pdf:
        _save(pdf, _summary_page(report, total_details=len(subjects), shown_details=len(shown)))
        for ticker, ticker_report, proposal in shown:
            _save(pdf, _detail_page(report, ticker, ticker_report, proposal, history, revenue))
    return buffer.getvalue()


def _save(pdf: PdfPages, fig: Figure) -> None:
    try:
        pdf.savefig(fig)
    finally:
        plt.close(fig)


def _detail_subjects(
    report: RunReport,
) -> list[tuple[str, TickerReport | None, ScoutProposal | None]]:
    """One detail page per proposed scout candidate (rank order) or per
    watched ticker (alphabetical — same ordering the digest uses)."""
    by_ticker = {t.context.ticker: t for t in report.tickers}
    if report.kind == "scout":
        return [
            (p.ticker, by_ticker.get(p.ticker), p)
            for p in report.scout
            if p.status == "proposed"
        ]
    ordered = sorted(report.tickers, key=lambda t: t.context.ticker)
    return [(t.context.ticker, t, None) for t in ordered]


# --- Page 1: run summary ------------------------------------------------------


class _Cursor:
    """Top-down text layout on a figure — deterministic line placement
    without measuring rendered text."""

    def __init__(self, fig: Figure, y: float = 0.955) -> None:
        self.fig = fig
        self.y = y

    def line(
        self,
        text: str,
        *,
        x: float = 0.07,
        size: float = 9.0,
        color: str = _INK,
        weight: str = "normal",
        style: str = "normal",
        family: str | None = None,
        step: float | None = None,
    ) -> None:
        kwargs = {"family": family} if family else {}
        self.fig.text(
            x, self.y, text, ha="left", va="top",
            fontsize=size, color=color, fontweight=weight, fontstyle=style, **kwargs,
        )
        # ~1.55em line height, in figure fraction (1pt ≈ 1/792 of the page).
        self.y -= step if step is not None else size * 1.55 / 792

    def wrapped(self, text: str, *, width: int = 112, max_lines: int = 4, **kwargs) -> None:
        lines = textwrap.wrap(text, width) or [""]
        if len(lines) > max_lines:
            lines = lines[: max_lines - 1] + [_clip(lines[max_lines - 1], width - 2) + " …"]
        for line in lines:
            self.line(line, **kwargs)

    def gap(self, dy: float) -> None:
        self.y -= dy


def _summary_page(report: RunReport, *, total_details: int, shown_details: int) -> Figure:
    fig = plt.figure(figsize=_PAGE)
    cur = _Cursor(fig)
    cur.line(
        f"Argus {report.kind} digest — run {report.run_id} — {report.as_of.date().isoformat()}",
        size=15, weight="bold",
    )
    cur.gap(0.012)
    cur.wrapped(_status_line(report), size=9.5, color=_SECONDARY)
    if report.notes:
        cur.gap(0.004)
        cur.wrapped(f"Note: {report.notes}", size=9, color=_SECONDARY, style="italic")
    cur.gap(0.018)
    if report.kind == "scout":
        _scout_summary(fig, cur, report)
    else:
        _watch_summary(fig, cur, report)
    if total_details > shown_details:
        cur.gap(0.012)
        cur.line(
            f"Detail pages are capped: showing the first {shown_details} of {total_details}.",
            size=9, color=_CRITICAL,
        )
    fig.text(
        0.5, 0.03,
        f"Run {report.run_id} — regenerate with: argus report --run {report.run_id}",
        ha="center", va="bottom", fontsize=8, color=_MUTED,
    )
    return fig


def _status_line(report: RunReport) -> str:
    # Same phrasing as the markdown digest — the two artifacts must never
    # tell different stories about the same run.
    if report.status == "complete":
        return "Status: complete."
    if report.status == "partial":
        return (
            "Status: PARTIAL — some tickers or sources failed this run; "
            "degradation is detailed in the markdown digest's Data health section."
        )
    return "Status: FAILED — this run produced no usable data."


def _scout_summary(fig: Figure, cur: _Cursor, report: RunReport) -> None:
    proposed = [p for p in report.scout if p.status == "proposed"]
    excluded = [p for p in report.scout if p.status == "excluded"]
    leaders = [p for p in report.scout if p.status == "leader"]
    snapshots = {t.context.ticker: t.snapshot for t in report.tickers}
    profiles = {t.context.ticker: t.profile for t in report.tickers}

    cur.line("Proposals", size=11, weight="bold")
    cur.gap(0.008)
    if not report.scout and report.status == "failed":
        # An outage is not a verdict: "evaluated nothing" must never read as
        # "nothing passed".
        cur.line("No candidates were evaluated this run — see the note above.", color=_SECONDARY)
    elif not proposed:
        cur.line("No candidates passed the screen and the quality gates this run.", color=_SECONDARY)
    else:
        columns = [
            "#", "Ticker", "Industry", "Streak", "Price",
            "Fwd P/E", "Gross m.", "Op m.", "ROE", "D/E",
        ]
        widths = [0.05, 0.11, 0.19, 0.08, 0.10, 0.10, 0.10, 0.10, 0.085, 0.085]
        rows: list[list[str]] = []
        bands: set[int] = set()  # row indices of the sector sub-header bands
        for sector, group in _sector_groups(proposed):
            bands.add(len(rows))
            rows.append([_clip(sector, 40)] + [""] * (len(columns) - 1))
            for p in group:
                rows.append(
                    [
                        str(p.rank),
                        _clip(p.ticker, 10),
                        _industry_cell(profiles.get(p.ticker)),
                        _streak_cell(p.streak),
                        _table_cell(snapshots.get(p.ticker), Field.PRICE),
                        _table_cell(snapshots.get(p.ticker), Field.PE_FWD),
                        _table_cell(snapshots.get(p.ticker), Field.GROSS_MARGIN),
                        _table_cell(snapshots.get(p.ticker), Field.OPERATING_MARGIN),
                        _table_cell(snapshots.get(p.ticker), Field.ROE),
                        _table_cell(snapshots.get(p.ticker), Field.DEBT_TO_EQUITY),
                    ]
                )
        _table(fig, cur, columns, rows, widths, band_rows=frozenset(bands))
        cur.gap(0.006)
        cur.wrapped(
            "Grouped by canonical sector; '#' is the global screen rank. Every value is "
            "gate-verified from this run's snapshots — '—' means the gates accepted nothing.",
            size=8, color=_MUTED, style="italic", width=120, max_lines=2,
        )

    if leaders:
        cur.gap(0.016)
        cur.line(
            "Sector leaders beyond the shortlist (screener claims, not enriched)",
            size=10, weight="bold",
        )
        cur.gap(0.006)
        shown_leaders = leaders[:10]
        for p in shown_leaders:
            cur.line(_clip(_leader_line(p), 118), size=8.5, color=_SECONDARY)
        if len(leaders) > len(shown_leaders):
            cur.line(f"… and {len(leaders) - len(shown_leaders)} more.", size=8.5, color=_MUTED)
        cur.gap(0.002)
        cur.line(
            "The best screen passer of each sector the shortlist left unrepresented — "
            "coverage context only, no detail page.",
            size=8, color=_MUTED, style="italic",
        )

    cur.gap(0.016)
    cur.line("Excluded after enrichment", size=11, weight="bold")
    cur.gap(0.008)
    if not excluded:
        cur.line("No screen survivors were excluded by the quality gates.", color=_SECONDARY)
    else:
        shown = excluded[:15]
        for p in shown:
            cur.line(
                _clip(f"{p.ticker} (screen rank {p.rank}): {p.exclusion_reason or ''}", 118),
                size=8.5, color=_SECONDARY,
            )
        if len(excluded) > len(shown):
            cur.line(f"… and {len(excluded) - len(shown)} more.", size=8.5, color=_MUTED)
        cur.gap(0.004)
        cur.wrapped(
            "Exclusion is a data-quality verdict, not an investment one — these names "
            "passed the screen but their fundamentals could not be verified cleanly this run.",
            size=8, color=_MUTED, style="italic", width=120,
        )

    cur.gap(0.016)
    cur.line("Data health", size=11, weight="bold")
    cur.gap(0.008)
    for text, tone in _health_lines(report):
        cur.line(text, size=8.5, color=tone)


def _sector_groups(proposed: Sequence[ScoutProposal]) -> list[tuple[str, list[ScoutProposal]]]:
    """Proposals grouped by canonical sector: groups ordered by their best
    (lowest) global rank then name, rows within a group by rank then ticker —
    fully deterministic, the shortlist still reads top-down."""
    groups: dict[str, list[ScoutProposal]] = {}
    for p in proposed:
        groups.setdefault(p.sector or "Other", []).append(p)
    for group in groups.values():
        group.sort(key=lambda p: (p.rank, p.ticker))
    return sorted(groups.items(), key=lambda kv: (min(p.rank for p in kv[1]), kv[0]))


def _leader_line(p: ScoutProposal) -> str:
    """One sector-leader strip line — screener claims only (leaders are never
    enriched, so there is nothing gate-verified to show)."""
    fwd = p.screener_metrics.get("fwd_pe")
    if _finite(fwd):
        detail = f"claimed fwd P/E {float(fwd):.1f}, #{p.rank} overall"  # type: ignore[arg-type]
    else:
        detail = f"#{p.rank} overall"
    return f"{p.sector} — {p.ticker} ({detail})"


def _health_lines(report: RunReport) -> list[tuple[str, str]]:
    """Per-source health rollups — the same tallies, first-error, skipped
    cross-checks, and not-configured statements as the markdown digest's Data
    health section (the two artifacts must never tell different health
    stories), plus failed tickers and the run-level note when present."""
    tickers = sorted(report.tickers, key=lambda t: t.context.ticker)
    source_order = {s: i for i, s in enumerate(Source)}
    tallies: dict[Source, dict[str, int]] = {}
    first_error: dict[Source, str] = {}
    for ticker in tickers:  # alphabetical → "first error" is deterministic
        for health in sorted(ticker.sources, key=lambda s: source_order[s.source]):
            tally = tallies.setdefault(health.source, {"ok": 0, "error": 0, "not_applicable": 0})
            tally[health.status] += 1
            if health.status == "error" and health.error and health.source not in first_error:
                first_error[health.source] = health.error
    lines: list[tuple[str, str]] = []
    if not tallies:
        lines.append(("No source health recorded.", _SECONDARY))
    for source in Source:
        tally = tallies.get(source)
        if tally is None:
            if tallies:
                # Zero rows on a run that fetched anything = never wired in
                # (no API key / contact email) — permanent degradation belongs
                # in the report, not just a CLI echo.
                lines.append(
                    (f"{source.value}: not configured — its cross-checks never ran", _MUTED)
                )
            continue
        parts = []
        if tally["ok"]:
            parts.append(f"{tally['ok']} ok")
        if tally["error"]:
            parts.append(f"{tally['error']} error" + ("s" if tally["error"] != 1 else ""))
        if tally["not_applicable"]:
            parts.append(f"{tally['not_applicable']} not applicable")
        line = f"{source.value}: " + ", ".join(parts)
        if tally["error"]:
            if source in first_error:
                line += f" (first: {first_error[source]})"
            skipped = [
                _FIELD_LABELS.get(f, f.value.replace("_", " ")).lower()
                for f in Field
                if SPECS[f].cross_source_rel_tol is not None
                and source in SPECS[f].priority
                and SPECS[f].priority[0] is not source
            ]
            if skipped:
                n = tally["error"]
                line += (
                    f" — {', '.join(skipped)} cross-checks skipped"
                    f" ({n} ticker{'s' if n != 1 else ''})"
                )
        lines.append((_clip(line, 122), _CRITICAL if tally["error"] else _SECONDARY))
    failed = [t for t in tickers if t.status == "failed"]
    if failed:
        lines.append(("Failed tickers:", _CRITICAL))
        for t in failed[:8]:
            lines.append(
                (_clip(f"  {t.context.ticker}: {t.error or 'unknown error'}", 122), _SECONDARY)
            )
        if len(failed) > 8:
            lines.append((f"  … and {len(failed) - 8} more.", _MUTED))
    if report.notes:
        lines.append((_clip(f"Run note: {report.notes}", 122), _SECONDARY))
    return lines


def _watch_summary(fig: Figure, cur: _Cursor, report: RunReport) -> None:
    tickers = sorted(report.tickers, key=lambda t: t.context.ticker)
    cur.line("Watchlist", size=11, weight="bold")
    cur.gap(0.008)
    if not tickers:
        cur.line("No tickers in this run.", color=_SECONDARY)
        return
    columns = ["Ticker", "Price", "Fwd P/E", "Gross m.", "Op m.", "ROE", "D/E", "Events"]
    widths = [0.16, 0.13, 0.12, 0.12, 0.12, 0.11, 0.11, 0.13]
    shown = tickers[:28]  # keeps the table inside page 1; detail pages carry the rest
    rows = [
        [
            _clip(t.context.ticker, 10),
            _table_cell(t.snapshot, Field.PRICE),
            _table_cell(t.snapshot, Field.PE_FWD),
            _table_cell(t.snapshot, Field.GROSS_MARGIN),
            _table_cell(t.snapshot, Field.OPERATING_MARGIN),
            _table_cell(t.snapshot, Field.ROE),
            _table_cell(t.snapshot, Field.DEBT_TO_EQUITY),
            str(len(t.events)),
        ]
        for t in shown
    ]
    _table(fig, cur, columns, rows, widths)
    if len(tickers) > len(shown):
        cur.gap(0.006)
        cur.line(f"… and {len(tickers) - len(shown)} more tickers.", size=8.5, color=_MUTED)
    # A breach must never be buried on a detail page — surface it here the way
    # the markdown puts thesis drift in its Changes section. Scans ALL tickers,
    # not just the table's first page.
    drifted = [(t.context.ticker, n) for t in tickers if (n := _thesis_breaches(t))]
    if drifted:
        cur.gap(0.012)
        cur.line(
            "Thesis drift — human-declared conditions breached:",
            size=9, weight="bold", color=_CRITICAL,
        )
        for name, n in drifted:
            cur.line(
                f"⚠ {name} ({n} breach{'es' if n != 1 else ''})",
                size=8.5, color=_SECONDARY,
            )
    failures = [t for t in tickers if t.status == "failed"]
    if failures:
        cur.gap(0.012)
        cur.line("Fetch failures (no data this run):", size=9, weight="bold", color=_CRITICAL)
        for t in failures[:10]:
            cur.line(
                _clip(f"{t.context.ticker}: {t.error or 'unknown error'}", 118),
                size=8.5, color=_SECONDARY,
            )


def _table(
    fig: Figure,
    cur: _Cursor,
    columns: list[str],
    rows: list[list[str]],
    widths: list[float],
    band_rows: frozenset[int] = frozenset(),
) -> None:
    """A minimal hairline table: horizontal rules only, recessive header,
    every row a fixed height so the layout never depends on a renderer.
    band_rows are 0-based indices into `rows` rendered as full-width group
    sub-headers (first cell carries the label, left-aligned on a panel
    band) — how the proposals table groups by sector without a second
    table per group."""
    row_height = 0.024
    height = row_height * (len(rows) + 1)
    ax = fig.add_axes((0.07, cur.y - height, 0.86, height))
    ax.axis("off")
    table = ax.table(
        cellText=rows, colLabels=columns, colWidths=widths,
        cellLoc="center", loc="upper left", edges="horizontal",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    for (row, col), cell in table.get_celld().items():
        cell.set_height(1.0 / (len(rows) + 1))
        cell.set_edgecolor(_GRID)
        cell.set_linewidth(0.6)
        cell.set_facecolor("none")
        if row == 0:
            cell.set_text_props(color=_SECONDARY, fontweight="bold", fontsize=7.5)
        elif row - 1 in band_rows:
            cell.set_facecolor(_PANEL)
            if col == 0:  # the label cell; siblings are empty and unbordered
                cell.set_text_props(
                    color=_SECONDARY, fontweight="bold", fontsize=7.5, ha="left"
                )
        else:
            cell.set_text_props(color=_INK)
    cur.gap(height + 0.008)


# --- Detail pages --------------------------------------------------------------


def _detail_page(
    report: RunReport,
    ticker: str,
    ticker_report: TickerReport | None,
    proposal: ScoutProposal | None,
    history: History,
    revenue_series: RevenueSeries,
) -> Figure:
    fig = plt.figure(figsize=_PAGE)
    fig.text(0.07, 0.955, ticker, ha="left", va="top", fontsize=16, fontweight="bold", color=_INK)
    fig.text(
        0.93, 0.952, _detail_subtitle(ticker_report, proposal),
        ha="right", va="top", fontsize=9.5, color=_SECONDARY,
    )

    cur = _Cursor(fig, y=0.915)
    _business_block(cur, ticker_report.profile if ticker_report is not None else None)
    if proposal is not None:
        snapshot = ticker_report.snapshot if ticker_report is not None else None
        cur.wrapped(
            _why_surfaced(proposal, snapshot),
            size=8.5, color=_SECONDARY, width=112, max_lines=5,
        )
        cur.gap(0.004)
        claims = "; ".join(proposal.screen_reasons.values())
        if claims:
            cur.wrapped(
                "Screen (screener claims — every value in the panel below is "
                f"gate-verified): {claims}",
                size=8, color=_MUTED, style="italic", width=118, max_lines=2,
            )
        peers = _peer_line(proposal, snapshot)
        if peers is not None:
            cur.gap(0.004)
            cur.wrapped(
                f"Industry peers (screener claims): {peers}",
                size=8, color=_MUTED, style="italic", width=118, max_lines=2,
            )
    else:
        assert ticker_report is not None  # watch subjects always carry their report
        if ticker_report.context.thesis:
            cur.wrapped(
                ticker_report.context.thesis, size=9, color=_SECONDARY, style="italic",
                width=110, max_lines=2,
            )
            cur.gap(0.004)
        n = len(ticker_report.events)
        cur.line(
            f"Change events this run: {n}" if n else "Change events this run: none.",
            size=8.5, color=_SECONDARY,
        )
        _thesis_panel(cur, ticker_report)

    _price_chart(fig, (0.08, 0.435, 0.50, 0.165), history.get(ticker))
    _revenue_chart(fig, (0.665, 0.435, 0.275, 0.165), revenue_series.get(ticker))

    cur = _Cursor(fig, y=0.375)
    cur.line("Verified metrics (gate-accepted, with provenance)", size=9.5, weight="bold")
    cur.gap(0.005)
    for text, tone in _metric_lines(ticker_report):
        cur.line(text, size=7.5, family="monospace", color=tone, step=0.0142)

    fig.text(0.5, 0.03, _UNGATED_FOOTER, ha="center", va="bottom", fontsize=8, color=_MUTED)
    return fig


# --- Thesis checks (watch only — the human's falsifiable conditions) ------------


def _thesis_panel(cur: _Cursor, ticker_report: TickerReport) -> None:
    """The human's falsifiable thesis conditions and how each stands this run.

    One row per check — the raw condition plus HOLDS (the quiet reassurance),
    BREACHED (critical, with the observed value that crossed the line), or
    UNVERIFIABLE (no accepted value this run) — under a header carrying the
    tally, mirroring the digest's _thesis_standing so the two artifacts never
    tell different stories. Omitted entirely when the ticker carries no checks
    (or has no snapshot to evaluate against): an absent panel means the human
    attached no conditions, never that everything silently holds. Argus only
    reports these human-declared lines; it never writes the thesis itself."""
    checks = ticker_report.context.thesis_checks
    snapshot = ticker_report.snapshot
    if not checks or snapshot is None:
        return
    results = evaluate_thesis_checks(checks, snapshot)
    header, tone = _thesis_header(results)
    cur.gap(0.010)
    cur.line(header, size=9, weight="bold", color=tone)
    cur.gap(0.003)
    for text, row_tone in _thesis_rows(results):
        cur.line(_clip(text, 110), size=8, color=row_tone)


def _thesis_header(results: Sequence[ThesisCheckResult]) -> tuple[str, str]:
    """The panel's tally line, e.g. 'Thesis checks — 3/4 holding' or, when any
    condition is breached, the critical '⚠ Thesis checks — 1/4 BREACHED'. An
    unverifiable count rides along (as in the digest) so 'nothing breached' is
    never confused with 'everything checked out'."""
    holding = sum(1 for r in results if r.status == "holds")
    breached = sum(1 for r in results if r.status == "breached")
    unverifiable = sum(1 for r in results if r.status == "undeterminable")
    total = len(results)
    if breached:
        text, tone = f"⚠ Thesis checks — {breached}/{total} BREACHED", _CRITICAL
    else:
        text, tone = f"Thesis checks — {holding}/{total} holding", _SECONDARY
    if unverifiable:
        text += f" ({unverifiable} unverifiable this run)"
    return text, tone


def _thesis_rows(results: Sequence[ThesisCheckResult]) -> list[tuple[str, str]]:
    """One (text, tone) row per evaluated check. Observed values on a breach
    are formatted with the shared _fmt_value, so a fraction like a margin reads
    as a percent exactly as it does in the metrics panel and the digest."""
    rows: list[tuple[str, str]] = []
    for r in results:
        raw = r.check.raw
        if r.status == "holds":
            rows.append((f"{raw} — HOLDS", _SECONDARY))
        elif r.status == "breached":
            observed = _fmt_value(r.check.field, r.observed)
            rows.append((f"{raw} — BREACHED (now {observed})", _CRITICAL))
        else:  # undeterminable: the field had no accepted value this run
            rows.append((f"{raw} — UNVERIFIABLE (no accepted value this run)", _MUTED))
    return rows


def _thesis_breaches(ticker_report: TickerReport) -> int:
    """How many of a watched ticker's thesis checks are BREACHED this run —
    0 when it carries no checks or its fetch failed (no snapshot to judge
    against). Drives the summary-page drift marker."""
    checks = ticker_report.context.thesis_checks
    if not checks or ticker_report.snapshot is None:
        return 0
    results = evaluate_thesis_checks(checks, ticker_report.snapshot)
    return sum(1 for r in results if r.status == "breached")


# --- Business block & scout narrative (facts only — the thesis is human) --------


def _business_block(cur: _Cursor, profile: CompanyProfile | None) -> None:
    """What the business IS: bold name, 'sector · industry · ~N employees',
    then the profile summary verbatim (wrapped, '…'-truncated — profiles are
    prose from the source, never invented here). None or an empty shell →
    a visible 'unavailable' line, never a silent gap."""
    identity = _identity_line(profile) if profile is not None else None
    if profile is None or not (profile.name or identity or profile.summary):
        cur.line("business profile unavailable", size=8.5, color=_MUTED, style="italic")
        cur.gap(0.006)
        return
    if profile.name:
        cur.line(profile.name, size=9.5, weight="bold")
    if identity:
        cur.line(identity, size=8.5, color=_SECONDARY)
    if profile.summary:
        cur.wrapped(profile.summary, size=8, color=_SECONDARY, width=118, max_lines=5)
    cur.gap(0.008)


def _identity_line(profile: CompanyProfile) -> str | None:
    """'Technology · Semiconductors · ~36,000 employees' — whichever parts
    the profile carries; None when it carries none of them."""
    parts = [p for p in (profile.sector, profile.industry) if p]
    if profile.employees is not None and profile.employees > 0:
        parts.append(f"~{profile.employees:,} employees")
    return " · ".join(parts) if parts else None


def _why_surfaced(proposal: ScoutProposal, snapshot: Snapshot | None) -> str:
    """Why the screen surfaced this name — a template over data already in
    the report: rank, streak, gate-verified snapshot values, and the
    screener's own humanized pass-reason strings. Factual restatement ONLY:
    no adjectives, no outlook, no recommendation (the hard constraints
    forbid Argus from writing a thesis). The fixed closing line hands the
    decision to the human."""
    clauses: list[str] = []
    fwd = _verified_num(snapshot, Field.PE_FWD)
    if fwd is not None:
        clauses.append(f"trades at {fwd:.1f}× forward earnings (verified)")
    growth = _verified_num(snapshot, Field.REVENUE_GROWTH)
    if growth is not None:
        clauses.append(f"with verified revenue growth of {growth * 100:+.1f}%")
    else:
        claim = proposal.screen_reasons.get("revenue_growth")
        if claim:
            clauses.append(f"with the screener reporting {_clip(claim, 60)}")
    quality: list[str] = []
    gross = _verified_num(snapshot, Field.GROSS_MARGIN)
    if gross is not None:
        quality.append(f"gross margins of {gross * 100:.1f}%")
    roe = _verified_num(snapshot, Field.ROE)
    if roe is not None:
        quality.append(f"ROE of {roe * 100:.1f}%")
    if quality:
        clauses.append("on verified " + " and ".join(quality))
    streak = (
        "new this week" if proposal.streak <= 1
        else f"{_ordinal(proposal.streak)} consecutive week"
    )
    sentence = f"Surfaced at #{proposal.rank} ({streak})"
    if clauses:
        sentence += ": " + ", ".join(clauses)
    sentence += "."
    if proposal.screen_reasons:
        n = len(proposal.screen_reasons)
        sentence += f" Passed all {n} screen rule{'s' if n != 1 else ''}."
    return f"{sentence} {_PROMOTE_LINE.format(ticker=proposal.ticker)}"


def _peer_line(proposal: ScoutProposal, snapshot: Snapshot | None) -> str | None:
    """The industry-peer context line, e.g.:
    'Semiconductors (n=12) — median fwd P/E 28.4 · AVGO 32.1, AMD 29.9,
    INTC — · AAA (verified) 18.4'. Everything before the final clause is a
    SCREENER CLAIM (the caller labels the line as such); the final clause is
    the proposal's own gate-verified forward P/E so claim and verified value
    sit visibly side by side ('—' when the gates accepted none). None when
    the proposal carries no peer context — it is optional context, and its
    absence needs no note."""
    ctx = proposal.peer_context
    if not ctx:
        return None
    industry = str(ctx.get("industry") or "").strip() or "industry unknown"
    head = _clip(industry, 40)
    n = ctx.get("n")
    if isinstance(n, int) and not isinstance(n, bool) and n > 0:
        head += f" (n={n})"
    median = ctx.get("median_fwd_pe")
    if _finite(median):
        head += f" — median fwd P/E {float(median):g}"
    else:
        head += " — median fwd P/E —"
    peers: list[str] = []
    for peer in tuple(ctx.get("peers") or ())[:8]:
        if not isinstance(peer, Mapping):
            continue
        name = str(peer.get("ticker") or "").strip()
        if not name:
            continue
        pe = peer.get("fwd_pe")
        peers.append(f"{_clip(name, 8)} {float(pe):.1f}" if _finite(pe) else f"{_clip(name, 8)} —")
    own = _verified_num(snapshot, Field.PE_FWD)
    own_text = (
        f"{proposal.ticker} (verified) {own:.1f}"
        if own is not None
        else f"{proposal.ticker} (verified) —"
    )
    parts = [head]
    if peers:
        parts.append(", ".join(peers))
    parts.append(own_text)
    return " · ".join(parts)


def _finite(value: object) -> bool:
    """True for a real, finite number — the guard every screener-claim
    field passes through before being formatted (claims are untrusted)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _verified_num(snapshot: Snapshot | None, field: Field) -> float | None:
    """A gate-accepted, finite numeric value — or None. The narrative only
    ever restates numbers the gates accepted."""
    if snapshot is None:
        return None
    fv = snapshot.values.get(field)
    if fv is None:
        return None
    try:
        number = float(fv.value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _detail_subtitle(ticker_report: TickerReport | None, proposal: ScoutProposal | None) -> str:
    if proposal is not None:
        streak = (
            "new this week" if proposal.streak <= 1
            else f"{proposal.streak} consecutive weeks on the list"
        )
        return f"screen rank {proposal.rank} — {streak}"
    assert ticker_report is not None
    return f"status: {ticker_report.status}"


def _price_chart(
    fig: Figure,
    rect: tuple[float, float, float, float],
    points: Sequence[tuple[date, float]] | None,
) -> None:
    """Trailing-year close line — the one ungated element, hence the page
    footer. None or no plottable points → a visible 'unavailable' note in
    the chart's place, never a silent gap."""
    plottable = [
        (d, float(v))
        for d, v in (points or ())
        if isinstance(d, date) and isinstance(v, (int, float)) and math.isfinite(float(v))
    ]
    if not plottable:
        _unavailable_panel(fig, rect, "price history unavailable")
        return

    plottable.sort(key=lambda p: p[0])
    dates = [p[0] for p in plottable]
    closes = [p[1] for p in plottable]
    ax = fig.add_axes(rect)
    ax.plot(dates, closes, color=_SERIES, linewidth=1.6, solid_capstyle="round")
    ax.plot([dates[-1]], [closes[-1]], marker="o", markersize=4, color=_SERIES)
    ax.annotate(  # selective direct label: the latest close only, in ink
        f"{closes[-1]:.2f}", (dates[-1], closes[-1]),
        xytext=(6, 0), textcoords="offset points",
        fontsize=8, color=_INK, va="center", annotation_clip=False,
    )
    ax.set_title("Trailing-year price", loc="left", fontsize=9, color=_SECONDARY)
    ax.margins(x=0.03)
    ax.yaxis.grid(True, color=_GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(_BASELINE)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.tick_params(colors=_MUTED, labelsize=7.5, length=3, width=0.6)
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.xaxis.get_offset_text().set_color(_MUTED)
    ax.xaxis.get_offset_text().set_fontsize(7.5)


def _revenue_chart(
    fig: Figure,
    rect: tuple[float, float, float, float],
    points: Sequence[tuple[int, float]] | None,
) -> None:
    """Annual revenue bars — ungated display data like the price chart,
    hence the shared page footer. One series, direct-labeled in ink (no
    y-axis: with ~4 bars the labels ARE the scale). None, empty, or fully
    malformed input → a visible 'unavailable' note, never a silent gap."""
    plottable = _revenue_points(points)
    if not plottable:
        _unavailable_panel(fig, rect, "revenue history unavailable")
        return
    plottable = plottable[-6:]  # display sanity: ~4 fiscal years expected
    years = [str(year) for year, _ in plottable]
    values = [value for _, value in plottable]
    ax = fig.add_axes(rect)
    ax.bar(years, values, color=_SERIES, width=0.6)
    for i, value in enumerate(values):
        ax.annotate(
            _humanize_cap(value), (i, max(value, 0.0)),
            xytext=(0, 3), textcoords="offset points",
            ha="center", va="bottom", fontsize=7.5, color=_INK, annotation_clip=False,
        )
    ax.set_title("Annual revenue (USD)", loc="left", fontsize=9, color=_SECONDARY)
    ax.set_xlabel(_FISCAL_YEAR_CAPTION, fontsize=6.5, color=_MUTED, style="italic", labelpad=4)
    top = max(values + [0.0])
    bottom = min(values + [0.0])
    pad = 0.15 * ((top - bottom) or 1.0)
    ax.set_ylim(bottom - (pad if bottom < 0 else 0.0), top + pad)
    ax.yaxis.set_visible(False)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(_BASELINE)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.tick_params(colors=_MUTED, labelsize=7.5, length=0)


def _revenue_points(points: Sequence[tuple[int, float]] | None) -> list[tuple[int, float]]:
    """Tolerant parse of caller-supplied display data: anything that is not
    a (year, finite number) pair is skipped, and the survivors are sorted
    chronologically — bad points degrade the chart, never crash it."""
    out: list[tuple[int, float]] = []
    for item in points or ():
        try:
            year_raw, value_raw = item
        except (TypeError, ValueError):
            continue
        if isinstance(year_raw, bool) or isinstance(value_raw, bool):
            continue
        try:
            year, value = int(year_raw), float(value_raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            out.append((year, value))
    out.sort(key=lambda p: p[0])
    return out


def _unavailable_panel(
    fig: Figure, rect: tuple[float, float, float, float], message: str
) -> None:
    x, y, w, h = rect
    fig.patches.append(
        Rectangle(
            (x, y), w, h, transform=fig.transFigure,
            facecolor=_PANEL, edgecolor=_GRID, linewidth=0.8,
        )
    )
    fig.text(
        x + w / 2, y + h / 2, message,
        ha="center", va="center", fontsize=10, color=_MUTED, fontstyle="italic",
    )


def _metric_lines(ticker_report: TickerReport | None) -> list[tuple[str, str]]:
    """The tri-state panel, one line per field in enum order:
    value + provenance (ink), DATA QUARANTINED + reason (critical — the
    label carries the meaning, the color only flags it), or '— no data'
    (muted). A missing snapshot renders all-dash — disclosed, never crashed."""
    lines: list[tuple[str, str]] = []
    snapshot: Snapshot | None = ticker_report.snapshot if ticker_report else None
    if ticker_report is None:
        lines.append(("No enrichment snapshot was recorded for this candidate.", _CRITICAL))
    elif snapshot is None:
        error = ticker_report.error or "unknown error"
        lines.append((_clip(f"Fetch failed — no data this run ({error}).", 100), _CRITICAL))
    for field in Field:
        label = _FIELD_LABELS.get(field, field.value.replace("_", " "))
        prefix = f"{label:<26}"  # widest label: 'Revenue growth (MRQ YoY)' = 24
        fv = snapshot.values.get(field) if snapshot else None
        if fv is not None:
            marks = "".join(f"  ✓{s.value}" for s in sorted(fv.corroborated_by))
            provenance = f"{fv.source.value}, {_ts(fv.fetched_at)}{marks}"
            lines.append((f"{prefix}{_fmt_value(field, fv.value):<14}{provenance}", _INK))
            continue
        hits = snapshot.quarantined.get(field) if snapshot else None
        if hits:
            details = "; ".join(hit.detail for hit in hits)
            lines.append((_clip(f"{prefix}DATA QUARANTINED — {details}", 105), _CRITICAL))
            continue
        lines.append((f"{prefix}— no data", _MUTED))
    return lines


# --- Formatting (mirrors the markdown digest's conventions) --------------------


def _table_cell(snapshot: Snapshot | None, field: Field) -> str:
    """Gate-verified value or '—'. Anything not accepted — missing,
    quarantined, malformed — is a dash; the detail page tells the fuller
    tri-state story with reasons."""
    if snapshot is None:
        return "—"
    fv = snapshot.values.get(field)
    if fv is None:
        return "—"
    return _fmt_value(field, fv.value)


def _streak_cell(streak: int) -> str:
    return f"{streak}w" if streak > 1 else "new"


def _industry_cell(profile: CompanyProfile | None) -> str:
    """Compact business identity for the proposals table: the profile's
    industry, truncated (the canonical sector is already the group band, so
    repeating it would waste the column) — the detail page carries the full
    profile. No profile or no industry → '—', same tri-state dash as the
    metric cells."""
    if profile is None or not profile.industry:
        return "—"
    return _clip(profile.industry, 18)


def _fmt_value(field: Field, value: object) -> str:
    """Render one accepted value; anything malformed degrades to '—' rather
    than crashing the report path (bad data is disclosed, not fatal)."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, str):
        if SPECS[field].kind != "num":
            return _clip(value, 24)
        try:
            value = float(value)
        except ValueError:
            return "—"
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    number = float(value)
    if not math.isfinite(number):
        return "—"
    if field in _HUMANIZED_FIELDS:
        return _humanize_cap(number)
    if field is Field.ANALYST_COUNT:
        return f"{number:.0f}"
    if field in _PERCENT_FIELDS:
        return f"{number * 100:.1f}%"  # stored as a fraction; read as a percent
    return f"{number:.2f}"


def _humanize_cap(value: float) -> str:
    """1.23T / 456.7B / 89.0M — three-ish significant figures, never raw."""

    def _render(scaled: float, suffix: str) -> str:
        return f"{scaled:.2f}{suffix}" if abs(scaled) < 10 else f"{scaled:.1f}{suffix}"

    magnitude = abs(value)
    units = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))
    for i, (divisor, suffix) in enumerate(units):
        if magnitude >= divisor:
            rendered = _render(value / divisor, suffix)
            if rendered.lstrip("-").startswith("1000.") and i > 0:
                bigger_divisor, bigger_suffix = units[i - 1]
                rendered = _render(value / bigger_divisor, bigger_suffix)
            return rendered
    return f"{value:.2f}"


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
