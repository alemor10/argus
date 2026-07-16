"""Typer CLI. Parses args, resolves paths, calls engine/queries — no logic.

Exit-code policy (see ARCHITECTURE.md): 0 whenever the user will SEE a
digest (complete OR partial — data degradation is disclosed inside the
digest, which is the alerting channel); 1 when they won't: no digest was
produced, or it was produced but a delivery sink failed (on a headless box
an undelivered digest is an unseen digest). A wrapper that pages on nonzero
must not page weekly on a flaky free feed — and won't: partial data exits 0.
"""

import json
import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer

import argus
from argus import engine
from argus.config import (
    Paths,
    build_consider_contexts,
    build_contexts,
    build_macro_contexts,
    ensure_no_overlap,
    load_consider,
    load_macro_config,
    load_watch_config,
    load_watch_config_text,
    resolve_discord_webhook,
    resolve_email_config,
    resolve_paths,
    resolve_secrets,
)
from argus.fields import Source
from argus.digest import (
    CompositeSink,
    DigestSink,
    DiscordDigestSink,
    EmailDigestSink,
    FileDigestSink,
    render,
)
from argus.thesis import parse_thesis_check
from argus.gates import DEFAULT_PROFILE
from argus.sources import EdgarSource, FinnhubSource, FredSource, YahooSource
from argus.sources.base import DataSource
from argus.store import connect, migrate, queries

app = typer.Typer(
    no_args_is_help=True,
    help="Argus — personal equity monitor. Watches and reports; never acts.",
)

RootOpt = Annotated[
    Optional[Path],
    typer.Option("--root", help="Project directory (default: $ARGUS_HOME or cwd)."),
]

WATCHLIST_TEMPLATE = """\
# Argus watchlist — the human edits this file; Argus only reads it.
# Per-ticker `thresholds` override `defaults`; unknown keys are an error.

defaults:
  price_move_pct: 5.0
  target_move_pct: 10.0
  earnings_within_days: 7

# Argus never adds tickers on its own — uncomment and edit, or use
# `argus promote TICKER --thesis "..." --check "revenue_growth >= 20%"`.
#
# thesis_checks are YOUR falsifiable lines: the watch digest flags a breach
# against them (e.g. revenue_growth dropping below 20%). Grammar:
#   <field> <op> <value>   ops: >= <= > < == != (numbers), == != in (rating)
#   write margins/growth as percents ("gross_margin >= 65%").
tickers: []
# tickers:
#   - ticker: NVDA
#     thesis: "Datacenter capex supercycle; CUDA moat."
#     thresholds: { price_move_pct: 8.0 }   # volatile name, raise the bar
#     thesis_checks:
#       - "revenue_growth >= 20%"           # the supercycle claim
#       - "gross_margin >= 65%"             # the pricing-power / moat claim
#       - "analyst_rating in [strong_buy, buy]"
"""

MACRO_TEMPLATE = """\
# Argus macro watch — the market backdrop the digest carries beside your
# watchlist. Two series kinds behind one file:
#   yahoo (default) — live market quotes (^TNX yields, ^VIX, indexes)
#   fred            — official economic releases (CPI, jobs, policy rate),
#                     via the St. Louis Fed; a NEW PRINT is itself the alert.
#
# Alert knobs, all optional, all YOUR lines (Argus reports crossings, never
# interprets):
#   alert_move: absolute move in the series' OWN units since the last run
#               (0.15 on a yield = 15bp). At daily cadence that means DAILY
#               moves — slow drift shows in the Macro section's Δ instead.
#   alert_when: alert-WHEN-TRUE level lines, e.g. "value >= 25" pages while
#               VIX is at or above 25 (grammar: value <op> <number>).
#   sanity:     [low, high] plausibility band — outside renders "check
#               units" (guards a silent ×10 unit change at the source).
#
# Note: BTC-USD trades 24/7 — a Saturday alert_move can trigger a weekend
# delivery when everything else is closed.

series:
  - symbol: "^TNX"
    label: "US 10Y yield"
    unit: "%"
    alert_move: 0.15
    sanity: [0, 25]
  - symbol: "^IRX"
    label: "US 3M yield"
    unit: "%"
    sanity: [0, 25]
  - symbol: "^VIX"
    label: "VIX"
    alert_when: ["value >= 25"]
  - symbol: "^GSPC"
    label: "S&P 500"
    decimals: 0
  - symbol: "CPIAUCSL"
    source: fred
    transform: yoy_pct
    label: "CPI inflation (YoY)"
    unit: "%"
    decimals: 1
  - symbol: "PCEPILFE"
    source: fred
    transform: yoy_pct
    label: "Core PCE inflation (YoY)"
    unit: "%"
    decimals: 1
  - symbol: "UNRATE"
    source: fred
    label: "Unemployment rate"
    unit: "%"
    decimals: 1
  - symbol: "PAYEMS"
    source: fred
    transform: mom_change
    label: "Payrolls (MoM change)"
    unit: "k"
    decimals: 0
  - symbol: "DFF"
    source: fred
    label: "Fed funds (effective)"
    unit: "%"
  # -- crypto & commodities strip (note: BTC trades 24/7 — weekend moves are real) --
  - symbol: "DX-Y.NYB"
    label: "US dollar index"
  - symbol: "GC=F"
    label: "Gold (front future)"
    decimals: 0
  - symbol: "CL=F"
    label: "WTI crude (front future)"
  - symbol: "BTC-USD"
    label: "Bitcoin"
    decimals: 0
#  - symbol: "ICSA"
#    source: fred
#    label: "Initial jobless claims"
#    decimals: 0
#  - symbol: "MORTGAGE30US"
#    source: fred
#    label: "30Y mortgage rate"
#    unit: "%"
#  - symbol: "HOUST"
#    source: fred
#    label: "Housing starts (SAAR)"
#    decimals: 0
#  - symbol: "A191RL1Q225SBEA"
#    source: fred
#    label: "Real GDP (QoQ SAAR)"
#    unit: "%"
#    decimals: 1

# Megacap earnings context — dates + estimates upcoming, actual vs estimate
# as they land. Claims-labeled (finnhub, unverified), context only: it never
# triggers a delivery and never enters the gated store. Empty list turns the
# section off.
bellwethers: [AAPL, MSFT, NVDA, GOOGL, AMZN, META, AVGO, TSLA, BRK-B, JPM]

# Well-known ETFs to watch for rebalancing — Argus snapshots each one's daily
# holdings and reports when constituents are added or dropped (an index add is
# forced buying). Two issuer feeds: SPDR (SPY, DIA, sector XL*, SDY, MDY, …)
# and Vanguard (VOO, VTI, VYM, VUG, …). SCHD is Schwab (blocks headless) — use
# SDY or VYM as dividend stand-ins. An unsupported ticker is skipped with a
# note. Empty list turns the feature off.
etfs: [SPY, DIA, XLK, XLF, XLE, XLV, XLI, XLY, XLP, XLU, XLB, XLRE, XLC,
       VOO, VTI, VYM, VUG, SDY]
"""

CONSIDER_TEMPLATE = """\
# Argus consider list — the Radar's middle rung, MACHINE-managed:
#   argus consider TICKER    adds a name (no thesis required — "keep eyes on it")
#   argus promote TICKER     graduates it to the watchlist (and removes it here)
# Considered names are fetched, gated, and shown in every issue; they never
# alert-page you into the watchlist — that decision stays yours.
tickers: []
"""

SCOUT_TEMPLATE = """\
# Argus scout screening criteria — Quality-GARP, forward-looking. Every value
# shown is the default; delete a line to keep its default, edit to tune.
# Unknown keys are an error. Market-cap / volume floors apply server-side at
# the screener; the rest are local rules. See ARCHITECTURE.md, Scout.

min_market_cap: 2000000000    # $2B
min_avg_volume: 1000000       # 30-day average shares/day
max_forward_pe: 25.0          # what you pay for what comes NEXT (never naive low-P/E)
min_revenue_growth_pct: 10.0  # base-effect resistant, unlike TTM EPS growth
min_gross_margin_pct: 40.0
min_operating_margin_pct: 12.0
min_roe_pct: 15.0             # quality floor: cheap must also be good
max_debt_to_equity: 1.0
max_eps_decline_pct: -30.0    # value-trap guard: revenue up + earnings collapsing = trap
max_per_sector: 3             # shortlist concentration cap (0 disables) — no one-sector bets
top_n: 15                     # shortlist size sent through enrichment + gates
"""


class DeliverPolicy(str, Enum):
    """When the delivery channels (Discord/email) fire. The file sink always
    writes — the disk copy is the record; the channels are the pager.
    NEVER = file record only (the Sunday scout runs quietly at 09:00; the
    Sunday Edition delivers both PDFs in one post right after)."""

    ALWAYS = "always"
    EVENTS_ONLY = "events-only"
    NEVER = "never"


def _build_sinks(paths: Paths) -> tuple[DigestSink, list[DigestSink]]:
    """(file sink — written every run; delivery channels — Discord/email when
    configured). Callers compose them per delivery policy; kept flat so
    CompositeSink error messages stay readable."""
    file_sink = FileDigestSink(paths.reports)
    channels: list[DigestSink] = []
    webhook = resolve_discord_webhook()
    if webhook is not None:
        channels.append(DiscordDigestSink(webhook))
    email = resolve_email_config()  # ValueError on half-configured — caller handles
    if email is not None:
        channels.append(
            EmailDigestSink(
                host=email.host,
                port=email.port,
                username=email.username,
                password=email.password,
                sender=email.sender,
                recipient=email.recipient,
            )
        )
    if not channels:
        typer.echo(
            "No delivery channel configured (ARGUS_DISCORD_WEBHOOK / ARGUS_EMAIL_TO) — "
            "digest lands on disk only."
        )
    return file_sink, channels


def _compose_sinks(
    file_sink: DigestSink, channels: list[DigestSink], deliver: DeliverPolicy
) -> tuple[DigestSink, DigestSink | None]:
    """(always-sink, gated-sink-or-None) for engine.run. Under ALWAYS the
    channels ride with the file sink exactly as before; under EVENTS_ONLY
    they only fire when the run carries new information; under NEVER only
    the file record is written."""
    if deliver is DeliverPolicy.NEVER:
        return file_sink, None
    if deliver is DeliverPolicy.EVENTS_ONLY and channels:
        gated = channels[0] if len(channels) == 1 else CompositeSink(*channels)
        return file_sink, gated
    if channels:
        return CompositeSink(file_sink, *channels), None
    return file_sink, None


def _pdf_artifact_builder():
    """The PDF report attachment (ARGUS_PDF=0 disables). Charts use raw
    Yahoo history — ungated display data, captioned as such in the PDF;
    every table number remains gate-verified. Returns None when disabled."""
    from argus.config import pdf_enabled

    if not pdf_enabled():
        return None

    def build(report):
        from argus.digest import Attachment
        from argus.report_pdf import build_pdf
        from argus.sources.yahoo import fetch_annual_revenue, fetch_history

        if report.kind == "scout":
            tickers = [p.ticker for p in report.scout if p.status == "proposed"]
            macro_market: list[str] = []
        else:
            # Macro series get no detail pages; market-quote series DO get a
            # 30-day sparkline in the dashboard (ungated display data, like
            # the ticker charts). Econ prints show their period instead.
            tickers = [
                t.context.ticker
                for t in report.tickers
                if t.status != "failed" and t.context.macro is None
            ]
            macro_market = [
                t.context.ticker
                for t in report.tickers
                if t.context.macro is not None and t.context.macro.source is Source.YAHOO
            ]
        featured = [
            card.symbol
            for card in (report.market.features if report.market is not None else ())
        ]
        history = {ticker: fetch_history(ticker) for ticker in tickers}
        history |= {symbol: fetch_history(symbol, period="1mo") for symbol in macro_market}
        history |= {
            symbol: fetch_history(symbol)
            for symbol in featured
            if symbol not in history
        }
        revenue_series = {ticker: fetch_annual_revenue(ticker) for ticker in tickers}
        filename = (
            f"argus-{report.kind}-{report.as_of.date().isoformat()}-run{report.run_id}.pdf"
        )
        return [
            Attachment(filename, build_pdf(report, history, revenue_series), "application/pdf")
        ]

    return build


def _market_wire_step(bellwethers: tuple[str, ...], deliver: "DeliverPolicy"):
    """before_digest hook for MAGAZINE watch runs (--deliver always): one
    market scan + one calendar GET → the issue's market pages (movers, sector
    pulse, earnings wire, 52-week extremes), persisted per run. Quiet pulses
    (events-only) skip the wire — they are alerts, not issues. Failures
    append a run note; the digest must always land."""
    from datetime import date, timedelta

    from argus.store import writer

    if deliver is not DeliverPolicy.ALWAYS:
        return None
    api_key = resolve_secrets().finnhub_api_key
    pins = frozenset(symbol.strip().upper() for symbol in bellwethers)

    def step(con, run_id: int) -> None:
        from argus.market import MarketScanner, build_wire, fetch_feature_card, select_features

        today = date.today()
        try:
            rows = MarketScanner().scan()
        except Exception as exc:
            writer.append_run_note(con, run_id=run_id, note=f"market wire unavailable: {exc}")
            return
        calendar = []
        if api_key:
            try:
                calendar = FinnhubSource(api_key).earnings_calendar(
                    frm=today - timedelta(days=1), to=today + timedelta(days=7)
                )
            except Exception as exc:
                writer.append_run_note(
                    con, run_id=run_id, note=f"earnings calendar unavailable: {exc}"
                )
        wire = build_wire(rows, calendar, pins=pins, today=today)
        rows_by_symbol = {row.symbol.upper(): row for row in rows}
        cards = tuple(
            fetch_feature_card(symbol, why, rows_by_symbol)
            for symbol, why in select_features(wire)
        )
        wire = wire.model_copy(update={"features": cards})
        writer.write_market_wire(con, run_id=run_id, wire=wire)

    return step


def _etf_step(etfs: tuple[str, ...]):
    """before_digest hook on EVERY watch run (not just magazine ones — a
    rebalance should page even a quiet events-only Monday). Snapshots each
    ETF's membership and stores it ONLY when it changed since the last
    snapshot; the rebalance is then the diff (computed at report time).
    Per-ETF failures append a run note; the digest must always land."""
    from argus.store import queries, writer

    if not etfs:
        return None

    def step(con, run_id: int) -> None:
        from argus.etf import holdings_source_for, membership_diff

        for etf in etfs:
            symbol = etf.strip().upper()
            source = holdings_source_for(symbol)
            if source is None:  # no issuer feed for this ticker — disclosed, not guessed
                writer.append_run_note(
                    con, run_id=run_id, note=f"no holdings feed for {symbol} (unsupported issuer)"
                )
                continue
            try:
                current = source.fetch(symbol)
            except Exception as exc:
                writer.append_run_note(
                    con, run_id=run_id, note=f"etf holdings unavailable ({symbol}): {exc}"
                )
                continue
            prior = queries.latest_etf_holdings(con, symbol, run_id)
            if prior is None:  # first snapshot: baseline, stored but silent
                writer.write_etf_holdings(con, run_id=run_id, etf=symbol, holdings=current)
                continue
            added, dropped = membership_diff(prior, current)
            if added or dropped:  # store only on change — the change-log model
                writer.write_etf_holdings(con, run_id=run_id, etf=symbol, holdings=current)

    return step


def _insider_radar_step():
    """before_digest hook: fetch insider open-market buys for the scout
    shortlist names Argus is already surfacing (the 'anything of interest'
    set), excluding watchlist/consider names the engine already covered.
    Makes insider buying live without a promoted watchlist. Needs EDGAR
    configured; best-effort — a Form 4 outage yields no buys, never blocks."""
    from argus.store import queries, writer

    contact = resolve_secrets().edgar_contact_email
    if not contact:
        return None

    def step(con, run_id: int) -> None:
        shortlist = {p.ticker for p in queries._radar_shortlist(con, run_id)}
        if not shortlist:
            return
        watched = {
            row["ticker"]
            for row in con.execute("SELECT ticker FROM run_tickers WHERE run_id = ?", (run_id,))
        }
        source = EdgarSource(contact)
        for ticker in sorted(shortlist - watched):
            if not source.covers(ticker):
                continue  # not an EDGAR filer (OTC ADR / ETF)
            buys = source.insider_buys(ticker)
            if buys:
                # No per-ticker row for these shortlist names — write a
                # minimal run_tickers-free path: insider rows only.
                writer.write_insider_transactions(con, run_id=run_id, insider=buys)

    return step


def _compose_before_digest(*steps):
    """Run several before_digest hooks in order (engine takes one)."""
    live = [s for s in steps if s is not None]
    if not live:
        return None

    def run(con, run_id: int) -> None:
        for step in live:
            step(con, run_id)

    return run


def _build_sources(fred_series: dict[str, str] | None = None) -> list[DataSource]:
    secrets = resolve_secrets()
    sources: list[DataSource] = [YahooSource()]
    if secrets.finnhub_api_key:
        sources.append(FinnhubSource(secrets.finnhub_api_key))
    else:
        typer.echo("FINNHUB_API_KEY unset — price cross-checks will be skipped and disclosed.")
    if secrets.edgar_contact_email:
        sources.append(EdgarSource(secrets.edgar_contact_email))
    else:
        typer.echo(
            "ARGUS_CONTACT_EMAIL unset — EDGAR fundamentals cross-checks will be "
            "skipped and disclosed."
        )
    if fred_series:  # keyless; wired only when macro.yaml names fred series
        sources.append(FredSource(fred_series))
    return sources


def _exit_for(outcome: engine.RunOutcome) -> None:
    """The exit-code policy, shared by watch and scout: nonzero iff the user
    will not see a digest."""
    for swept in outcome.swept_run_ids:
        typer.echo(
            f"Note: run {swept} crashed before producing a digest — its detected "
            f"events are recoverable with `argus report --run {swept}`."
        )
    if outcome.attachment_error is not None:
        # Non-fatal: the digest still delivered; the PDF just didn't ride along.
        typer.echo(f"Note: {outcome.attachment_error}", err=True)
    if outcome.delivery_error is not None:
        location = (
            f"written to {outcome.digest_path} but NOT delivered"
            if outcome.digest_path is not None
            else "NOT delivered anywhere"
        )
        typer.echo(f"Digest {location}: {outcome.delivery_error}", err=True)
        raise typer.Exit(1)  # undelivered = unseen on a headless box
    if outcome.digest_path is not None:
        typer.echo(f"Digest: {outcome.digest_path}")
        raise typer.Exit(0)
    if outcome.status != "failed":
        raise typer.Exit(0)  # delivered through a pathless sink
    typer.echo("No digest produced. See run_sources for causes.", err=True)
    raise typer.Exit(1)


@app.command()
def watch(
    root: RootOpt = None,
    watchlist: Annotated[Optional[Path], typer.Option(help="Path to watchlist.yaml.")] = None,
    db: Annotated[Optional[Path], typer.Option(help="Path to the SQLite database.")] = None,
    reports: Annotated[Optional[Path], typer.Option(help="Digest output directory.")] = None,
    deliver: Annotated[
        DeliverPolicy,
        typer.Option(
            "--deliver",
            help="When Discord/email fire: 'always' (weekly anchor) or 'events-only' "
            "(daily cadence — channels post only when the run carries new information; "
            "the file digest is always written).",
        ),
    ] = DeliverPolicy.ALWAYS,
) -> None:
    """Run the monitor: fetch → gate → snapshot → diff → digest."""
    paths = resolve_paths(root, watchlist=watchlist, db=db, reports=reports)
    if not paths.watchlist.exists():
        typer.echo(f"No watchlist at {paths.watchlist} — run `argus init` first.", err=True)
        raise typer.Exit(1)
    try:
        macro_config = load_macro_config(paths.macro)
        macro_contexts = build_macro_contexts(macro_config)
        watch_contexts = build_contexts(load_watch_config(paths.watchlist))
        consider_contexts = build_consider_contexts(load_consider(paths.consider), watch_contexts)
        contexts = ensure_no_overlap(watch_contexts + consider_contexts, macro_contexts)
    except Exception as exc:  # typo'd key / bad line / overlap: crisp refusal
        typer.echo(f"Cannot build run contexts: {exc}", err=True)
        raise typer.Exit(1) from exc
    fred_series = {
        ctx.ticker: ctx.macro.transform
        for ctx in macro_contexts
        if ctx.macro is not None and ctx.macro.source is Source.FRED
    }
    try:
        file_sink, channels = _build_sinks(paths)
    except ValueError as exc:  # half-configured channel: refuse to run at all
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    sink, gated_sink = _compose_sinks(file_sink, channels, deliver)
    as_of = datetime.now(UTC)
    con = connect(paths.db)
    try:
        migrate(con)
        outcome = engine.run(
            contexts,
            con=con,
            sources=_build_sources(fred_series),
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=as_of,
            today=as_of.date(),
            app_version=argus.__version__,
            artifact_builder=_pdf_artifact_builder(),
            gated_sink=gated_sink,
            before_digest=_compose_before_digest(
                _market_wire_step(macro_config.bellwethers, deliver),
                _etf_step(macro_config.etfs),
                _insider_radar_step(),
            ),
        )
    finally:
        con.close()
    typer.echo(f"Run {outcome.run_id}: {outcome.status} ({len(contexts)} tickers)")
    _exit_for(outcome)


@app.command()
def report(
    run: Annotated[int, typer.Option("--run", help="Run ID to regenerate the digest for.")],
    root: RootOpt = None,
) -> None:
    """Regenerate the digest for a past run, bit-for-bit, from the store."""
    paths = resolve_paths(root)
    if not paths.db.exists():
        typer.echo(f"No database at {paths.db} — nothing to report on.", err=True)
        raise typer.Exit(1)
    con = connect(paths.db)
    try:
        migrate(con)
        try:
            run_report = queries.run_report(con, run)
        except ValueError as exc:
            typer.echo(f"Cannot report on run {run}: {exc}", err=True)
            raise typer.Exit(1) from exc
    finally:
        con.close()
    path = FileDigestSink(paths.reports).write(
        render(run_report), run_id=run, as_of=run_report.as_of.date()
    )
    typer.echo(f"Regenerated digest for run {run}: {path}")


@app.command()
def init(root: RootOpt = None) -> None:
    """Scaffold starter watchlist.yaml + scout.yaml + macro.yaml (never
    touches existing files)."""
    paths = resolve_paths(root)
    created = []
    paths.root.mkdir(parents=True, exist_ok=True)
    for path, template in (
        (paths.watchlist, WATCHLIST_TEMPLATE),
        (paths.scout, SCOUT_TEMPLATE),
        (paths.macro, MACRO_TEMPLATE),
        (paths.consider, CONSIDER_TEMPLATE),
    ):
        if path.exists():
            typer.echo(f"{path} already exists — not touching it.", err=True)
            continue
        path.write_text(template, encoding="utf-8")
        created.append(path)
    if not created:
        raise typer.Exit(1)
    for path in created:
        typer.echo(f"Created {path}")
    typer.echo("Edit them, then run `argus watch` and `argus scout`.")


@app.command()
def scout(
    root: RootOpt = None,
    deliver: Annotated[
        DeliverPolicy,
        typer.Option(
            "--deliver",
            help="'always' posts the scout report; 'never' writes the file record only "
            "(the Sunday Edition delivers it minutes later in one post).",
        ),
    ] = DeliverPolicy.ALWAYS,
) -> None:
    """Screen the universe for new candidates and propose a shortlist.

    Proposes only: nothing is ever added to the watchlist — promote a
    proposal yourself with `argus promote TICKER --thesis "..."`."""
    from argus.scout.criteria import load_scout_criteria
    from argus.scout.run import run_scout
    from argus.scout.screener import TradingViewScreener

    paths = resolve_paths(root)
    try:
        criteria = load_scout_criteria(paths.scout)
    except Exception as exc:  # typo'd key / malformed YAML: crisp refusal, no traceback
        typer.echo(f"Cannot load {paths.scout}: {exc}", err=True)
        raise typer.Exit(1) from exc
    exclude: set[str] = set()
    if paths.watchlist.exists():
        exclude = {
            context.ticker.upper()
            for context in build_contexts(load_watch_config(paths.watchlist))
        }
    try:
        file_sink, channels = _build_sinks(paths)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    sink, _ = _compose_sinks(file_sink, channels, deliver)
    as_of = datetime.now(UTC)
    con = connect(paths.db)
    try:
        migrate(con)
        outcome = run_scout(
            con=con,
            screener=TradingViewScreener(),
            criteria=criteria,
            sources=_build_sources(),
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=as_of,
            today=as_of.date(),
            app_version=argus.__version__,
            exclude=exclude,
            artifact_builder=_pdf_artifact_builder(),
        )
    finally:
        con.close()
    typer.echo(f"Scout run {outcome.run_id}: {outcome.status}")
    _exit_for(outcome)


@app.command()
def recap(
    root: RootOpt = None,
    week_ending: Annotated[
        Optional[str],
        typer.Option(
            "--week-ending",
            help="ISO date the week ends on (inclusive); default today. Regenerates "
            "any past week from the store (only the week-ahead section is print-time).",
        ),
    ] = None,
) -> None:
    """The Sunday Edition: the week in one PDF — events, macro week-over-week,
    shortlist churn, scorecard, and the week ahead — read from the store and
    delivered with the morning's scout report attached."""
    from datetime import date as date_, timedelta

    from argus.digest import Attachment
    from argus.recap import build_recap, build_recap_pdf, render_recap

    paths = resolve_paths(root)
    if not paths.db.exists():
        typer.echo(f"No database at {paths.db} — nothing to recap.", err=True)
        raise typer.Exit(1)
    ending = date_.fromisoformat(week_ending) if week_ending else date_.today()
    macro_config = load_macro_config(paths.macro)

    # The one print-time fetch: next week's pinned reporters (labeled, not archived).
    week_ahead: list = []
    note: str | None = None
    api_key = resolve_secrets().finnhub_api_key
    pins = {s.strip().upper() for s in macro_config.bellwethers}
    if api_key and pins:
        try:
            calendar = FinnhubSource(api_key).earnings_calendar(
                frm=ending, to=ending + timedelta(days=7)
            )
            week_ahead = [r for r in calendar if r.symbol.upper() in pins]
            others = len(calendar) - len(week_ahead)
            note = f"{others} more companies report next week (unfiltered count)."
        except Exception as exc:  # the edition must land; the calendar is context
            note = f"week-ahead calendar unavailable: {exc}"
    else:
        note = "week-ahead calendar not configured (FINNHUB_API_KEY / bellwethers)."

    con = connect(paths.db)
    try:
        migrate(con)
        report = build_recap(con, week_ending=ending, week_ahead=week_ahead, week_ahead_note=note)
    finally:
        con.close()
    markdown = render_recap(report)
    pdf = build_recap_pdf(report)

    paths.reports.mkdir(parents=True, exist_ok=True)
    md_path = paths.reports / f"sunday-edition-{ending.isoformat()}.md"
    md_path.write_text(markdown, encoding="utf-8")
    pdf_name = f"argus-sunday-edition-{ending.isoformat()}.pdf"
    (paths.reports / pdf_name).write_bytes(pdf)

    attachments = [Attachment(pdf_name, pdf, "application/pdf")]
    if report.scout_run_id is not None:
        # One Sunday post: the edition + the morning's full scout report.
        scout_pdf = paths.reports / f"argus-scout-{ending.isoformat()}-run{report.scout_run_id}.pdf"
        if scout_pdf.exists():
            attachments.append(
                Attachment(scout_pdf.name, scout_pdf.read_bytes(), "application/pdf")
            )
    try:
        _file_sink, channels = _build_sinks(paths)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    failures = []
    for channel in channels:
        try:
            channel.write(
                markdown,
                run_id=report.scout_run_id or 0,
                as_of=ending,
                attachments=tuple(attachments),
            )
        except Exception as exc:  # undelivered = unseen on a headless box
            failures.append(f"{type(channel).__name__}: {exc}")
    typer.echo(f"Sunday Edition: {md_path}")
    if failures:
        typer.echo("Edition written but NOT delivered: " + "; ".join(failures), err=True)
        raise typer.Exit(1)


_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")


def _write_consider(path: Path, tickers: tuple[str, ...]) -> None:
    """consider.yaml is MACHINE-managed — full rewrite (atomic) is the
    contract, unlike the human-owned watchlist that promote only appends to."""
    header = CONSIDER_TEMPLATE.rsplit("tickers:", 1)[0]
    body = "tickers: [" + ", ".join(tickers) + "]\n" if tickers else "tickers: []\n"
    staging = path.with_suffix(".yaml.tmp")
    staging.write_text(header + body, encoding="utf-8")
    staging.replace(path)


@app.command()
def consider(
    ticker: Annotated[
        str, typer.Argument(help="Ticker to keep eyes on — the Radar's middle rung.")
    ],
    root: RootOpt = None,
) -> None:
    """Add a name to the consider list: tracked through the full fetch→gate
    pipeline and shown in every issue, no thesis required. Graduate it with
    `argus promote` when conviction forms. Human-invoked only."""
    symbol = ticker.strip().upper()
    if not _TICKER_RE.match(symbol):
        typer.echo(f"'{ticker}' does not look like a ticker symbol.", err=True)
        raise typer.Exit(1)
    paths = resolve_paths(root)
    if paths.watchlist.exists():
        watch = build_contexts(load_watch_config(paths.watchlist))
        if any(c.ticker.upper() == symbol for c in watch):
            typer.echo(f"{symbol} is already on the watchlist — nothing to consider.", err=True)
            raise typer.Exit(1)
    existing = tuple(t.strip().upper() for t in load_consider(paths.consider).tickers)
    if symbol in existing:
        typer.echo(f"{symbol} is already being considered.", err=True)
        raise typer.Exit(1)
    _write_consider(paths.consider, (*existing, symbol))
    typer.echo(
        f"{symbol} is on the Radar — gated tracking starts with the next issue. "
        f'Graduate it with: argus promote {symbol} --thesis "..."'
    )


@app.command()
def promote(
    ticker: Annotated[str, typer.Argument(help="Ticker to add to the watchlist.")],
    thesis: Annotated[
        str,
        typer.Option(
            "--thesis",
            help="Why you believe in this name — writing it IS the decision. Required.",
        ),
    ],
    check: Annotated[
        Optional[list[str]],
        typer.Option(
            "--check",
            help='Falsifiable thesis condition, repeatable — e.g. --check "revenue_growth >= 20%". '
            "Watch flags a breach against your own line.",
        ),
    ] = None,
    root: RootOpt = None,
) -> None:
    """Add a scout proposal (or any ticker) to the watchlist — human-invoked
    only; the scheduled path can never call this."""
    symbol = ticker.strip().upper()
    if not _TICKER_RE.match(symbol):
        typer.echo(f"'{ticker}' does not look like a ticker symbol.", err=True)
        raise typer.Exit(1)
    if not thesis.strip():
        typer.echo("An empty thesis is not a decision — write why.", err=True)
        raise typer.Exit(1)
    checks = [c.strip() for c in (check or []) if c.strip()]
    for raw in checks:  # validate before writing — a bad check must fail loud now
        try:
            parse_thesis_check(raw)
        except ValueError as exc:
            typer.echo(f"Bad thesis check {raw!r}: {exc}", err=True)
            raise typer.Exit(1) from exc
    paths = resolve_paths(root)
    if not paths.watchlist.exists():
        typer.echo(f"No watchlist at {paths.watchlist} — run `argus init` first.", err=True)
        raise typer.Exit(1)
    existing = build_contexts(load_watch_config(paths.watchlist))
    if any(context.ticker.upper() == symbol for context in existing):
        typer.echo(f"{symbol} is already on the watchlist — not touching it.", err=True)
        raise typer.Exit(1)

    original = paths.watchlist.read_text(encoding="utf-8")
    entry = f"  - ticker: {symbol}\n    thesis: {json.dumps(thesis.strip())}\n"
    if checks:
        entry += "    thesis_checks:\n" + "".join(
            f"      - {json.dumps(raw)}\n" for raw in checks
        )
    if re.search(r"^tickers:\s*\[\]\s*$", original, flags=re.MULTILINE):
        # Replacement via lambda: a plain string here is a TEMPLATE, and the
        # json.dumps-escaped thesis would be re-interpreted (backslashes
        # collapse, \g crashes) — silent corruption of the user's words.
        updated = re.sub(
            r"^tickers:\s*\[\]\s*$",
            lambda _match: f"tickers:\n{entry.rstrip()}",
            original,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        # Appending works when the tickers list is the file's last section —
        # true for the scaffold and for files grown by promote itself.
        updated = original if original.endswith("\n") else original + "\n"
        updated += entry
    # Never write a watchlist that does not parse back: validate first.
    try:
        contexts = build_contexts(load_watch_config_text(updated))
    except Exception as exc:
        typer.echo(
            f"Refusing to write: the updated watchlist would not parse ({exc}). "
            f"Add the entry to {paths.watchlist} by hand.",
            err=True,
        )
        raise typer.Exit(1) from exc
    if len(contexts) != len(existing) + 1:
        typer.echo(
            "Refusing to write: the appended entry would not round-trip cleanly. "
            f"Add it to {paths.watchlist} by hand.",
            err=True,
        )
        raise typer.Exit(1)
    # Atomic replace: a crash mid-write must never truncate the watchlist.
    staging = paths.watchlist.with_suffix(".yaml.tmp")
    staging.write_text(updated, encoding="utf-8")
    staging.replace(paths.watchlist)
    # Graduation: a promoted name leaves the consider tier (one row per
    # ticker per run — the tiers are exclusive by construction).
    considered = tuple(t.strip().upper() for t in load_consider(paths.consider).tickers)
    if symbol in considered:
        _write_consider(paths.consider, tuple(t for t in considered if t != symbol))
        typer.echo(f"{symbol} graduated off the consider list.")
    typer.echo(f"{symbol} promoted to the watchlist — it will appear in the next watch digest.")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"argus {argus.__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        Optional[bool],
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    pass


if __name__ == "__main__":
    app()
