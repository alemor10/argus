"""The DDL contracts, tested against real SQLite (never mocked). These are the
database-level guarantees the architecture leans on: verdict-less rows are
impossible, at most one primary per (run, ticker, field), exactly one typed
value column, quarantined rows can't be primary."""

import sqlite3

import pytest

from argus.store import SCHEMA_VERSION, connect, migrate


@pytest.fixture()
def con(tmp_path):
    con = connect(tmp_path / "argus.db")
    migrate(con)
    con.execute(
        "INSERT INTO runs (kind, started_at, app_version) VALUES ('watch', '2026-07-12T14:00:00Z', 'test')"
    )
    yield con
    con.close()


def _insert_obs(con, **overrides):
    row = dict(
        run_id=1,
        ticker="NVDA",
        field="price",
        source="yahoo",
        fetched_at="2026-07-12T14:00:00Z",
        value_num=181.25,
        value_text=None,
        value_date=None,
        verdict="accepted",
        gate_reasons=None,
        is_primary=0,
    )
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join(f":{k}" for k in row)
    con.execute(f"INSERT INTO observations ({columns}) VALUES ({placeholders})", row)


def test_migrate_is_idempotent(tmp_path):
    con = connect(tmp_path / "t.db")
    migrate(con)
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "run_tickers", "run_sources", "observations", "analyst_actions",
            "earnings_results", "change_events"} <= tables
    con.close()


def test_migrate_refuses_unknown_versions(tmp_path):
    con = connect(tmp_path / "t.db")
    con.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="schema version 99"):
        migrate(con)
    con.close()


def test_migrate_v2_rebuilds_scout_candidates_preserving_rows(tmp_path):
    """v1.3's leader status + sector + peer_context require a table rebuild
    (SQLite cannot alter a CHECK); existing rows must survive with defaults."""
    con = connect(tmp_path / "t.db")
    migrate(con)
    con.executescript(
        """
        DROP TABLE scout_candidates;
        CREATE TABLE scout_candidates (
            run_id           INTEGER NOT NULL REFERENCES runs(run_id),
            ticker           TEXT    NOT NULL,
            rank             INTEGER NOT NULL,
            status           TEXT    NOT NULL CHECK (status IN ('proposed','excluded')),
            exclusion_reason TEXT,
            screen_reasons   TEXT    NOT NULL,
            screener_metrics TEXT    NOT NULL,
            PRIMARY KEY (run_id, ticker),
            CHECK ((status = 'excluded') = (exclusion_reason IS NOT NULL))
        ) WITHOUT ROWID;
        INSERT INTO runs (kind, started_at, app_version)
            VALUES ('scout', '2026-07-13T15:00:00+00:00', 't');
        INSERT INTO scout_candidates VALUES (1, 'OLDCO', 1, 'proposed', NULL, '{}', '{}');
        PRAGMA user_version = 2;
        """
    )
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    row = con.execute("SELECT * FROM scout_candidates").fetchone()
    assert (row["ticker"], row["sector"], row["peer_context"]) == ("OLDCO", "Other", None)
    con.execute(  # the rebuilt CHECK admits the new status
        "INSERT INTO scout_candidates (run_id, ticker, rank, status, screen_reasons, screener_metrics) "
        "VALUES (1, 'LEADCO', 2, 'leader', '{}', '{}')"
    )
    con.close()


def test_migrate_upgrades_older_databases_stepwise(tmp_path):
    """A database born at schema version 1 (pre company_profiles) upgrades in
    place — the numbered-migration path the deployed box will rely on."""
    con = connect(tmp_path / "t.db")
    migrate(con)
    con.execute("DROP TABLE company_profiles")  # recreate a v1-shaped database
    con.execute("PRAGMA user_version = 1")
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "company_profiles" in tables
    con.close()


def test_migrate_v5_adds_earnings_results(tmp_path):
    """The deployed box sits at v5 (scorecard_marks); the v1.6 step must add
    earnings_results in place."""
    con = connect(tmp_path / "t.db")
    migrate(con)
    con.execute("DROP TABLE earnings_results")  # recreate a v5-shaped database
    con.execute("PRAGMA user_version = 5")
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "earnings_results" in tables
    con.close()


def test_migrate_v6_adds_macro_column_and_bellwethers(tmp_path):
    """v1.7: run_tickers gains the MacroSpec snapshot column and the
    bellwether context table appears — one callable step, idempotent."""
    con = connect(tmp_path / "t.db")
    migrate(con)
    con.execute("ALTER TABLE run_tickers DROP COLUMN macro")  # v6-shaped
    con.execute("DROP TABLE bellwether_earnings")
    con.execute("PRAGMA user_version = 6")
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    columns = {row[1] for row in con.execute("PRAGMA table_info(run_tickers)")}
    assert "macro" in columns
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bellwether_earnings" in tables
    con.close()


def test_migrate_v7_adds_market_wire(tmp_path):
    """The box sits at v7 after v1.7; the v1.9 step must add market_wire."""
    con = connect(tmp_path / "t.db")
    migrate(con)
    con.execute("DROP TABLE market_wire")  # recreate a v7-shaped database
    con.execute("PRAGMA user_version = 7")
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "market_wire" in tables
    con.close()


def test_earnings_results_dedup_on_natural_key(con):
    insert = """INSERT OR IGNORE INTO earnings_results
        (ticker, quarter_end, eps_actual, eps_estimate, source, fetched_at, first_seen_run_id)
        VALUES ('NVDA', '2026-04-26', ?, 0.75, 'yahoo', ?, 1)"""
    con.execute(insert, (0.81, "2026-07-12T14:00:00Z"))
    con.execute(insert, (0.82, "2026-07-19T14:00:00Z"))  # re-fetched (revised): ignored
    rows = con.execute("SELECT eps_actual, first_seen_run_id FROM earnings_results").fetchall()
    assert len(rows) == 1
    assert rows[0]["eps_actual"] == 0.81  # first write wins — never revised
    assert rows[0]["first_seen_run_id"] == 1


def test_verdict_is_mandatory_and_constrained(con):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_obs(con, verdict=None)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_obs(con, verdict="maybe")


def test_exactly_one_value_column(con):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_obs(con, value_text="also set")  # two values
    with pytest.raises(sqlite3.IntegrityError):
        _insert_obs(con, value_num=None)  # no value


def test_quarantined_rows_cannot_be_primary(con):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_obs(con, verdict="quarantined", gate_reasons='[{"code":"out_of_bounds"}]', is_primary=1)


def test_gate_reasons_null_iff_accepted_is_db_enforced(con):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_obs(con, verdict="quarantined", gate_reasons=None)  # reason-less quarantine
    with pytest.raises(sqlite3.IntegrityError):
        _insert_obs(con, verdict="accepted", gate_reasons='[{"code":"stale"}]')  # accepted w/ reasons


def test_at_most_one_primary_per_run_ticker_field(con):
    _insert_obs(con, source="yahoo", is_primary=1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_obs(con, source="finnhub", is_primary=1)
    # a non-primary second source is fine
    _insert_obs(con, source="finnhub", value_num=181.30, is_primary=0)


def test_one_accepted_row_per_run_ticker_field_source(con):
    _insert_obs(con)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_obs(con)  # second ACCEPTED row for the same (run, ticker, field, source)


def test_quarantined_rows_are_exempt_from_source_uniqueness(con):
    """An accepted value may coexist with an UNPARSEABLE sibling from the same
    source, and several malformed records may quarantine together — a single
    bad analyst row must not roll back the whole ticker."""
    _insert_obs(con)  # accepted
    _insert_obs(
        con,
        value_num=None,
        value_text="N/A garbled",
        verdict="quarantined",
        gate_reasons='[{"code": "unparseable", "detail": "raw: N/A garbled"}]',
    )
    _insert_obs(
        con,
        value_num=None,
        value_text="also garbled",
        verdict="quarantined",
        gate_reasons='[{"code": "unparseable", "detail": "raw: also garbled"}]',
    )
    rows = con.execute(
        "SELECT verdict, COUNT(*) AS n FROM observations GROUP BY verdict ORDER BY verdict"
    ).fetchall()
    assert [(r["verdict"], r["n"]) for r in rows] == [("accepted", 1), ("quarantined", 2)]


def test_quarantined_rows_live_beside_accepted_ones(con):
    _insert_obs(con, is_primary=1)
    _insert_obs(
        con,
        field="analyst_target_mean",
        value_num=35.0,
        verdict="quarantined",
        gate_reasons='[{"code": "target_price_ratio", "detail": "3.19 outside [0.3, 3.0]"}]',
    )
    quarantined = con.execute(
        "SELECT field, gate_reasons FROM observations WHERE run_id = 1 AND verdict = 'quarantined'"
    ).fetchall()
    assert len(quarantined) == 1
    assert quarantined[0]["field"] == "analyst_target_mean"


def test_analyst_actions_dedup_on_natural_key(con):
    insert = """INSERT OR IGNORE INTO analyst_actions
        (ticker, action_date, firm, action, to_grade, source, fetched_at, first_seen_run_id)
        VALUES ('NVDA', '2026-07-10', 'Morgan Stanley', 'up', 'Overweight', 'yahoo', ?, 1)"""
    con.execute(insert, ("2026-07-12T14:00:00Z",))
    con.execute(insert, ("2026-07-19T14:00:00Z",))  # re-fetched next run: ignored
    rows = con.execute("SELECT first_seen_run_id FROM analyst_actions").fetchall()
    assert len(rows) == 1
    assert rows[0]["first_seen_run_id"] == 1
