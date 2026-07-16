"""The one loop. The only module that touches sources, gates, and store
together — and the seam scout reuses: it operates on list[TickerContext],
never on "the watchlist".

Flow per run (see ARCHITECTURE.md, Data flow):
  open (sweep stale runs, begin_run)
  per ticker, sequentially:
    fetch   each covering source; adapter exceptions → run_sources error rows,
            never fatal to other sources or tickers
    gate    gates.run_gates → GatedObservation list, primaries resolved
    persist one transaction — durable and baseline-eligible from commit
    diff    changes.detect against the per-ticker baseline → events persisted
  close (finish_run: complete / partial / failed)
  digest  queries.run_report → digest.render → sink (written whenever ANY
          data was produced — silence is a statement)
"""

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from argus import changes
from argus.digest import Attachment, DeliveryError, DigestSink, render
from argus.gates import GateProfile, run_gates
from argus.models import (
    AnalystActionRecord,
    CompanyProfile,
    EarningsResultRecord,
    InsiderTransaction,
    ParseFailure,
    RawObservation,
    SourceHealth,
    TickerContext,
    require_aware,
)
from argus.sources.base import DataSource
from argus.store import queries, writer


@dataclass(frozen=True)
class RunOutcome:
    run_id: int
    # The caller's exit code hinges on status alone: nonzero iff "failed"
    # (failed ⇔ no digest was produced).
    status: Literal["complete", "partial", "failed"]
    # Where the sink wrote the digest if it produced a file; None for
    # non-file sinks and for failed runs. Informational only.
    digest_path: Path | None
    # Crashed runs swept to 'failed' at startup: their committed events were
    # never digested — the caller should offer `argus report --run N`.
    swept_run_ids: tuple[int, ...] = ()
    # Set when the digest was rendered but one or more sinks failed to
    # deliver it (e.g. mail server down). The file copy may still exist at
    # digest_path. An undelivered digest is an unseen digest — the caller
    # must surface this and exit nonzero.
    delivery_error: str | None = None
    # Set when the artifact builder (PDF report) failed: the digest still
    # delivers without attachments — degraded, disclosed, never blocking.
    attachment_error: str | None = None


@dataclass
class _TickerFetch:
    observations: list[RawObservation]
    parse_failures: list[ParseFailure]
    actions: list[AnalystActionRecord]
    earnings: list[EarningsResultRecord]
    insider: list[InsiderTransaction]
    health: list[SourceHealth]
    profile: CompanyProfile | None = None  # first source to offer one wins

    @property
    def status(self) -> Literal["ok", "partial", "failed"]:
        """ok: every applicable source delivered; partial: at least one did;
        failed: none did (incl. the no-source-covers-this-ticker edge)."""
        ok = sum(1 for h in self.health if h.status == "ok")
        errors = sum(1 for h in self.health if h.status == "error")
        if ok and not errors:
            return "ok"
        if ok:
            return "partial"
        return "failed"

    @property
    def error(self) -> str | None:
        messages = [f"{h.source}: {h.error}" for h in self.health if h.status == "error"]
        if not messages and not any(h.status == "ok" for h in self.health):
            messages = ["no source covers this ticker"]
        return "; ".join(messages) or None


def _write_digest(
    sink: DigestSink,
    markdown: str,
    *,
    run_id: int,
    as_of: datetime,
    attachments: Sequence[Attachment],
) -> tuple[Path | None, str | None]:
    """One sink write → (path, delivery error). A DeliveryError is disclosed,
    never fatal — the caller decides the exit code."""
    try:
        if attachments:
            path = sink.write(
                markdown, run_id=run_id, as_of=as_of.date(), attachments=attachments
            )
        else:
            path = sink.write(markdown, run_id=run_id, as_of=as_of.date())
    except DeliveryError as exc:
        return exc.digest_path, str(exc)
    return path, None


def _fetch_ticker(ticker: str, sources: Sequence[DataSource]) -> _TickerFetch:
    fetch = _TickerFetch([], [], [], [], [], [])
    for source in sources:
        started = time.perf_counter()
        try:
            if not source.covers(ticker):
                fetch.health.append(
                    SourceHealth(source=source.source_id, status="not_applicable")
                )
                continue
            result = source.fetch(ticker)
        except Exception as exc:  # a source dying must never touch the others
            fetch.health.append(
                SourceHealth(
                    source=source.source_id,
                    status="error",
                    error=str(exc) or type(exc).__name__,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            continue
        fetch.observations.extend(result.observations)
        fetch.parse_failures.extend(result.parse_failures)
        fetch.actions.extend(result.analyst_actions)
        fetch.earnings.extend(result.earnings_results)
        fetch.insider.extend(result.insider_transactions)
        if fetch.profile is None and result.profile is not None:
            fetch.profile = result.profile
        fetch.health.append(
            SourceHealth(
                source=source.source_id,
                status="ok",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        )
    return fetch


def run(
    contexts: Sequence[TickerContext],
    *,
    con: sqlite3.Connection,
    sources: Sequence[DataSource],
    profile: GateProfile,
    sink: DigestSink,
    as_of: datetime,
    today: date,
    app_version: str,
    kind: Literal["watch", "scout"] = "watch",
    before_digest: Callable[[sqlite3.Connection, int], None] | None = None,
    artifact_builder: Callable[..., Sequence[Attachment]] | None = None,
    gated_sink: DigestSink | None = None,
) -> RunOutcome:
    """Execute one run. `as_of`/`today` are injected — nothing below the CLI
    reads the clock, which is what makes golden end-to-end tests exact.
    `as_of` MUST be timezone-aware UTC (models.require_aware at entry).

    Scout runs skip the diff phase: candidate sets churn weekly, and their
    continuity is carried by scout_candidates streaks, not change events.
    `before_digest(con, run_id)` runs after finish_run and before the report
    is assembled — scout uses it to persist candidate verdicts so the digest
    reads them from SQL like everything else.

    `sink` is written on every digesting run; `gated_sink` (the events-only
    delivery policy) only when the run carries NEW information
    (changes.has_new_information) — a skipped gated delivery is policy, not
    failure, and is disclosed in the digest header via a run note."""
    require_aware(as_of)
    swept = writer.sweep_stale_runs(con, now=as_of)
    run_id = writer.begin_run(con, kind=kind, started_at=as_of, app_version=app_version)

    any_data = False
    all_ok = True
    for ctx in contexts:
        # Two guards, split at the persistence commit: the pre-commit handler
        # may write the failed run_tickers row (nothing was persisted — the
        # writer's single transaction rolled back), but the post-commit
        # handler must NOT re-insert — the ticker's data is already durable
        # and baseline-eligible; only its diff for this run is lost, and the
        # degradation is disclosed instead of cascading into the other
        # tickers via a primary-key violation.
        try:
            # A macro series is fetched from the ONE source its spec names —
            # Finnhub covers() says yes to everything and would add per-run
            # health noise for index symbols; FRED must never be consulted
            # for equities.
            ticker_sources = (
                sources
                if ctx.macro is None
                else [s for s in sources if s.source_id == ctx.macro.source]
            )
            fetch = _fetch_ticker(ctx.ticker, ticker_sources)
            gated = run_gates(profile, fetch.observations, fetch.parse_failures, as_of)
            status = fetch.status
            writer.write_ticker_result(
                con,
                run_id=run_id,
                context=ctx,
                gated=gated,
                actions=fetch.actions,
                source_health=fetch.health,
                status=status,
                error=fetch.error,
                company_profile=fetch.profile,
                earnings=fetch.earnings,
                insider=fetch.insider,
            )
        except Exception as exc:  # a ticker dying must never touch the others
            writer.write_ticker_result(
                con,
                run_id=run_id,
                context=ctx,
                gated=[],
                actions=[],
                source_health=[],
                status="failed",
                error=f"unexpected: {exc}",
            )
            status = "failed"
        if status != "failed" and kind == "watch":
            try:
                baseline_id = queries.baseline_run(con, ctx.ticker, run_id)
                baseline = (
                    queries.snapshot(con, baseline_id, ctx.ticker)
                    if baseline_id is not None
                    else None
                )
                current = queries.snapshot(con, run_id, ctx.ticker)
                assert current is not None  # write_ticker_result just committed it
                events = changes.detect(
                    baseline,
                    current,
                    ctx,
                    queries.new_analyst_actions(con, run_id, ctx.ticker),
                    today,
                    latest_accepted=lambda field, _t=ctx.ticker: queries.latest_accepted(
                        con, _t, field, run_id
                    ),
                    new_earnings=queries.new_earnings_results(con, run_id, ctx.ticker),
                    new_insider=queries.new_insider_transactions(con, run_id, ctx.ticker),
                )
                # Known narrow window: this commit is separate from the data
                # commit above, so a crash exactly between them loses this
                # ticker's events for the window (the data itself is durable
                # and shows in the next watchlist). Accepted tradeoff — see
                # ARCHITECTURE.md.
                writer.record_events(
                    con,
                    run_id=run_id,
                    ticker=ctx.ticker,
                    events=events,
                    baseline_run_id=baseline_id,
                )
            except Exception:
                # Data is committed; the run degrades to partial and the
                # ticker still renders (snapshot, no events) in the digest.
                all_ok = False
        if status != "failed":
            any_data = True
        if status != "ok":
            all_ok = False

    run_status: Literal["complete", "partial", "failed"]
    if all_ok:  # vacuously complete for an empty watchlist — still digests
        run_status = "complete"
    elif any_data:
        run_status = "partial"
    else:
        run_status = "failed"
    writer.finish_run(con, run_id=run_id, status=run_status, finished_at=as_of)
    if before_digest is not None:
        before_digest(con, run_id)  # persists regardless of run status

    digest_path: Path | None = None
    delivery_error: str | None = None
    attachment_error: str | None = None
    # Scout runs digest even when failed: the candidate verdicts persisted by
    # before_digest ("every fetch died" is a full page of exclusions) are
    # information, and the outage path already sets this precedent.
    if run_status != "failed" or kind == "scout":
        report = queries.run_report(con, run_id)
        deliver_gated = False
        if gated_sink is not None:
            deliver_gated = changes.has_new_information(report)
            if not deliver_gated:
                # The skip must be visible in the digest ITSELF (header note),
                # so the file copy and any later `report --run N` regeneration
                # agree — rebuild the report after the note lands.
                writer.append_run_note(
                    con, run_id=run_id, note="delivery skipped: no new events (events-only)"
                )
                report = queries.run_report(con, run_id)
        attachments: Sequence[Attachment] = ()
        if artifact_builder is not None:
            try:
                attachments = tuple(artifact_builder(report))
            except Exception as exc:  # attachments are optional; the digest is not
                attachment_error = f"report attachment failed: {exc}"
                writer.append_run_note(con, run_id=run_id, note=attachment_error)
        markdown = render(report)
        digest_path, delivery_error = _write_digest(
            sink, markdown, run_id=run_id, as_of=as_of, attachments=attachments
        )
        if deliver_gated:
            gated_path, gated_error = _write_digest(
                gated_sink, markdown, run_id=run_id, as_of=as_of, attachments=attachments
            )
            if digest_path is None:
                digest_path = gated_path
            if gated_error is not None:
                delivery_error = (
                    gated_error
                    if delivery_error is None
                    else f"{delivery_error}; {gated_error}"
                )
    return RunOutcome(
        run_id=run_id,
        status=run_status,
        digest_path=digest_path,
        swept_run_ids=tuple(swept),
        delivery_error=delivery_error,
        attachment_error=attachment_error,
    )
