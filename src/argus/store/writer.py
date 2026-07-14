"""The append-only write side. Only this module INSERTs.

Accepts GatedObservation only — RawObservation has no verdict and cannot be
persisted, by construction. One transaction per ticker: from its commit, that
ticker's data is durable and baseline-eligible no matter what happens to the
rest of the run.

A ParseFailure payload persists as an UNPARSEABLE quarantined row with its
raw wire text in value_text, whatever the field's declared kind — the schema
CHECK only enforces exactly-one-value.

All datetime parameters MUST be timezone-aware UTC (models.require_aware is
the shared guard) — naive input is rejected at the seam, not discovered as a
TypeError mid-pipeline.
"""

import json
import math
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from argus.models import (
    AnalystActionRecord,
    ChangeEvent,
    CompanyProfile,
    GatedObservation,
    ParseFailure,
    ScoutCandidateRecord,
    SourceHealth,
    TickerContext,
    require_aware,
)


def begin_run(
    con: sqlite3.Connection,
    *,
    kind: Literal["watch", "scout"],
    started_at: datetime,
    app_version: str,
) -> int:
    """INSERT a runs row with status='running'; returns run_id."""
    require_aware(started_at)
    with con:
        cur = con.execute(
            "INSERT INTO runs (kind, started_at, app_version) VALUES (?, ?, ?)",
            (kind, started_at.isoformat(), app_version),
        )
    assert cur.lastrowid is not None  # INTEGER PRIMARY KEY always yields one
    return cur.lastrowid


def sweep_stale_runs(
    con: sqlite3.Connection, *, now: datetime, max_age: timedelta = timedelta(hours=6)
) -> list[int]:
    """Mark runs stuck in 'running' older than max_age as 'failed'. Their
    committed run_tickers rows remain valid baselines. Returns the swept
    run ids so the caller can point the user at `argus report --run N` —
    a crashed run's committed events exist in the store but were never
    digested, and recovery must be offered, not silent."""
    require_aware(now)
    # ISO-8601 UTC strings compare lexicographically — the schema-wide convention.
    with con:
        swept = [
            row["run_id"]
            for row in con.execute(
                "SELECT run_id FROM runs WHERE status = 'running' AND started_at < ?",
                ((now - max_age).isoformat(),),
            )
        ]
        con.execute(
            "UPDATE runs SET status = 'failed', finished_at = ? "
            "WHERE status = 'running' AND started_at < ?",
            (now.isoformat(), (now - max_age).isoformat()),
        )
    return swept


def write_ticker_result(
    con: sqlite3.Connection,
    *,
    run_id: int,
    context: TickerContext,
    gated: Sequence[GatedObservation],
    actions: Sequence[AnalystActionRecord],
    source_health: Sequence[SourceHealth],
    status: Literal["ok", "partial", "failed"],
    error: str | None = None,
    company_profile: CompanyProfile | None = None,
) -> None:
    """ONE transaction: observations (accepted AND quarantined), analyst
    actions (INSERT OR IGNORE, first_seen_run_id=run_id), run_sources rows,
    run_tickers row — including context.thesis and
    context.thresholds.model_dump_json(), so run_report regenerates digests
    from SQL alone even after the watchlist changes."""
    with con:
        for g in gated:
            con.execute(
                """INSERT INTO observations
                     (run_id, ticker, field, source, fetched_at, observed_at,
                      value_num, value_text, value_date, verdict, gate_reasons,
                      corroborated_by, is_primary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    g.obs.ticker,
                    g.obs.field.value,
                    g.obs.source.value,
                    g.obs.fetched_at.isoformat(),
                    *_value_columns(g),
                    g.verdict,
                    _gate_reasons_json(g),
                    _corroborated_json(g),
                    int(g.is_primary),
                ),
            )
        for a in actions:
            # OR IGNORE on the natural key: a re-fetched action keeps its
            # original fetched_at and first_seen_run_id — "new since last
            # run" stays a set-membership fact across failed runs.
            con.execute(
                """INSERT OR IGNORE INTO analyst_actions
                     (ticker, action_date, firm, action, from_grade, to_grade,
                      source, fetched_at, first_seen_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    a.ticker,
                    a.action_date.isoformat(),
                    a.firm,
                    a.action,
                    a.from_grade,
                    a.to_grade,
                    a.source.value,
                    a.fetched_at.isoformat(),
                    run_id,
                ),
            )
        for h in source_health:
            con.execute(
                "INSERT INTO run_sources (run_id, ticker, source, status, error, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, context.ticker, h.source.value, h.status, h.error, h.latency_ms),
            )
        con.execute(
            "INSERT INTO run_tickers "
            "(run_id, ticker, status, error, thesis, thresholds, thesis_checks) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                context.ticker,
                status,
                error,
                context.thesis,
                context.thresholds.model_dump_json(),
                json.dumps([c.model_dump(mode="json") for c in context.thesis_checks]),
            ),
        )
        if company_profile is not None:
            # OR IGNORE: re-processing the same fetch must stay idempotent.
            con.execute(
                """INSERT OR IGNORE INTO company_profiles
                     (ticker, fetched_at, source, name, sector, industry, employees, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    company_profile.ticker,
                    company_profile.fetched_at.isoformat(),
                    company_profile.source.value,
                    company_profile.name,
                    company_profile.sector,
                    company_profile.industry,
                    company_profile.employees,
                    company_profile.summary,
                ),
            )


def record_events(
    con: sqlite3.Connection,
    *,
    run_id: int,
    ticker: str,
    events: Sequence[ChangeEvent],
    baseline_run_id: int | None,
) -> None:
    """Persist detected events (model_dump_json payloads) so digests render
    what was detected and never re-derive."""
    # Insertion order is the canonical order changes.detect produced;
    # event_id (autoincrementing PK) freezes it for run_report.
    with con:
        for event in events:
            con.execute(
                "INSERT INTO change_events (run_id, ticker, kind, payload, baseline_run_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, ticker, event.kind, event.model_dump_json(), baseline_run_id),
            )


def write_scout_candidates(
    con: sqlite3.Connection, *, run_id: int, records: Sequence[ScoutCandidateRecord]
) -> None:
    """One transaction: every screened candidate's fate this run — proposed
    or excluded-with-reason. Screener metrics persist as labeled claims."""
    with con:
        for record in records:
            con.execute(
                """INSERT INTO scout_candidates
                     (run_id, ticker, rank, status, sector, exclusion_reason,
                      screen_reasons, screener_metrics, peer_context)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    record.ticker,
                    record.rank,
                    record.status,
                    record.sector,
                    record.exclusion_reason,
                    json.dumps(record.screen_reasons),
                    json.dumps(record.screener_metrics),
                    json.dumps(record.peer_context) if record.peer_context is not None else None,
                ),
            )


def append_run_note(con: sqlite3.Connection, *, run_id: int, note: str) -> None:
    """Append a line to runs.notes — rendered in the digest header. The one
    sanctioned post-finish update besides finish_run itself."""
    with con:
        con.execute(
            "UPDATE runs SET notes = COALESCE(notes || '; ', '') || ? WHERE run_id = ?",
            (note, run_id),
        )


def finish_run(
    con: sqlite3.Connection,
    *,
    run_id: int,
    status: Literal["complete", "partial", "failed"],
    finished_at: datetime,
    notes: str | None = None,
) -> None:
    require_aware(finished_at)
    with con:
        con.execute(
            "UPDATE runs SET status = ?, finished_at = ?, notes = COALESCE(?, notes) "
            "WHERE run_id = ?",
            (status, finished_at.isoformat(), notes, run_id),
        )


def _value_columns(g: GatedObservation) -> tuple[str | None, float | None, str | None, str | None]:
    """(observed_at, value_num, value_text, value_date) for one row.

    A ParseFailure lands its raw wire text in value_text whatever the field's
    declared kind — sent-but-unreadable data is evidence, not an absence.
    Non-finite numerics get the same treatment: sqlite3 binds NaN as NULL,
    which would trip the exactly-one-value CHECK and roll back the whole
    ticker — so the (always NON_FINITE-quarantined) value is preserved as
    text instead."""
    if isinstance(g.obs, ParseFailure):
        return None, None, g.obs.raw, None
    obs = g.obs
    value_num, value_text = obs.value_num, obs.value_text
    if value_num is not None and not math.isfinite(value_num):
        value_num, value_text = None, repr(value_num)
    return (
        obs.observed_at.isoformat() if obs.observed_at is not None else None,
        value_num,
        value_text,
        obs.value_date.isoformat() if obs.value_date is not None else None,
    )


def _gate_reasons_json(g: GatedObservation) -> str | None:
    """JSON reasons iff quarantined; NULL iff accepted (DB-enforced CHECK)."""
    if g.verdict != "quarantined":
        return None
    return json.dumps([{"code": hit.code.value, "detail": hit.detail} for hit in g.reasons])


def _corroborated_json(g: GatedObservation) -> str | None:
    """Sorted JSON source list; NULL when uncorroborated."""
    if not g.corroborated_by:
        return None
    return json.dumps(sorted(s.value for s in g.corroborated_by))
