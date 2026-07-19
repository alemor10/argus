"""The Sunday Edition — the week in one report, read straight off the store.

The append-only design pays for itself here: every event, observation, and
shortlist verdict of the week is already persisted, so the recap is pure
aggregation — no re-fetching, no re-deriving. Sections:

  - Your week in events: everything that fired across the week's watch runs,
    day-stamped. Re-fired standing states (a thesis still breached, a macro
    line still crossed — `newly=False`) are rolled up into one suppressed
    count instead of repeating five mornings in a row.
  - Macro, week over week: current level vs the last accepted value BEFORE
    the window opened — where slow drift the daily Δ can't show becomes
    visible.
  - Discovery: the week's scout shortlist with churn vs the prior run
    (entered / dropped) and the self-scoring scorecard.
  - The week ahead: the pinned bellwethers reporting next week. This ONE
    section is fetched at print time (Finnhub) and labeled so — it is next
    week's schedule, not this week's record, and is not archived.

Reproducibility: `argus recap --week-ending YYYY-MM-DD` regenerates the
edition from the store for any past week; only the week-ahead section is
print-time (disclosed in its caption and the decision log).
"""

import sqlite3
from collections.abc import Sequence
from datetime import date, timedelta

from argus.digest import _event_line
from argus.models import (
    CHANGE_EVENT_ADAPTER,
    BellwetherEarning,
    MacroPrint,
    MacroSpec,
    RecapEvent,
    RecapMacroLine,
    RecapReport,
)
from argus.store import queries


def build_recap(
    con: sqlite3.Connection,
    *,
    week_ending: date,
    week_ahead: Sequence[BellwetherEarning] = (),
    week_ahead_note: str | None = None,
) -> RecapReport:
    """Aggregate the seven days ending `week_ending` (inclusive) from the
    store. `week_ahead` rows are supplied by the caller (the CLI's one
    print-time calendar fetch) — this function itself never touches the
    network."""
    start = (week_ending - timedelta(days=6)).isoformat()
    end = (week_ending + timedelta(days=1)).isoformat()  # ISO strings compare lexicographically

    watch_runs = [
        row["run_id"]
        for row in con.execute(
            """SELECT run_id FROM runs
               WHERE kind = 'watch' AND status != 'running'
                 AND started_at >= ? AND started_at < ?
               ORDER BY run_id""",
            (start, end),
        )
    ]
    events, suppressed = _week_events(con, start, end)
    macro = _macro_week_over_week(con, watch_runs)
    scout_run, proposals, entered, dropped, scorecard = _discovery(con, start, end)
    return RecapReport(
        week_ending=week_ending,
        watch_runs=len(watch_runs),
        events=tuple(events),
        standing_suppressed=suppressed,
        macro=tuple(macro),
        scout_run_id=scout_run,
        proposals=proposals,
        entered=tuple(entered),
        dropped=tuple(dropped),
        scorecard=scorecard,
        week_ahead=tuple(week_ahead),
        week_ahead_note=week_ahead_note,
    )


def _week_events(
    con: sqlite3.Connection, start: str, end: str
) -> tuple[list[RecapEvent], int]:
    rows = con.execute(
        """SELECT e.ticker, e.payload, r.started_at
           FROM change_events e JOIN runs r ON r.run_id = e.run_id
           WHERE r.kind = 'watch' AND r.started_at >= ? AND r.started_at < ?
           ORDER BY e.run_id, e.event_id""",
        (start, end),
    ).fetchall()
    events: list[RecapEvent] = []
    suppressed = 0
    for row in rows:
        event = CHANGE_EVENT_ADAPTER.validate_json(row["payload"])
        if getattr(event, "newly", True) is False:
            suppressed += 1  # a standing state re-firing is a reminder, not the week's news
            continue
        # An unchanged macro re-print is not news: daily series (Fed funds
        # effective) print a new "period" every day at the same value, which
        # otherwise drowns the section. A genuine release (value moved, or a
        # first print with no prior) still fires. Rolled into the suppressed
        # count so the reader knows it was seen, not lost.
        if isinstance(event, MacroPrint) and event.delta is not None and event.delta == 0:
            suppressed += 1
            continue
        events.append(
            RecapEvent(
                day=date.fromisoformat(row["started_at"][:10]),
                ticker=row["ticker"],
                event=event,
            )
        )
    return events, suppressed


def _macro_week_over_week(
    con: sqlite3.Connection, watch_runs: Sequence[int]
) -> list[RecapMacroLine]:
    if not watch_runs:
        return []
    latest, first = watch_runs[-1], watch_runs[0]
    lines: list[RecapMacroLine] = []
    for row in con.execute(
        "SELECT ticker, macro FROM run_tickers WHERE run_id = ? AND macro IS NOT NULL "
        "ORDER BY ticker",
        (latest,),
    ):
        spec = MacroSpec.model_validate_json(row["macro"])
        ticker = row["ticker"]
        # The week's path: the accepted value in each of the week's watch runs,
        # oldest→newest — the sparkline, and the robust baseline source.
        path: list[float] = []
        for run_id in watch_runs:
            snapshot = queries.snapshot(con, run_id, ticker)
            fv = snapshot.values.get(spec.value_field) if snapshot else None
            if fv is not None and isinstance(fv.value, (int, float)):
                path.append(fv.value)
        if not path:
            continue
        current = path[-1]
        # "Week ago" = the last accepted value BEFORE the window's first run
        # (true week-over-week, the diff engine's baseline machinery). When a
        # series has no prior-week value yet — its first tracked week — fall
        # back to the week's OWN first print so the move is still shown, never
        # silently dropped to a bare level (the Jul-19 issue's failure).
        week_ago = queries.latest_accepted(con, ticker, spec.value_field, first)
        prior: float | None = None
        if week_ago is not None and isinstance(week_ago.value, (int, float)):
            prior = week_ago.value
        elif len(path) >= 2:
            prior = path[0]
        delta = round(current - prior, spec.decimals) if prior is not None else None
        lines.append(
            RecapMacroLine(
                label=spec.label,
                unit=spec.unit,
                decimals=spec.decimals,
                current=current,
                week_ago=prior,
                delta=delta,
                path=tuple(path),
            )
        )
    return sorted(lines, key=lambda line: line.label)


def _discovery(con: sqlite3.Connection, start: str, end: str):
    row = con.execute(
        """SELECT MAX(r.run_id) AS run_id FROM runs r
           WHERE r.kind = 'scout' AND r.status != 'running'
             AND r.started_at >= ? AND r.started_at < ?
             AND EXISTS (SELECT 1 FROM scout_candidates sc WHERE sc.run_id = r.run_id)""",
        (start, end),
    ).fetchone()
    scout_run = row["run_id"]
    if scout_run is None:
        return None, (), [], [], None
    report = queries.run_report(con, scout_run)
    proposed_now = [p.ticker for p in report.scout if p.status == "proposed"]
    prior = con.execute(
        """SELECT MAX(r.run_id) AS run_id FROM runs r
           WHERE r.kind = 'scout' AND r.run_id < ?
             AND EXISTS (SELECT 1 FROM scout_candidates sc WHERE sc.run_id = r.run_id)""",
        (scout_run,),
    ).fetchone()["run_id"]
    proposed_before: set[str] = set()
    if prior is not None:
        proposed_before = {
            r["ticker"]
            for r in con.execute(
                "SELECT ticker FROM scout_candidates WHERE run_id = ? AND status = 'proposed'",
                (prior,),
            )
        }
    entered = [t for t in proposed_now if t not in proposed_before]
    dropped = sorted(proposed_before - set(proposed_now))
    return scout_run, report.scout, entered, dropped, report.scorecard


# --- Rendering ----------------------------------------------------------------


def render_recap(recap: RecapReport) -> str:
    """RecapReport → markdown (the on-disk record; the PDF is the delivery)."""
    sections = [_header(recap), _events_section(recap), _macro_section(recap)]
    sections.append(_discovery_section(recap))
    if recap.scorecard is not None:
        sections.append(_scorecard_section(recap))
    sections.append(_week_ahead_section(recap))
    sections.append(
        [
            "---",
            "",
            f"Regenerate this edition: `argus recap --week-ending "
            f"{recap.week_ending.isoformat()}` (the week-ahead section is print-time).",
        ]
    )
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def _header(recap: RecapReport) -> list[str]:
    lines = [f"# Argus Sunday Edition — week ending {recap.week_ending.isoformat()}", ""]
    lines.append(
        f"{recap.watch_runs} watch run(s) this week"
        + (f"; scout run {recap.scout_run_id}." if recap.scout_run_id else "; no scout run.")
    )
    return lines


def _events_section(recap: RecapReport) -> list[str]:
    lines = ["## Your week in events", ""]
    if not recap.events:
        lines.append("No events fired this week — a quiet week is information.")
    else:
        by_ticker: dict[str, list[RecapEvent]] = {}
        for item in recap.events:
            by_ticker.setdefault(item.ticker, []).append(item)
        for ticker in sorted(by_ticker):
            lines += [f"### {ticker}", ""]
            lines += [
                f"- {item.day.isoformat()} — {_event_line(item.event)}"
                for item in by_ticker[ticker]
            ]
            lines.append("")
    if recap.standing_suppressed:
        lines.append(
            f"_{recap.standing_suppressed} routine reminder(s) rolled up "
            "(still-breached lines, in-window earnings reminders, unchanged daily prints)._"
        )
    return lines


def _macro_section(recap: RecapReport) -> list[str]:
    lines = ["## Macro — week over week", ""]
    if not recap.macro:
        lines.append("No macro series recorded this week.")
        return lines
    for line in recap.macro:
        text = f"- {line.label}: {line.current:.{line.decimals}f}{line.unit}"
        if line.delta is not None and line.week_ago is not None:
            text += (
                f" (Δ {line.delta:+.{line.decimals}f} over the week, "
                f"from {line.week_ago:.{line.decimals}f}{line.unit})"
            )
        lines.append(text)
    return lines


def _discovery_section(recap: RecapReport) -> list[str]:
    lines = ["## Discovery — the shortlist this week", ""]
    if recap.scout_run_id is None:
        lines.append("No scout run this week.")
        return lines
    if recap.entered:
        lines.append("NEW to the list: " + ", ".join(recap.entered) + ".")
    if recap.dropped:
        lines.append("Dropped off: " + ", ".join(recap.dropped) + ".")
    if not recap.entered and not recap.dropped:
        lines.append("No churn — the shortlist held steady.")
    lines.append("")
    proposed = [p for p in recap.proposals if p.status == "proposed"]
    lines += [
        f"- #{p.rank} {p.ticker} ({p.sector}, streak {p.streak})" for p in proposed
    ]
    return lines


def _scorecard_section(recap: RecapReport) -> list[str]:
    card = recap.scorecard
    lines = ["## Scorecard — past proposals vs SPY", ""]
    if card.overall_n == 0:
        lines.append("No proposal has had time to play out yet — the forward log is young.")
        return lines
    lines.append(
        f"{card.overall_n} names ever proposed — median α "
        f"{card.overall_median_alpha * 100:+.1f}%, {card.overall_beat_spy}/{card.overall_n} "
        "beat SPY."
        + (f" ({card.unpriceable} unpriceable, excluded)" if card.unpriceable else "")
    )
    return lines


def _week_ahead_section(recap: RecapReport) -> list[str]:
    lines = ["## The week ahead (finnhub, print-time — not archived)", ""]
    if not recap.week_ahead:
        lines.append(recap.week_ahead_note or "No pinned bellwethers report next week.")
        return lines
    for b in sorted(recap.week_ahead, key=lambda b: (b.report_date, b.symbol)):
        when = b.report_date.isoformat() + (f" {b.hour}" if b.hour else "")
        line = f"- {b.symbol} — {when}"
        if b.eps_estimate is not None:
            line += f" (est {b.eps_estimate:.2f})"
        lines.append(line)
    if recap.week_ahead_note:
        lines += ["", f"_{recap.week_ahead_note}_"]
    return lines


def build_recap_pdf(recap: RecapReport) -> bytes:
    """The Sunday Edition as a PDF — the delivered artifact (PDF-first), and a
    proper week-in-review issue in the magazine's visual language (v1.18): a
    masthead, a macro scoreboard (level + colored week-over-week move + the
    week's sparkline, biggest moves first), the week's events, then the
    discovery page (shortlist churn + the scorecard's per-name α-vs-SPY bars)
    and the week ahead. Pure/deterministic: no clock, CreationDate suppressed.
    """
    import io

    import matplotlib.lines as mlines
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    from argus.report_pdf import (
        _CRITICAL,
        _INK,
        _MUTED,
        _SECONDARY,
        _UP,
        _Cursor,
        _block,
        _clip,
        _price_strip,
        _scorecard_chart,
    )

    def _masthead(fig, cur, *, subtitle: str) -> None:
        fig.text(0.07, cur.y, "THE ARGUS SUNDAY EDITION", ha="left", va="top",
                 fontsize=17, fontweight="bold", color=_INK)
        fig.text(0.93, cur.y - 0.004, f"week ending {recap.week_ending.isoformat()}",
                 ha="right", va="top", fontsize=9.5, color=_SECONDARY)
        cur.gap(0.034)
        fig.add_artist(mlines.Line2D([0.07, 0.93], [cur.y, cur.y],
                                     transform=fig.transFigure, color=_INK, linewidth=1.1))
        cur.gap(0.012)
        cur.line(subtitle, size=8.5, color=_MUTED)
        cur.gap(0.012)

    def _macro_tile(fig, line: RecapMacroLine, *, x: float, y: float, width: float) -> None:
        fig.text(x, y, line.label, ha="left", va="top", fontsize=7, color=_SECONDARY)
        fig.text(x, y - 0.013, f"{line.current:,.{line.decimals}f}{line.unit}",
                 ha="left", va="top", fontsize=12.5, fontweight="bold", color=_INK)
        if line.delta is not None:
            tone = _UP if line.delta > 0 else _CRITICAL if line.delta < 0 else _SECONDARY
            fig.text(x, y - 0.031, f"{line.delta:+,.{line.decimals}f}{line.unit} wk",
                     ha="left", va="top", fontsize=7.5, color=tone)
        else:
            fig.text(x, y - 0.031, "no prior week", ha="left", va="top",
                     fontsize=7, color=_MUTED)
        if len(line.path) >= 2:
            _price_strip(fig, list(enumerate(line.path)),
                         x=x, y=y - 0.072, width=width, height=0.022)

    def _scoreboard(fig, cur) -> None:
        cur.line("The week in macro", size=11, weight="bold")
        cur.gap(0.003)
        if not recap.macro:
            cur.line("No macro series recorded this week.", size=9, color=_SECONDARY)
            cur.gap(0.010)
            return
        cur.line("Level · week-over-week move · the week's path — biggest moves first.",
                 size=7.5, color=_MUTED, style="italic")
        cur.gap(0.014)

        def pct(line: RecapMacroLine) -> float:
            # Rank by proportional move so a heterogeneous board is comparable
            # (a 16% VIX pop leads a 1.6% S&P drift); unmovable/no-prior last.
            if line.delta is None or not line.week_ago:
                return -1.0
            return abs(line.delta / line.week_ago)

        ordered = sorted(recap.macro, key=lambda line: (-pct(line), line.label))
        columns = 4
        tile_w = 0.86 / columns
        tile_h = 0.092
        top = cur.y
        for i, line in enumerate(ordered):
            row, col = divmod(i, columns)
            _macro_tile(fig, line, x=0.07 + col * tile_w, y=top - row * tile_h,
                        width=tile_w - 0.014)
        rows = -(-len(ordered) // columns)
        cur.y = top - rows * tile_h
        cur.gap(0.008)

    def _events_block(cur) -> None:
        lines: list[tuple[str, str]] = []
        by_ticker: dict[str, list[RecapEvent]] = {}
        for item in recap.events:
            by_ticker.setdefault(item.ticker, []).append(item)
        for ticker in sorted(by_ticker):
            lines.append((ticker, _INK))
            for item in by_ticker[ticker]:
                text = f"   {item.day.isoformat()} — {_event_line(item.event)}"
                lines.append((_clip(text, 112), _CRITICAL if "⚠" in text else _INK))
        if not lines:
            lines.append(("No events fired this week — a quiet week is information.", _SECONDARY))
        if recap.standing_suppressed:
            lines.append(
                (f"({recap.standing_suppressed} routine reminders rolled up — "
                 "still-breached lines, in-window earnings, unchanged daily prints.)", _MUTED)
            )
        _block(cur, "Your week in events", lines, 24)

    def _discovery(fig, cur) -> None:
        cur.line("Discovery — the shortlist this week", size=11, weight="bold")
        cur.gap(0.006)
        if recap.scout_run_id is None:
            cur.line("No scout run this week.", size=9, color=_SECONDARY)
            return
        if recap.entered:
            cur.line("Entered the list: " + ", ".join(recap.entered),
                     size=9.5, weight="bold", color=_UP)
        if recap.dropped:
            cur.line("Dropped off: " + ", ".join(recap.dropped),
                     size=9.5, weight="bold", color=_CRITICAL)
        if not recap.entered and not recap.dropped:
            cur.line("No churn — the shortlist held steady.",
                     size=8.5, color=_MUTED, style="italic")
        cur.gap(0.008)
        proposed = [p for p in recap.proposals if p.status == "proposed"]
        for p in proposed[:18]:
            cur.line(_clip(f"   #{p.rank}  {p.ticker} · {p.sector} · streak {p.streak}w", 110),
                     size=8.5, color=_INK)
        cur.gap(0.010)
        card = recap.scorecard
        if card is not None and card.overall_n:
            tone = _SECONDARY if card.overall_median_alpha >= 0 else _CRITICAL
            cur.line(
                f"Scorecard: {card.overall_n} proposals ever — median α "
                f"{card.overall_median_alpha * 100:+.1f}%, "
                f"{card.overall_beat_spy}/{card.overall_n} beat SPY.",
                size=9, weight="bold", color=tone,
            )
            _scorecard_chart(fig, cur, card)  # per-name bars when marks exist; else nothing

    def _week_ahead_block(cur) -> None:
        lines: list[tuple[str, str]] = []
        for b in sorted(recap.week_ahead, key=lambda b: (b.report_date, b.symbol)):
            when = b.report_date.isoformat() + (f" {b.hour}" if b.hour else "")
            text = f"   {b.symbol} — {when}"
            if b.eps_estimate is not None:
                text += f" (est {b.eps_estimate:.2f})"
            lines.append((text, _INK))
        if not lines:
            lines.append(
                (recap.week_ahead_note or "No pinned bellwethers report next week.", _SECONDARY)
            )
        elif recap.week_ahead_note:
            lines.append((recap.week_ahead_note, _MUTED))
        _block(cur, "The week ahead (finnhub, print-time — not archived)", lines, 14)

    def _footer(fig) -> None:
        fig.text(0.5, 0.03,
                 f"Regenerate: argus recap --week-ending {recap.week_ending.isoformat()}",
                 ha="center", va="bottom", fontsize=8, color=_MUTED)

    def _save(pdf, fig) -> None:
        try:
            pdf.savefig(fig)
        finally:
            plt.close(fig)

    metadata = {
        "Title": f"Argus Sunday Edition — week ending {recap.week_ending.isoformat()}",
        "Creator": "argus",
        "Producer": "argus",
        "CreationDate": None,  # identical inputs → identical bytes
    }
    subtitle = (
        f"{recap.watch_runs} watch run(s) this week"
        + (f" · scout run {recap.scout_run_id}" if recap.scout_run_id else " · no scout run")
    )
    buffer = io.BytesIO()
    with PdfPages(buffer, metadata=metadata) as pdf:
        page1 = plt.figure(figsize=(8.5, 11.0))
        cur = _Cursor(page1)
        _masthead(page1, cur, subtitle=subtitle)
        _scoreboard(page1, cur)
        cur.gap(0.006)
        _events_block(cur)
        _footer(page1)
        _save(pdf, page1)

        page2 = plt.figure(figsize=(8.5, 11.0))
        cur = _Cursor(page2)
        _masthead(page2, cur, subtitle="the week in discovery")
        _discovery(page2, cur)
        cur.gap(0.010)
        _week_ahead_block(cur)
        _footer(page2)
        _save(pdf, page2)
    return buffer.getvalue()
