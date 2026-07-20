"""Digest rendering (pure) and delivery sinks (the one IO seam for output).

Silence is a statement: a digest is written on every run that produced any
data, including a run with zero change events — "nothing changed" is
information, and degraded runs disclose their own degradation.
"""

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable

from argus.fields import SPECS, Field, Source
from argus.models import (
    AnalystAction,
    ChangeEvent,
    ConsensusShift,
    EarningsImminent,
    EarningsReported,
    FieldQuarantined,
    FieldRecovered,
    FieldValue,
    InsiderActivity,
    MacroLineCrossed,
    MacroPrint,
    MacroShift,
    MarketWire,
    PriceMove,
    QuarantineHit,
    RunReport,
    Snapshot,
    TargetMove,
    ThesisDrift,
    TickerReport,
)
from argus.redact import redact
from argus.thesis import evaluate_thesis_checks

# Human-facing field names. Adding a Field without a label falls back to the
# enum value — never a KeyError in the report path.
_FIELD_LABELS: dict[Field, str] = {
    Field.PRICE: "Price",
    Field.MARKET_CAP: "Market cap",
    Field.REVENUE: "Revenue (TTM)",
    Field.REVENUE_GROWTH: "Revenue growth (MRQ YoY)",
    Field.FCF_MARGIN: "FCF margin",
    Field.TOTAL_CASH: "Total cash",
    Field.TOTAL_DEBT: "Total debt",
    Field.EV_EBITDA: "EV/EBITDA",
    Field.DIVIDEND_YIELD: "Dividend yield",
    Field.BETA: "Beta",
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

# Deterministic sort keys: declaration order of the enums, not alphabetical —
# the watchlist reads price-first the way a human scans it.
_FIELD_ORDER = {f: i for i, f in enumerate(Field)}
_SOURCE_ORDER = {s: i for i, s in enumerate(Source)}


def render(report: RunReport) -> str:
    """RunReport → markdown.

    Rules (see ARCHITECTURE.md):
      - Tri-state per watched field, always one of:
          value with provenance   `Fwd P/E 31.2 (yahoo, 2026-07-12 14:03Z)`
          quarantined with reason `⚠ DATA QUARANTINED — target/price 3.19 …`
          no data with cause      `— no data (finnhub: HTTP 502)`
      - Headline "Changes" section renders the typed events (incl.
        FieldQuarantined — going dark is news); numeric moves print old_as_of.
      - Data-health section from run_sources (source down → which cross-checks
        were skipped); quarantine report table rendered from
        report.tickers[*].quarantines — EVERY quarantined observation,
        including those coexisting with an accepted primary; per-ticker
        thesis printed beside its changes.
      - Deterministic: tickers alphabetical, events by severity then kind —
        golden tests byte-compare the output.
    """
    tickers = sorted(report.tickers, key=lambda t: t.context.ticker)
    if report.kind == "scout":
        sections = (
            _header(report),
            _proposals_section(report),
            _sector_board_section(report),
            _deterioration_section(report),
            _scorecard_section(report),
            _scout_exclusions_section(report),
            _quarantine_section(tickers),
            _health_section(tickers),
            _scout_footer(report),
        )
    else:
        watch = [t for t in tickers if t.context.macro is None]
        macro = [t for t in tickers if t.context.macro is not None]
        parts: list[list[str]] = [_header(report)]
        if macro:
            parts.append(_macro_section(macro))
        # Changes reads equities first, then macro — both alphabetical.
        parts.append(_changes_section(watch + macro))
        considering = [t for t in watch if t.context.tier == "consider"]
        if report.radar or considering:
            parts.append(_radar_section(report, considering))
        if report.etf_rebalances:
            parts.append(_etf_rebalance_section(report))
        if report.market is not None:  # the issue's market pages (magazine mode)
            parts += [
                _movers_section(report.market),
                _sector_pulse_section(report.market),
                _earnings_wire_section(report.market),
                _extremes_section(report.market),
            ]
            if report.market.features:
                parts.append(_featured_section(report.market))
        parts.append(_watchlist_section(watch))
        if report.bellwethers:
            parts.append(_bellwether_section(report))
        sections = tuple(parts) + (
            _quarantine_section(tickers),
            _health_section(tickers),
            _footer(report),
        )
    return "\n\n".join("\n".join(_trimmed(section)) for section in sections) + "\n"


# --- Sections ---------------------------------------------------------------


def _header(report: RunReport) -> list[str]:
    lines = [f"# Argus {report.kind} digest — run {report.run_id} — {report.as_of.date().isoformat()}", ""]
    if report.status == "complete":
        lines.append("Status: complete.")
    elif report.status == "partial":
        lines.append(
            "Status: PARTIAL — some tickers or sources failed this run; "
            "degradation is detailed under Data health."
        )
    else:
        lines.append("Status: FAILED — this run produced no usable data.")
    if report.notes:
        lines += ["", f"**Note:** {report.notes}"]
    return lines


# --- Scout sections ----------------------------------------------------------


def _proposals_section(report: RunReport) -> list[str]:
    """The shortlist. Every number in the table is OUR gated value from the
    enrichment snapshots; the screener's numbers appear only in the
    screen-reasons column, labeled as claims."""
    proposed = [p for p in report.scout if p.status == "proposed"]
    leaders = [p for p in report.scout if p.status == "leader"]
    lines = ["## Conviction — the graded shortlist", ""]
    if not report.scout and report.status == "failed":
        # An outage is not a verdict: "evaluated nothing" must never read as
        # "nothing passed" — those are different statements.
        lines.append("No candidates were evaluated this run — see the note above.")
        return lines
    if not proposed:
        # Silence is a statement: no survivors is information — but leaders
        # (below) still render: the strip must never vanish with the table.
        lines += ["No candidates passed the screen and the quality gates this run.", ""]
    else:
        # Foreground the fresh names: a name on the list for the first time
        # (streak <= 1) is what a reader who saw last week's issue scans for. A
        # stable shortlist is information, not a bug — say so plainly (the
        # Sunday Edition rolls up the full churn).
        new_names = [p.ticker for p in proposed if p.streak <= 1]
        if new_names:
            lines += ["**New this week:** " + ", ".join(new_names), ""]
        else:
            lines += [
                "_No new names cleared the screen this week — the shortlist held._",
                "",
            ]
        snapshots = {t.context.ticker: t.snapshot for t in report.tickers}
        lines += [
            "| # | Ticker | Sector | Streak | Price | Fwd P/E | Gross margin | Op margin | ROE | D/E |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        # '#' is the GLOBAL screen rank everywhere — table, leaders strip,
        # and exclusion lines all speak one scale (and match the PDF). Gaps
        # mean names excluded after enrichment or sector-capped.
        for p in proposed:
            snapshot = snapshots.get(p.ticker)
            values = snapshot.values if snapshot is not None else {}

            def cell(field: Field) -> str:
                fv = values.get(field)
                return _fmt_value(field, fv.value) if fv is not None else "—"

            streak = f"{p.streak}w" if p.streak > 1 else "new"
            lines.append(
                f"| {p.rank} | {_cell(p.ticker)} | {_cell(p.sector)} | {streak} "
                f"| {cell(Field.PRICE)} | {cell(Field.PE_FWD)} | {cell(Field.GROSS_MARGIN)} "
                f"| {cell(Field.OPERATING_MARGIN)} | {cell(Field.ROE)} | {cell(Field.DEBT_TO_EQUITY)} |"
            )
        lines.append("")
        lines.append(
            "_'#' is the global screen rank; gaps are names excluded after "
            "enrichment or sector-capped._"
        )
        lines.append("")
    if leaders:
        # Category coverage without dilution: the best passer of each sector
        # the shortlist left unrepresented — screener claims, not enriched.
        lines.append(
            "Sector leaders beyond the shortlist (screener claims, not enriched): "
            + "; ".join(
                f"{_cell(p.sector)} — **{_cell(p.ticker)}**"
                + (
                    f" (fwd P/E {p.screener_metrics.get('fwd_pe'):.1f}, #{p.rank} overall)"
                    if _finite_number(p.screener_metrics.get("fwd_pe"))
                    else f" (#{p.rank} overall)"
                )
                for p in leaders
            )
        )
        lines.append("")
    if not proposed:
        return lines
    lines.append("Screen (screener claims, verified independently above):")
    profiles = {t.context.ticker: t.profile for t in report.tickers}
    for p in proposed:
        profile = profiles.get(p.ticker)
        business = ""
        if profile is not None and (profile.sector or profile.industry):
            parts = " · ".join(x for x in (profile.sector, profile.industry) if x)
            business = f" ({_cell(parts)})"
        peer_note = ""
        median = (p.peer_context or {}).get("median_fwd_pe")
        if _finite_number(median):
            # peer_context is an unvalidated JSON round-trip: every value is
            # guarded here, or a stray string kills render() and the digest.
            count = (p.peer_context or {}).get("n")
            count_note = f", n={count:g}" if _finite_number(count) else ""
            peer_note = (
                f" · vs industry median fwd P/E {median:g} "
                f"({_cell(str((p.peer_context or {}).get('industry') or 'industry'))}{count_note})"
            )
        lines.append(
            f"- **{_cell(p.ticker)}**{business} — "
            f"{_cell('; '.join(p.screen_reasons.values()))}{peer_note}"
        )
    return lines


def _finite_number(value: object) -> bool:
    """JSON round-trips admit strings, None, and NaN where numbers belong —
    formatting must never crash the digest over a stray claim value."""
    import math

    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _scorecard_section(report: RunReport) -> list[str]:
    """Grade the grader: how scout's past proposals have actually done vs SPY.
    Realized data, forward log — the market is the answer key, never the
    engine. Empty until proposals have had time to play out."""
    card = report.scorecard
    lines = ["## Scorecard — how past proposals have done vs SPY", ""]
    if card is None or card.overall_n == 0:
        # Absence of signal must be distinguishable from absence of data: a
        # card with unpriceable>0 means eligible names existed but could not
        # be priced this run (fetch down / delisted), NOT "nothing has matured".
        if card and card.unpriceable:
            lines.append(
                f"Price data was unavailable for all {card.unpriceable} eligible past "
                "proposal(s) this run — scoring resumes when it returns."
            )
        else:
            lines.append("No proposal has had time to play out yet — the forward log starts now.")
        return lines
    lines += [
        "| First proposed | Names | Median return | SPY | Median excess | Beat SPY |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for c in card.cohorts:
        lines.append(
            f"| {c.label} | {c.n} | {_pct(c.median_return)} | {_pct(c.median_spy)} "
            f"| {_pct(c.median_alpha)} | {c.beat_spy}/{c.n} |"
        )
    lines += [
        "",
        f"**Overall:** {card.overall_n} names ever proposed — median excess return "
        f"{_pct(card.overall_median_alpha)} vs SPY, {card.overall_beat_spy}/{card.overall_n} beat SPY."
        + (f" ({card.unpriceable} unpriceable, excluded)" if card.unpriceable else ""),
        "",
        "_Total return incl. dividends (adjusted close), every proposal counted "
        "from its first appearance (no survivorship), never revised. The market "
        "is the answer key — Argus never grades itself._",
    ]
    return lines


def _pct(fraction: float) -> str:
    return f"{fraction * 100:+.1f}%"


def _sector_board_section(report: RunReport) -> list[str]:
    """Relative-value breadth: the cheapest-for-growth names in EACH sector,
    from the market scan — a looser lens than the quality shortlist, so every
    sector (banks, utilities, REITs) can fill. Screener claims, never gated or
    scored."""
    board = [p for p in report.scout if p.status == "board"]
    lines = ["## Worth watching — relative value per sector (screener claims)", ""]
    if not board:
        lines.append("No sector-board names this run.")
        return lines
    from argus.scout.sectors import CANONICAL_SECTORS

    by_sector: dict[str, list] = {}
    for p in board:
        by_sector.setdefault(p.sector, []).append(p)
    for sector in CANONICAL_SECTORS:
        picks = sorted(by_sector.get(sector, []), key=lambda p: p.rank)
        if picks:
            lines.append(f"- **{_cell(sector)}:** " + " · ".join(_board_pick(p) for p in picks))
    lines += [
        "",
        "_Top names per sector by forward P/E per point of revenue growth "
        "(cheap-for-growth), from the market scan — sanity floors only, NOT the "
        "quality gates, NOT verified, NOT scored. Breadth context beside the "
        "graded shortlist, never a recommendation._",
    ]
    return lines


def _board_pick(p) -> str:
    metrics = p.screener_metrics or {}
    detail = []
    price = metrics.get("close")
    if _finite_number(price):
        detail.append(f"{price:.2f}")
    fwd = metrics.get("fwd_pe")
    if _finite_number(fwd):
        detail.append(f"fwd P/E {fwd:.1f}")
    peg = metrics.get("peg_ttm")
    if _finite_number(peg):
        detail.append(f"PEG {peg:.2f}")
    growth = metrics.get("revenue_growth_ttm_pct")
    if _finite_number(growth):
        detail.append(f"{growth:+.0f}% gr")
    return f"{_cell(p.ticker)} ({', '.join(detail)})" if detail else _cell(p.ticker)


def _deterioration_section(report: RunReport) -> list[str]:
    """Names whose fundamentals are visibly WEAKENING in the scan — reported as
    FACTS, never a forecast, recommendation, or trade signal (the hard
    constraint: Argus informs, the human decides). Never gated, never scored."""
    det = [p for p in report.scout if p.status == "deterioration"]
    lines = ["## Under pressure — weakening fundamentals (screener claims)", ""]
    if not det:
        lines.append("No names flagged deteriorating this run.")
        return lines
    for p in sorted(det, key=lambda p: p.rank):
        flags = _cell("; ".join(p.screen_reasons.values()))
        sector = f" ({_cell(p.sector)})" if p.sector else ""
        lines.append(f"- **{_cell(p.ticker)}**{sector}: {flags}")
    lines += [
        "",
        "_Factual signs of weakening fundamentals from the market scan — "
        "reported as DATA, not a forecast, recommendation, or trade signal. "
        "Never gated, never scored. The human decides what it means._",
    ]
    return lines


def _scout_exclusions_section(report: RunReport) -> list[str]:
    excluded = [p for p in report.scout if p.status == "excluded"]
    if not excluded:
        return ["No screen survivors were excluded by the quality gates."]
    lines = ["## Excluded after enrichment", ""]
    lines += [
        f"- {_cell(p.ticker)} (screen rank {p.rank}): {_cell(p.exclusion_reason or '')}"
        for p in excluded
    ]
    lines.append("")
    lines.append(
        "_Exclusion is a data-quality verdict, not an investment one — these "
        "names passed the screen but their fundamentals could not be verified "
        "cleanly this run._"
    )
    return lines


def _scout_footer(report: RunReport) -> list[str]:
    return [
        "---",
        "",
        "Argus proposes; the human decides. To start watching a name: "
        "`argus promote TICKER --thesis \"why you believe it\"`.",
        "",
        f"Run {report.run_id} — regenerate with `argus report --run {report.run_id}`.",
    ]


# One render-derived context line per pair, shown iff BOTH legs carry an
# accepted value this run — derived at render from accepted values only
# (the sanctioned derived-metric path). Config-driven spreads are the
# upgrade path if more are ever wanted.
_SPREAD_PAIRS: tuple[tuple[str, str, str], ...] = (("^TNX", "^IRX", "10Y − 3M spread"),)


def _macro_section(tickers: Sequence[TickerReport]) -> list[str]:
    """The market backdrop: one tri-state line per series, label-sorted
    (ASCII '^' sorts after 'Z', so symbol order reads wrong). Alerts live in
    the Changes flow — the Discord headline only carries that section; this
    section is the standing level."""
    lines = ["## Macro", ""]
    ordered = sorted(tickers, key=lambda t: (t.context.macro.label, t.context.ticker))
    lines += [_macro_line(t) for t in ordered]
    spreads = _spread_lines(tickers)
    if spreads:
        lines += ["", *spreads]
    return lines


def _macro_line(ticker: TickerReport) -> str:
    spec = ticker.context.macro
    assert spec is not None  # only macro-role tickers reach this section
    label = spec.label
    snapshot = ticker.snapshot
    if snapshot is None:
        return f"- {label}: fetch failed — no data this run ({ticker.error or 'unknown error'})"
    fv = snapshot.values.get(spec.value_field)
    if fv is None:
        hits = snapshot.quarantined.get(spec.value_field)
        if hits:
            return f"- {label}: ⚠ DATA QUARANTINED — {_details(hits)}"
        return f"- {label}: — no data ({_no_data_cause(spec.value_field, ticker)})"
    line = f"- {label}: {fv.value:.{spec.decimals}f}{spec.unit}"
    delta = _macro_delta(ticker, fv)
    if delta:
        line += f" ({delta})"
    if spec.source == Source.FRED and fv.observed_at is not None:
        line += f" ({fv.source.value}, period {fv.observed_at.date().isoformat()})"
    else:
        line += f" ({fv.source.value}, {_ts(fv.fetched_at)})"
    if spec.sanity is not None and not (spec.sanity[0] <= fv.value <= spec.sanity[1]):
        low, high = spec.sanity
        line += f" — ⚠ implausible (outside sanity [{low:g}, {high:g}]) — check units"
    for result in evaluate_thesis_checks(spec.alert_when, snapshot):
        if result.status == "holds":  # crossed ⇔ holds; see changes._macro_events
            line += f" — ⚠ line crossed: {result.check.raw}"
    return line


def _macro_delta(ticker: TickerReport, fv: FieldValue) -> str:
    """Δ vs the baseline snapshot's value, suppressed at zero (quiet weekends
    render clean, the held-checks-are-silent precedent). Econ series compare
    print-to-print, so the window reads 'prior print' rather than a date."""
    spec = ticker.context.macro
    baseline = ticker.baseline
    if spec is None or baseline is None:
        return ""
    old = baseline.values.get(spec.value_field)
    if old is None or not isinstance(old.value, (int, float)):
        return ""
    delta = round(fv.value - old.value, spec.decimals)
    if delta == 0:
        return ""
    if spec.source == Source.FRED:
        window = "prior print"
    elif baseline.as_of.date() == ticker.snapshot.as_of.date():
        window = "earlier today"  # a manual same-day rerun; dates would read absurd
    else:
        window = baseline.as_of.date().isoformat()
    return f"Δ {delta:+.{spec.decimals}f} vs {window}"


def _spread_lines(tickers: Sequence[TickerReport]) -> list[str]:
    by_symbol = {t.context.ticker: t for t in tickers}
    lines = []
    for a, b, label in _SPREAD_PAIRS:
        va = _accepted_price(by_symbol.get(a))
        vb = _accepted_price(by_symbol.get(b))
        if va is None or vb is None:
            continue
        lines.append(f"- {label}: {va - vb:+.2f}pp _(derived at render from the two yields)_")
    return lines


def _accepted_price(ticker: TickerReport | None) -> float | None:
    if ticker is None or ticker.snapshot is None:
        return None
    fv = ticker.snapshot.values.get(Field.PRICE)
    return fv.value if fv is not None and isinstance(fv.value, (int, float)) else None


def _changes_section(tickers: Sequence[TickerReport]) -> list[str]:
    lines = ["## Changes", ""]
    if not any(t.events for t in tickers):
        # Silence is a statement: zero events is information, stated plainly.
        lines += ["No changes since last run.", ""]
    else:
        for ticker in tickers:
            if not ticker.events:
                continue
            lines += [f"### {ticker.context.ticker}", ""]
            if ticker.context.thesis:
                lines += [f"_{ticker.context.thesis}_", ""]
            lines += [f"- {_event_line(event)}" for event in ticker.events]
            lines.append("")
    # status-based, not snapshot-based: queries.snapshot returns an EMPTY
    # (non-None) Snapshot for a failed ticker's run_tickers row, so a
    # snapshot-is-None guard never fires and a never-succeeded ticker would
    # read as "baseline established" forever.
    firsts = [
        t.context.ticker for t in tickers if t.baseline_run_id is None and t.status != "failed"
    ]
    if firsts:
        lines += [
            "Baseline established this run (no prior run to diff against): " + ", ".join(firsts) + ".",
            "",
        ]
    failures = [t for t in tickers if t.status == "failed"]
    if failures:
        lines.append(
            "Fetch failures (no data this run): "
            + ", ".join(f"{t.context.ticker} ({t.error or 'unknown error'})" for t in failures)
            + "."
        )
    return lines


def _watchlist_section(tickers: Sequence[TickerReport]) -> list[str]:
    lines = ["## Watchlist", ""]
    for ticker in tickers:
        lines += [f"### {ticker.context.ticker}", ""]
        if ticker.context.tier == "consider":
            lines += ["_Considering — promote with a thesis to graduate._", ""]
        if ticker.baseline is not None and ticker.snapshot is not None and ticker.snapshot.values:
            # Names the drift window once per ticker (baselines are
            # per-ticker, so it can differ across tickers after failures).
            # Skipped when nothing this run has a value to drift.
            lines += [f"_Δ vs {ticker.baseline.as_of.date().isoformat()}_", ""]
        if ticker.snapshot is None:
            # Never silently absent: a dead ticker is named, with its error.
            lines += [f"Fetch failed — no data this run ({ticker.error or 'unknown error'}).", ""]
            continue
        thesis_line = _thesis_standing(ticker)
        if thesis_line:
            lines += [thesis_line, ""]
        # ECON_VALUE is the macro-only field — an equity panel never carries it.
        lines += [_field_line(field, ticker) for field in Field if field is not Field.ECON_VALUE]
        lines.append("")
    return lines


def _thesis_standing(ticker: TickerReport) -> str:
    """One line summarizing whether the human's thesis conditions still hold —
    the quiet reassurance ('3/3 holding') or the flag ('⚠ 1/3 breached'), so a
    breach is never buried and a holding thesis is visibly confirmed."""
    checks = ticker.context.thesis_checks
    if not checks or ticker.snapshot is None:
        return ""
    results = evaluate_thesis_checks(checks, ticker.snapshot)
    holding = sum(1 for r in results if r.status == "holds")
    breached = [r for r in results if r.status == "breached"]
    unverifiable = sum(1 for r in results if r.status == "undeterminable")
    total = len(results)
    if breached:
        broken = "; ".join(r.check.raw for r in breached)
        summary = f"⚠ Thesis: {len(breached)}/{total} checks BREACHED — {broken}"
    else:
        summary = f"Thesis: {holding}/{total} checks holding"
    if unverifiable:
        summary += f" ({unverifiable} unverifiable this run)"
    return f"_{summary}_"


# --- The Radar -----------------------------------------------------------------


def _radar_section(report: RunReport, considering: Sequence[TickerReport]) -> list[str]:
    """The discovery funnel, ambient in every issue: the standing scout
    shortlist, mechanical crossings against today's market wire (a name
    you're circling DOING something is the highest-signal awareness there
    is), and the names you've marked `consider`. Argus surfaces; you
    promote — never the other way around."""
    lines = ["## Radar", ""]
    if report.radar:
        lines.append("On the shortlist (scout):")
        lines += [
            f"- #{p.rank} {p.ticker} — {p.sector}, streak {p.streak}w" for p in report.radar
        ]
        lines.append("")
    crossings = _radar_crossings(report.radar, report.market)
    crossings += _radar_insider_crossings(report)
    if crossings:
        lines += crossings
        lines.append("")
    if considering:
        lines.append("Considering (graduate with `argus promote TICKER --thesis ...`):")
        lines += [_considering_line(t) for t in considering]
        lines.append("")
    lines.append(
        "_Mechanical joins of persisted data — the shortlist is scout's, the crossings "
        "are rule-based, and only you move a name up a tier._"
    )
    return lines


def _radar_insider_crossings(report: RunReport) -> list[str]:
    """The highest-signal Radar crossing: a name scout flagged is being bought
    by its own insiders. Grouped per ticker (a cluster of buyers reads as one
    line), with the shortlist streak for context."""
    if not report.radar_insider:
        return []
    streaks = {p.ticker: p.streak for p in report.radar}
    by_ticker: dict[str, list] = {}
    for buy in report.radar_insider:
        by_ticker.setdefault(buy.ticker, []).append(buy)
    lines = []
    for ticker in sorted(by_ticker):
        buys = by_ticker[ticker]
        owners = ", ".join(sorted({f"{b.owner} ({b.role})" for b in buys}))
        total = sum(b.shares for b in buys)
        streak = f", {streaks[ticker]}w" if streaks.get(ticker) else ""
        lines.append(
            f"- ⚡ {ticker} (shortlist{streak}) insider buying: {owners} — "
            f"{total:,.0f} sh across {len(buys)} filing(s)"
        )
    return lines


def _radar_crossings(radar, market: MarketWire | None) -> list[str]:
    """Shortlist ∩ market wire, purely mechanical."""
    if market is None or not radar:
        return []
    streaks = {p.ticker: p.streak for p in radar}
    hits: list[str] = []
    for m in (*market.gainers, *market.losers):
        if m.symbol in streaks:
            hits.append(
                f"- ⚡ {m.symbol} (shortlist, {streaks[m.symbol]}w) was a top-5 mover "
                f"({m.change_pct:+.1f}%)"
            )
    for e in (*market.highs, *market.lows):
        if e.symbol in streaks:
            kind = "52-week high" if e.kind == "high" else "52-week low"
            hits.append(f"- ⚡ {e.symbol} (shortlist, {streaks[e.symbol]}w) hit a {kind}")
    for b in market.earnings_reported:
        if b.symbol in streaks and b.eps_actual is not None:
            line = f"- ⚡ {b.symbol} (shortlist, {streaks[b.symbol]}w) reported: EPS {b.eps_actual:.2f}"
            if b.eps_estimate:
                line += f" vs {b.eps_estimate:.2f} est"
            hits.append(line)
    for b in market.earnings_upcoming:
        if b.symbol in streaks:
            when = b.report_date.isoformat() + (f" {b.hour}" if b.hour else "")
            hits.append(f"- ⚡ {b.symbol} (shortlist, {streaks[b.symbol]}w) reports {when}")
    return hits


def _etf_rebalance_section(report: RunReport) -> list[str]:
    """Well-known ETFs whose constituents changed since the last snapshot — a
    forced-flow signal (an index add means index funds must buy). Shown only
    when something changed; a quiet run stores and says nothing."""
    from argus.etf import is_nport_etf

    lines = ["## ETF rebalancing (holdings, unverified)", ""]
    for r in report.etf_rebalances:
        tag = " · SEC N-PORT, latest filing (lagged)" if is_nport_etf(r.etf) else ""
        if r.added:
            lines.append(f"- {r.etf} added: {', '.join(r.added)}{tag}")
        if r.dropped:
            lines.append(f"- {r.etf} dropped: {', '.join(r.dropped)}{tag}")
    lines.append(
        "_Constituent changes in each source's holdings — a claims-labeled diff, "
        "never gated. An index add is forced buying; a drop the reverse. Issuer "
        "feeds are same-day; N-PORT reflects the latest monthly SEC filing._"
    )
    return lines


def _considering_line(ticker: TickerReport) -> str:
    name = ticker.context.ticker
    snapshot = ticker.snapshot
    if snapshot is None:
        return f"- {name}: fetch failed this run ({ticker.error or 'unknown error'})"
    parts = []
    price = snapshot.values.get(Field.PRICE)
    if price is not None:
        parts.append(f"{price.value:.2f}")
    fwd = snapshot.values.get(Field.PE_FWD)
    if fwd is not None:
        parts.append(f"fwd P/E {fwd.value:.1f}")
    earnings = snapshot.values.get(Field.NEXT_EARNINGS_DATE)
    if earnings is not None:
        parts.append(f"reports {earnings.value.isoformat()}")
    detail = " · ".join(parts) if parts else "no gated data this run"
    return f"- {name}: {detail}"


# --- The market wire (magazine issues) ----------------------------------------
# All four sections are claims-labeled market context from one scan + one
# calendar call, curated by the mechanical rules in market.py — never a
# delivery trigger, never observations.


def _movers_section(wire: "MarketWire") -> list[str]:
    from argus.market import MOVER_CAP_FLOOR, MOVERS_SHOWN

    lines = ["## Market movers (tradingview, unverified)", ""]
    if not wire.gainers and not wire.losers:
        lines.append("No large-cap gainers or losers last session — a flat tape is information.")
        return lines
    for title, movers in (("Gainers:", wire.gainers), ("Losers:", wire.losers)):
        if not movers:
            continue
        lines.append(title)
        for m in movers:
            name = f" — {m.company}" if m.company else ""
            lines.append(f"- {m.symbol} {m.change_pct:+.1f}% → {m.close:.2f}{name} ({m.sector})")
        lines.append("")
    lines.append(
        f"_Top {MOVERS_SHOWN} each way, last session, caps ≥ "
        f"${MOVER_CAP_FLOOR / 1e9:.0f}B ({wire.universe} names scanned)._"
    )
    return lines


def _sector_pulse_section(wire: "MarketWire") -> list[str]:
    lines = ["## Sector pulse (tradingview, unverified)", ""]
    if not wire.sectors:
        lines.append("No sector data in the scan this issue.")
        return lines
    lines += [
        f"- {p.sector}: {p.median_change_pct:+.1f}% median ({p.n} names)" for p in wire.sectors
    ]
    return lines


def _earnings_wire_section(wire: "MarketWire") -> list[str]:
    from argus.market import MOVER_CAP_FLOOR

    lines = ["## Earnings wire (finnhub + tradingview, unverified)", ""]
    if not wire.earnings_reported and not wire.earnings_upcoming:
        lines.append("No large-cap earnings reported or scheduled in the window.")
        return lines
    if wire.earnings_reported:
        lines.append("Reported:")
        for b in wire.earnings_reported:
            line = f"- {b.symbol} ({b.report_date.isoformat()}): EPS {b.eps_actual:.2f}"
            if b.eps_estimate is not None:
                line += f" vs {b.eps_estimate:.2f} est"
                surprise = _eps_surprise_pct(b.eps_actual, b.eps_estimate)
                if surprise is not None:
                    line += f" ({surprise:+.1f}%)"
            lines.append(line)
        lines.append("")
    if wire.earnings_upcoming:
        lines.append("Upcoming:")
        for b in wire.earnings_upcoming:
            when = b.report_date.isoformat() + (f" {b.hour}" if b.hour else "")
            line = f"- {b.symbol} — {when}"
            if b.eps_estimate is not None:
                line += f" (est {b.eps_estimate:.2f})"
            lines.append(line)
        if wire.earnings_more_upcoming:
            lines.append(f"- … and {wire.earnings_more_upcoming} more large caps this week.")
        lines.append("")
    lines.append(
        f"_Caps ≥ ${MOVER_CAP_FLOOR / 1e9:.0f}B plus your pinned bellwethers; surprise "
        "computed from the two claimed numbers. Never a delivery trigger._"
    )
    return lines


def _extremes_section(wire: "MarketWire") -> list[str]:
    from argus.market import EXTREME_TOLERANCE, MOVER_CAP_FLOOR

    lines = ["## New 52-week extremes (tradingview, unverified)", ""]
    if not wire.highs and not wire.lows:
        lines.append("No large caps at 52-week marks last session.")
        return lines
    for title, extremes in (("At highs:", wire.highs), ("At lows:", wire.lows)):
        if not extremes:
            continue
        lines.append(title)
        for e in extremes:
            name = f" — {e.company}" if e.company else ""
            lines.append(f"- {e.symbol} {e.close:.2f}{name}")
        lines.append("")
    lines.append(
        f"_Within {EXTREME_TOLERANCE:.1%} of the 52-week mark, caps ≥ "
        f"${MOVER_CAP_FLOOR / 1e9:.0f}B._"
    )
    return lines


def _featured_section(wire: MarketWire) -> list[str]:
    """The issue's reading material: who the featured companies ARE. Picked
    by disclosed mechanical rules, prose rendered verbatim, numbers labeled
    claims — Argus curates by rule and never editorializes."""
    lines = ["## Featured (yahoo, unverified)", ""]
    for card in wire.features:
        title = f"### {card.symbol}" + (f" — {card.name}" if card.name else "")
        lines += [title, ""]
        lines.append(f"_{card.why}._")
        for fact_line in _feature_fact_lines(card):
            lines.append("- " + fact_line)
        if card.summary:
            lines += ["", card.summary]
        lines.append("")
    lines.append("_Selection is mechanical (top two movers each way, two largest upcoming reporters)._")
    return lines


def _feature_fact_lines(card) -> list[str]:
    """Three claim rows per card: the business, the numbers, the street."""
    lines: list[str] = []
    business = []
    if card.sector:
        business.append(card.sector + (f" · {card.industry}" if card.industry else ""))
    if card.market_cap:
        business.append(f"cap {_humanize_cap(card.market_cap)}")
    if card.employees:
        business.append(f"{card.employees:,} employees")
    if business:
        lines.append(" · ".join(business))
    valuation = []
    if card.close is not None:
        span = ""
        if card.low_52w and card.high_52w:
            span = f" (52w {card.low_52w:,.2f}–{card.high_52w:,.2f})"
        valuation.append(f"close {card.close:,.2f}{span}")
    if card.fwd_pe:
        valuation.append(f"fwd P/E {card.fwd_pe:.1f}")
    elif card.pe_ttm:
        valuation.append(f"P/E {card.pe_ttm:.1f}")
    if card.beta is not None:
        valuation.append(f"beta {card.beta:.2f}")
    if valuation:
        lines.append(" · ".join(valuation))
    quality = []
    if card.revenue:
        rev = f"revenue {_humanize_cap(card.revenue)}"
        if card.revenue_growth_pct is not None:
            rev += f" ({card.revenue_growth_pct:+.1f}% YoY)"
        quality.append(rev)
    if card.gross_margin_pct is not None:
        quality.append(f"gross margin {card.gross_margin_pct:.1f}%")
    if card.roe_pct is not None:
        quality.append(f"ROE {card.roe_pct:.1f}%")
    if card.dividend_yield_pct:
        quality.append(f"yield {card.dividend_yield_pct:.2f}%")
    if quality:
        lines.append(" · ".join(quality))
    street = []
    if card.analyst_rating:
        street.append(f"consensus {card.analyst_rating}")
    if card.analyst_target:
        street.append(f"mean target {card.analyst_target:,.2f}")
    if card.analyst_count:
        street.append(f"{card.analyst_count} analysts")
    if street:
        lines.append("street: " + " · ".join(street))
    return lines


def _bellwether_section(report: RunReport) -> list[str]:
    """Megacap earnings context — dates and estimates ahead, actual vs
    estimate as they land. Single-source claims: labeled, never gated, and
    deliberately NOT a delivery trigger (in season these names report almost
    daily, which would defeat event-gated delivery)."""
    lines = ["## Bellwether earnings (finnhub, unverified)", ""]
    reported = sorted(
        (b for b in report.bellwethers if b.eps_actual is not None),
        key=lambda b: (b.report_date, b.symbol),
    )
    upcoming = sorted(
        (b for b in report.bellwethers if b.eps_actual is None),
        key=lambda b: (b.report_date, b.symbol),
    )
    if reported:
        lines.append("Reported:")
        for b in reported:
            line = f"- {b.symbol} ({b.report_date.isoformat()}): EPS {b.eps_actual:.2f}"
            if b.eps_estimate is not None:
                line += f" vs {b.eps_estimate:.2f} est"
                surprise = _eps_surprise_pct(b.eps_actual, b.eps_estimate)
                if surprise is not None:
                    line += f" ({surprise:+.1f}%)"
            lines.append(line)
        lines.append("")
    if upcoming:
        lines.append("Upcoming:")
        for b in upcoming:
            when = b.report_date.isoformat() + (f" {b.hour}" if b.hour else "")
            line = f"- {b.symbol} — {when}"
            if b.eps_estimate is not None:
                line += f" (est {b.eps_estimate:.2f})"
            lines.append(line)
        lines.append("")
    lines.append("_Context only — single-source claims, never gated, never a delivery trigger._")
    return lines


def _quarantine_section(tickers: Sequence[TickerReport]) -> list[str]:
    rows: list[tuple[str, object]] = []
    for ticker in tickers:
        ordered = sorted(
            ticker.quarantines,
            key=lambda q: (_FIELD_ORDER[q.field], _SOURCE_ORDER[q.source], q.fetched_at),
        )
        rows += [(ticker.context.ticker, q) for q in ordered]
    if not rows:
        return ["No data quarantined this run."]
    lines = [
        "## Data quarantined",
        "",
        "| Ticker | Field | Source | Reasons | Fetched at |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, q in rows:
        reasons = _cell("; ".join(f"{hit.code.value}: {hit.detail}" for hit in q.reasons))
        lines.append(
            f"| {name} | {_label(q.field)} | {q.source.value} | {reasons} | {_ts(q.fetched_at)} |"
        )
    return lines


def _health_section(tickers: Sequence[TickerReport]) -> list[str]:
    lines = ["## Data health", ""]
    tallies: dict[Source, dict[str, int]] = {}
    first_error: dict[Source, str] = {}
    for ticker in tickers:  # already alphabetical → "first error" is deterministic
        for health in sorted(ticker.sources, key=lambda s: _SOURCE_ORDER[s.source]):
            tally = tallies.setdefault(health.source, {"ok": 0, "error": 0, "not_applicable": 0})
            tally[health.status] += 1
            if health.status == "error" and health.error and health.source not in first_error:
                first_error[health.source] = health.error
    if not tallies:
        lines.append("No source health recorded.")
    for source in Source:
        tally = tallies.get(source)
        if tally is None:
            if tallies and source is not Source.FRED:
                # Zero rows on a run that fetched anything: either no key is
                # configured OR nothing this run needed the source (a
                # macro-only run never consults edgar/finnhub). The report
                # cannot tell which — say so honestly, claim neither.
                # FRED is the exception: it is wired by macro.yaml, not a
                # secret, and a run with no econ series has nothing to
                # disclose (its failures still tally when configured).
                lines.append(
                    f"- {source.value}: not consulted — no key configured or nothing required it"
                )
            continue
        parts = []
        if tally["ok"]:
            parts.append(f"{tally['ok']} ok")
        if tally["error"]:
            parts.append(f"{tally['error']} error" + ("s" if tally["error"] != 1 else ""))
        if tally["not_applicable"]:
            parts.append(f"{tally['not_applicable']} not applicable")
        line = f"- {source.value}: " + ", ".join(parts)
        if tally["error"]:
            if source in first_error:
                line += f" (first: {first_error[source]})"
            skipped = [
                _label(f).lower()
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
        lines.append(line)
    failed = [t for t in tickers if t.status == "failed"]
    if failed:
        lines += ["", "Failed tickers:"]
        lines += [f"- {t.context.ticker}: {t.error or 'unknown error'}" for t in failed]
    return lines


def _footer(report: RunReport) -> list[str]:
    # Self-identifying: `argus report --run N` output names its run.
    return ["---", "", f"Run {report.run_id} — regenerate with `argus report --run {report.run_id}`."]


# --- Lines ------------------------------------------------------------------


def _event_line(event: ChangeEvent) -> str:
    match event:
        case ThesisDrift(check=check, field=field, observed=observed, newly=newly):
            shown = _fmt_value(field, observed) if isinstance(observed, (int, float)) else observed
            status = "newly breached" if newly else "still breached"
            return (
                f"⚠ THESIS DRIFT — your line \"{check}\" is broken: "
                f"{_label(field)} is now {shown} ({status})"
            )
        case MacroLineCrossed(
            label=label, check=check, observed=observed, unit=unit, decimals=decimals, newly=newly
        ):
            status = "newly crossed" if newly else "still crossed"
            return (
                f'⚠ LINE CROSSED — "{check}": {label} is at '
                f"{observed:.{decimals}f}{unit} ({status})"
            )
        case MacroPrint(
            label=label, period=period, value=value, prev_value=prev_value,
            delta=delta, unit=unit, decimals=decimals,
        ):
            line = f"New print — {label}: {value:.{decimals}f}{unit} (period {period.isoformat()})"
            if prev_value is not None and delta is not None:
                line += f", prior {prev_value:.{decimals}f}{unit} ({delta:+.{decimals}f})"
            return line
        case MacroShift(
            label=label, old=old, new=new, delta=delta, unit=unit,
            decimals=decimals, threshold=threshold, old_as_of=old_as_of,
        ):
            return (
                f"{label} {old:.{decimals}f}{unit} → {new:.{decimals}f}{unit} "
                f"({delta:+.{decimals}f}, alert ≥ {format(threshold, 'g')})"
                f" vs {old_as_of.date().isoformat()}"
            )
        case PriceMove(old=old, new=new, pct=pct, threshold=threshold, old_as_of=old_as_of):
            return (
                f"Price {old:.2f} → {new:.2f} ({pct:+.1f}%, threshold {threshold:.1f}%)"
                f" vs {old_as_of.date().isoformat()}"
            )
        case TargetMove(old=old, new=new, pct=pct, threshold=threshold, old_as_of=old_as_of):
            return (
                f"Analyst target {old:.2f} → {new:.2f} ({pct:+.1f}%, threshold {threshold:.1f}%)"
                f" vs {old_as_of.date().isoformat()}"
            )
        case ConsensusShift(old=old, new=new, direction=direction):
            return f"Consensus rating {old} → {new} ({direction})"
        case AnalystAction(firm=firm, action=action, from_grade=from_grade, to_grade=to_grade, action_date=when):
            grades = f"{from_grade} → {to_grade}" if from_grade else to_grade
            return f"Analyst action ({when.isoformat()}): {firm} {action} — {grades}"
        case EarningsReported(
            quarter_end=quarter_end,
            eps_actual=eps_actual,
            eps_estimate=eps_estimate,
            surprise_pct=surprise_pct,
        ):
            line = f"Earnings reported (quarter ended {quarter_end.isoformat()}): EPS {eps_actual:.2f}"
            if eps_estimate is None:
                return line + " (no street estimate)"
            line += f" vs {eps_estimate:.2f} est"
            if surprise_pct is not None:
                line += f" ({surprise_pct:+.1f}%)"
            return line
        case InsiderActivity(
            owner=owner, role=role, shares=shares, price=price, transaction_date=when
        ):
            amount = f"{shares:,.0f} sh" + (f" @ {price:.2f}" if price else "")
            value = f" (~{_humanize_cap(shares * price)})" if price else ""
            return (
                f"Insider buy: {owner} ({role}) bought {amount}{value} "
                f"on {when.isoformat()}"
            )
        case EarningsImminent(earnings_date=earnings_date, days_until=days_until):
            return f"Earnings imminent: {earnings_date.isoformat()} ({_days_phrase(days_until)})"
        case FieldQuarantined(field=field, reasons=reasons):
            # Going dark is news, not a footnote.
            return f"⚠ {_label(field)} went dark — DATA QUARANTINED: {_details(reasons)}"
        case FieldRecovered(field=field):
            return f"✓ {_label(field)} recovered — accepted data resumed"
        case _:  # pragma: no cover — the union is closed
            raise AssertionError(f"unhandled event kind: {event!r}")


def _field_line(field: Field, ticker: TickerReport) -> str:
    """The tri-state: value with provenance, quarantined with reasons, or an
    explained absence. Absence of signal is never confusable with absence of
    data."""
    snapshot = ticker.snapshot
    assert snapshot is not None  # callers filter fetch failures first
    label = _label(field)
    fv = snapshot.values.get(field)
    if fv is not None:
        marks = "".join(f" ✓{s.value}" for s in sorted(fv.corroborated_by))
        drift = _drift_suffix(field, fv.value, ticker.baseline)
        return (
            f"- {label}: {_fmt_value(field, fv.value)}{drift} "
            f"({fv.source.value}, {_ts(fv.fetched_at)}){marks}"
        )
    hits = snapshot.quarantined.get(field)
    if hits:
        return f"- {label}: ⚠ DATA QUARANTINED — {_details(hits)}"
    return f"- {label}: — no data ({_no_data_cause(field, ticker)})"


# Scale-free fields drift in percent; ratios drift in absolute points;
# margins (stored as fractions, rendered as percents) drift in percentage
# points. Sub-threshold drift is information — quiet weeks should still show
# which way things are leaning.
_PCT_DRIFT_FIELDS = frozenset(
    {
        Field.PRICE,
        Field.MARKET_CAP,
        Field.REVENUE,
        Field.TOTAL_CASH,
        Field.TOTAL_DEBT,
        Field.ANALYST_TARGET_MEAN,
    }
)
_PP_DRIFT_FIELDS = frozenset(
    {
        Field.GROSS_MARGIN,
        Field.OPERATING_MARGIN,
        Field.FCF_MARGIN,
        Field.ROE,
        Field.REVENUE_GROWTH,
        Field.DIVIDEND_YIELD,
    }
)


def _drift_suffix(field: Field, value: float | str | date, baseline: Snapshot | None) -> str:
    """Run-over-run drift for a numeric watchlist line, e.g. ' (+6.6%)'.
    Empty when there is no accepted baseline value for the field (first
    sighting, prior quarantine/outage — the Changes section owns gap
    stories) or when the drift rounds to zero (genuinely unchanged)."""
    if baseline is None or not isinstance(value, (int, float)):
        return ""
    old = baseline.values.get(field)
    if old is None or not isinstance(old.value, (int, float)):
        return ""
    if field in _PCT_DRIFT_FIELDS:
        if old.value == 0:
            return ""
        rendered = f"{(value - old.value) / old.value * 100:+.1f}%"
        zero = "+0.0%", "-0.0%"
    elif field in _PP_DRIFT_FIELDS:
        rendered = f"{(value - old.value) * 100:+.1f}pp"
        zero = "+0.0pp", "-0.0pp"
    elif field is Field.ANALYST_COUNT:
        rendered = f"{value - old.value:+.0f}"
        zero = ("+0", "-0")
    else:  # P/E, PEG, debt/equity — absolute points
        rendered = f"{value - old.value:+.2f}"
        zero = "+0.00", "-0.00"
    if rendered in zero:
        return ""
    return f" ({rendered})"


def _no_data_cause(field: Field, ticker: TickerReport) -> str:
    """Why is this field absent? Blame any down/inapplicable source in the
    field's priority chain; otherwise the sources were fine and the field
    simply wasn't provided."""
    health = {h.source: h for h in ticker.sources}
    causes = []
    for source in SPECS[field].priority:
        h = health.get(source)
        if h is None or h.status == "ok":
            continue
        causes.append(f"{source.value}: {h.error or ('not applicable' if h.status == 'not_applicable' else 'error')}")
    return "; ".join(causes) if causes else "not provided"


# --- Formatting helpers -----------------------------------------------------


def _label(field: Field) -> str:
    return _FIELD_LABELS.get(field, field.value.replace("_", " "))


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _details(hits: Sequence[QuarantineHit]) -> str:
    return "; ".join(hit.detail for hit in hits)


def _days_phrase(days: int) -> str:
    if days == 0:
        return "today"
    return f"in {days} day" + ("" if days == 1 else "s")


def _eps_surprise_pct(actual: float, estimate: float | None) -> float | None:
    """(actual − estimate) / |estimate| · 100 — signed, so a beat is positive
    whatever the estimate's sign. None when there is nothing to be surprised
    against (no estimate, or a zero estimate makes the ratio undefined). One
    home for the earnings-wire + bellwether formula, in both artifacts."""
    if estimate is None or estimate == 0:
        return None
    return (actual - estimate) / abs(estimate) * 100


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
                # 999.96B rounds past its unit boundary — roll to 1.00T
                # rather than printing 1000.0B.
                bigger_divisor, bigger_suffix = units[i - 1]
                rendered = _render(value / bigger_divisor, bigger_suffix)
            return rendered
    return f"{value:.2f}"


def _fmt_value(field: Field, value: float | str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if field in (Field.MARKET_CAP, Field.REVENUE, Field.TOTAL_CASH, Field.TOTAL_DEBT):
        return _humanize_cap(value)
    if field is Field.ANALYST_COUNT:
        return f"{value:.0f}"
    if field in (
        Field.GROSS_MARGIN,
        Field.OPERATING_MARGIN,
        Field.FCF_MARGIN,
        Field.ROE,
        Field.REVENUE_GROWTH,
        Field.DIVIDEND_YIELD,
    ):
        return f"{value * 100:.1f}%"  # stored as a fraction; read as a percent
    return f"{value:.2f}"


def _cell(text: str) -> str:
    """Make arbitrary text safe inside a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _trimmed(lines: list[str]) -> list[str]:
    """Strip leading/trailing blank lines so section joins never double up
    (and no digest ever carries trailing-whitespace lines)."""
    start, end = 0, len(lines)
    while start < end and not lines[start]:
        start += 1
    while end > start and not lines[end - 1]:
        end -= 1
    return lines[start:end]


class Attachment(NamedTuple):
    """A binary artifact riding alongside the digest (e.g. the PDF report).
    Attachments are additive: a sink that only knows markdown still works."""

    filename: str
    content: bytes
    mime: str


@runtime_checkable
class DigestSink(Protocol):
    """Where a rendered digest goes. FileDigestSink ships in v1; email or
    notification sinks are additional implementations, no engine changes.
    The engine only passes `attachments` when there are any, so minimal
    sinks (and test stubs) may omit the parameter entirely."""

    def write(
        self, markdown: str, *, run_id: int, as_of: date, attachments: Sequence[Attachment] = ()
    ) -> Path | None: ...


class FileDigestSink:
    def __init__(self, reports_dir: Path) -> None:
        self.reports_dir = reports_dir

    def write(
        self, markdown: str, *, run_id: int, as_of: date, attachments: Sequence[Attachment] = ()
    ) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / f"digest-{as_of.isoformat()}-run{run_id}.md"
        path.write_text(markdown, encoding="utf-8")
        for attachment in attachments:
            (self.reports_dir / attachment.filename).write_bytes(attachment.content)
        return path


class EmailDigestSink:
    """Delivers the digest as a plain-text email (markdown reads fine as
    text) via authenticated SMTP submission — port 465 implicit TLS or
    587 STARTTLS. Built for Gmail-with-app-password but any submission
    server works. Network/auth failures raise; CompositeSink turns them
    into a disclosed delivery failure rather than silence."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipient: str,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.recipient = recipient

    def write(
        self, markdown: str, *, run_id: int, as_of: date, attachments: Sequence[Attachment] = ()
    ) -> None:
        import smtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["Subject"] = f"Argus digest — {as_of.isoformat()} — run {run_id}"
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content(markdown)
        for attachment in attachments:
            maintype, _, subtype = attachment.mime.partition("/")
            message.add_attachment(
                attachment.content,
                maintype=maintype,
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )

        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as smtp:
                smtp.login(self.username, self.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.username, self.password)
                smtp.send_message(message)
        return None


class DiscordDigestSink:
    """Posts a headline message (title, status, the Changes section) with the
    report attached to a Discord webhook. PDF-first (v1.8): the attachments —
    normally exactly the PDF report, which now carries the whole digest — ARE
    the delivered artifact; the markdown record rides along only as the
    fallback when no attachment exists (ARGUS_PDF=0 or a disclosed build
    failure), because an attachment-less post would deliver nothing but the
    headline. HTTP failures raise; CompositeSink turns them into a disclosed
    delivery failure rather than silence."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def write(
        self, markdown: str, *, run_id: int, as_of: date, attachments: Sequence[Attachment] = ()
    ) -> None:
        import httpx

        payload = {
            "content": _discord_headline(markdown),
            "allowed_mentions": {"parse": []},  # a digest must never ping anyone
        }
        if attachments:
            files = {
                f"files[{index}]": (a.filename, a.content, a.mime)
                for index, a in enumerate(attachments[:10])  # webhook cap: 10 files
            }
        else:
            files = {
                "files[0]": (
                    f"digest-{as_of.isoformat()}-run{run_id}.md",
                    markdown.encode("utf-8"),
                    "text/markdown",
                )
            }
        response = httpx.post(
            self.webhook_url,
            data={"payload_json": json.dumps(payload)},
            files=files,
            timeout=60.0,
        )
        response.raise_for_status()
        return None


_DISCORD_HEADLINE_LINES = 20
_DISCORD_HEADLINE_CHARS = 1900  # hard API limit is 2000; leave headroom


def _discord_headline(markdown: str) -> str:
    """Title + status + the Changes section, truncated — the message is the
    hook, the attachment is the report."""
    lines = markdown.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else "Argus digest"
    status = next((line for line in lines if line.startswith("Status:")), "")
    changes: list[str] = []
    in_changes = False
    for line in lines:
        if line.startswith("## "):
            # Watch digests hook with Changes; scout digests with Proposals.
            in_changes = line.strip() in ("## Changes", "## Conviction — the graded shortlist")
            continue
        if in_changes and line.strip():
            changes.append(line)
    shown = changes[:_DISCORD_HEADLINE_LINES]
    headline = "\n".join(part for part in (f"**{title}**", status, "", *shown) if part is not None)
    if len(changes) > len(shown):
        headline += "\n… (full digest attached)"
    if len(headline) > _DISCORD_HEADLINE_CHARS:
        headline = headline[:_DISCORD_HEADLINE_CHARS] + "\n… (truncated — full digest attached)"
    return headline.strip()


class DeliveryError(RuntimeError):
    """Some sink(s) failed AFTER the digest was rendered (and possibly after
    other sinks succeeded). Carries whatever path a successful file sink
    produced, so the caller can say 'written here, but not delivered'."""

    def __init__(self, message: str, digest_path: Path | None) -> None:
        super().__init__(message)
        self.digest_path = digest_path


class CompositeSink:
    """Fan a digest out to several sinks. Every sink is ATTEMPTED even when
    an earlier one fails — a broken mail server must not stop the file copy —
    then failures are raised together as DeliveryError. On a headless box an
    undelivered digest is an unseen digest; failure must be loud."""

    def __init__(self, *sinks: DigestSink) -> None:
        self.sinks = sinks

    def write(
        self, markdown: str, *, run_id: int, as_of: date, attachments: Sequence[Attachment] = ()
    ) -> Path | None:
        path: Path | None = None
        failures: list[str] = []
        for sink in self.sinks:
            try:
                if attachments:
                    result = sink.write(
                        markdown, run_id=run_id, as_of=as_of, attachments=attachments
                    )
                else:  # minimal sinks/test stubs may not accept the parameter
                    result = sink.write(markdown, run_id=run_id, as_of=as_of)
            except Exception as exc:
                # A webhook/SMTP error embeds the secret-bearing URL — scrub it
                # before it reaches the DeliveryError, the echo, or a run note.
                failures.append(f"{type(sink).__name__}: {redact(str(exc))}")
                continue
            if path is None and result is not None:
                path = result
        if failures:
            raise DeliveryError("; ".join(failures), digest_path=path)
        return path
