"""Scout orchestration: screen the universe, enrich survivors through the
SAME engine (kind='scout'), persist candidate verdicts, digest.

Stricter-than-watch eligibility lives here: a candidate whose core fields
(price, and P/E or PEG) are missing or quarantined after enrichment is
EXCLUDED from the proposal list with the reason shown — unknown names skew
thinner-data, and scout proposes only clean ones. A screener outage still
produces a digest that says so (silence is a statement), exits through the
normal delivery path, and marks the run failed in the store.
"""

import sqlite3
from collections.abc import Sequence, Set
from datetime import date, datetime

from argus import engine
from argus.digest import DeliveryError, DigestSink, render
from argus.fields import Field
from argus.gates import GateProfile
from argus.models import ScoutCandidateRecord, TickerContext
from argus.scout.criteria import ScoutCriteria, ScreenedCandidate, screen
from argus.scout.screener import Screener, ScreenerError
from argus.sources.base import DataSource
from argus.store import queries, writer


def run_scout(
    *,
    con: sqlite3.Connection,
    screener: Screener,
    criteria: ScoutCriteria,
    sources: Sequence[DataSource],
    profile: GateProfile,
    sink: DigestSink,
    as_of: datetime,
    today: date,
    app_version: str,
    exclude: Set[str],
) -> engine.RunOutcome:
    """One scout run. `exclude` is the current watchlist (already-watched
    names are never proposed). Screener values only select candidates —
    every reported number comes from the enrichment pipeline."""
    try:
        rows = screener.scan(
            min_market_cap=criteria.min_market_cap, min_avg_volume=criteria.min_avg_volume
        )
    except ScreenerError as exc:
        return _outage_run(con, sink, as_of, app_version, str(exc))

    candidates = screen(rows, criteria, exclude)
    # TradingView reports dotted class shares (BRK.B); house symbology — and
    # the fetch stack — use dashes. Normalized ONCE here so run_tickers,
    # observations, and scout_candidates all share one spelling.
    contexts = [TickerContext(ticker=_house_symbol(c.row.ticker)) for c in candidates]

    def before_digest(con_: sqlite3.Connection, run_id: int) -> None:
        writer.write_scout_candidates(
            con_, run_id=run_id, records=_verdicts(con_, run_id, candidates, criteria)
        )

    return engine.run(
        contexts,
        con=con,
        sources=sources,
        profile=profile,
        sink=sink,
        as_of=as_of,
        today=today,
        app_version=app_version,
        kind="scout",
        before_digest=before_digest,
    )


def _house_symbol(ticker: str) -> str:
    """TV's dotted class shares → the dash symbology used stack-wide."""
    return ticker.strip().upper().replace(".", "-")


def _verdicts(
    con: sqlite3.Connection,
    run_id: int,
    candidates: Sequence[ScreenedCandidate],
    criteria: ScoutCriteria,
) -> list[ScoutCandidateRecord]:
    """Post-enrichment eligibility: proposed iff the gated snapshot carries
    accepted PRICE and (PEG or P/E TTM), AND the verified PEG (when we have
    one) honors the screen's ceiling — the first live run surfaced a name
    the screener called PEG 0.008 that verified at 11.99 (base-effect TTM
    growth), exactly the value-trap class the screen exists to exclude.
    Everything else is excluded with a reason the digest prints verbatim."""
    records = []
    for candidate in candidates:
        ticker = _house_symbol(candidate.row.ticker)
        row = con.execute(
            "SELECT status, error FROM run_tickers WHERE run_id = ? AND ticker = ?",
            (run_id, ticker),
        ).fetchone()
        snapshot = queries.snapshot(con, run_id, ticker)
        status, reason = "proposed", None
        if row is None or row["status"] == "failed" or snapshot is None:
            status = "excluded"
            reason = f"fetch failed: {row['error'] if row is not None else 'no result'}"
        else:
            missing = []
            if Field.PRICE not in snapshot.values:
                missing.append("price")
            if Field.PEG not in snapshot.values and Field.PE_TTM not in snapshot.values:
                missing.append("P/E or PEG")
            verified_peg = snapshot.values.get(Field.PEG)
            if missing:
                quarantined = sorted(
                    field.value
                    for field in (Field.PRICE, Field.PEG, Field.PE_TTM)
                    if field in snapshot.quarantined
                )
                status = "excluded"
                reason = "core fields not verifiable: " + ", ".join(missing)
                if quarantined:
                    reason += f" (quarantined: {', '.join(quarantined)})"
            elif verified_peg is not None and verified_peg.value > criteria.max_peg:
                status = "excluded"
                claimed = candidate.row.peg_ttm
                reason = (
                    f"verified PEG {verified_peg.value:.2f} exceeds the screen "
                    f"ceiling {criteria.max_peg:g}"
                    + (f" (screener claimed {claimed:g})" if claimed is not None else "")
                )
        records.append(
            ScoutCandidateRecord(
                ticker=ticker,
                rank=candidate.rank,
                status=status,
                exclusion_reason=reason,
                screen_reasons=candidate.reasons,
                screener_metrics=candidate.row.model_dump(),
            )
        )
    return records


def _outage_run(
    con: sqlite3.Connection,
    sink: DigestSink,
    as_of: datetime,
    app_version: str,
    error: str,
) -> engine.RunOutcome:
    """The screener is down: no candidates were evaluated, and the digest
    must SAY so — an empty-looking report would read as 'nothing passed the
    screen', which is a different (and false) statement."""
    run_id = writer.begin_run(con, kind="scout", started_at=as_of, app_version=app_version)
    writer.finish_run(
        con,
        run_id=run_id,
        status="failed",
        finished_at=as_of,
        notes=f"screener unavailable — no candidates evaluated: {error}",
    )
    digest_path = None
    delivery_error = None
    try:
        digest_path = sink.write(
            render(queries.run_report(con, run_id)), run_id=run_id, as_of=as_of.date()
        )
    except DeliveryError as exc:
        digest_path, delivery_error = exc.digest_path, str(exc)
    return engine.RunOutcome(
        run_id=run_id,
        status="failed",
        digest_path=digest_path,
        delivery_error=delivery_error,
    )
