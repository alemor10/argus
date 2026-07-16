"""Connection + migration. schema.sql is the single source of truth for a
FRESH database; existing databases upgrade through the numbered _MIGRATIONS
steps — PRAGMA user_version gates both. No ORM, no migration framework."""

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 10

# version N → the script that upgrades N to N+1. Each step runs in its own
# transaction with its user_version bump, so a crash mid-upgrade resumes
# exactly where it stopped. schema.sql always reflects the LATEST shape —
# steps here recreate history for databases born earlier.
_MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE IF NOT EXISTS company_profiles (
    ticker     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source     TEXT NOT NULL,
    name       TEXT,
    sector     TEXT,
    industry   TEXT,
    employees  INTEGER,
    summary    TEXT,
    PRIMARY KEY (ticker, fetched_at)
) WITHOUT ROWID;
""",
    # v1.3: scout_candidates gains sector, peer_context, and the 'leader'
    # status. SQLite cannot alter a CHECK, so the table is rebuilt in place.
    2: """
CREATE TABLE scout_candidates_v3 (
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    ticker           TEXT    NOT NULL,
    rank             INTEGER NOT NULL,
    status           TEXT    NOT NULL CHECK (status IN ('proposed','excluded','leader')),
    sector           TEXT    NOT NULL DEFAULT 'Other',
    exclusion_reason TEXT,
    screen_reasons   TEXT    NOT NULL,
    screener_metrics TEXT    NOT NULL,
    peer_context     TEXT,
    PRIMARY KEY (run_id, ticker),
    CHECK ((status = 'excluded') = (exclusion_reason IS NOT NULL))
) WITHOUT ROWID;
INSERT INTO scout_candidates_v3
    (run_id, ticker, rank, status, exclusion_reason, screen_reasons, screener_metrics)
    SELECT run_id, ticker, rank, status, exclusion_reason, screen_reasons, screener_metrics
    FROM scout_candidates;
DROP TABLE scout_candidates;
ALTER TABLE scout_candidates_v3 RENAME TO scout_candidates;
""",
    # v1.4: run_tickers carries the thesis checks in force at run time, so the
    # digest's holding/breached summary reproduces from SQL. Guarded ADD
    # COLUMN — idempotent, so a re-run or a fresh-then-downgraded database
    # never trips "duplicate column".
    3: lambda con: _add_column_if_absent(
        con, "run_tickers", "thesis_checks", "TEXT NOT NULL DEFAULT '[]'"
    ),
    # v1.5: the scout self-scoring forward log. New table, IF NOT EXISTS so a
    # re-run or fresh-then-downgraded database is a no-op.
    4: """
CREATE TABLE IF NOT EXISTS scorecard_marks (
    run_id            INTEGER NOT NULL REFERENCES runs(run_id),
    ticker            TEXT    NOT NULL,
    first_proposed_at TEXT    NOT NULL,
    weeks_out         INTEGER NOT NULL,
    name_return       REAL    NOT NULL,
    spy_return        REAL    NOT NULL,
    PRIMARY KEY (run_id, ticker)
) WITHOUT ROWID;
""",
    # v1.6: earnings results — reported quarters (actual vs street estimate),
    # event-shaped like analyst_actions. IF NOT EXISTS, same no-op guarantee.
    5: """
CREATE TABLE IF NOT EXISTS earnings_results (
    ticker            TEXT NOT NULL,
    quarter_end       TEXT NOT NULL,
    eps_actual        REAL NOT NULL,
    eps_estimate      REAL,
    source            TEXT NOT NULL,
    fetched_at        TEXT NOT NULL,
    first_seen_run_id INTEGER NOT NULL REFERENCES runs(run_id),
    PRIMARY KEY (ticker, quarter_end)
) WITHOUT ROWID;
""",
    # v1.7: macro watch (run_tickers carries the MacroSpec in force at run
    # time — NULL = watch role) + the bellwether earnings context table.
    # Guarded ADD COLUMN + IF NOT EXISTS: idempotent like v3/v4/v5.
    6: lambda con: (
        _add_column_if_absent(con, "run_tickers", "macro", "TEXT"),
        con.execute(
            """CREATE TABLE IF NOT EXISTS bellwether_earnings (
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    symbol           TEXT    NOT NULL,
    report_date      TEXT    NOT NULL,
    hour             TEXT,
    eps_estimate     REAL,
    eps_actual       REAL,
    revenue_estimate REAL,
    revenue_actual   REAL,
    PRIMARY KEY (run_id, symbol, report_date)
) WITHOUT ROWID"""
        ),
    ),
    # v1.9: the magazine's market wire — one claims JSON blob per watch run.
    7: """
CREATE TABLE IF NOT EXISTS market_wire (
    run_id  INTEGER PRIMARY KEY REFERENCES runs(run_id),
    payload TEXT    NOT NULL
) WITHOUT ROWID;
""",
    # v1.11: the Radar's consider tier — run_tickers carries each name's tier
    # at run time (watch | consider). Guarded ADD COLUMN, idempotent.
    8: lambda con: _add_column_if_absent(
        con, "run_tickers", "tier", "TEXT NOT NULL DEFAULT 'watch'"
    ),
    # v1.14: ETF membership snapshots — one blob per (run, etf), on change.
    9: """
CREATE TABLE IF NOT EXISTS etf_holdings (
    run_id   INTEGER NOT NULL REFERENCES runs(run_id),
    etf      TEXT    NOT NULL,
    holdings TEXT    NOT NULL,
    PRIMARY KEY (run_id, etf)
) WITHOUT ROWID;
""",
}


def _add_column_if_absent(con: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def migrate(con: sqlite3.Connection) -> None:
    """Bring the database to SCHEMA_VERSION: fresh → full schema.sql; older →
    numbered steps applied in order; newer → refuse (a downgraded binary must
    never guess at a future schema)."""
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database is at schema version {version}, this build expects {SCHEMA_VERSION} — "
            "refusing to guess"
        )
    if version == 0:
        schema = resources.files("argus.store").joinpath("schema.sql").read_text(encoding="utf-8")
        # executescript autocommits statement-by-statement unless the script
        # carries its own transaction control — embed it, so a mid-script
        # failure rolls back to a pristine DB and the next migrate() retries.
        with con:
            con.executescript(f"BEGIN;\n{schema}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
        return
    while version < SCHEMA_VERSION:
        step = _MIGRATIONS[version]
        nxt = version + 1
        if callable(step):
            # Python step: normal execute() participates in `with con:`, and
            # the version bump commits atomically with it.
            with con:
                step(con)
                con.execute(f"PRAGMA user_version = {nxt}")
        else:
            # SQL step: executescript autocommits, so it carries its own
            # transaction + version bump (a mid-step failure rolls back).
            with con:
                con.executescript(f"BEGIN;\n{step}\nPRAGMA user_version = {nxt};\nCOMMIT;")
        version += 1
