"""Connection + migration. schema.sql is the single source of truth for a
FRESH database; existing databases upgrade through the numbered _MIGRATIONS
steps — PRAGMA user_version gates both. No ORM, no migration framework."""

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 3

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
}


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
        with con:
            con.executescript(f"BEGIN;\n{step}\nPRAGMA user_version = {version + 1};\nCOMMIT;")
        version += 1
