-- Argus schema — single source of truth, applied under PRAGMA user_version.
-- Append-only: the mutation surface of the whole program is INSERTs here,
-- two UPDATEs on runs (finish/sweep), and one digest file write.
-- All timestamps are UTC ISO-8601 TEXT.

CREATE TABLE runs (
    run_id      INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('watch','scout')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running','complete','partial','failed')),
    app_version TEXT NOT NULL,
    notes       TEXT
);

-- Per-ticker outcome, committed as each ticker finishes: a crash mid-run
-- leaves completed tickers durable and baseline-eligible. Carries the
-- TickerContext as of the run (thesis + thresholds JSON) so run_report and
-- `argus report --run N` regenerate bit-for-bit from SQL alone, even after
-- the watchlist changes.
CREATE TABLE run_tickers (
    run_id        INTEGER NOT NULL REFERENCES runs(run_id),
    ticker        TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN ('ok','partial','failed')),
    error         TEXT,
    thesis        TEXT,
    thresholds    TEXT    NOT NULL,   -- Thresholds.model_dump_json() at run time
    thesis_checks TEXT    NOT NULL DEFAULT '[]',  -- JSON [ThesisCheck, ...] at run time
    macro         TEXT,               -- MacroSpec JSON at run time; NULL = watch role.
                                      -- The whole spec is snapshotted so report --run N
                                      -- reproduces the Macro section after macro.yaml edits.
    PRIMARY KEY (run_id, ticker)
) WITHOUT ROWID;

-- Per (run, ticker, source) fetch outcome: the digest's data-health section
-- distinguishes "source down" from "source doesn't carry this ticker".
CREATE TABLE run_sources (
    run_id     INTEGER NOT NULL REFERENCES runs(run_id),
    ticker     TEXT    NOT NULL,
    source     TEXT    NOT NULL,
    status     TEXT    NOT NULL CHECK (status IN ('ok','error','not_applicable')),
    error      TEXT,
    latency_ms INTEGER,
    PRIMARY KEY (run_id, ticker, source)
) WITHOUT ROWID;

-- THE table. Append-only. Quarantined rows live beside accepted ones:
-- quarantine is a verdict on data, not a different kind of data.
CREATE TABLE observations (
    obs_id          INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),
    ticker          TEXT    NOT NULL,
    field           TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    fetched_at      TEXT    NOT NULL,
    observed_at     TEXT,
    value_num       REAL,
    value_text      TEXT,
    value_date      TEXT,
    verdict         TEXT    NOT NULL CHECK (verdict IN ('accepted','quarantined')),
    gate_reasons    TEXT,   -- JSON [{"code":…,"detail":…}]
    corroborated_by TEXT,   -- JSON ["finnhub"]; NULL if uncorroborated
    is_primary      INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
    CHECK ((value_num IS NOT NULL) + (value_text IS NOT NULL) + (value_date IS NOT NULL) = 1),
    CHECK (NOT (verdict = 'quarantined' AND is_primary = 1)),
    -- "NULL iff accepted" is a database guarantee, not a comment:
    CHECK ((verdict = 'quarantined') = (gate_reasons IS NOT NULL))
);

-- One-row-per-(run, ticker, field, source) holds for ACCEPTED observations
-- only: several quarantined rows per pair are legitimate (an accepted value
-- coexisting with an UNPARSEABLE sibling from the same source, or multiple
-- malformed records). Partial, like idx_obs_one_primary below.
CREATE UNIQUE INDEX idx_obs_one_accepted_per_source
    ON observations (run_id, ticker, field, source) WHERE verdict = 'accepted';

-- "At most one primary per (run, ticker, field)" is a DATABASE guarantee,
-- not an application invariant.
CREATE UNIQUE INDEX idx_obs_one_primary
    ON observations (run_id, ticker, field) WHERE is_primary = 1;
CREATE INDEX idx_obs_lookup ON observations (ticker, field, verdict, run_id);
CREATE INDEX idx_obs_run    ON observations (run_id, verdict);

-- Event-shaped source data gets its own honest shape (per-firm dated actions
-- from yfinance upgrades_downgrades). INSERT OR IGNORE on the natural key;
-- first_seen_run_id makes "new since last run" a set-membership fact that is
-- automatically correct across failed runs.
CREATE TABLE analyst_actions (
    ticker            TEXT NOT NULL,
    action_date       TEXT NOT NULL,
    firm              TEXT NOT NULL,
    action            TEXT NOT NULL,
    from_grade        TEXT,
    to_grade          TEXT NOT NULL,
    source            TEXT NOT NULL,
    fetched_at        TEXT NOT NULL,
    first_seen_run_id INTEGER NOT NULL REFERENCES runs(run_id),
    PRIMARY KEY (ticker, action_date, firm, to_grade)
) WITHOUT ROWID;

-- Event-shaped like analyst_actions: one REPORTED quarter per row — realized
-- EPS beside the street estimate at report time. Scheduled future quarters
-- have no actual and never land here. INSERT OR IGNORE on the natural key;
-- first_seen_run_id makes "reported since last run" a set-membership fact
-- that is automatically correct across failed runs. First write wins: a
-- later revision of an actual never rewrites what Argus first reported.
CREATE TABLE earnings_results (
    ticker            TEXT NOT NULL,
    quarter_end       TEXT NOT NULL,      -- fiscal quarter end (ISO date)
    eps_actual        REAL NOT NULL,
    eps_estimate      REAL,               -- street consensus at report time; NULL if none
    source            TEXT NOT NULL,
    fetched_at        TEXT NOT NULL,
    first_seen_run_id INTEGER NOT NULL REFERENCES runs(run_id),
    PRIMARY KEY (ticker, quarter_end)
) WITHOUT ROWID;

-- Descriptive business identity per ticker, append-only (latest fetched_at
-- wins on read). Not gate-material — no plausibility bounds exist for prose —
-- but provenance-stamped like everything else. Reports render it; the diff
-- engine never looks at it.
CREATE TABLE company_profiles (
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

-- Scout candidates per run (event-shaped, like analyst_actions): which names
-- the screen surfaced, how they ranked, and whether enrichment+gates kept
-- them (proposed) or dropped them (excluded, with the reason). Streaks are
-- derived by walking prior scout runs. Screener numbers live only here, as
-- labeled claims — never in observations.
CREATE TABLE scout_candidates (
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    ticker           TEXT    NOT NULL,
    rank             INTEGER NOT NULL,   -- global rank among all screen passers
    status           TEXT    NOT NULL CHECK (status IN ('proposed','excluded','leader')),
    sector           TEXT    NOT NULL DEFAULT 'Other',  -- canonical bucket
    exclusion_reason TEXT,
    screen_reasons   TEXT    NOT NULL,   -- JSON {rule: "fwd P/E 20.4 ≤ 25", ...}
    screener_metrics TEXT    NOT NULL,   -- JSON raw screener row (labeled claims)
    peer_context     TEXT,               -- JSON industry peers + median fwd P/E (claims)
    PRIMARY KEY (run_id, ticker),
    CHECK ((status = 'excluded') = (exclusion_reason IS NOT NULL))
) WITHOUT ROWID;

-- Bellwether earnings context per run: the megacap calendar window fetched
-- from Finnhub, filtered to macro.yaml's bellwethers list. CLAIMS-labeled
-- display data (single unofficial source, never gated) — persisted per run
-- only so report --run N reproduces the section; never in observations or
-- earnings_results.
CREATE TABLE bellwether_earnings (
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    symbol           TEXT    NOT NULL,
    report_date      TEXT    NOT NULL,   -- ISO date
    hour             TEXT,               -- bmo | amc | '' as reported
    eps_estimate     REAL,
    eps_actual       REAL,
    revenue_estimate REAL,
    revenue_actual   REAL,
    PRIMARY KEY (run_id, symbol, report_date)
) WITHOUT ROWID;

-- The magazine issue's market pages (v1.9): movers, sector pulse, earnings
-- wire, 52-week extremes — ONE claims-labeled JSON blob per watch run
-- (models.MarketWire), persisted so `report --run N` reproduces the issue.
-- Curation is mechanical (cap floors, top-N — see market.py), never judgment.
CREATE TABLE market_wire (
    run_id  INTEGER PRIMARY KEY REFERENCES runs(run_id),
    payload TEXT    NOT NULL
) WITHOUT ROWID;

-- Scout self-scoring — an immutable forward log ("grade the grader"). On each
-- scout run, every name scout has EVER proposed is scored: total return since
-- it first surfaced vs SPY over the same window. Persisted per scoring run so
-- the scorecard reproduces bit-for-bit and is never retroactively revised.
-- The market is the answer key; the engine never grades itself.
CREATE TABLE scorecard_marks (
    run_id            INTEGER NOT NULL REFERENCES runs(run_id),  -- the SCORING run
    ticker            TEXT    NOT NULL,
    first_proposed_at TEXT    NOT NULL,   -- date scout first proposed this name
    weeks_out         INTEGER NOT NULL,   -- whole weeks from first proposal to this run
    name_return       REAL    NOT NULL,   -- fraction; total return incl. divs (adjusted close)
    spy_return        REAL    NOT NULL,   -- SPY total return over the same window
    PRIMARY KEY (run_id, ticker)
) WITHOUT ROWID;

-- Emitted events, persisted so `argus report --run N` regenerates any digest
-- exactly and the digest never re-derives differently from what was reported.
CREATE TABLE change_events (
    event_id        INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),
    ticker          TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    payload         TEXT    NOT NULL,     -- ChangeEvent.model_dump_json()
    baseline_run_id INTEGER REFERENCES runs(run_id)  -- NULL for state events
);
CREATE INDEX idx_events_run ON change_events (run_id);
