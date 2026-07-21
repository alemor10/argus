"""PDF report rendering (pure): RunReport + price/revenue history → bytes.

The PDF is the digest's visual companion. Page 1 summarizes the run with the
same tri-state honesty as the markdown — a value the gates did not accept is
'—', never a blank and never a guess. For scout runs the proposals table is
grouped by canonical sector, followed by the sector-leaders strip (screener
claims, never enriched, no detail pages), the exclusions, the Scorecard grading
past proposals vs SPY (the market is the answer key, mirroring the markdown's),
and a Data health block mirroring the markdown's — the summary is the whole run
on one page.
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
from datetime import date, datetime

import matplotlib

matplotlib.use("Agg")  # before pyplot: headless-safe, never a GUI backend

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from argus.digest import (
    # The digest's own line renderers — the two artifacts must never tell
    # different stories about the same run, so the PDF reuses them verbatim
    # (markdown chrome stripped at the call sites).
    _considering_line as _digest_considering_line,
    _feature_fact_lines as _digest_feature_fact_lines,
    _event_line as _digest_event_line,
    _radar_crossings as _digest_radar_crossings,
    _radar_insider_crossings as _digest_radar_insider_crossings,
    _spread_lines as _digest_spread_lines,
    # ...and the shared formatters/formula — ONE source each, for the same
    # reason (a divergence would be the two artifacts disagreeing on a number).
    # `_fmt_value` stays local: the PDF panels format via _HUMANIZED_FIELDS /
    # _PERCENT_FIELDS, which the markdown tri-state does not.
    _eps_surprise_pct,
    _humanize_cap,
    _pct,
    _ts,
)
from argus.evidence import data_flags, evidence_state, screen_exit_conditions
from argus.fields import SPECS, Field, Source
from argus.models import (
    CompanyProfile,
    RunReport,
    Scorecard,
    ScoutProposal,
    Snapshot,
    ThesisCheckResult,
    TickerReport,
)
from argus.scorecard import MIN_SAMPLE
from argus.thesis import evaluate_thesis_checks

# ticker → chronological (date, close) points for ~1y, or None when history
# could not be fetched. Plain tuples — NOT pandas — supplied by the caller
# and labeled as UNGATED display data (see module docstring).
History = Mapping[str, Sequence[tuple[date, float]] | None]

# ticker → chronological (fiscal_year, revenue_dollars) points, ~4 annual
# values, or None when unavailable. UNGATED display data like `history`.
RevenueSeries = Mapping[str, Sequence[tuple[int, float]] | None]

_MAX_DETAIL_PAGES = 25  # watch: a watchlist longer than this is unusual — disclose the cap
_SCOUT_DETAIL_CAP = 12  # scout: only the top-ranked proposals earn a full chart page; the
#                         rest ride the page-1 compact table (every proposal is listed there)

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
_UP = "#1e7d46"  # positive changes on charts/wires; sign is always printed too
_PANEL = "#f9f9f7"

# Dashboard tile order — rates & policy, inflation & jobs, markets, then
# commodities & crypto; unknown symbols append label-sorted after these.
_TILE_ORDER = (
    "^TNX", "^IRX", "DFF", "CPIAUCSL", "PCEPILFE", "UNRATE", "PAYEMS",
    "^GSPC", "^VIX", "DX-Y.NYB", "GC=F", "CL=F", "BTC-USD",
)

_UNGATED_FOOTER = (
    "Price & revenue charts: raw Yahoo data, ungated — the metrics panel is gate-accepted."
)

# Under the revenue bars: they are fiscal-year totals, while the panel's
# Revenue (TTM) is a trailing window — the two legitimately differ, and the
# reader must not mistake that for a data-quality bug.
_FISCAL_YEAR_CAPTION = "fiscal years — TTM revenue in the panel may differ"

# The fixed hand-off line on every scout detail page: Argus surfaces facts,
# the human writes the thesis (hard constraint — no self-generated theses).
_PROMOTE_LINE = 'Argus proposes; the thesis is yours — argus promote {ticker} --thesis "..."'

# Scorecard wording — mirrors the markdown digest's _scorecard_section so the
# two artifacts never tell different stories about how past proposals have done.
_SCORECARD_EMPTY = "No proposal has had time to play out yet — the forward log starts now."
_SCORECARD_CAPTION = (
    "Each horizon is the realized return from entry to the close that many weeks "
    "later (adjusted, incl. dividends) vs SPY over the identical window — locked "
    "once measured, never revised. The market is the answer key."
)

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


_SCOUT_CARDS_TITLE = "Reading — worth watching & under pressure"
_SCOUT_CARDS_CAPTION = (
    "A curated few from the broader lenses — business summary, claimed numbers, "
    "and a 1-year price strip. Yahoo claims, unverified; not gated, not scored."
)


def scout_card_subjects(report: RunReport) -> list[tuple[str, str]]:
    """Which broader-lens names get a reading card, and why — a curated few,
    deterministic from the run so `report --run N` reproduces them: up to 3
    'worth watching' sector leaders (rank 1 in their sector) and up to 3 'under
    pressure' names, both in a stable order."""
    board = sorted(
        (p for p in report.scout if p.status == "board" and p.rank == 1),
        key=lambda p: (p.sector, p.ticker),
    )[:3]
    det = sorted(
        (p for p in report.scout if p.status == "deterioration"), key=lambda p: p.rank
    )[:3]
    subjects = [
        (p.ticker, f"Worth watching — cheapest-for-growth in {p.sector}") for p in board
    ]
    subjects += [
        (p.ticker, f"Under pressure — {'; '.join(p.screen_reasons.values())}") for p in det
    ]
    return subjects


def build_pdf(
    report: RunReport,
    history: History,
    revenue_series: RevenueSeries | None = None,
    scout_cards: Sequence[object] = (),
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
    detail_cap = _SCOUT_DETAIL_CAP if report.kind == "scout" else _MAX_DETAIL_PAGES
    shown = subjects[:detail_cap]
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
        if report.kind == "watch":
            # PDF-first (v1.8): the PDF is the delivered artifact, so it
            # carries the WHOLE digest — page 1 is the news (masthead, macro
            # dashboard, Changes, Radar), the market-wire page follows on
            # magazine issues (v1.9, charts), then the state page (watchlist
            # table, quarantines, data health — skipped when there is no
            # state to show, its health block riding the wire page instead),
            # then the per-ticker detail pages.
            _save(
                pdf,
                _summary_page(
                    report, history, total_details=len(subjects), shown_details=len(shown)
                ),
            )
            if report.market is not None:
                _save(pdf, _market_wire_page(report))
                features = report.market.features
                for start in range(0, len(features), 3):  # 3 cards per page
                    _save(
                        pdf,
                        _featured_page(
                            report, history, features[start : start + 3], first=start == 0
                        ),
                    )
            _save(pdf, _watch_status_page(report))
        else:
            # Scout paginates its own front matter: the shortlist (compact
            # table + new-this-week) on page 1, the back matter (exclusions,
            # scorecard, health) on page 2 — a failed run collapses to one.
            for page in _scout_pages(
                report, total_details=len(subjects), shown_details=len(shown)
            ):
                _save(pdf, page)
            # Page 3 — the broader lenses (sector board + deterioration watch),
            # only when the scan surfaced something for them.
            if any(p.status in ("board", "deterioration") for p in report.scout):
                _save(pdf, _scout_lenses_page(report))
            # Reading cards for a curated few of the broader-lens names, so they
            # can be read (business + numbers + price strip), not just scanned.
            for start in range(0, len(scout_cards), 3):
                _save(
                    pdf,
                    _featured_page(
                        report, history, scout_cards[start : start + 3],
                        first=start == 0,
                        title=_SCOUT_CARDS_TITLE, caption=_SCOUT_CARDS_CAPTION,
                    ),
                )
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
    # Macro series get no detail page: a 21-field equity panel of dashes for
    # ^VIX is junk, and each page costs two network fetches (history+revenue).
    ordered = sorted(
        (t for t in report.tickers if t.context.macro is None),
        key=lambda t: t.context.ticker,
    )
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


def _summary_page(
    report: RunReport, history: History, *, total_details: int, shown_details: int
) -> Figure:
    """Watch page 1 — masthead + the day's news (macro dashboard, Changes,
    Radar). Scout paginates its front matter through _scout_pages instead."""
    fig = plt.figure(figsize=_PAGE)
    cur = _Cursor(fig)
    _masthead(fig, cur, report)
    cur.wrapped(_status_line(report), size=9.5, color=_SECONDARY)
    if report.notes:
        cur.gap(0.004)
        cur.wrapped(f"Note: {report.notes}", size=9, color=_SECONDARY, style="italic")
    cur.gap(0.014)
    _watch_news(fig, cur, report, history)
    if total_details > shown_details:
        cur.gap(0.012)
        cur.line(
            f"Detail pages are capped: showing the first {shown_details} of {total_details}.",
            size=9, color=_CRITICAL,
        )
    _page_footer(fig, report)
    return fig


def _page_footer(fig: Figure, report: RunReport) -> None:
    fig.text(
        0.5, 0.03,
        f"Run {report.run_id} — regenerate with: argus report --run {report.run_id}",
        ha="center", va="bottom", fontsize=8, color=_MUTED,
    )


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


def _scout_pages(
    report: RunReport, *, total_details: int, shown_details: int
) -> list[Figure]:
    """Scout front matter, paginated so the shortlist can grow and the charts
    have room: page 1 is the shortlist (new-this-week callout + the compact
    proposals table — every proposed name, one row — plus the sector-leaders
    strip), page 2 the back matter (exclusions, the scorecard grading past
    proposals vs SPY with its diverging-bars chart, data health). A failed run
    (screener outage) collapses to a single honest page. The compact table is
    the long tail's home: only the top _SCOUT_DETAIL_CAP proposals earn a full
    detail page; the rest are read from the table."""
    fig1 = plt.figure(figsize=_PAGE)
    cur = _Cursor(fig1)
    cur.line(
        f"Argus scout digest — run {report.run_id} — {report.as_of.date().isoformat()}",
        size=15, weight="bold",
    )
    cur.gap(0.012)
    cur.wrapped(_status_line(report), size=9.5, color=_SECONDARY)
    if report.notes:
        cur.gap(0.004)
        cur.wrapped(f"Note: {report.notes}", size=9, color=_SECONDARY, style="italic")
    cur.gap(0.014)
    _scout_proposals_block(fig1, cur, report)
    if total_details > shown_details:
        cur.gap(0.010)
        cur.line(
            f"Detail pages: the top {shown_details} of {total_details} proposals by rank; "
            "every proposal is in the table above.",
            size=8.5, color=_MUTED,
        )
    if report.status == "failed":
        # An outage has no back matter worth a second page — health explains it
        # here, and the run collapses to one honest page.
        cur.gap(0.016)
        cur.line("Data health", size=11, weight="bold")
        cur.gap(0.008)
        for text, tone in _health_lines(report):
            cur.line(text, size=8.5, color=tone)
        _page_footer(fig1, report)
        return [fig1]
    _page_footer(fig1, report)

    fig2 = plt.figure(figsize=_PAGE)
    cur2 = _Cursor(fig2)
    cur2.line(f"Argus scout — run {report.run_id} (continued)", size=10, color=_SECONDARY)
    cur2.gap(0.016)
    _scout_back_blocks(fig2, cur2, report)
    _page_footer(fig2, report)
    return [fig1, fig2]


def _scout_proposals_block(fig: Figure, cur: _Cursor, report: RunReport) -> None:
    """The shortlist: the new-this-week callout, the compact proposals table
    (every proposed name, grouped by canonical sector — gate-verified values),
    and the sector-leaders strip."""
    proposed = [p for p in report.scout if p.status == "proposed"]
    leaders = [p for p in report.scout if p.status == "leader"]
    snapshots = {t.context.ticker: t.snapshot for t in report.tickers}
    profiles = {t.context.ticker: t.profile for t in report.tickers}

    cur.line("Research shortlist — screened candidates, graded vs SPY", size=11, weight="bold")
    cur.gap(0.008)
    if not report.scout and report.status == "failed":
        # An outage is not a verdict: "evaluated nothing" must never read as
        # "nothing passed".
        cur.line("No candidates were evaluated this run — see the note above.", color=_SECONDARY)
    elif not proposed:
        cur.line("No candidates passed the screen and the quality gates this run.", color=_SECONDARY)
    else:
        _new_this_week_line(cur, proposed)
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
            "gate-accepted from this run's snapshots — '—' means the gates accepted nothing.",
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


def _scout_lenses_page(report: RunReport) -> Figure:
    """Scout page 3 — the two non-quality lenses on the full scan: the sector
    board (relative-value leaders per sector, so every sector fills) and the
    deterioration watch (weakening fundamentals, reported as facts). Screener
    claims, never gated or scored; rendered only when there is something to show."""
    fig = plt.figure(figsize=_PAGE)
    cur = _Cursor(fig)
    cur.line(f"Argus scout — run {report.run_id}: broader lenses", size=13, weight="bold")
    cur.gap(0.014)
    _sector_board_block(fig, cur, report)
    cur.gap(0.012)
    _deterioration_block(cur, report)
    _page_footer(fig, report)
    return fig


def _sector_board_block(fig: Figure, cur: _Cursor, report: RunReport) -> None:
    from argus.scout.sectors import CANONICAL_SECTORS

    board = [p for p in report.scout if p.status == "board"]
    cur.line("Worth watching — relative value by sector (screener claims)", size=11, weight="bold")
    cur.gap(0.004)
    if not board:
        cur.line("No sector-board names this run.", size=9, color=_SECONDARY)
        return
    cur.wrapped(
        "Cheapest-for-growth per sector from the market scan — sanity floors only, "
        "not the quality gates, not verified, not scored. Every sector can fill.",
        size=8, color=_MUTED, style="italic", width=120, max_lines=2,
    )
    cur.gap(0.006)
    by_sector: dict[str, list[ScoutProposal]] = {}
    for p in board:
        by_sector.setdefault(p.sector, []).append(p)
    # Columns that populate across ALL sectors — gross margin/ROE are null for
    # banks/utilities/REITs (the very sectors this lens exists to show), so the
    # readings here are price, valuation, growth, leverage, and size instead.
    columns = ["Ticker", "Price", "Fwd P/E", "PEG", "Rev growth", "D/E", "Market cap"]
    widths = [0.16, 0.14, 0.14, 0.12, 0.16, 0.11, 0.17]
    rows: list[list[str]] = []
    bands: set[int] = set()
    for sector in CANONICAL_SECTORS:
        picks = sorted(by_sector.get(sector, []), key=lambda p: p.rank)
        if not picks:
            continue
        bands.add(len(rows))
        rows.append([_clip(sector, 40)] + [""] * (len(columns) - 1))
        for p in picks:
            m = p.screener_metrics or {}
            rows.append(
                [
                    _clip(p.ticker, 10),
                    _claim_num(m.get("close"), "{:.2f}"),
                    _claim_num(m.get("fwd_pe"), "{:.1f}"),
                    _claim_num(m.get("peg_ttm"), "{:.2f}"),
                    _claim_num(m.get("revenue_growth_ttm_pct"), "{:+.1f}%"),
                    _claim_num(m.get("debt_to_equity"), "{:.2f}"),
                    _claim_num(m.get("market_cap"), "cap"),
                ]
            )
    _table(fig, cur, columns, rows, widths, band_rows=frozenset(bands))


def _deterioration_block(cur: _Cursor, report: RunReport) -> None:
    det = [p for p in report.scout if p.status == "deterioration"]
    cur.line(
        "Under pressure — weakening fundamentals (screener claims)",
        size=11, weight="bold",
    )
    cur.gap(0.004)
    if not det:
        cur.line("No names flagged deteriorating this run.", size=9, color=_SECONDARY)
        return
    cur.wrapped(
        "Factual signs of weakening fundamentals from the scan — reported as DATA, "
        "not a forecast, recommendation, or trade signal. Never gated, never scored; "
        "the human decides.",
        size=8, color=_MUTED, style="italic", width=120, max_lines=2,
    )
    cur.gap(0.006)
    for p in sorted(det, key=lambda p: p.rank):
        if cur.y < _PAGE_FLOOR + 0.02:
            cur.line("… more in the markdown digest.", size=8, color=_MUTED)
            break
        flags = "; ".join(p.screen_reasons.values())
        sector = f" · {p.sector}" if p.sector else ""
        cur.line(_clip(f"{p.ticker}{sector} — {flags}", 118), size=8.5, color=_INK)


def _claim_num(value: object, fmt: str) -> str:
    """A screener-claim number formatted, or '—'. `fmt` of 'cap' humanizes a
    dollar magnitude; otherwise it is a str.format template."""
    if not _finite(value):
        return "—"
    number = float(value)  # type: ignore[arg-type]
    return _humanize_cap(number) if fmt == "cap" else fmt.format(number)


def _new_this_week_line(cur: _Cursor, proposed: Sequence[ScoutProposal]) -> None:
    """Foreground the fresh names: a name on the list for the first time
    (streak ≤ 1) is what a reader who saw last week's issue is scanning for.
    When nothing new cleared the screen, say so plainly — a stable shortlist
    is information, not a bug (the Sunday Edition tracks the churn in full)."""
    new_names = [p.ticker for p in proposed if p.streak <= 1]
    if new_names:
        cur.line("⚡ New this week: " + ", ".join(new_names), size=9.5, weight="bold", color=_UP)
    else:
        cur.line(
            "No new names cleared the screen this week — the shortlist held.",
            size=8.5, color=_MUTED, style="italic",
        )
    cur.gap(0.006)


def _scout_back_blocks(fig: Figure, cur: _Cursor, report: RunReport) -> None:
    """Scout page 2 — the back matter: names excluded after enrichment, the
    scorecard grading past proposals vs SPY (table + diverging-bars chart), and
    the data-health rollup mirroring the markdown digest's."""
    excluded = [p for p in report.scout if p.status == "excluded"]
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

    _scout_scorecard(fig, cur, report)

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


def _scout_scorecard(fig: Figure, cur: _Cursor, report: RunReport) -> None:
    """Grade the grader: how scout's past proposals have actually done vs SPY.
    Realized data, forward log — the market is the answer key, never the engine,
    and this block mirrors the digest's Scorecard so the two artifacts tell one
    story. Empty (just the forward-log line) until proposals have had time to
    play out; otherwise a cohort table plus an overall roll-up whose tone flags
    good vs bad the same way the rest of the PDF does."""
    card = report.scorecard
    cur.gap(0.016)
    cur.line("Scorecard — past proposals vs SPY", size=11, weight="bold")
    cur.gap(0.008)
    if card is None or card.overall_n == 0:
        cur.wrapped(
            _scorecard_empty_line(card), size=8.5, color=_SECONDARY, width=118, max_lines=2
        )
        return
    columns = ["Horizon", "Names", "Median return", "SPY", "Median excess", "Beat SPY"]
    widths = [0.24, 0.12, 0.20, 0.13, 0.17, 0.14]
    _table(fig, cur, columns, _scorecard_rows(card), widths)
    cur.gap(0.004)
    text, tone = _scorecard_overall_line(card)
    cur.line(text, size=9, weight="bold", color=tone)
    _scorecard_chart(fig, cur, card)
    cur.gap(0.002)
    cur.wrapped(_SCORECARD_CAPTION, size=8, color=_MUTED, style="italic", width=120, max_lines=2)


# The cohort table summarizes; this chart shows every scored name so the shape
# of the grade — how many beat, by how much — is legible at a glance.
_SCORECARD_CHART_CAP = 26  # more scored names than fit; disclose the overflow


def _scorecard_chart(fig: Figure, cur: _Cursor, card: Scorecard) -> None:
    """Per-name α vs SPY as diverging bars — the honest self-grade made
    visible: green beat the market, red lagged, the sign always printed too
    (never color alone). One bar per name, at its LONGEST matured horizon (the
    most-seasoned read for that name), best-to-worst; realized data only, the
    market the answer key. Skipped when the run persisted no graded marks (all
    names still maturing), so the cohort table still stands alone."""
    # card.marks carries one entry per (name, horizon); collapse to each name's
    # longest matured horizon so a name is one bar, not several.
    longest: dict[str, object] = {}
    for m in card.marks:
        best = longest.get(m.ticker)
        if best is None or m.horizon_weeks > best.horizon_weeks:
            longest[m.ticker] = m
    marks = sorted(longest.values(), key=lambda m: m.alpha, reverse=True)
    if not marks:
        return
    shown = marks[:_SCORECARD_CHART_CAP]
    cur.gap(0.008)
    cur.line(
        "Per-name excess return vs SPY (realized, at each name's longest matured horizon)",
        size=8.5, color=_SECONDARY,
    )
    cur.gap(0.004)
    height = 0.016 * len(shown)
    ax = fig.add_axes((0.16, cur.y - height, 0.72, height))
    values = [m.alpha * 100 for m in shown][::-1]  # matplotlib barh plots bottom-up
    labels = [m.ticker for m in shown][::-1]
    colors = [_UP if v > 0 else _CRITICAL if v < 0 else _BASELINE for v in values]
    ax.barh(range(len(values)), values, color=colors, height=0.62)
    ax.axvline(0, color=_BASELINE, linewidth=0.8)
    ax.set_yticks([])
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    biggest = max(abs(v) for v in values) or 1.0
    ax.set_xlim(-biggest * 1.6, biggest * 1.6)
    for i, (v, label) in enumerate(zip(values, labels)):
        # Ticker on the empty side of zero; the α value at the bar's tip.
        ax.annotate(
            label, xy=(0, i), xytext=(-5 if v >= 0 else 5, 0),
            textcoords="offset points", va="center",
            ha="right" if v >= 0 else "left",
            fontsize=6.5, fontweight="bold", color=_INK,
        )
        ax.annotate(
            f"{v:+.1f}%", xy=(v, i), xytext=(4 if v >= 0 else -4, 0),
            textcoords="offset points", va="center",
            ha="left" if v >= 0 else "right",
            fontsize=6, color=_SECONDARY,
        )
    cur.gap(height + 0.012)
    if len(marks) > len(shown):
        cur.line(
            f"… and {len(marks) - len(shown)} more scored names.", size=7.5, color=_MUTED
        )


def _scorecard_rows(card: Scorecard) -> list[list[str]]:
    """One row per matured horizon — signed percents throughout (+3.4% / -1.2%),
    the same columns and ordering as the digest's Scorecard table. A horizon
    that has not cleared the min-sample gate shows its count but withholds the
    medians ('—'), never a misleading one- or two-name median."""
    rows: list[list[str]] = []
    for c in card.cohorts:
        if c.enough:
            rows.append(
                [
                    _clip(c.label, 30),
                    str(c.n),
                    _pct(c.median_return),
                    _pct(c.median_spy),
                    _pct(c.median_alpha),
                    f"{c.beat_spy}/{c.n}",
                ]
            )
        else:
            rows.append([_clip(c.label, 30), str(c.n), "—", "—", "—", "—"])
    return rows


def _scorecard_overall_line(card: Scorecard) -> tuple[str, str]:
    """The roll-up line and its tone. The headline is the LONGEST horizon that
    clears the min-sample gate (the most-seasoned honest read): a non-negative
    median excess wears the quiet ok tone, a negative one the critical tone,
    and 'no horizon seasoned enough yet' the muted tone. The coverage tail
    keeps pending/unpriceable counts visible so absence of signal never reads
    as absence of data."""
    coverage = f"{card.overall_n} matured to ≥1 horizon"
    if card.pending:
        coverage += f", {card.pending} maturing"
    if card.unpriceable:
        coverage += f", {card.unpriceable} unpriceable"
    if card.overall_label:
        text = (
            f"Best-seasoned read ({card.overall_label}): median excess "
            f"{_pct(card.overall_median_alpha)} vs SPY, "
            f"{card.overall_beat_spy}/{card.overall_horizon_n} beat SPY · {coverage}."
        )
        tone = _SECONDARY if card.overall_median_alpha >= 0 else _CRITICAL
        return text, tone
    return f"No horizon has {MIN_SAMPLE}+ matured names yet · {coverage}.", _MUTED


def _scorecard_empty_line(card: Scorecard | None) -> str:
    """The empty-state line, distinguishing absence of signal from absence of
    data three ways (mirrors the digest): names still maturing toward the
    4-week mark (pending), a price fetch down this run (unpriceable), or simply
    nothing eligible yet."""
    if card and card.pending:
        tail = (
            f" ({card.unpriceable} could not be priced this run.)" if card.unpriceable else ""
        )
        return (
            f"No proposal has reached the 4-week mark yet — {card.pending} name(s) are "
            f"priced and maturing; grading begins at 4 weeks.{tail}"
        )
    if card and card.unpriceable:
        return (
            f"Price data was unavailable for all {card.unpriceable} eligible past "
            "proposal(s) this run — scoring resumes when it returns."
        )
    return _SCORECARD_EMPTY


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
            if tallies and source is not Source.FRED:
                # Zero rows on a run that fetched anything = never wired in
                # (no API key / contact email) — permanent degradation belongs
                # in the report, not just a CLI echo. FRED is wired by
                # macro.yaml, not a secret — nothing to disclose when absent.
                lines.append(
                    (f"{source.value}: not consulted — no key configured or nothing required it", _MUTED)
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


# Page-space rails: a matplotlib figure silently draws past the page edge, so
# every flowing block is capped with a disclosed overflow line (the markdown
# digest and the store always carry the full record).
_MAX_CHANGES_LINES = 30
_MAX_BELLWETHER_LINES = 14
_MAX_QUARANTINE_LINES = 12


_PAGE_FLOOR = 0.075  # the footer's airspace — no block line may enter it


def _block(cur: _Cursor, title: str, lines: list[tuple[str, str]], cap: int) -> None:
    """A titled list with two overflow rails: the block's own cap, and the
    page floor — whichever bites first, the truncation is disclosed (the
    markdown digest always carries the full record)."""
    if cur.y < _PAGE_FLOOR + 0.05:  # no room for a header plus one line
        return
    cur.line(title, size=11, weight="bold")
    cur.gap(0.008)
    shown = lines[:cap]
    truncated_at: int | None = None
    for i, (text, tone) in enumerate(shown):
        if cur.y < _PAGE_FLOOR + 0.018 and i < len(shown) - 1:
            truncated_at = i
            break
        cur.line(text, size=8.5, color=tone)
    hidden = (len(lines) - truncated_at) if truncated_at is not None else len(lines) - len(shown)
    if hidden > 0 and cur.y >= _PAGE_FLOOR:
        cur.line(
            f"… and {hidden} more — the markdown digest carries the full list.",
            size=8.5, color=_MUTED,
        )
    cur.gap(0.014)


def _watch_news(fig: Figure, cur: _Cursor, report: RunReport, history: History) -> None:
    """Watch page 1 — what the reader opens the report for: the macro
    dashboard (stat tiles + sparklines), then every change event, then the
    Radar (discovery funnel)."""
    macro = [t for t in report.tickers if t.context.macro is not None]
    if macro:
        _macro_dashboard(fig, cur, macro, history)
    _block(cur, "Changes", _changes_pdf_lines(report), _MAX_CHANGES_LINES)
    radar_lines = _radar_pdf_lines(report)
    if radar_lines:
        _block(cur, "Radar", radar_lines, 22)
    if report.etf_rebalances:
        _block(cur, "ETF rebalancing (holdings, unverified)", _etf_rebalance_pdf_lines(report), 16)
    bellwether_lines = _bellwether_pdf_lines(report)
    if bellwether_lines:
        _block(cur, "Bellwether earnings (finnhub, unverified)", bellwether_lines,
               _MAX_BELLWETHER_LINES)


def _masthead(fig: Figure, cur: _Cursor, report: RunReport) -> None:
    """The issue's front-page identity: name left, date right, hairline under.
    Run number and regeneration live in the footer — a masthead is not a log
    line."""
    import matplotlib.lines as mlines

    fig.text(0.07, cur.y, "THE ARGUS DAILY", ha="left", va="top",
             fontsize=19, fontweight="bold", color=_INK)
    fig.text(0.93, cur.y - 0.004, report.as_of.strftime("%A · %B %-d, %Y"),
             ha="right", va="top", fontsize=9.5, color=_SECONDARY)
    cur.gap(0.036)
    fig.add_artist(
        mlines.Line2D([0.07, 0.93], [cur.y, cur.y], transform=fig.transFigure,
                      color=_INK, linewidth=1.1)
    )
    cur.gap(0.012)


def _macro_dashboard(
    fig: Figure, cur: _Cursor, macro: Sequence[TickerReport], history: History
) -> None:
    """Stat tiles, four per row: small label, BIG value, colored Δ vs the
    baseline run, and a 30-day sparkline for market series (ungated display
    data, same policy as the ticker charts). Econ prints show their period
    instead — a monthly series barely sparkles. Sanity violations and crossed
    lines flag in the caption slot; the number always dominates."""
    order = {symbol: i for i, symbol in enumerate(_TILE_ORDER)}
    tiles = sorted(
        macro,
        key=lambda t: (order.get(t.context.ticker, 99), t.context.macro.label),
    )
    columns = 4
    tile_w = 0.86 / columns
    tile_h = 0.088
    top = cur.y
    for i, ticker in enumerate(tiles):
        row, col = divmod(i, columns)
        x = 0.07 + col * tile_w
        y = top - row * tile_h
        _stat_tile(fig, ticker, history, x=x, y=y, width=tile_w - 0.012)
    rows = -(-len(tiles) // columns)
    cur.y = top - rows * tile_h
    spread = _digest_spread_lines(list(macro))
    if spread:
        text = spread[0].removeprefix("- ").replace("_(", "(").replace(")_", ")")
        cur.line(text, size=8, color=_SECONDARY)
    cur.gap(0.012)


def _stat_tile(
    fig: Figure, ticker: TickerReport, history: History, *, x: float, y: float, width: float
) -> None:
    spec = ticker.context.macro
    assert spec is not None
    fig.text(x, y, spec.label, ha="left", va="top", fontsize=7, color=_SECONDARY)
    snapshot = ticker.snapshot
    fv = snapshot.values.get(spec.value_field) if snapshot is not None else None
    if fv is None or not isinstance(fv.value, (int, float)):
        hits = snapshot.quarantined.get(spec.value_field) if snapshot else None
        fig.text(x, y - 0.014, "—", ha="left", va="top", fontsize=13, color=_MUTED)
        fig.text(x, y - 0.034, "quarantined" if hits else "no data",
                 ha="left", va="top", fontsize=6.5, color=_CRITICAL if hits else _MUTED)
        return
    value_text = f"{fv.value:,.{spec.decimals}f}{spec.unit}"
    fig.text(x, y - 0.012, value_text, ha="left", va="top",
             fontsize=13.5, fontweight="bold", color=_INK)
    delta = _tile_delta(ticker, fv, spec)
    if delta is not None:
        tone = _UP if delta > 0 else _CRITICAL if delta < 0 else _SECONDARY
        fig.text(x, y - 0.031, f"{delta:+,.{spec.decimals}f}", ha="left", va="top",
                 fontsize=8, color=tone)
    caption: tuple[str, str] | None = None
    if spec.sanity is not None and not (spec.sanity[0] <= fv.value <= spec.sanity[1]):
        caption = ("⚠ check units", _CRITICAL)
    elif any(
        r.status == "holds"
        for r in evaluate_thesis_checks(spec.alert_when, snapshot)
    ):
        caption = ("⚠ line crossed", _CRITICAL)
    elif spec.source == Source.FRED and fv.observed_at is not None:
        caption = (f"period {fv.observed_at.date().isoformat()}", _MUTED)
    if caption is not None:
        fig.text(x, y - 0.045, caption[0], ha="left", va="top", fontsize=6.5,
                 color=caption[1])
    points = history.get(ticker.context.ticker)
    if points and len(points) >= 2 and spec.source != Source.FRED:
        _price_strip(fig, points, x=x, y=y - 0.075, width=width, height=0.024)


def _price_strip(
    fig: Figure,
    points,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    labels: bool = False,
) -> None:
    """A single-series time strip per the house chart specs: thin line in the
    series hue, subtle area fill, and — when `labels` — selective context in
    text tokens (start/end values, min/max at their points), never a label
    per point and never text in the series color."""
    values = [p[1] for p in points]
    if len(values) < 2:
        return
    ax = fig.add_axes((x, y, width, height))
    ax.axis("off")
    xs = range(len(values))
    floor = min(values)
    ax.fill_between(xs, values, floor, color=_SERIES, alpha=0.10, linewidth=0)
    ax.plot(xs, values, color=_SERIES, linewidth=1.0)
    ax.plot([len(values) - 1], [values[-1]], "o", color=_SERIES, markersize=2)
    ax.margins(x=0.015, y=0.18)
    if not labels:
        return
    lo_i = min(xs, key=lambda i: values[i])
    hi_i = max(xs, key=lambda i: values[i])
    ax.annotate(f"{values[0]:,.2f}", xy=(0, values[0]), xytext=(-2, 8),
                textcoords="offset points", ha="left", fontsize=6, color=_SECONDARY)
    ax.annotate(f"{values[-1]:,.2f}", xy=(len(values) - 1, values[-1]), xytext=(2, 6),
                textcoords="offset points", ha="right", fontsize=6.5,
                fontweight="bold", color=_INK)
    if hi_i not in (0, len(values) - 1):
        ax.annotate(f"{values[hi_i]:,.2f}", xy=(hi_i, values[hi_i]), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=5.5, color=_MUTED)
    if lo_i not in (0, len(values) - 1):
        ax.annotate(f"{values[lo_i]:,.2f}", xy=(lo_i, values[lo_i]), xytext=(0, -9),
                    textcoords="offset points", ha="center", fontsize=5.5, color=_MUTED)


def _tile_delta(ticker: TickerReport, fv, spec) -> float | None:
    baseline = ticker.baseline
    if baseline is None:
        return None
    old = baseline.values.get(spec.value_field)
    if old is None or not isinstance(old.value, (int, float)):
        return None
    delta = round(fv.value - old.value, spec.decimals)
    return delta if delta != 0 else None


def _sector_chart(fig: Figure, cur: _Cursor, wire) -> None:
    """The rotation, visible from across the room: one horizontal bar per
    canonical sector, median last-session change, red/green with the sign
    printed too (never color alone)."""
    pulses = list(wire.sectors)
    if not pulses:
        return
    cur.line("Sector pulse", size=11, weight="bold")
    cur.gap(0.006)
    height = 0.018 * len(pulses)
    ax = fig.add_axes((0.30, cur.y - height, 0.52, height))
    values = [p.median_change_pct for p in pulses][::-1]
    labels = [f"{p.sector}  ({p.n})" for p in pulses][::-1]
    colors = [_UP if v > 0 else _CRITICAL if v < 0 else _BASELINE for v in values]
    ax.barh(range(len(values)), values, color=colors, height=0.62)
    ax.axvline(0, color=_BASELINE, linewidth=0.8)
    ax.set_yticks(range(len(values)))
    ax.set_yticklabels(labels, fontsize=7, color=_INK)
    ax.tick_params(axis="y", length=0)
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    biggest = max(abs(v) for v in values) or 1.0
    ax.set_xlim(-biggest * 1.35, biggest * 1.35)
    for i, v in enumerate(values):
        ax.annotate(
            f"{v:+.1f}%",
            xy=(v, i),
            xytext=(4 if v >= 0 else -4, 0),
            textcoords="offset points",
            va="center",
            ha="left" if v >= 0 else "right",
            fontsize=7,
            color=_INK,
        )
    cur.gap(height + 0.014)


def _movers_chart(fig: Figure, cur: _Cursor, wire) -> None:
    """Top movers as diverging bars: gainers up top in green, losers below in
    red, company names annotated — the day's outliers at a glance."""
    movers = [*wire.gainers, *wire.losers]
    if not movers:
        cur.line("Market movers", size=11, weight="bold")
        cur.gap(0.006)
        cur.line("No large-cap gainers or losers last session — a flat tape is information.",
                 size=8.5, color=_SECONDARY)
        cur.gap(0.012)
        return
    cur.line("Market movers", size=11, weight="bold")
    cur.gap(0.006)
    height = 0.018 * len(movers)
    ax = fig.add_axes((0.12, cur.y - height, 0.76, height))
    values = [m.change_pct for m in movers][::-1]
    labels = [m.symbol for m in movers][::-1]
    names = [(m.company or m.sector) for m in movers][::-1]
    colors = [_UP if v > 0 else _CRITICAL for v in values]
    ax.barh(range(len(values)), values, color=colors, height=0.6)
    ax.axvline(0, color=_BASELINE, linewidth=0.8)
    ax.set_yticks([])
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    biggest = max(abs(v) for v in values) or 1.0
    ax.set_xlim(-biggest * 1.75, biggest * 1.75)
    for i, (v, label, name) in enumerate(zip(values, labels, names)):
        # Symbol on the empty side of the zero line; value + name at the tip.
        ax.annotate(
            label,
            xy=(0, i),
            xytext=(-5 if v >= 0 else 5, 0),
            textcoords="offset points",
            va="center",
            ha="right" if v >= 0 else "left",
            fontsize=7.5, fontweight="bold", color=_INK,
        )
        ax.annotate(
            f"{v:+.1f}%  {_clip(name, 24)}",
            xy=(v, i),
            xytext=(4 if v >= 0 else -4, 0),
            textcoords="offset points",
            va="center",
            ha="left" if v >= 0 else "right",
            fontsize=6.5, color=_SECONDARY,
        )
    cur.gap(height + 0.014)


def _etf_rebalance_pdf_lines(report: RunReport) -> list[tuple[str, str]]:
    """Constituent adds (green) / drops (red) per changed ETF — forced-flow
    signal, claims-labeled."""
    from argus.etf import is_nport_etf

    lines: list[tuple[str, str]] = []
    for r in report.etf_rebalances:
        tag = " · N-PORT (lagged)" if is_nport_etf(r.etf) else ""
        if r.added:
            lines.append((_clip(f"{r.etf} added: {', '.join(r.added)}{tag}", 118), _UP))
        if r.dropped:
            lines.append((_clip(f"{r.etf} dropped: {', '.join(r.dropped)}{tag}", 118), _CRITICAL))
    return lines


def _radar_pdf_lines(report: RunReport) -> list[tuple[str, str]]:
    """The discovery funnel via the digest's own renderers: shortlist strip,
    mechanical crossings against the wire, and considered names."""
    lines: list[tuple[str, str]] = []
    if report.radar:
        lines.append(("On the shortlist (scout):", _SECONDARY))
        lines += [
            (f"   #{p.rank} {p.ticker} — {p.sector}, streak {p.streak}w", _INK)
            for p in report.radar
        ]
    for hit in _digest_radar_crossings(report.radar, report.market):
        lines.append((_clip(hit.removeprefix("- "), 114), _CRITICAL))
    for hit in _digest_radar_insider_crossings(report):
        lines.append((_clip(hit.removeprefix("- "), 114), _UP))
    considering = [
        t for t in report.tickers
        if t.context.macro is None and t.context.tier == "consider"
    ]
    if considering:
        lines.append(("Considering (promote with a thesis to graduate):", _SECONDARY))
        lines += [
            (_clip("   " + _digest_considering_line(t).removeprefix("- "), 114), _INK)
            for t in considering
        ]
    return lines


def _watch_status_page(report: RunReport) -> Figure:
    """Watch page 2 — the standing state: watchlist table, quarantine table,
    data health. Mirrors the markdown's back half."""
    fig = plt.figure(figsize=_PAGE)
    cur = _Cursor(fig)
    _watch_summary(fig, cur, report)
    cur.gap(0.016)
    if report.market is not None:
        _block(cur, "New 52-week extremes", _extremes_pdf_lines(report.market), 20)
    _block(cur, "Data quarantined", _quarantine_pdf_lines(report), _MAX_QUARANTINE_LINES)
    _block(cur, "Data health", _health_lines(report), 12)
    _page_footer(fig, report)
    return fig


def _market_wire_page(report: RunReport) -> Figure:
    """The magazine's market pages: movers and sector rotation as charts,
    the earnings wire as a colored list — claims-labeled, curated by the
    mechanical rules in market.py. The 52-week extremes live on the state
    page, where there is room to list them all."""
    fig = plt.figure(figsize=_PAGE)
    cur = _Cursor(fig)
    cur.line("The market, last session", size=13, weight="bold")
    cur.gap(0.014)
    wire = report.market
    assert wire is not None  # build_pdf only routes here when present
    _movers_chart(fig, cur, wire)
    _sector_chart(fig, cur, wire)
    _block(cur, "Earnings wire", _earnings_wire_pdf_lines(wire), 20)
    cur.line(
        "Claims-labeled context (tradingview + finnhub), mechanical curation — "
        "never gated, never a delivery trigger.",
        size=8, color=_MUTED, style="italic",
    )
    _page_footer(fig, report)
    return fig


def _earnings_wire_pdf_lines(wire) -> list[tuple[str, str]]:
    if not wire.earnings_reported and not wire.earnings_upcoming:
        return [("No large-cap earnings reported or scheduled in the window.", _SECONDARY)]
    lines: list[tuple[str, str]] = []
    if wire.earnings_reported:
        lines.append(("Reported:", _SECONDARY))
        for b in wire.earnings_reported:
            text = f"   {b.symbol} ({b.report_date.isoformat()}): EPS {b.eps_actual:.2f}"
            tone = _INK
            if b.eps_estimate is not None:
                text += f" vs {b.eps_estimate:.2f} est"
                surprise = _eps_surprise_pct(b.eps_actual, b.eps_estimate)
                if surprise is not None:
                    text += f" ({surprise:+.1f}%)"
                    tone = _UP if surprise > 0 else _CRITICAL if surprise < 0 else _INK
            lines.append((text, tone))
    if wire.earnings_upcoming:
        lines.append(("Upcoming:", _SECONDARY))
        for b in wire.earnings_upcoming:
            when = b.report_date.isoformat() + (f" {b.hour}" if b.hour else "")
            text = f"   {b.symbol} — {when}"
            if b.eps_estimate is not None:
                text += f" (est {b.eps_estimate:.2f})"
            lines.append((text, _INK))
        if wire.earnings_more_upcoming:
            lines.append(
                (f"   … and {wire.earnings_more_upcoming} more large caps this week.", _MUTED)
            )
    return lines


def _extremes_pdf_lines(wire) -> list[tuple[str, str]]:
    if not wire.highs and not wire.lows:
        return [("No large caps at 52-week marks last session.", _SECONDARY)]
    lines: list[tuple[str, str]] = []
    for title, extremes, tone in (
        ("At highs:", wire.highs, _UP),
        ("At lows:", wire.lows, _CRITICAL),
    ):
        if not extremes:
            continue
        lines.append((title, _SECONDARY))
        for e in extremes:
            name = f" — {e.company}" if e.company else ""
            lines.append((_clip(f"   {e.symbol} {e.close:.2f}{name}", 114), tone))
    return lines


_FEATURED_TITLE = "Worth reading about"
_FEATURED_CAPTION = (
    "Selection is mechanical — top two movers each way, two largest upcoming "
    "reporters. All numbers are yahoo claims, unverified."
)


def _featured_page(
    report: RunReport,
    history: History,
    cards,
    *,
    first: bool = True,
    title: str = _FEATURED_TITLE,
    caption: str = _FEATURED_CAPTION,
) -> Figure:
    """Reading cards — who the companies are, why they surfaced (mechanical
    rules), their claimed numbers, and a 1-year price strip (raw history,
    ungated — captioned as such). Three cards per page; further cards continue
    on the next. Reused for the Daily's featured stocks and the scout's
    broader-lens reading cards (title/caption vary)."""
    fig = plt.figure(figsize=_PAGE)
    cur = _Cursor(fig)
    cur.line(title + ("" if first else " (continued)"), size=13, weight="bold")
    cur.gap(0.006)
    if first:
        cur.wrapped(caption, size=8, color=_MUTED, style="italic", width=124, max_lines=2)
    cur.gap(0.014)
    for card in cards:
        if cur.y < _PAGE_FLOOR + 0.26:  # a full card's height — no partial cards
            break
        title = card.symbol + (f" — {card.name}" if card.name else "")
        cur.line(_clip(title, 90), size=11.5, weight="bold")
        cur.gap(0.002)
        cur.line(card.why + ".", size=8.5, color=_SECONDARY, style="italic")
        fact_lines = _digest_feature_fact_lines(card)
        for fact_line in fact_lines:
            tone = _SECONDARY if fact_line.startswith("street:") else _INK
            cur.line(_clip(fact_line, 120), size=8.5, color=tone)
        if card.summary:
            cur.gap(0.004)
            cur.wrapped(card.summary, size=8.5, color=_SECONDARY, width=110, max_lines=5)
        points = history.get(card.symbol)
        if points and len(points) >= 2 and cur.y - 0.075 > _PAGE_FLOOR:
            chart_h = 0.055
            cur.gap(0.010)  # headroom for the max label
            _price_strip(
                fig, points, x=0.07, y=cur.y - chart_h, width=0.86, height=chart_h,
                labels=True,
            )
            fig.text(0.07, cur.y - chart_h - 0.012, "1y — raw yahoo history, ungated",
                     ha="left", va="top", fontsize=6, color=_MUTED)
            cur.gap(chart_h + 0.020)
        cur.gap(0.018)
    _page_footer(fig, report)
    return fig


def _sorted_watch_then_macro(report: RunReport) -> list[TickerReport]:
    """The digest's Changes ordering: equities first, then macro, each
    alphabetical."""
    watch = sorted(
        (t for t in report.tickers if t.context.macro is None), key=lambda t: t.context.ticker
    )
    macro = sorted(
        (t for t in report.tickers if t.context.macro is not None),
        key=lambda t: t.context.ticker,
    )
    return watch + macro


def _changes_pdf_lines(report: RunReport) -> list[tuple[str, str]]:
    """Every change event, rendered with the digest's own event lines —
    the counts-only summary this replaced buried the actual news."""
    lines: list[tuple[str, str]] = []
    ordered = _sorted_watch_then_macro(report)
    for ticker in ordered:
        if not ticker.events:
            continue
        lines.append((ticker.context.ticker, _INK))
        for event in ticker.events:
            text = _digest_event_line(event)
            lines.append(
                ("   " + _clip(text, 114), _CRITICAL if text.startswith("⚠") else _INK)
            )
    if not lines:
        lines.append(("No changes since last run.", _SECONDARY))
    firsts = [
        t.context.ticker for t in ordered if t.baseline_run_id is None and t.status != "failed"
    ]
    if firsts:
        lines.append(
            (_clip("Baseline established this run: " + ", ".join(firsts) + ".", 118), _SECONDARY)
        )
    return lines


def _bellwether_pdf_lines(report: RunReport) -> list[tuple[str, str]]:
    """Mirrors the digest's bellwether section (same numbers, same split) —
    claims-labeled context, never gated."""
    if not report.bellwethers:
        return []
    lines: list[tuple[str, str]] = []
    reported = sorted(
        (b for b in report.bellwethers if b.eps_actual is not None),
        key=lambda b: (b.report_date, b.symbol),
    )
    upcoming = sorted(
        (b for b in report.bellwethers if b.eps_actual is None),
        key=lambda b: (b.report_date, b.symbol),
    )
    if reported:
        lines.append(("Reported:", _SECONDARY))
        for b in reported:
            text = f"{b.symbol} ({b.report_date.isoformat()}): EPS {b.eps_actual:.2f}"
            if b.eps_estimate is not None:
                text += f" vs {b.eps_estimate:.2f} est"
                surprise = _eps_surprise_pct(b.eps_actual, b.eps_estimate)
                if surprise is not None:
                    text += f" ({surprise:+.1f}%)"
            lines.append(("   " + text, _INK))
    if upcoming:
        lines.append(("Upcoming:", _SECONDARY))
        for b in upcoming:
            when = b.report_date.isoformat() + (f" {b.hour}" if b.hour else "")
            text = f"{b.symbol} — {when}"
            if b.eps_estimate is not None:
                text += f" (est {b.eps_estimate:.2f})"
            lines.append(("   " + text, _INK))
    lines.append(("Context only — single-source claims, never a delivery trigger.", _MUTED))
    return lines


def _quarantine_pdf_lines(report: RunReport) -> list[tuple[str, str]]:
    """Every quarantined observation this run (the digest table's content),
    one line each — including legs coexisting with an accepted primary."""
    lines: list[tuple[str, str]] = []
    for ticker in sorted(report.tickers, key=lambda t: t.context.ticker):
        for q in sorted(
            ticker.quarantines, key=lambda q: (q.field.value, q.source.value, q.fetched_at)
        ):
            label = _FIELD_LABELS.get(q.field, q.field.value.replace("_", " "))
            reasons = "; ".join(f"{hit.code.value}: {hit.detail}" for hit in q.reasons)
            lines.append(
                (
                    _clip(f"{ticker.context.ticker} {label} ({q.source.value}): {reasons}", 118),
                    _CRITICAL,
                )
            )
    if not lines:
        return [("No data quarantined this run.", _SECONDARY)]
    return lines


def _watch_summary(fig: Figure, cur: _Cursor, report: RunReport) -> None:
    # Macro series live in the markdown digest's Macro section, not the
    # equity table (a PDF macro strip is a possible follow-up).
    tickers = sorted(
        (t for t in report.tickers if t.context.macro is None),
        key=lambda t: t.context.ticker,
    )
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
    if proposal is not None:
        _rank_trajectory(fig, proposal)

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
                f"gate-accepted): {claims}",
                size=8, color=_MUTED, style="italic", width=118, max_lines=2,
            )
        _peer_dotplot(fig, cur, proposal, snapshot)
        _evidence_contract(cur, proposal, ticker_report)
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
    cur.line("Metrics — gate-accepted, with provenance & evidence "
        "(\u2713 = corroborated by a second source)",
        size=9.5, weight="bold",
    )
    cur.gap(0.005)
    for text, tone in _metric_lines(ticker_report, proposal):
        cur.line(text, size=7.5, family="monospace", color=tone, step=0.0142)

    fig.text(0.5, 0.03, _UNGATED_FOOTER, ha="center", va="bottom", fontsize=8, color=_MUTED)
    return fig


# The charts occupy the page from y≈0.60 down; the evidence contract lives in
# the whitespace above them, and stops before it would collide (the markdown
# digest is canonical, so a rare truncation here loses no information).
_EVIDENCE_FLOOR = 0.615


def _evidence_contract(
    cur: _Cursor, proposal: ScoutProposal, ticker_report: TickerReport | None
) -> None:
    """The two per-name honesty blocks, drawn in the mid-page whitespace above
    the charts: the screen-exit conditions (the human's screen thresholds
    restated as the factual lines that would drop this name — the same for every
    proposal, shown per page because a reader reads one page at a time) and the
    data flags (factual, per-name observations about the evidence: claim-only or
    quarantined metrics, a single-source price, a value near a screen boundary).
    No forecast, no opinion — data vs the stated lines. Guarded by a floor so it
    never overruns the charts."""
    snapshot = ticker_report.snapshot if ticker_report is not None else None
    exits = screen_exit_conditions(proposal.screen_reasons)
    if exits and cur.y > _EVIDENCE_FLOOR + 0.03:
        cur.gap(0.008)
        cur.wrapped(
            "Screen exit — leaves the shortlist if: " + " · ".join(exits),
            size=7.5, color=_SECONDARY, width=125, max_lines=2,
        )
    flags = data_flags(proposal.screen_reasons, proposal.screener_metrics, snapshot)
    if flags and cur.y > _EVIDENCE_FLOOR + 0.02:
        cur.gap(0.006)
        cur.line("Data flags (factual):", size=8, weight="bold", color=_INK)
        for flag in flags:
            if cur.y <= _EVIDENCE_FLOOR:
                break  # out of room — the markdown carries the full list
            cur.wrapped(f"• {flag}", size=7.5, color=_SECONDARY, width=122, max_lines=2)


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
        cur.wrapped(profile.summary, size=8, color=_SECONDARY, width=118, max_lines=4)
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
        clauses.append(f"trades at {fwd:.1f}× forward earnings (gate-accepted)")
    growth = _verified_num(snapshot, Field.REVENUE_GROWTH)
    if growth is not None:
        clauses.append(f"with gate-accepted revenue growth of {growth * 100:+.1f}%")
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
        clauses.append("on gate-accepted " + " and ".join(quality))
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
        f"{proposal.ticker} (gate-accepted) {own:.1f}"
        if own is not None
        else f"{proposal.ticker} (gate-accepted) —"
    )
    parts = [head]
    if peers:
        parts.append(", ".join(peers))
    parts.append(own_text)
    return " · ".join(parts)


def _peer_dotplot(
    fig: Figure, cur: _Cursor, proposal: ScoutProposal, snapshot: Snapshot | None
) -> None:
    """The industry-peer valuation, drawn: each peer's screener forward P/E as
    a dot along a shared axis, the industry median as a dashed reference, and
    the candidate's OWN gate-verified forward P/E highlighted in the series
    hue — claim and verified value side by side, the screen's 'cheap for its
    growth' thesis made visual (lower/left = cheaper). Falls back to the text
    peer line when the data is thin or the page is out of room, so the context
    is never silently dropped."""
    ctx = proposal.peer_context or {}
    own = _verified_num(snapshot, Field.PE_FWD)
    peers: list[tuple[str, float]] = []
    for peer in tuple(ctx.get("peers") or ())[:8]:
        if isinstance(peer, Mapping) and _finite(peer.get("fwd_pe")):
            name = str(peer.get("ticker") or "").strip()
            if name:
                peers.append((_clip(name, 8), float(peer["fwd_pe"])))
    median = ctx.get("median_fwd_pe")
    industry = str(ctx.get("industry") or "").strip() or "industry"
    height = 0.052
    # Need the candidate's own verified value plus a reference (a peer or the
    # median) to draw anything meaningful; and enough room above the charts
    # (top at y=0.60). Otherwise the text line carries the same facts.
    if own is None or not (peers or _finite(median)) or cur.y - height - 0.03 < 0.60:
        line = _peer_line(proposal, snapshot)
        if line is not None:
            cur.gap(0.004)
            cur.wrapped(
                f"Industry peers (screener claims): {line}",
                size=8, color=_MUTED, style="italic", width=118, max_lines=2,
            )
        return

    cur.gap(0.006)
    cur.line(
        f"Valuation vs {_clip(industry, 44)} peers — forward P/E "
        "(screener claims; ● = verified)",
        size=8, color=_SECONDARY,
    )
    cur.gap(0.004)
    ax = fig.add_axes((0.10, cur.y - height, 0.80, height))
    xs = [pe for _, pe in peers] + [own] + ([float(median)] if _finite(median) else [])
    lo, hi = min(xs), max(xs)
    span = (hi - lo) or 1.0
    ax.set_xlim(lo - 0.14 * span, hi + 0.14 * span)
    ax.set_ylim(0, 1)
    ax.axhline(0.5, color=_GRID, linewidth=0.6, zorder=0)
    for name, pe in peers:
        ax.plot([pe], [0.5], "o", color=_BASELINE, markersize=6, zorder=2)
        ax.annotate(
            name, xy=(pe, 0.5), xytext=(0, 7), textcoords="offset points",
            ha="center", va="bottom", fontsize=5.5, color=_MUTED,
        )
    if _finite(median):
        m = float(median)
        ax.axvline(m, color=_SECONDARY, linewidth=0.8, linestyle=(0, (3, 2)), zorder=1)
        ax.annotate(
            f"median {m:g}", xy=(m, 0.0), ha="center", va="bottom",
            fontsize=5.5, color=_SECONDARY,
        )
    ax.plot([own], [0.5], "o", color=_SERIES, markersize=9, zorder=3)
    ax.annotate(
        f"{proposal.ticker} {own:.1f}", xy=(own, 0.5), xytext=(0, -11),
        textcoords="offset points", ha="center", va="top",
        fontsize=7, fontweight="bold", color=_INK,
    )
    ax.axis("off")
    cur.gap(height + 0.016)


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


def _rank_trajectory(fig: Figure, proposal: ScoutProposal) -> None:
    """A tiny sparkline of the candidate's screen rank across recent proposed
    weeks, tucked into the header's right-side whitespace under the subtitle.
    Rank 1 is pinned to the TOP (best), so a line rising = a name the screen
    likes more each week, falling = fading. Needs at least two proposed
    appearances; a name in its first week has no trajectory yet."""
    ranks = list(proposal.rank_history)
    if len(ranks) < 2:
        return
    # Sits directly under the subtitle ("screen rank N — … weeks"), which
    # already names it — so the strip carries only its endpoint ranks.
    x, y, w, h = 0.72, 0.908, 0.155, 0.016
    ax = fig.add_axes((x, y, w, h))
    xs = range(len(ranks))
    ax.plot(xs, ranks, color=_SERIES, linewidth=1.0)
    ax.plot([len(ranks) - 1], [ranks[-1]], "o", color=_SERIES, markersize=2.5)
    ax.invert_yaxis()  # rank 1 (best) at the top
    ax.margins(x=0.10, y=0.45)
    ax.axis("off")
    fig.text(x - 0.006, y + h / 2, f"#{ranks[0]}",
             ha="right", va="center", fontsize=6, color=_MUTED)
    fig.text(x + w + 0.006, y + h / 2, f"#{ranks[-1]}",
             ha="left", va="center", fontsize=6.5, color=_INK)


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


def _metric_lines(
    ticker_report: TickerReport | None, proposal: ScoutProposal | None = None
) -> list[tuple[str, str]]:
    """The evidence panel, one line per field in enum order, each carrying its
    four-state backing label: value + provenance + [corroborated]/[single-source]
    (ink), DATA QUARANTINED + reason (critical — the label carries the meaning,
    the color only flags it), '— screener-claimed only (unverified)' when the
    gates did not confirm a value the screener claimed (muted), or '— no data'
    (muted). A missing snapshot renders all-dash — disclosed, never crashed."""
    lines: list[tuple[str, str]] = []
    snapshot: Snapshot | None = ticker_report.snapshot if ticker_report else None
    screener_metrics = proposal.screener_metrics if proposal is not None else {}
    if ticker_report is None:
        lines.append(("No enrichment snapshot was recorded for this candidate.", _CRITICAL))
    elif snapshot is None:
        error = ticker_report.error or "unknown error"
        lines.append((_clip(f"Fetch failed — no data this run ({error}).", 100), _CRITICAL))
    for field in Field:
        if field is Field.ECON_VALUE:  # macro-only field — never on an equity panel
            continue
        label = _FIELD_LABELS.get(field, field.value.replace("_", " "))
        prefix = f"{label:<26}"  # widest label: 'Revenue growth (MRQ YoY)' = 24
        fv = snapshot.values.get(field) if snapshot else None
        if fv is not None:
            marks = "".join(f"  ✓{s.value}" for s in sorted(fv.corroborated_by))
            provenance = f"{fv.source.value}, {_ts(fv.fetched_at)}{marks}"
            tag = "[corroborated]" if fv.corroborated_by else "[single-source]"
            lines.append(
                (_clip(f"{prefix}{_fmt_value(field, fv.value):<14}{provenance}  {tag}", 118), _INK)
            )
            continue
        hits = snapshot.quarantined.get(field) if snapshot else None
        if hits:
            details = "; ".join(hit.detail for hit in hits)
            lines.append((_clip(f"{prefix}DATA QUARANTINED — {details}", 105), _CRITICAL))
            continue
        # Absent: distinguish a value the screener claimed but the gates never
        # confirmed (claim-only) from one no source ever offered (missing).
        if evidence_state(field, snapshot, screener_metrics) == "claim-only":
            lines.append((f"{prefix}— screener-claimed only (unverified this run)", _MUTED))
        else:
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


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
