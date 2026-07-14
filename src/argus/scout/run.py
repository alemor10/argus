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

    result = screen(rows, criteria, exclude)
    # Dedupe on the canonical symbol: two screener rows can collapse to one
    # house ticker (DUP.A/DUP-A, or a feed hiccup repeating a symbol), and a
    # duplicate would violate the store's per-run primary keys and kill the
    # run. screen() returns rank order, so first occurrence = best rank.
    unique: dict[str, ScreenedCandidate] = {}
    for candidate in result.shortlist:
        unique.setdefault(house_symbol(candidate.row.ticker), candidate)
    candidates = list(unique.values())
    # Leaders dedupe on the same canonical symbol — against the shortlist AND
    # each other (a dot/dash duplicate can lead two different buckets, and a
    # ticker collision in scout_candidates would kill the whole write).
    leader_map: dict[str, ScreenedCandidate] = {}
    for leader in result.sector_leaders:
        symbol = house_symbol(leader.row.ticker)
        if symbol not in unique:
            leader_map.setdefault(symbol, leader)
    leaders = list(leader_map.values())
    peer_contexts = {
        house_symbol(c.row.ticker): _peer_context(c.row, rows) for c in candidates
    }
    contexts = [TickerContext(ticker=symbol) for symbol in unique]

    skipped = getattr(screener, "last_skipped", 0)

    def before_digest(con_: sqlite3.Connection, run_id: int) -> None:
        records = _verdicts(con_, run_id, candidates, criteria, peer_contexts)
        records += [
            ScoutCandidateRecord(
                ticker=house_symbol(leader.row.ticker),
                rank=leader.rank,
                status="leader",
                sector=leader.sector,
                screen_reasons=leader.reasons,
                screener_metrics=leader.row.model_dump(),
            )
            for leader in leaders
        ]
        writer.write_scout_candidates(con_, run_id=run_id, records=records)
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


def _peer_context(row, rows) -> dict | None:
    """Industry peers + relative valuation from the SAME scan — context we
    already paid for. Screener claims, labeled as such downstream."""
    if not row.industry:
        return None
    own = house_symbol(row.ticker)
    same = [
        r
        for r in rows
        if r.industry == row.industry and house_symbol(r.ticker) != own
    ]
    if not same:
        return None
    fwd = sorted(r.fwd_pe for r in same + [row] if r.fwd_pe is not None and r.fwd_pe > 0)
    median = None
    if fwd:
        middle = len(fwd) // 2
        median = fwd[middle] if len(fwd) % 2 else (fwd[middle - 1] + fwd[middle]) / 2
    top = sorted(same, key=lambda r: r.market_cap or 0, reverse=True)[:3]
    return {
        "industry": row.industry,
        "n": len(same) + 1,
        "median_fwd_pe": round(median, 1) if median is not None else None,
        "peers": [
            {
                "ticker": house_symbol(peer.ticker),
                "fwd_pe": round(peer.fwd_pe, 1) if peer.fwd_pe is not None else None,
            }
            for peer in top
        ],
    }


def _verdicts(
    con: sqlite3.Connection,
    run_id: int,
    candidates: Sequence[ScreenedCandidate],
    criteria: ScoutCriteria,
    peer_contexts: dict[str, dict | None],
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
            elif (
                (verified_roe := snapshot.values.get(Field.ROE)) is not None
                and verified_roe.value * 100 < criteria.min_roe_pct
            ):
                # The SSRM case: passed on a claimed 17.3% ROE that verified
                # at 12.4% — verified numbers decide for quality floors too.
                status = "excluded"
                reason = (
                    f"verified ROE {verified_roe.value * 100:.1f}% is below the screen "
                    f"floor {criteria.min_roe_pct:g}%"
                )
                claimed_roe = candidate.row.roe_pct
                if claimed_roe is not None:
                    reason += f" (screener claimed {claimed_roe:.1f}%)"
            elif (
                (verified_growth := snapshot.values.get(Field.REVENUE_GROWTH)) is not None
                and verified_growth.value <= 0
            ):
                # Window honesty: our verified figure is MRQ YoY, the screen's
                # floor is TTM — different windows, so only the DIRECTION is
                # enforced. Shrinking latest-quarter revenue on a "growth"
                # candidate is disqualifying whatever the TTM says.
                status = "excluded"
                reason = (
                    f"verified revenue growth (MRQ YoY) "
                    f"{verified_growth.value * 100:+.1f}% is not positive"
                )
                claimed_growth = candidate.row.revenue_growth_ttm_pct
                if claimed_growth is not None:
                    reason += f" (screener claimed {claimed_growth:+.1f}% TTM)"
        records.append(
            ScoutCandidateRecord(
                ticker=ticker,
                rank=candidate.rank,
                status=status,
                sector=candidate.sector,
                exclusion_reason=reason,
                screen_reasons=candidate.reasons,
                screener_metrics=candidate.row.model_dump(),
                peer_context=peer_contexts.get(ticker),
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
