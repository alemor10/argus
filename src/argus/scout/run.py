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
from collections.abc import Callable, Sequence, Set
from datetime import date, datetime

from argus import engine
from argus.digest import DeliveryError, DigestSink, render
from argus.fields import Field
from argus.gates import GateProfile
from argus.models import ScoutCandidateRecord, TickerContext
from argus.scout.criteria import ScoutCriteria, ScreenedCandidate, house_symbol, screen
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
    artifact_builder: Callable[..., Sequence] | None = None,
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
    # Dedupe on the canonical symbol: two screener rows can collapse to one
    # house ticker (DUP.A/DUP-A, or a feed hiccup repeating a symbol), and a
    # duplicate would violate the store's per-run primary keys and kill the
    # run. screen() returns rank order, so first occurrence = best rank.
    unique: dict[str, ScreenedCandidate] = {}
    for candidate in candidates:
        unique.setdefault(house_symbol(candidate.row.ticker), candidate)
    candidates = list(unique.values())
    contexts = [TickerContext(ticker=symbol) for symbol in unique]

    skipped = getattr(screener, "last_skipped", 0)

    def before_digest(con_: sqlite3.Connection, run_id: int) -> None:
        writer.write_scout_candidates(
            con_, run_id=run_id, records=_verdicts(con_, run_id, candidates, criteria)
        )
        if skipped:
            # The screener dropped rows it could not identify — countable
            # degradation belongs in the digest header, not a log nobody reads.
            writer.append_run_note(
                con_, run_id=run_id, note=f"screener skipped {skipped} unparseable row(s)"
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
        artifact_builder=artifact_builder,
    )


def _verdicts(
    con: sqlite3.Connection,
    run_id: int,
    candidates: Sequence[ScreenedCandidate],
    criteria: ScoutCriteria,
) -> list[ScoutCandidateRecord]:
    """Post-enrichment eligibility, per ARCHITECTURE's core-fields rule
    (price, forward or trailing P/E, margins — missing OR quarantined
    excludes), plus the verified-forward-P/E window: screener numbers
    nominate, gated numbers decide. The founding case: a screener-claimed
    PEG of 0.008 that verified at 11.99 (base-effect TTM growth) — the same
    class of divergence applies to the forward P/E this strategy screens on,
    in both directions (a negative verified fwd P/E means expected losses).
    Every exclusion reason is printed verbatim in the digest."""
    core_fields = (
        Field.PRICE,
        Field.PE_FWD,
        Field.PE_TTM,
        Field.GROSS_MARGIN,
        Field.OPERATING_MARGIN,
    )
    records = []
    for candidate in candidates:
        ticker = house_symbol(candidate.row.ticker)
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
            if Field.PE_FWD not in snapshot.values and Field.PE_TTM not in snapshot.values:
                missing.append("forward or trailing P/E")
            if (
                Field.GROSS_MARGIN not in snapshot.values
                and Field.OPERATING_MARGIN not in snapshot.values
            ):
                missing.append("margins")
            quarantined = sorted(
                field.value for field in core_fields if field in snapshot.quarantined
            )
            verified_fpe = snapshot.values.get(Field.PE_FWD)
            if missing or quarantined:
                status = "excluded"
                parts = []
                if missing:
                    parts.append("missing: " + ", ".join(missing))
                if quarantined:
                    parts.append("quarantined: " + ", ".join(quarantined))
                reason = "core fields not verifiable — " + "; ".join(parts)
            elif verified_fpe is not None and not (
                0 < verified_fpe.value <= criteria.max_forward_pe
            ):
                status = "excluded"
                claimed = candidate.row.fwd_pe
                if verified_fpe.value > criteria.max_forward_pe:
                    reason = (
                        f"verified fwd P/E {verified_fpe.value:.1f} exceeds the screen "
                        f"ceiling {criteria.max_forward_pe:g}"
                    )
                else:
                    reason = (
                        f"verified fwd P/E {verified_fpe.value:.1f} is zero or negative — "
                        "expected losses fail the screen's valuation window"
                    )
                if claimed is not None:
                    reason += f" (screener claimed {claimed:.1f})"
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
