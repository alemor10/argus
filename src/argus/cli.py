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
from pathlib import Path
from typing import Annotated, Optional

import typer

import argus
from argus import engine
from argus.config import (
    Paths,
    build_contexts,
    load_watch_config,
    load_watch_config_text,
    resolve_discord_webhook,
    resolve_email_config,
    resolve_paths,
    resolve_secrets,
)
from argus.digest import (
    CompositeSink,
    DigestSink,
    DiscordDigestSink,
    EmailDigestSink,
    FileDigestSink,
    render,
)
from argus.gates import DEFAULT_PROFILE
from argus.sources import EdgarSource, FinnhubSource, YahooSource
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
# `argus promote TICKER --thesis "..."` on a scout proposal:
tickers: []
# tickers:
#   - ticker: NVDA
#     thesis: "Datacenter capex supercycle; CUDA moat."
#     thresholds: { price_move_pct: 8.0 }   # volatile name, raise the bar
#   - ticker: NTDOY
#     thesis: "Switch 2 cycle + IP monetization."
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
top_n: 15                     # shortlist size sent through enrichment + gates
"""


def _build_sinks(paths: Paths) -> DigestSink:
    """File always; Discord/email when configured. Shared by watch and scout."""
    sinks: list[DigestSink] = [FileDigestSink(paths.reports)]
    webhook = resolve_discord_webhook()
    if webhook is not None:
        sinks.append(DiscordDigestSink(webhook))
    email = resolve_email_config()  # ValueError on half-configured — caller handles
    if email is not None:
        sinks.append(
            EmailDigestSink(
                host=email.host,
                port=email.port,
                username=email.username,
                password=email.password,
                sender=email.sender,
                recipient=email.recipient,
            )
        )
    if len(sinks) == 1:
        typer.echo(
            "No delivery channel configured (ARGUS_DISCORD_WEBHOOK / ARGUS_EMAIL_TO) — "
            "digest lands on disk only."
        )
        return sinks[0]
    return CompositeSink(*sinks)


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
        from argus.sources.yahoo import fetch_history

        if report.kind == "scout":
            tickers = [p.ticker for p in report.scout if p.status == "proposed"]
        else:
            tickers = [t.context.ticker for t in report.tickers if t.status != "failed"]
        history = {ticker: fetch_history(ticker) for ticker in tickers}
        filename = (
            f"argus-{report.kind}-{report.as_of.date().isoformat()}-run{report.run_id}.pdf"
        )
        return [Attachment(filename, build_pdf(report, history), "application/pdf")]

    return build


def _build_sources() -> list[DataSource]:
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
) -> None:
    """Run the monitor: fetch → gate → snapshot → diff → digest."""
    paths = resolve_paths(root, watchlist=watchlist, db=db, reports=reports)
    if not paths.watchlist.exists():
        typer.echo(f"No watchlist at {paths.watchlist} — run `argus init` first.", err=True)
        raise typer.Exit(1)
    contexts = build_contexts(load_watch_config(paths.watchlist))
    try:
        sink = _build_sinks(paths)
    except ValueError as exc:  # half-configured channel: refuse to run at all
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    as_of = datetime.now(UTC)
    con = connect(paths.db)
    try:
        migrate(con)
        outcome = engine.run(
            contexts,
            con=con,
            sources=_build_sources(),
            profile=DEFAULT_PROFILE,
            sink=sink,
            as_of=as_of,
            today=as_of.date(),
            app_version=argus.__version__,
            artifact_builder=_pdf_artifact_builder(),
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
    """Scaffold starter watchlist.yaml + scout.yaml (never touches existing files)."""
    paths = resolve_paths(root)
    created = []
    paths.root.mkdir(parents=True, exist_ok=True)
    for path, template in ((paths.watchlist, WATCHLIST_TEMPLATE), (paths.scout, SCOUT_TEMPLATE)):
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
def scout(root: RootOpt = None) -> None:
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
        sink = _build_sinks(paths)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
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


_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")


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
