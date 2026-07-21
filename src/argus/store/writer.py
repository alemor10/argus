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

Free-text error/note strings are redacted HERE, at the persistence boundary —
not only inside individual providers. Anything stored in runs.notes,
run_tickers.error, or run_sources.error is re-rendered in every digest, PDF,
and `report --run N` regeneration forever, so a secret that slips past an
adapter (an httpx error embeds the request URL; for Finnhub that carries
?token=, for a webhook the URL IS the secret) must die at this seam.
"""

import json
import math
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from argus.models import (
    AnalystActionRecord,
    BellwetherEarning,
    ChangeEvent,
    CompanyProfile,
    EarningsResultRecord,
    EtfHolding,
    GatedObservation,
    InsiderTransaction,
    MarketWire,
    ParseFailure,
    ScorecardMark,
    ScoutCandidateRecord,
    SourceHealth,
    TickerContext,
    require_aware,
)
from argus.redact import redact


def begin_run(
    con: sqlite3.Connection,
    *,
    kind: Literal["watch", "scout"],
    started_at: datetime,
    app_version: str,
) -> int:
    """INSERT a runs row with status='running' and the publication lifecycle
    at its first phase ('collecting'); returns run_id."""
    require_aware(started_at)
    with con:
        cur = con.execute(
            "INSERT INTO runs (kind, started_at, app_version, publication_status) "
            "VALUES (?, ?, ?, 'collecting')",
            (kind, started_at.isoformat(), app_version),
        )
    assert cur.lastrowid is not None  # INTEGER PRIMARY KEY always yields one
    return cur.lastrowid


PublicationStatus = Literal[
    "collecting",
    "assembled",
    "artifact_committed",
    "delivery_pending",
    "delivered",
    "delivery_failed",
    "file_only",
    "artifact_failed",
]


def record_artifact(
    con: sqlite3.Connection,
    *,
    filename: str,
    kind: Literal["md", "pdf"],
    sha256: str,
    size: int,
    renderer: str,
    written_at: datetime,
    run_id: int | None = None,
    label: str | None = None,
    original: bool = True,
) -> None:
    """Record one written report file: its hash, size, and renderer version at
    the moment of writing — the immutability reference `report --run N`
    verifies against and `argus deliver` refuses to violate. Keyed by
    (run_id, filename) or (label, filename) for run-less publications (the
    Sunday Edition). OR REPLACE mirrors the filesystem's own
    overwrite-by-deterministic-filename semantics."""
    require_aware(written_at)
    if (run_id is None) == (label is None):
        raise ValueError("exactly one of run_id/label identifies an artifact")
    with con:
        con.execute(
            "INSERT OR REPLACE INTO artifacts "
            "(run_id, label, filename, kind, sha256, bytes, renderer, written_at, original) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, label, filename, kind, sha256, size, renderer,
                written_at.isoformat(), 1 if original else 0,
            ),
        )


def enqueue_delivery(
    con: sqlite3.Connection,
    *,
    channel: str,
    created_at: datetime,
    run_id: int | None = None,
    label: str | None = None,
    fingerprint: str | None = None,
) -> int:
    """One outbox row per (publication, channel). Returns outbox_id. The
    fingerprint is a hash PREFIX of the endpoint — identity without the
    secret. Idempotent: an existing UNDELIVERED row for the same publication
    and channel is reused (a re-run of the same Sunday edition must not stack
    duplicate rows that a later `argus deliver` would post N times); a
    delivered row stays as the audit record and a fresh row is created."""
    require_aware(created_at)
    if (run_id is None) == (label is None):
        raise ValueError("exactly one of run_id/label identifies a delivery")
    key, value = ("run_id", run_id) if run_id is not None else ("label", label)
    existing = con.execute(
        f"SELECT outbox_id FROM delivery_outbox WHERE {key} = ? AND channel = ? "  # noqa: S608
        "AND delivered_at IS NULL",
        (value, channel),
    ).fetchone()
    if existing is not None:
        return existing["outbox_id"]
    with con:
        cur = con.execute(
            "INSERT INTO delivery_outbox (run_id, label, channel, fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, label, channel, fingerprint, created_at.isoformat()),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def mark_delivery(
    con: sqlite3.Connection,
    *,
    outbox_id: int,
    attempted_at: datetime,
    delivered: bool,
    error: str | None = None,
    next_retry_at: datetime | None = None,
) -> None:
    """Record one delivery attempt's outcome. delivered_at set exactly once —
    the idempotence anchor (webhooks have no idempotency keys, so `argus
    deliver` never re-attempts a row whose delivered_at is set)."""
    require_aware(attempted_at)
    with con:
        con.execute(
            "UPDATE delivery_outbox SET attempts = attempts + 1, attempted_at = ?, "
            "delivered_at = COALESCE(delivered_at, ?), last_error = ?, next_retry_at = ? "
            "WHERE outbox_id = ?",
            (
                attempted_at.isoformat(),
                attempted_at.isoformat() if delivered else None,
                redact(error) if error else None,
                next_retry_at.isoformat() if next_retry_at is not None else None,
                outbox_id,
            ),
        )


def mark_publication(
    con: sqlite3.Connection,
    *,
    run_id: int,
    status: PublicationStatus,
    at: datetime,
    error: str | None = None,
) -> None:
    """Advance the run's publication lifecycle — the third sanctioned runs
    UPDATE (finish/sweep/publication). `at` is the ACTUAL transition time
    (injected; the CLI passes the wall clock, tests pass a fixed instant).
    A run is never marked delivered before its artifact exists — the engine
    sequences the calls; this function just records, redacting the error."""
    require_aware(at)
    with con:
        con.execute(
            "UPDATE runs SET publication_status = ?, publication_error = ?, "
            "published_at = ? WHERE run_id = ?",
            (status, redact(error) if error else None, at.isoformat(), run_id),
        )


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
    earnings: Sequence[EarningsResultRecord] = (),
    insider: Sequence[InsiderTransaction] = (),
) -> None:
    """ONE transaction: observations (accepted AND quarantined), analyst
    actions and earnings results (INSERT OR IGNORE, first_seen_run_id=run_id),
    run_sources rows, run_tickers row — including context.thesis and
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
        for e in earnings:
            # OR IGNORE on (ticker, quarter_end): a re-served history keeps its
            # original fetched_at and first_seen_run_id, and a later revision
            # of an actual never rewrites what Argus first reported.
            con.execute(
                """INSERT OR IGNORE INTO earnings_results
                     (ticker, quarter_end, eps_actual, eps_estimate,
                      source, fetched_at, first_seen_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    e.ticker,
                    e.quarter_end.isoformat(),
                    e.eps_actual,
                    e.eps_estimate,
                    e.source.value,
                    e.fetched_at.isoformat(),
                    run_id,
                ),
            )
        for x in insider:
            # OR IGNORE on the natural key: a re-fetched Form 4 keeps its
            # first_seen_run_id, so "new insider buy since last run" is a
            # set-membership fact (the analyst_actions precedent).
            con.execute(
                """INSERT OR IGNORE INTO insider_transactions
                     (ticker, accession, transaction_date, shares, filing_date,
                      owner, role, price, source, fetched_at, first_seen_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    x.ticker,
                    x.accession,
                    x.transaction_date.isoformat(),
                    x.shares,
                    x.filing_date.isoformat(),
                    x.owner,
                    x.role,
                    x.price,
                    x.source.value,
                    x.fetched_at.isoformat(),
                    run_id,
                ),
            )
        for h in source_health:
            con.execute(
                "INSERT INTO run_sources (run_id, ticker, source, status, error, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    context.ticker,
                    h.source.value,
                    h.status,
                    redact(h.error) if h.error else h.error,
                    h.latency_ms,
                ),
            )
        con.execute(
            "INSERT INTO run_tickers "
            "(run_id, ticker, status, error, thesis, thresholds, thesis_checks, macro, tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                context.ticker,
                status,
                redact(error) if error else error,
                context.thesis,
                context.thresholds.model_dump_json(),
                json.dumps([c.model_dump(mode="json") for c in context.thesis_checks]),
                context.macro.model_dump_json() if context.macro is not None else None,
                context.tier,
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


def write_bellwether_earnings(
    con: sqlite3.Connection, *, run_id: int, rows: Sequence[BellwetherEarning]
) -> None:
    """Persist this run's bellwether calendar window (claims-labeled context)
    so the digest section reproduces from SQL. One transaction; OR IGNORE
    keeps re-entry idempotent."""
    with con:
        for r in rows:
            con.execute(
                """INSERT OR IGNORE INTO bellwether_earnings
                     (run_id, symbol, report_date, hour, eps_estimate, eps_actual,
                      revenue_estimate, revenue_actual)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    r.symbol,
                    r.report_date.isoformat(),
                    r.hour,
                    r.eps_estimate,
                    r.eps_actual,
                    r.revenue_estimate,
                    r.revenue_actual,
                ),
            )


def write_market_wire(con: sqlite3.Connection, *, run_id: int, wire: "MarketWire") -> None:
    """Persist the issue's market pages — one claims JSON blob per run, so
    the magazine reproduces from SQL. OR REPLACE keeps re-entry idempotent
    (a retried before_digest hook must not die on the primary key)."""
    with con:
        con.execute(
            "INSERT OR REPLACE INTO market_wire (run_id, payload) VALUES (?, ?)",
            (run_id, wire.model_dump_json()),
        )


def write_insider_transactions(
    con: sqlite3.Connection, *, run_id: int, insider: Sequence[InsiderTransaction]
) -> None:
    """Persist insider buys for names WITHOUT a run_tickers row (scout
    shortlist crossings). Same INSERT OR IGNORE + first_seen_run_id as the
    per-ticker path in write_ticker_result — one home for the statement."""
    with con:
        for x in insider:
            con.execute(
                """INSERT OR IGNORE INTO insider_transactions
                     (ticker, accession, transaction_date, shares, filing_date,
                      owner, role, price, source, fetched_at, first_seen_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    x.ticker,
                    x.accession,
                    x.transaction_date.isoformat(),
                    x.shares,
                    x.filing_date.isoformat(),
                    x.owner,
                    x.role,
                    x.price,
                    x.source.value,
                    x.fetched_at.isoformat(),
                    run_id,
                ),
            )


def write_etf_holdings(
    con: sqlite3.Connection, *, run_id: int, etf: str, holdings: Sequence[EtfHolding]
) -> None:
    """Persist one ETF's membership snapshot for this run — the caller writes
    only when membership changed, so the diff against the prior blob IS the
    rebalance. OR REPLACE keeps a retried step idempotent."""
    payload = json.dumps(
        [{"t": h.ticker, "c": h.cusip, "w": h.weight, "n": h.name} for h in holdings]
    )
    with con:
        con.execute(
            "INSERT OR REPLACE INTO etf_holdings (run_id, etf, holdings) VALUES (?, ?, ?)",
            (run_id, etf, payload),
        )


def write_scorecard_marks(
    con: sqlite3.Connection, *, run_id: int, marks: Sequence["ScorecardMark"]
) -> None:
    """Persist this scoring run's marks — the immutable forward log. One
    transaction; INSERT OR IGNORE so a re-scored run is a no-op."""
    with con:
        for m in marks:
            con.execute(
                """INSERT OR IGNORE INTO scorecard_marks
                     (run_id, ticker, first_proposed_at, horizon_weeks, name_return, spy_return)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    m.ticker,
                    m.first_proposed_at.isoformat(),
                    m.horizon_weeks,
                    m.name_return,
                    m.spy_return,
                ),
            )


def append_run_note(con: sqlite3.Connection, *, run_id: int, note: str) -> None:
    """Append a line to runs.notes — rendered in the digest header. The one
    sanctioned post-finish update besides finish_run itself. Redacted at this
    boundary: notes frequently carry provider error text."""
    with con:
        con.execute(
            "UPDATE runs SET notes = COALESCE(notes || '; ', '') || ? WHERE run_id = ?",
            (redact(note), run_id),
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
            (status, finished_at.isoformat(), redact(notes) if notes else notes, run_id),
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
