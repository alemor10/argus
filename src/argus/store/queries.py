"""The entire read side, as named functions over hand-written SQL.

Every question the digest (or a 7am debugging session) asks is a function
here: why did the digest say that → the SQL that said it.
"""

import json
import sqlite3
from datetime import datetime

from argus.fields import Field
from argus.models import (
    CHANGE_EVENT_ADAPTER,
    AnalystActionRecord,
    CompanyProfile,
    FieldValue,
    QuarantinedObservation,
    QuarantineHit,
    RunReport,
    ScoutProposal,
    Snapshot,
    SourceHealth,
    Thresholds,
    TickerContext,
    TickerReport,
)


def baseline_run(con: sqlite3.Connection, ticker: str, before_run: int) -> int | None:
    """Latest prior watch run where this ticker has status ok/partial.
    Per-ticker, so failed and crashed runs are never diffed against:

        SELECT MAX(rt.run_id) FROM run_tickers rt
        JOIN runs r ON r.run_id = rt.run_id
        WHERE rt.ticker = ? AND rt.run_id < ?
          AND rt.status IN ('ok','partial') AND r.kind = 'watch'
    """
    row = con.execute(
        """SELECT MAX(rt.run_id) FROM run_tickers rt
           JOIN runs r ON r.run_id = rt.run_id
           WHERE rt.ticker = ? AND rt.run_id < ?
             AND rt.status IN ('ok','partial') AND r.kind = 'watch'""",
        (ticker, before_run),
    ).fetchone()
    return row[0]


def snapshot(con: sqlite3.Connection, run_id: int, ticker: str) -> Snapshot | None:
    """Hydrate a Snapshot: primary accepted rows (is_primary = 1) into
    `values`, plus quarantined-only fields into `quarantined`."""
    if con.execute(
        "SELECT 1 FROM run_tickers WHERE run_id = ? AND ticker = ?", (run_id, ticker)
    ).fetchone() is None:
        return None  # ticker was not part of this run — distinct from "empty"
    started_at = con.execute(
        "SELECT started_at FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()[0]

    values: dict[Field, FieldValue] = {}
    for row in con.execute(
        """SELECT field, source, fetched_at, value_num, value_text, value_date, corroborated_by
           FROM observations WHERE run_id = ? AND ticker = ? AND is_primary = 1""",
        (run_id, ticker),
    ):
        values[Field(row["field"])] = _hydrate_field_value(row)

    # A field is "quarantined" (dark) only when NO accepted primary exists
    # this run — a quarantined sibling beside an accepted primary belongs to
    # the quarantine report, not the snapshot. Hits merge across sources, in
    # source order, deduplicated: a cross-source disagreement stamps the
    # identical hit on every leg, and the digest must not print it twice.
    quarantined: dict[Field, list[QuarantineHit]] = {}
    for row in con.execute(
        """SELECT field, gate_reasons FROM observations
           WHERE run_id = ? AND ticker = ? AND verdict = 'quarantined'
           ORDER BY field, source""",
        (run_id, ticker),
    ):
        field = Field(row["field"])
        if field in values:
            continue
        quarantined.setdefault(field, []).extend(_hydrate_hits(row["gate_reasons"]))

    return Snapshot(
        ticker=ticker,
        run_id=run_id,
        as_of=datetime.fromisoformat(started_at),
        values=values,
        quarantined={field: tuple(dict.fromkeys(hits)) for field, hits in quarantined.items()},
    )


def latest_accepted(
    con: sqlite3.Connection, ticker: str, field: Field, before_run: int
) -> FieldValue | None:
    """Most recent primary accepted value across any prior run — the fallback
    baseline so a quarantine or outage gap cannot swallow a real move."""
    # Restricted to watch runs, consistent with baseline_run: scout runs are
    # a different universe and must never seed a monitor baseline.
    row = con.execute(
        """SELECT o.field, o.source, o.fetched_at, o.value_num, o.value_text, o.value_date,
                  o.corroborated_by
           FROM observations o
           JOIN runs r ON r.run_id = o.run_id
           WHERE o.ticker = ? AND o.field = ? AND o.is_primary = 1
             AND o.run_id < ? AND r.kind = 'watch'
           ORDER BY o.run_id DESC LIMIT 1""",
        (ticker, field.value, before_run),
    ).fetchone()
    return _hydrate_field_value(row) if row is not None else None


def new_analyst_actions(
    con: sqlite3.Connection, run_id: int, ticker: str
) -> list[AnalystActionRecord]:
    """Rows first seen in this run — exactly changes.detect's new_actions
    input: SELECT … FROM analyst_actions WHERE ticker = ? AND
    first_seen_run_id = ?. Set membership, no window arithmetic."""
    rows = con.execute(
        """SELECT ticker, action_date, firm, action, from_grade, to_grade, source, fetched_at
           FROM analyst_actions
           WHERE ticker = ? AND first_seen_run_id = ?
           ORDER BY action_date, firm""",
        (ticker, run_id),
    ).fetchall()
    return [AnalystActionRecord.model_validate(dict(row)) for row in rows]


def quarantine_report(con: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    """All quarantined observations for a run, with reasons — including those
    coexisting with an accepted primary from another source:
    SELECT … WHERE run_id = ? AND verdict = 'quarantined'. run_report hydrates
    the same rows into TickerReport.quarantines for the digest."""
    return con.execute(
        """SELECT * FROM observations
           WHERE run_id = ? AND verdict = 'quarantined'
           ORDER BY ticker, field, source""",
        (run_id,),
    ).fetchall()


def company_profile(con: sqlite3.Connection, ticker: str) -> CompanyProfile | None:
    """Latest known business identity for a ticker (append-only table;
    newest fetched_at wins)."""
    row = con.execute(
        """SELECT ticker, fetched_at, source, name, sector, industry, employees, summary
           FROM company_profiles WHERE ticker = ?
           ORDER BY fetched_at DESC LIMIT 1""",
        (ticker,),
    ).fetchone()
    return CompanyProfile.model_validate(dict(row)) if row is not None else None


def scout_streak(con: sqlite3.Connection, ticker: str, run_id: int) -> int:
    """Consecutive scout runs, up to and including run_id, in which this
    ticker was PROPOSED. A scout run where it was absent or excluded breaks
    the streak — continuity is earned, not assumed. Runs that evaluated
    NOTHING (screener outage: zero scout_candidates rows) are skipped, not
    streak-breaking: an outage is not a verdict."""
    streak = 0
    for row in con.execute(
        "SELECT run_id FROM runs WHERE kind = 'scout' AND run_id <= ? ORDER BY run_id DESC",
        (run_id,),
    ):
        evaluated = con.execute(
            "SELECT 1 FROM scout_candidates WHERE run_id = ? LIMIT 1", (row["run_id"],)
        ).fetchone()
        if evaluated is None:
            continue
        proposed = con.execute(
            "SELECT 1 FROM scout_candidates WHERE run_id = ? AND ticker = ? AND status = 'proposed'",
            (row["run_id"], ticker),
        ).fetchone()
        if proposed is None:
            break
        streak += 1
    return streak


def _scout_proposals(con: sqlite3.Connection, run_id: int) -> tuple[ScoutProposal, ...]:
    """Hydrate the run's scout rows: proposed first (by rank), then sector
    leaders, then excluded."""
    rows = con.execute(
        """SELECT ticker, rank, status, sector, exclusion_reason, screen_reasons,
                  screener_metrics, peer_context
           FROM scout_candidates WHERE run_id = ?
           ORDER BY CASE status WHEN 'proposed' THEN 0 WHEN 'leader' THEN 1 ELSE 2 END, rank""",
        (run_id,),
    ).fetchall()
    return tuple(
        ScoutProposal(
            ticker=row["ticker"],
            rank=row["rank"],
            status=row["status"],
            sector=row["sector"],
            exclusion_reason=row["exclusion_reason"],
            screen_reasons=json.loads(row["screen_reasons"]),
            screener_metrics=json.loads(row["screener_metrics"]),
            peer_context=json.loads(row["peer_context"]) if row["peer_context"] else None,
            streak=scout_streak(con, row["ticker"], run_id) if row["status"] == "proposed" else 0,
        )
        for row in rows
    )


def run_report(con: sqlite3.Connection, run_id: int) -> RunReport:
    """Assemble the digest's full input entirely from SQL: persisted events,
    per-ticker snapshots with provenance, every quarantined observation
    (TickerReport.quarantines), run/source health, and each ticker's context
    as of the run (run_tickers.thesis + thresholds JSON — nothing is read
    from the live watchlist). `argus report --run N` is exactly this +
    render — bit-for-bit."""
    run = con.execute(
        "SELECT kind, started_at, status, notes FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if run is None:
        raise ValueError(f"no such run: {run_id}")
    if run["status"] == "running":
        raise ValueError(f"run {run_id} is still running — reporting on it is a caller bug")

    tickers = tuple(
        _ticker_report(con, run_id, row)
        for row in con.execute(
            """SELECT ticker, status, error, thesis, thresholds
               FROM run_tickers WHERE run_id = ? ORDER BY ticker""",
            (run_id,),
        )
    )
    return RunReport(
        run_id=run_id,
        kind=run["kind"],
        as_of=datetime.fromisoformat(run["started_at"]),
        status=run["status"],
        notes=run["notes"],
        tickers=tickers,
        scout=_scout_proposals(con, run_id) if run["kind"] == "scout" else (),
    )


def _ticker_report(con: sqlite3.Connection, run_id: int, rt: sqlite3.Row) -> TickerReport:
    """One TickerReport, from the persisted run_tickers row outward."""
    ticker = rt["ticker"]
    events = tuple(
        CHANGE_EVENT_ADAPTER.validate_json(row["payload"])
        for row in con.execute(
            "SELECT payload FROM change_events WHERE run_id = ? AND ticker = ? ORDER BY event_id",
            (run_id, ticker),
        )
    )
    quarantines = tuple(
        QuarantinedObservation(
            field=row["field"],
            source=row["source"],
            fetched_at=row["fetched_at"],
            reasons=_hydrate_hits(row["gate_reasons"]),
        )
        for row in con.execute(
            """SELECT field, source, fetched_at, gate_reasons FROM observations
               WHERE run_id = ? AND ticker = ? AND verdict = 'quarantined'
               ORDER BY field, source""",
            (run_id, ticker),
        )
    )
    sources = tuple(
        SourceHealth(
            source=row["source"],
            status=row["status"],
            error=row["error"],
            latency_ms=row["latency_ms"],
        )
        for row in con.execute(
            """SELECT source, status, error, latency_ms FROM run_sources
               WHERE run_id = ? AND ticker = ? ORDER BY source""",
            (run_id, ticker),
        )
    )
    baseline_id = baseline_run(con, ticker, run_id)
    baseline_as_of = None
    if baseline_id is not None:
        baseline_as_of = datetime.fromisoformat(
            con.execute(
                "SELECT started_at FROM runs WHERE run_id = ?", (baseline_id,)
            ).fetchone()[0]
        )
    return TickerReport(
        context=TickerContext(
            ticker=ticker,
            thesis=rt["thesis"],
            thresholds=Thresholds.model_validate_json(rt["thresholds"]),
        ),
        status=rt["status"],
        snapshot=snapshot(con, run_id, ticker),
        baseline=snapshot(con, baseline_id, ticker) if baseline_id is not None else None,
        profile=company_profile(con, ticker),
        events=events,
        quarantines=quarantines,
        sources=sources,
        baseline_run_id=baseline_id,
        baseline_as_of=baseline_as_of,
        error=rt["error"],
    )


def _hydrate_field_value(row: sqlite3.Row) -> FieldValue:
    """Row → FieldValue. The value comes from whichever value_ column is set;
    FieldValue coerces it to the field's declared kind (SQLite hands dates
    back as TEXT), so a kind mismatch fails loudly here, not in the diff."""
    value = next(
        row[column]
        for column in ("value_num", "value_text", "value_date")
        if row[column] is not None
    )
    corroborated = json.loads(row["corroborated_by"]) if row["corroborated_by"] else ()
    return FieldValue(
        field=row["field"],
        value=value,
        source=row["source"],
        fetched_at=row["fetched_at"],
        corroborated_by=corroborated,
    )


def _hydrate_hits(gate_reasons: str) -> tuple[QuarantineHit, ...]:
    return tuple(QuarantineHit.model_validate(item) for item in json.loads(gate_reasons))
