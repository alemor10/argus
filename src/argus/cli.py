"""Typer CLI. Parses args, resolves paths, calls engine/queries — no logic.

Exit-code policy (see ARCHITECTURE.md): 0 whenever a digest was produced
(complete OR partial — degradation is disclosed inside the digest, which is
the alerting channel); 1 only when no digest could be produced. A wrapper
that pages on nonzero must not page weekly on a flaky free feed.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Optional

import typer

import argus
from argus import engine
from argus.config import build_contexts, load_watch_config, resolve_paths, resolve_secrets
from argus.digest import FileDigestSink, render
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

# Argus never adds tickers on its own — uncomment and edit:
tickers: []
# tickers:
#   - ticker: NVDA
#     thesis: "Datacenter capex supercycle; CUDA moat."
#     thresholds: { price_move_pct: 8.0 }   # volatile name, raise the bar
#   - ticker: NTDOY
#     thesis: "Switch 2 cycle + IP monetization."
"""


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
    as_of = datetime.now(UTC)
    con = connect(paths.db)
    try:
        migrate(con)
        outcome = engine.run(
            contexts,
            con=con,
            sources=sources,
            profile=DEFAULT_PROFILE,
            sink=FileDigestSink(paths.reports),
            as_of=as_of,
            today=as_of.date(),
            app_version=argus.__version__,
        )
    finally:
        con.close()
    typer.echo(f"Run {outcome.run_id}: {outcome.status} ({len(contexts)} tickers)")
    for swept in outcome.swept_run_ids:
        typer.echo(
            f"Note: run {swept} crashed before producing a digest — its detected "
            f"events are recoverable with `argus report --run {swept}`."
        )
    if outcome.digest_path is not None:
        typer.echo(f"Digest: {outcome.digest_path}")
        raise typer.Exit(0)
    typer.echo("No digest produced — every ticker failed. See run_sources for causes.", err=True)
    raise typer.Exit(1)


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
    """Scaffold a starter watchlist.yaml (refuses to touch an existing one)."""
    paths = resolve_paths(root)
    if paths.watchlist.exists():
        typer.echo(f"{paths.watchlist} already exists — not touching it.", err=True)
        raise typer.Exit(1)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.watchlist.write_text(WATCHLIST_TEMPLATE, encoding="utf-8")
    typer.echo(f"Created {paths.watchlist} — edit it, then run `argus watch`.")


@app.command()
def scout(root: RootOpt = None) -> None:
    """Screen a broad universe for new candidates (proposes only)."""
    typer.echo(
        "scout is post-v1 — gated on the paid-data decision (see CLAUDE.md, "
        "Discovery data decision). It will reuse the same engine on a screener-fed "
        "ticker list with a stricter gate profile."
    )
    raise typer.Exit(1)


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
