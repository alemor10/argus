"""The entire read side, as named functions over hand-written SQL.

Every question the digest (or a 7am debugging session) asks is a function
here: why did the digest say that → the SQL that said it.
"""

import json
import sqlite3
from datetime import date as date_
from datetime import datetime

from argus.fields import Field
from argus.models import (
    CHANGE_EVENT_ADAPTER,
    AnalystActionRecord,
    BellwetherEarning,
    CompanyProfile,
    EarningsResultRecord,
    EtfHolding,
    EtfRebalance,
    FieldValue,
    InsiderTransaction,
    MacroSpec,
    MarketWire,
    QuarantinedObservation,
    QuarantineHit,
    RunReport,
    Scorecard,
    ScorecardMark,
    ScoutProposal,
    Snapshot,
    SourceHealth,
    ThesisCheck,
    Thresholds,
    TickerContext,
    TickerReport,
)


def artifacts_for(
    con: sqlite3.Connection, *, run_id: int | None = None, label: str | None = None
) -> list[sqlite3.Row]:
    """The immutability records for one publication (a run or a labeled
    edition), originals first."""
    if (run_id is None) == (label is None):
        raise ValueError("exactly one of run_id/label identifies a publication")
    key, value = ("run_id", run_id) if run_id is not None else ("label", label)
    return con.execute(
        f"SELECT * FROM artifacts WHERE {key} = ? ORDER BY original DESC, filename",  # noqa: S608
        (value,),
    ).fetchall()


def undelivered_outbox(
    con: sqlite3.Connection, *, run_id: int | None = None
) -> list[sqlite3.Row]:
    """Outbox rows never successfully delivered — `argus deliver`'s worklist.
    delivered_at IS NULL is the whole predicate: a delivered row is never
    retried (idempotence), a failed row always remains visible."""
    if run_id is not None:
        return con.execute(
            "SELECT * FROM delivery_outbox WHERE delivered_at IS NULL AND run_id = ? "
            "ORDER BY outbox_id",
            (run_id,),
        ).fetchall()
    return con.execute(
        "SELECT * FROM delivery_outbox WHERE delivered_at IS NULL ORDER BY outbox_id"
    ).fetchall()


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
        """SELECT field, source, fetched_at, observed_at,
                  value_num, value_text, value_date, corroborated_by
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
        """SELECT o.field, o.source, o.fetched_at, o.observed_at,
                  o.value_num, o.value_text, o.value_date, o.corroborated_by
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


def new_earnings_results(
    con: sqlite3.Connection, run_id: int, ticker: str
) -> list[EarningsResultRecord]:
    """Reported quarters first seen in this run — exactly changes.detect's
    new_earnings input: SELECT … FROM earnings_results WHERE ticker = ? AND
    first_seen_run_id = ?. Set membership, no window arithmetic (the
    analyst_actions precedent)."""
    rows = con.execute(
        """SELECT ticker, quarter_end, eps_actual, eps_estimate, source, fetched_at
           FROM earnings_results
           WHERE ticker = ? AND first_seen_run_id = ?
           ORDER BY quarter_end""",
        (ticker, run_id),
    ).fetchall()
    return [EarningsResultRecord.model_validate(dict(row)) for row in rows]


def new_insider_transactions(
    con: sqlite3.Connection, run_id: int, ticker: str
) -> list[InsiderTransaction]:
    """Insider buys first seen in this run — exactly changes.detect's
    new_insider input (the analyst_actions set-membership precedent)."""
    rows = con.execute(
        """SELECT ticker, accession, transaction_date, shares, filing_date,
                  owner, role, price, source, fetched_at
           FROM insider_transactions
           WHERE ticker = ? AND first_seen_run_id = ?
           ORDER BY transaction_date, owner""",
        (ticker, run_id),
    ).fetchall()
    return [InsiderTransaction.model_validate(dict(row)) for row in rows]


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


def first_proposals(con: sqlite3.Connection) -> list[tuple[str, date_, int]]:
    """Every name scout has EVER proposed, with the date AND run_id it FIRST
    surfaced — the scorecard's universe (a dropped name
    stays tracked from its first proposal; unpriceable names are counted). Eligibility keys on the run_id
    (monotonic) rather than the date, so a clock step-back can never
    retroactively change an older run's scorecard."""
    rows = con.execute(
        """SELECT ticker, first_run,
                  (SELECT started_at FROM runs WHERE run_id = first_run) AS first_at
           FROM (
             SELECT sc.ticker AS ticker, MIN(sc.run_id) AS first_run
             FROM scout_candidates sc JOIN runs r ON r.run_id = sc.run_id
             WHERE sc.status = 'proposed' AND r.kind = 'scout'
             GROUP BY sc.ticker
           )
           ORDER BY ticker""",
    ).fetchall()
    return [
        (row["ticker"], datetime.fromisoformat(row["first_at"]).date(), row["first_run"])
        for row in rows
    ]


def _scorecard(con: sqlite3.Connection, run_id: int, run_started: datetime) -> Scorecard | None:
    """Build the scorecard summary from THIS run's persisted marks —
    deterministic, so `argus report --run N` reproduces it. `unpriceable` is
    the eligible-but-not-marked count, so an all-unpriceable run (SPY fetch
    down, delistings) reports the gap loudly rather than reading as 'nothing
    has matured'. Returns None only when nothing was eligible yet."""
    from argus import scorecard as scorecard_mod

    # Eligible = names first proposed in an EARLIER run (run_id monotonic).
    eligible = [ticker for ticker, _d, first_run in first_proposals(con) if first_run < run_id]
    rows = con.execute(
        """SELECT ticker, first_proposed_at, weeks_out, name_return, spy_return
           FROM scorecard_marks WHERE run_id = ?""",
        (run_id,),
    ).fetchall()
    if not eligible and not rows:
        return None
    marks = [
        ScorecardMark(
            ticker=row["ticker"],
            first_proposed_at=date_.fromisoformat(row["first_proposed_at"]),
            weeks_out=row["weeks_out"],
            name_return=row["name_return"],
            spy_return=row["spy_return"],
        )
        for row in rows
    ]
    unpriceable = max(len(eligible) - len(marks), 0)
    return scorecard_mod.summarize(marks, run_started.date(), unpriceable)


def scout_streak(con: sqlite3.Connection, ticker: str, run_id: int) -> int:
    """Consecutive CALENDAR WEEKS (ISO), up to and including run_id's week, in
    which this ticker was PROPOSED — so the '5w' label means five weeks, not
    five runs. Several scout runs in the same week (e.g. a manual re-run) count
    once; a week where it was absent or excluded breaks the streak — continuity
    is earned, not assumed. Runs that evaluated NOTHING (screener outage: zero
    scout_candidates rows) are skipped, not streak-breaking: an outage is not a
    verdict."""
    weeks: set[tuple[int, int]] = set()
    for row in con.execute(
        "SELECT run_id, started_at FROM runs WHERE kind = 'scout' AND run_id <= ? "
        "ORDER BY run_id DESC",
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
        iso = datetime.fromisoformat(row["started_at"]).isocalendar()
        weeks.add((iso[0], iso[1]))
    return len(weeks)


def scout_rank_history(
    con: sqlite3.Connection, ticker: str, run_id: int, limit: int = 8
) -> tuple[int, ...]:
    """The ticker's global screen rank across the most recent scout runs (up to
    and including run_id) in which it was PROPOSED, chronological (oldest →
    newest). Proposed-run ranks only — weeks it fell off the shortlist are
    simply absent, never faked with a placeholder rank — so a rising line is a
    real climb up the screen. Powers the rank-trajectory sparkline."""
    ranks: list[int] = []
    for row in con.execute(
        "SELECT run_id FROM runs WHERE kind = 'scout' AND run_id <= ? ORDER BY run_id DESC",
        (run_id,),
    ):
        hit = con.execute(
            "SELECT rank FROM scout_candidates "
            "WHERE run_id = ? AND ticker = ? AND status = 'proposed'",
            (row["run_id"], ticker),
        ).fetchone()
        if hit is not None:
            ranks.append(hit["rank"])
            if len(ranks) >= limit:
                break
    return tuple(reversed(ranks))


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
            rank_history=(
                scout_rank_history(con, row["ticker"], run_id)
                if row["status"] == "proposed"
                else ()
            ),
        )
        for row in rows
    )


def _bellwether_earnings(con: sqlite3.Connection, run_id: int) -> tuple[BellwetherEarning, ...]:
    """This run's persisted bellwether calendar window (claims-labeled)."""
    rows = con.execute(
        """SELECT symbol, report_date, hour, eps_estimate, eps_actual,
                  revenue_estimate, revenue_actual
           FROM bellwether_earnings WHERE run_id = ?
           ORDER BY report_date, symbol""",
        (run_id,),
    ).fetchall()
    return tuple(
        BellwetherEarning.model_validate(dict(row) | {"hour": row["hour"] or ""})
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
            """SELECT ticker, status, error, thesis, thresholds, thesis_checks, macro, tier
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
        scorecard=(
            _scorecard(con, run_id, datetime.fromisoformat(run["started_at"]))
            if run["kind"] == "scout"
            else None
        ),
        bellwethers=_bellwether_earnings(con, run_id) if run["kind"] == "watch" else (),
        market=_market_wire(con, run_id) if run["kind"] == "watch" else None,
        radar=_radar_shortlist(con, run_id) if run["kind"] == "watch" else (),
        radar_insider=_radar_insider(con, run_id) if run["kind"] == "watch" else (),
        etf_rebalances=_etf_rebalances(con, run_id) if run["kind"] == "watch" else (),
    )


def _radar_insider(con: sqlite3.Connection, run_id: int) -> tuple[InsiderTransaction, ...]:
    """Insider buys first seen THIS run on shortlist names — the discovery
    crossing 'a name scout flagged is being bought by its insiders'. Excludes
    names on the watchlist/consider tier, whose buys already surface in their
    own per-ticker Changes."""
    shortlist = {p.ticker for p in _radar_shortlist(con, run_id)}
    if not shortlist:
        return ()
    watched = {
        row["ticker"]
        for row in con.execute("SELECT ticker FROM run_tickers WHERE run_id = ?", (run_id,))
    }
    out: list[InsiderTransaction] = []
    for ticker in sorted(shortlist - watched):
        out.extend(new_insider_transactions(con, run_id, ticker))
    return tuple(out)


def latest_etf_holdings(
    con: sqlite3.Connection, etf: str, before_run: int
) -> list[EtfHolding] | None:
    """The most recent membership snapshot for this ETF strictly before
    `before_run` — the change-check's baseline AND the rebalance's prior
    side. None when the ETF has never been snapshotted."""
    row = con.execute(
        "SELECT holdings FROM etf_holdings WHERE etf = ? AND run_id < ? "
        "ORDER BY run_id DESC LIMIT 1",
        (etf, before_run),
    ).fetchone()
    return _hydrate_holdings(row["holdings"]) if row is not None else None


def _etf_rebalances(con: sqlite3.Connection, run_id: int) -> tuple[EtfRebalance, ...]:
    """Reproducible from stored blobs: for each ETF snapshotted THIS run,
    diff against the prior snapshot. A first-ever snapshot has no prior and
    is baseline, not news — skipped, like a ticker's first analyst history."""
    from argus.etf import membership_diff

    result: list[EtfRebalance] = []
    for row in con.execute(
        "SELECT etf, holdings FROM etf_holdings WHERE run_id = ? ORDER BY etf", (run_id,)
    ):
        prior = latest_etf_holdings(con, row["etf"], run_id)
        if prior is None:
            continue
        added, dropped = membership_diff(prior, _hydrate_holdings(row["holdings"]))
        if added or dropped:
            result.append(EtfRebalance(etf=row["etf"], added=added, dropped=dropped))
    return tuple(result)


def _hydrate_holdings(blob: str) -> list[EtfHolding]:
    return [
        EtfHolding(
            ticker=d.get("t"), cusip=d.get("c"), weight=d.get("w", 0.0), name=d.get("n")
        )
        for d in json.loads(blob)
    ]


def _radar_shortlist(con: sqlite3.Connection, before_run: int) -> tuple[ScoutProposal, ...]:
    """The Radar strip: the latest evaluated scout shortlist as of this run —
    proposed names only, streaks intact. Deterministic from SQL, so the
    issue reproduces."""
    row = con.execute(
        """SELECT MAX(r.run_id) AS run_id FROM runs r
           WHERE r.kind = 'scout' AND r.run_id <= ? AND r.status != 'running'
             AND EXISTS (SELECT 1 FROM scout_candidates sc WHERE sc.run_id = r.run_id)""",
        (before_run,),
    ).fetchone()
    if row["run_id"] is None:
        return ()
    return tuple(p for p in _scout_proposals(con, row["run_id"]) if p.status == "proposed")


def _market_wire(con: sqlite3.Connection, run_id: int) -> MarketWire | None:
    """The issue's persisted market pages; None for quiet pulses and old runs."""
    row = con.execute(
        "SELECT payload FROM market_wire WHERE run_id = ?", (run_id,)
    ).fetchone()
    return MarketWire.model_validate_json(row["payload"]) if row is not None else None


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
            thesis_checks=tuple(
                ThesisCheck.model_validate(c) for c in json.loads(rt["thesis_checks"])
            ),
            macro=MacroSpec.model_validate_json(rt["macro"]) if rt["macro"] else None,
            tier=rt["tier"] or "watch",
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
        observed_at=row["observed_at"],
        corroborated_by=corroborated,
    )


def _hydrate_hits(gate_reasons: str) -> tuple[QuarantineHit, ...]:
    return tuple(QuarantineHit.model_validate(item) for item in json.loads(gate_reasons))
