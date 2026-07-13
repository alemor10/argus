"""Connection + migration. schema.sql is the single source of truth; no ORM,
no migration framework — PRAGMA user_version gates application."""

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def migrate(con: sqlite3.Connection) -> None:
    """Apply schema.sql to a fresh database; no-op at current version.
    Future schema changes append numbered migration steps here."""
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version == 0:
        schema = resources.files("argus.store").joinpath("schema.sql").read_text(encoding="utf-8")
        # executescript autocommits statement-by-statement unless the script
        # carries its own transaction control — embed it, so a mid-script
        # failure rolls back to a pristine DB and the next migrate() retries.
        with con:
            con.executescript(f"BEGIN;\n{schema}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
        return
    raise RuntimeError(
        f"database is at schema version {version}, this build expects {SCHEMA_VERSION} — "
        "refusing to guess"
    )
