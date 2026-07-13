"""PDF report rendering (pure): RunReport + price history → bytes.

The PDF is the digest's visual companion. Page 1 summarizes the run with the
same tri-state honesty as the markdown — a value the gates did not accept is
'—', never a blank and never a guess. Then one page per proposed scout
candidate (or per watched ticker) pairs a trailing-year price chart with a
panel of gate-verified metrics, each with its provenance stamp.

The chart is the ONE place ungated data appears anywhere in Argus: price
history arrives from the caller as plain (date, close) tuples, labeled
ungated display data, and is never persisted. Every chart page therefore
carries a footer saying exactly that — the reader must always know which
numbers earned trust and which are decoration.

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

from argus.fields import SPECS, Field
from argus.models import RunReport, ScoutProposal, Snapshot, TickerReport

# ticker → chronological (date, close) points for ~1y, or None when history
# could not be fetched. Plain tuples — NOT pandas — supplied by the caller
# and labeled as UNGATED display data (see module docstring).
History = Mapping[str, Sequence[tuple[date, float]] | None]

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

_UNGATED_FOOTER = "Price history: raw Yahoo data, ungated — tables above are gate-verified."

# Human-facing field names (ROE included — the PDF postdates the Quality-GARP
# fields). A Field without a label falls back to the enum value, never a
# KeyError in the report path.
_FIELD_LABELS: dict[Field, str] = {
    Field.PRICE: "Price",
    Field.MARKET_CAP: "Market cap",
    Field.PE_TTM: "P/E (TTM)",
    Field.PE_FWD: "Fwd P/E",
    Field.PEG: "PEG",
    Field.GROSS_MARGIN: "Gross margin",
    Field.OPERATING_MARGIN: "Operating margin",
    Field.ROE: "ROE",
    Field.DEBT_TO_EQUITY: "Debt/equity",
    Field.NEXT_EARNINGS_DATE: "Next earnings",
    Field.ANALYST_RATING: "Analyst rating",
    Field.ANALYST_TARGET_MEAN: "Analyst target (mean)",
    Field.ANALYST_COUNT: "Analyst count",
}

# Margins and ROE are stored as fractions, read as percents (same convention
# as the markdown digest).
_PERCENT_FIELDS = frozenset({Field.GROSS_MARGIN, Field.OPERATING_MARGIN, Field.ROE})


def build_pdf(report: RunReport, history: History) -> bytes:
    """RunReport + price history → PDF bytes. Pure; deterministic layout.

    Page 1 is the run summary (proposals + exclusions for scout, the
    watchlist table + event counts for watch). Then one detail page per
    proposed candidate / watched ticker, capped at _MAX_DETAIL_PAGES with
    the cap disclosed on page 1. Malformed or absent snapshot values render
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
    buffer = io.BytesIO()
    with PdfPages(buffer, metadata=metadata) as pdf:
        _save(pdf, _summary_page(report, total_details=len(subjects), shown_details=len(shown)))
        for ticker, ticker_report, proposal in shown:
            _save(pdf, _detail_page(report, ticker, ticker_report, proposal, history))
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
    snapshots = {t.context.ticker: t.snapshot for t in report.tickers}

    cur.line("Proposals", size=11, weight="bold")
    cur.gap(0.008)
    if not report.scout and report.status == "failed":
        # An outage is not a verdict: "evaluated nothing" must never read as
        # "nothing passed".
        cur.line("No candidates were evaluated this run — see the note above.", color=_SECONDARY)
    elif not proposed:
        cur.line("No candidates passed the screen and the quality gates this run.", color=_SECONDARY)
    else:
        columns = ["#", "Ticker", "Streak", "Price", "Fwd P/E", "Gross m.", "Op m.", "ROE", "D/E"]
        widths = [0.06, 0.15, 0.09, 0.12, 0.11, 0.12, 0.12, 0.11, 0.12]
        rows = [
            [
                str(p.rank),
                _clip(p.ticker, 10),
                _streak_cell(p.streak),
                _table_cell(snapshots.get(p.ticker), Field.PRICE),
                _table_cell(snapshots.get(p.ticker), Field.PE_FWD),
                _table_cell(snapshots.get(p.ticker), Field.GROSS_MARGIN),
                _table_cell(snapshots.get(p.ticker), Field.OPERATING_MARGIN),
                _table_cell(snapshots.get(p.ticker), Field.ROE),
                _table_cell(snapshots.get(p.ticker), Field.DEBT_TO_EQUITY),
            ]
            for p in proposed
        ]
        _table(fig, cur, columns, rows, widths)
        cur.gap(0.006)
        cur.line(
            "Every value above is gate-verified from this run's snapshots; "
            "'—' means the gates accepted nothing for that field.",
            size=8, color=_MUTED, style="italic",
        )

    cur.gap(0.02)
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
) -> None:
    """A minimal hairline table: horizontal rules only, recessive header,
    every row a fixed height so the layout never depends on a renderer."""
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
    for (row, _col), cell in table.get_celld().items():
        cell.set_height(1.0 / (len(rows) + 1))
        cell.set_edgecolor(_GRID)
        cell.set_linewidth(0.6)
        cell.set_facecolor("none")
        if row == 0:
            cell.set_text_props(color=_SECONDARY, fontweight="bold", fontsize=7.5)
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
) -> Figure:
    fig = plt.figure(figsize=_PAGE)
    fig.text(0.07, 0.955, ticker, ha="left", va="top", fontsize=16, fontweight="bold", color=_INK)
    fig.text(
        0.93, 0.952, _detail_subtitle(ticker_report, proposal),
        ha="right", va="top", fontsize=9.5, color=_SECONDARY,
    )

    cur = _Cursor(fig, y=0.915)
    if proposal is not None:
        cur.line(
            "Screen (screener claims — every value in the panel below is gate-verified):",
            size=8.5, color=_MUTED, style="italic",
        )
        cur.gap(0.002)
        for reason in list(proposal.screen_reasons.values())[:8]:
            cur.line(f"•  {_clip(reason, 110)}", size=8.5, color=_SECONDARY)
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

    _price_chart(fig, (0.08, 0.46, 0.86, 0.29), history.get(ticker))

    cur = _Cursor(fig, y=0.40)
    cur.line("Verified metrics (gate-accepted, with provenance)", size=9.5, weight="bold")
    cur.gap(0.006)
    for text, tone in _metric_lines(ticker_report):
        cur.line(text, size=8, family="monospace", color=tone, step=0.0165)

    fig.text(0.5, 0.03, _UNGATED_FOOTER, ha="center", va="bottom", fontsize=8, color=_MUTED)
    return fig


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
        x, y, w, h = rect
        fig.patches.append(
            Rectangle(
                (x, y), w, h, transform=fig.transFigure,
                facecolor=_PANEL, edgecolor=_GRID, linewidth=0.8,
            )
        )
        fig.text(
            x + w / 2, y + h / 2, "price history unavailable",
            ha="center", va="center", fontsize=11, color=_MUTED, fontstyle="italic",
        )
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
        prefix = f"{label:<23}"
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
    if field is Field.MARKET_CAP:
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
