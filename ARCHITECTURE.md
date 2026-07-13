# Argus — Architecture

This document is the agreed shape of the codebase. It was produced by a design
review (three independent proposals — minimalist, contracts-first,
provenance-first — scored by two adversarial judges against the CLAUDE.md hard
constraints) and records both the design and the reasoning behind the
contested calls, so future changes argue with the *reasons*, not just the code.

**Core idea: the SQLite file is the product.** The atomic unit is the
**observation** — one `(ticker, field, value, source, fetched_at)` fact per
row, stamped at write time with a gate verdict and machine-readable quarantine
reasons. Everything else — engine, gates, digest — is a thin, replaceable
shell around that store. A snapshot is the primary accepted observation per
field; a diff is a comparison of two runs; the quarantine report is a WHERE
clause; and any past digest can be regenerated bit-for-bit with
`argus report --run N`, because emitted events are persisted, never re-derived.

## Hard rules the architecture enforces (not just promises)

1. **Ungated data is unrepresentable.** Adapters return `RawObservation`,
   which has no verdict field. Only `gates.py` constructs `GatedObservation`;
   `store/writer.py` accepts nothing else. A CHECK constraint makes a
   verdict-less row impossible.
2. **Nothing is silently dropped.** Quarantined observations are written to
   the same table as accepted ones, with reasons. Values a source sent but the
   adapter could not parse become `UNPARSEABLE` quarantine rows (raw wire text
   preserved) — not silent absences.
3. **Every value carries provenance from birth.** `source` and `fetched_at`
   are required fields on `RawObservation`, stamped by the adapter (not the
   engine), plus optional `observed_at` when the source reports its own data
   timestamp.
4. **Read-only.** The mutation surface of the entire program: INSERTs into the
   append-only tables, two UPDATEs on `runs` (finish/sweep), one file write
   (the digest). One human-invoked exception: `argus init` scaffolds a
   commented example `watchlist.yaml` when none exists (it refuses to
   overwrite and adds no live tickers). No other command touches
   `watchlist.yaml`, and Argus never adds tickers on its own. Nothing trades,
   nothing predicts, nothing acts.
5. **Silence is a statement.** A digest is written on every run that produced
   any data — including a run with zero change events ("nothing changed" is
   information). Degraded runs disclose their degradation in the digest
   itself.

## Package layout

```
src/argus/
├── __init__.py          # version only
├── cli.py               # Typer app: watch, report --run N, init, scout (stub). Parses args,
│                        #   resolves paths, calls engine/queries. No logic.
├── config.py            # Path resolution (project-dir defaults, flag/env overrides);
│                        #   watchlist.yaml → WatchConfig → list[TickerContext]
├── fields.py            # THE field registry: Field enum + FieldSpec (kind, unary bounds,
│                        #   cross-source tolerance, max_age, source priority). Imports nothing.
├── models.py            # Pydantic v2 domain types (all frozen): RawObservation, ParseFailure,
│                        #   GatedObservation, Snapshot, TickerContext, ChangeEvent union, RunReport
├── gates.py             # PURE. Fixed pipeline: unary → staleness → cross-source → relational,
│                        #   then primary resolution. The only constructor of GatedObservation.
├── changes.py           # PURE. (baseline, current, thresholds, new analyst actions, today)
│                        #   → list[ChangeEvent]
├── engine.py            # The one loop: fetch → gate → persist → diff → events → digest.
│                        #   Takes list[TickerContext] — the seam scout reuses. Only module
│                        #   that touches sources, gates, and store together.
├── digest.py            # PURE render: RunReport → markdown (tri-state per field), plus
│                        #   DigestSink Protocol + FileDigestSink, EmailDigestSink (SMTP
│                        #   submission), CompositeSink (all sinks attempted; failures
│                        #   raised together as DeliveryError — undelivered must be loud)
├── sources/
│   ├── __init__.py      # ALL_SOURCE_TYPES registry (hand-written tuple, not entry-points)
│   ├── base.py          # DataSource Protocol: source_id, covers(ticker), fetch(ticker) → FetchResult;
│                        #   SourceError. Each adapter = thin _fetch_raw() + pure parse().
│   ├── yahoo.py         # yfinance adapter — primary for every field (stub in skeleton)
│   ├── edgar.py         # SEC companyfacts — fundamentals cross-check; covers() excludes
│                        #   OTC ADRs and ETFs (stub in skeleton)
│   └── finnhub.py       # Finnhub free tier — price cross-check only (stub in skeleton)
└── store/
    ├── __init__.py
    ├── schema.sql       # full DDL, single source of truth
    ├── db.py            # connect() (WAL, foreign_keys) + migrate() under PRAGMA user_version
    ├── writer.py        # append-only write side: begin_run, write_ticker_result (one txn
    │                    #   per ticker), record_events, finish_run, sweep_stale_runs
    └── queries.py       # entire read side as named functions over hand-written SQL

tests/
├── fixtures/            # recorded raw source payloads (incl. the pathological real NTDOY case)
├── golden/              # expected digest markdown + gate-verdict table
└── test_*.py            # see Testing
```

Dependency direction is strict and enforced by review, not tooling:
`fields`/`models` import nothing internal → `gates`/`changes`/`digest`-render
are pure over those → `sources` and `store` are the two IO edges → `engine`
is the only composition point → `cli` only calls `engine`/`queries`.

Dependencies (all of them): `typer`, `pydantic`, `pyyaml`; dev: `pytest`.
`yfinance`/`httpx` arrive with the fetch implementation. stdlib `sqlite3`,
no ORM, no migration framework.

## Domain model (the load-bearing types)

```python
# fields.py
class Source(StrEnum):
    YAHOO = "yahoo"; EDGAR = "edgar"; FINNHUB = "finnhub"

class Field(StrEnum):
    PRICE, MARKET_CAP, PE_TTM, PE_FWD, PEG, GROSS_MARGIN, OPERATING_MARGIN,
    DEBT_TO_EQUITY, NEXT_EARNINGS_DATE, ANALYST_RATING, ANALYST_TARGET_MEAN,
    ANALYST_COUNT  # closed set; adding a field = enum value + SPECS entry (test-enforced)

@dataclass(frozen=True)
class FieldSpec:
    kind: Literal["num", "text", "date"]
    bounds: tuple[float | None, float | None] | None = None  # unary sanity gate
    cross_source_rel_tol: float | None = None   # None → no pairwise check
    max_age: timedelta | None = None            # staleness vs observed_at, when source reports one
    not_in_past: bool = False                   # date-kind only → DATE_IN_PAST
    priority: tuple[Source, ...] = (Source.YAHOO,)  # primary resolution order

```

```python
# gates.py (fields.py stays import-free; the profile bundles specs + checks)
@dataclass(frozen=True)
class GateProfile:                # named, swappable bundle — scout's stricter
    specs: Mapping[Field, FieldSpec]   # gates become a second profile, not new code
    relational_checks: tuple[RelationalCheck, ...]

DEFAULT_PROFILE: GateProfile
```

```python
# models.py — Pydantic v2, all frozen
class RawObservation(BaseModel):
    ticker: str
    field: Field
    value_num: float | None      # exactly one of the three set,
    value_text: str | None       #   validated against SPECS[field].kind
    value_date: date | None
    source: Source
    fetched_at: AwareDatetime    # stamped by the ADAPTER at fetch time
    observed_at: AwareDatetime | None  # source-reported data timestamp, when available

class ParseFailure(BaseModel):   # source sent something; we couldn't parse it
    ticker: str; field: Field; raw: str; source: Source; fetched_at: AwareDatetime

class QuarantineHit(BaseModel):
    code: QuarantineCode         # NON_FINITE | OUT_OF_BOUNDS | STALE | UNPARSEABLE |
                                 # CROSS_SOURCE_DISAGREEMENT | TARGET_PRICE_RATIO | DATE_IN_PAST
    detail: str                  # "target 35.00 (yahoo) / price 10.97 = 3.19 outside [0.3, 3.0]"

class GatedObservation(BaseModel):        # only gates.py constructs this
    obs: RawObservation | ParseFailure           # a ParseFailure is always quarantined
    verdict: Literal["accepted", "quarantined"]  #   UNPARSEABLE, raw text → value_text
    reasons: tuple[QuarantineHit, ...] = ()      # non-empty iff quarantined
    corroborated_by: tuple[Source, ...] = ()     # other sources that agreed (accepted only)
    is_primary: bool = False                     # the resolved value for (ticker, field) this run

class Snapshot(BaseModel):       # per (run, ticker); hydrated from SQL, never mutated
    ticker: str
    run_id: int
    as_of: AwareDatetime
    values: dict[Field, FieldValue]                        # primary accepted, provenance intact
    quarantined: dict[Field, tuple[QuarantineHit, ...]]    # fields with ONLY quarantined obs
    # absent from both dicts = no source offered it → digest tri-state

class TickerContext(BaseModel):  # what the engine operates on — NOT "a watchlist entry".
    ticker: str                  # watch builds these from watchlist.yaml; scout will build
    thesis: str | None = None    # them from a screener feed and reuse the same pipeline.
    thresholds: Thresholds       # merged: defaults ← per-ticker overrides

class Thresholds(BaseModel):
    price_move_pct: float = 5.0
    target_move_pct: float = 10.0
    earnings_within_days: int = 7
```

Change events are a discriminated union (`kind` tag) so the renderer
pattern-matches exhaustively and the `change_events` table round-trips them
losslessly: `PriceMove`, `TargetMove`, `ConsensusShift`, `AnalystAction`
(per-firm dated upgrade/downgrade), `EarningsImminent`, `FieldQuarantined`,
`FieldRecovered`. Numeric move events carry `old_as_of` — the baseline's
timestamp — so gap-spanning comparisons are printed honestly
("−12% vs 2026-06-28").

## SQLite schema

**Position: per-field observation rows, not per-ticker JSON blobs** (all three
proposals independently converged on this). Provenance, cross-source
comparison, and queryable quarantine are per-`(field, source)` facts; "show me
every quarantined analyst target ever, with reasons" must be a WHERE clause,
not a Python script. EAV objections don't bite: the field set is a closed enum
validated before write, and volume is trivial (~900 rows/run, ~50k/year).
Value typing is enforced *in the database* by the three-column exactly-one
CHECK.

**Resolution is stamped at write time** (`is_primary`), not derived by a view.
An audit tool must freeze what it believed at run N against future code
changes; a partial unique index makes "at most one primary per
(run, ticker, field)" a database guarantee rather than an application
invariant. This also keeps the schema smaller (no priority table, no
window-function view).

```sql
CREATE TABLE runs (
    run_id      INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('watch','scout')),
    started_at  TEXT NOT NULL,               -- UTC ISO-8601 everywhere
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
    run_id     INTEGER NOT NULL REFERENCES runs(run_id),
    ticker     TEXT    NOT NULL,
    status     TEXT    NOT NULL CHECK (status IN ('ok','partial','failed')),
    error      TEXT,
    thesis     TEXT,
    thresholds TEXT    NOT NULL,   -- Thresholds.model_dump_json() at run time
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

-- One accepted row per (run, ticker, field, source); quarantined rows are
-- exempt — an accepted value can coexist with an UNPARSEABLE sibling from
-- the same source, and several malformed records may quarantine together.
CREATE UNIQUE INDEX idx_obs_one_accepted_per_source
    ON observations (run_id, ticker, field, source) WHERE verdict = 'accepted';
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
    action            TEXT NOT NULL,      -- up|down|init|reiterate|main
    from_grade        TEXT,
    to_grade          TEXT NOT NULL,
    source            TEXT NOT NULL,
    fetched_at        TEXT NOT NULL,
    first_seen_run_id INTEGER NOT NULL REFERENCES runs(run_id),
    PRIMARY KEY (ticker, action_date, firm, to_grade)
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
```

Key reads (all in `store/queries.py`, hand-written SQL):

- **baseline_run(ticker, current_run)** — latest prior watch run where this
  ticker has status `ok`/`partial`: `since last run` is per-ticker, so failed
  and crashed runs are never diffed against.
- **new_analyst_actions(run, ticker)** — rows with `first_seen_run_id =
  run`: exactly `changes.detect`'s `new_actions` input.
- **snapshot(run, ticker)** — primary accepted rows (`is_primary = 1`) plus
  quarantined-only fields → hydrates `Snapshot`.
- **latest_accepted(ticker, field, before_run)** — fallback baseline so a
  quarantine or outage gap cannot swallow a real move.
- **quarantine_report(run)** / **run_report(run)** — the digest's inputs,
  entirely from SQL (contexts come from `run_tickers.thesis`/`thresholds`,
  never the live watchlist). `run_report` hydrates EVERY quarantined
  observation into `TickerReport.quarantines` — including one coexisting
  with an accepted primary from another source, which `Snapshot.quarantined`
  (fields that went fully dark) deliberately does not carry.

## Data flow: one `argus watch` run

1. **Config.** Resolve paths (project dir by default; flags/env override) and
   secrets from the environment (`FINNHUB_API_KEY`, `ARGUS_CONTACT_EMAIL` —
   an unset secret omits that source at wiring time and the digest discloses
   the degradation). Email delivery: `ARGUS_EMAIL_TO` turns it on, with
   `ARGUS_SMTP_USER`/`ARGUS_SMTP_PASSWORD` (+ optional `ARGUS_SMTP_HOST`,
   `ARGUS_SMTP_PORT`, `ARGUS_EMAIL_FROM`; defaults fit Gmail app-password
   submission on 465) — half-configured email refuses to run rather than
   silently skipping delivery. Parse `watchlist.yaml`; reject duplicate
   tickers; merge per-ticker thresholds over defaults; produce
   `list[TickerContext]`. The engine never sees "the watchlist".
2. **Open.** `connect()` (WAL, foreign_keys) → `migrate()` (schema.sql under
   `PRAGMA user_version`). Sweep: runs stuck `running` > 6h → `failed` (their
   committed tickers remain valid baselines). `begin_run('watch')`.
3. **Per ticker, sequentially** (no async in v1 — ~25 tickers is
   seconds-to-minutes and sequential keeps failure semantics simple):
   - **Fetch.** For each source where `covers(ticker)` is true:
     `fetch(ticker)` → `FetchResult(observations, parse_failures,
     analyst_actions)`. An adapter exception is caught and recorded as a
     `run_sources` error row; other sources and tickers are unaffected.
   - **Gate.** `gates.run_gates(profile, raw, failures, as_of)` — pipeline
     below. Output: `GatedObservation` list, primaries resolved.
   - **Persist.** ONE transaction: observations (accepted + quarantined),
     analyst actions (`INSERT OR IGNORE`, `first_seen_run_id = run_id`),
     `run_sources` rows, `run_tickers` row (`ok` / `partial` / `failed`).
     From commit, this ticker's data is durable regardless of the rest of
     the run.
   - **Diff.** Hydrate baseline (per-ticker `baseline_run`) + current
     snapshots; `changes.detect(...)` → typed events → persisted with
     `baseline_run_id`.
4. **Close.** `finish_run`: `complete` if all tickers ok, `partial` if any
   data was produced, `failed` only if none.
5. **Digest.** `run_report(run_id)` assembles everything from SQL →
   `digest.render` → markdown → `FileDigestSink` →
   `reports/digest-YYYY-MM-DD-runN.md`.

**Exit codes: 0 whenever the user will SEE a digest (complete *or* partial —
data degradation is disclosed inside the digest, which is the alerting
channel); 1 when they won't: no digest was produced, or it was rendered but
a delivery sink failed (on a headless box an undelivered digest is an unseen
digest).** Rationale: a wrapper that pages on nonzero must not page weekly on
a flaky free feed — alarm fatigue is itself a silent-failure vector — so
partial *data* exits 0. Nonzero is reserved for "the human will not get a
report", the one condition that genuinely needs outside attention.

Partial-failure behavior, by construction: one source down → its fields show
"no data (finnhub: HTTP 502)" and cross-checks are skipped-and-disclosed; one
ticker dead → listed under fetch failures, next run diffs it against the last
good run (reported late, never lost); crash mid-run → completed tickers are
already committed and baseline-eligible, the stale `running` row is swept to
`failed` on next start.

## Quality gates (fixed order; each stage sees the survivors of the previous)

1. **Parse boundary.** `ParseFailure`s become `UNPARSEABLE` quarantine rows
   with the raw wire text preserved in `value_text`. A value the source sent
   but we couldn't read is *evidence*, not an absence.
2. **Unary plausibility** — data-driven from `FieldSpec.bounds`. Bounds are
   deliberately **wide sanity rails, not judgment**: price ∈ (0.0001, 10M)
   (BRK-A must pass), forward P/E may be negative (expected-loss names are
   real), margins allow deep negatives. Rationale: a false-positive machine
   trains the reader to skim the quarantine section, which must stay credible.
   Tighten empirically later — the observations table keeps the distributions
   forever. Codes: `NON_FINITE`, `OUT_OF_BOUNDS`, `DATE_IN_PAST`.
3. **Staleness** — when a source reports its own data timestamp
   (`observed_at`) and `as_of − observed_at > max_age`: quarantine `STALE`.
   Catches yfinance serving cached/lagging quotes. Skipped when the source
   reports no timestamp — we gate on evidence, not guesses.
4. **Cross-source agreement** — for fields with ≥2 accepted observations and
   a tolerance: price Yahoo-vs-Finnhub ±2%; fundamentals Yahoo-vs-EDGAR ±25%
   (wide: TTM-vs-fiscal-window mismatches are legitimate; tighten from
   observed distributions later). Beyond tolerance → **quarantine ALL
   disagreeing observations** (`CROSS_SOURCE_DISAGREEMENT`): with n=2 you
   cannot adjudicate, and picking a winner is a coin flip dressed as data.
   Within tolerance → all accepted, each stamped `corroborated_by`. Only one
   source responded → accepted uncorroborated; the digest's data-health
   section discloses that the cross-check didn't run.
5. **Relational cross-field** — plain pure functions over the ticker's
   accepted values, from `GateProfile.relational_checks`. The NTDOY gate:
   `analyst_target_mean / price ∉ [0.3, 3.0]` → quarantine — with
   **corroboration-aware blame**: quarantine only the *uncorroborated* leg
   when exactly one leg is corroborated; quarantine **both** when fault
   cannot be localized. (A statically-blamed gate accepts the bad value in
   exactly the scenario where the cross-check source is down.)
6. **Primary resolution** — among accepted observations per field, the first
   available source in `FieldSpec.priority` becomes `is_primary`.

NTDOY walkthrough (the founding case): Yahoo price 10.97, Finnhub 10.99 →
agree, both accepted, corroborated. Yahoo target 35.00 → passes unary (a
plausible number, wrong ticker), no second source. Relational: 35.00/10.97 =
3.19 → price leg is corroborated, target is not → **target quarantined**,
price untouched. "218% upside" is uncomputable, because derived metrics and
the digest read accepted values only.

**Digest tri-state.** Every watched field renders as exactly one of:
- `Fwd P/E 31.2 (yahoo, 2026-07-12 14:03Z)` — value with provenance;
- `⚠ DATA QUARANTINED — target/price 3.19 outside [0.3, 3.0] (yahoo 35.00)`;
- `— no data (edgar: not applicable for OTC ADR)` / `— no data (finnhub: HTTP 502)`.

Absence of signal is never confusable with absence of data. Quarantine
*transitions* additionally emit headline events (`FieldQuarantined` /
`FieldRecovered`) — a field going dark is news, not a footnote.

## Change detection (`changes.py`, pure)

- **PriceMove / TargetMove** — |Δ%| between accepted baseline and accepted
  current ≥ threshold. If the field was quarantined or missing in the
  baseline snapshot, fall back to `latest_accepted` — **a change is reported
  late, never lost** — and print `old_as_of` so the comparison window is
  honest. Never computed against a quarantined endpoint (recovery emits
  `FieldRecovered` + establishes a new baseline instead of a fake move).
- **ConsensusShift** — rating text moved along the ordered scale
  `strong_buy > buy > hold > underperform > sell` (`unclear` when either
  grade is off-scale — reported anyway, never suppressed).
- **AnalystAction** — exactly the `analyst_actions` rows with
  `first_seen_run_id = current_run`. No window arithmetic; correct across
  crashes by construction. Suppressed on a ticker's first-ever run: the
  source hands over its entire dated history then (a real first run yielded
  1,100 lines of 2012-era actions), and history at baseline time is
  baseline, not news — the rows are stored, so only genuinely new actions
  fire from the next run on.
- **EarningsImminent** — state event, re-fires each run inside the window
  (at weekly cadence ≤2 reminders; suppression logic's failure mode is
  silence, the one thing this tool exists to prevent).
- **FieldQuarantined / FieldRecovered** — verdict transitions per field.

Thresholds come merged from `watchlist.yaml` (`defaults:` + per-ticker
overrides); `changes.py` only ever sees a final `Thresholds`.

```yaml
defaults:
  price_move_pct: 5.0
  target_move_pct: 10.0
  earnings_within_days: 7
tickers:
  - ticker: NVDA
    thesis: "Datacenter capex supercycle; CUDA moat."
    thresholds: { price_move_pct: 8.0 }   # volatile name, raise the bar
  - ticker: NTDOY
    thesis: "Switch 2 cycle + IP monetization."
```

## Testing

The pure/IO split *is* the test strategy:

- **Pure, no IO (the bulk):** `gates.py` (feed observations, assert verdict +
  reason codes — `test_ntdoy_stale_target_quarantined` is a named regression
  test), `changes.py` (snapshot pairs → exact event lists, incl. the
  quarantine-gap fallback), `digest.py` render, config threshold merging, and
  a completeness test: every `Field` has a `FieldSpec`; every priority source
  is registered.
- **Store against real SQLite** (`tmp_path`; never mock sqlite3): primary
  resolution uniqueness (the partial index), baseline selection skipping
  failed/crashed runs, analyst-action dedup + `first_seen_run_id` across a
  simulated failed run, migration idempotence. Written as a contract-test
  class over the store's interface — the insurance policy for a future
  paid-feed store.
- **Adapters via recorded fixtures:** each adapter = `_fetch_raw()` (thin
  network) + `parse()` (pure). Tests exercise `parse()` over checked-in
  Phase-0 payloads only, including the pathological real NTDOY payload.
  A `@pytest.mark.live` smoke suite exists but is excluded by default —
  CI must never depend on free feeds.
- **Golden tests:** (1) fabricate two runs covering the pathology matrix
  (NTDOY bad target, a threshold-crossing price move, one dead ticker, one
  source down globally, a new analyst downgrade, earnings 4 days out), drive
  the engine end-to-end on a tmp DB with stub sources, byte-compare the
  digest — **including the negative assertion that the string "218" appears
  nowhere**. (2) A gate-verdict table (~30 `(field, value, context) →
  (verdict, code)` rows) compared wholesale, so any bound change is a
  reviewed diff, never a silent behavior change.
- **Determinism:** `now`/`today` are injected parameters everywhere (no Clock
  abstraction — arguments are enough); renderer sorts all output; fixture
  timestamps fixed.

## Non-goals for v1 (deliberate)

No live fetch logic in the skeleton (adapters stub `_fetch_raw`, `parse` is
implemented against fixture shapes when fetch lands). No scout (CLI stub
naming the gate: paid-data decision pending). No thesis-drift detection (the
thesis string is printed beside each ticker's changes — human adjacency now,
machine reasoning post-v1). No ETF look-through. No sink beyond
`FileDigestSink` (the Protocol exists; one implementation ships). No
async/parallel fetch, no retry framework (one inline retry, then record the
failure), no daemon (cron/launchd invokes `argus watch`), no derived-metric
storage (computed at render time from accepted values only).

**Deliberately not abstracted:** fields are a closed enum, not a dynamic
registry (every field must have a spec — enforced by test). Relational gates
are a plain tuple of functions in a `GateProfile` — scout's stricter profile
will be a second value, and any further abstraction gets extracted *then*,
from two real examples. Digest is f-string composition, not templates. Source
registration is a hand-written tuple, not entry-points. No ORM, no query
builder: `schema.sql` + `user_version` + `queries.py` is the entire data
layer — when the database is the product, hand-written SQL in one module is
the point.

## Decision log (contested calls and why they went this way)

| Decision | Alternatives considered | Resolution |
|---|---|---|
| Per-field observation rows | Per-ticker JSON blob | Unanimous across proposals: quarantine/provenance must be queryable per (field, source); volume is trivial. |
| `is_primary` stamped at write | Resolution VIEW over a priority table; materialized snapshot table | Write-time stamp freezes "what Argus believed at run N" against future code changes (audit requirement) and is DB-enforced by a partial unique index; a separate snapshot table stores values twice (drift risk), a view re-resolves history if priorities change. |
| Quarantine BOTH on 2-source disagreement | Trust the priority source | With n=2 there is no adjudication; a disclosed gap beats a confident coin flip. Majority-wins unlocks if a third source lands. |
| Corroboration-aware blame in relational gates | Statically blame one field | Static blame accepts the bad value exactly when the cross-check source is down — the poison class this project exists to kill. |
| Fall back past quarantine/outage gaps to `latest_accepted`, print `old_as_of` | Diff adjacent runs only; or fall back past outages but not quarantine | Both endpoints passed the gates; suppressing the comparison is a silent failure. Honesty comes from printing the window, not hiding the event. |
| `analyst_actions` as its own event-shaped table | JSON array in an observation; defer to post-v1 | Per-firm actions are an explicit v1 roadmap item and are events, not levels; `first_seen_run_id` set-membership is crash-correct with zero window arithmetic. |
| Exit 0 on partial runs (digest produced) | Exit 2 on partial | The digest is the alerting channel; nonzero must mean "no report exists". Weekly pages on a flaky free feed train the user to ignore alerts. |
| Wide unary bounds; EDGAR tolerance 25% | Tight bounds (price ≤ 100k, P/E > 0), 10% tolerance | Tight rails false-quarantine real securities (BRK-A, expected-loss names) and TTM-window mismatches; chronic noise erodes the quarantine section's credibility. Tighten empirically from stored data. |
| EarningsImminent re-fires in window | Dedupe/suppression key | ≤2 repeats at weekly cadence vs. suppression bugs whose failure mode is silence. |
| Single `gates.py`, injected `now`, flat-ish layout | gates/ package, Clock protocol, deeper layering | One maintainer, three years: same discipline, half the directories. |
| Per-ticker events commit separately from the data commit | One transaction spanning persist + diff | A crash exactly between the two commits loses that ticker's events for the window (data stays durable and shows in the next watchlist). Accepted: the window is milliseconds weekly, and merging would either re-derive snapshots outside SQL or complicate the writer's transaction contract. |
| Crashed runs: committed data stays baseline-eligible; recovery is offered, not automatic | Exclude crashed runs from baselines | Excluding them would re-report stale diffs; instead the sweep returns the crashed run ids and the CLI points at `argus report --run N`, which renders the crashed run's already-persisted events. |
| First-run analyst history is baseline, not news | Emit every first-seen action | The feed hands over its full dated history on a ticker's first run (1,100 lines observed live); suppressed then, set-membership from the next run on. |
| Delivery failure exits 1 (unlike partial data, which exits 0) | Treat any produced digest as success | Partial data still reaches the reader with its degradation disclosed; a failed delivery sink on a headless box means the reader gets NOTHING — that is the silent-failure class this tool exists to prevent. The file copy still lands and the error names it. |

## Post-v1 seams (built), and where extensions land

- **scout** → constructs its own `list[TickerContext]` from a paid screener
  feed and calls the same `engine.run(...)`; stricter gates = a second
  `GateProfile` value; `runs.kind='scout'` already in the schema.
- **New/replacement data source (EODHD etc.)** → one new module in
  `sources/` implementing the Protocol + a priority entry; `corroborated_by`
  and the store contract tests are the migration insurance.
- **Thesis drift** → consumes persisted `change_events` history next to the
  stored thesis line.
- **ETF look-through** → follows the `analyst_actions` precedent: a dedicated
  relation-shaped table (constituents) beside `observations`, not forced into
  the scalar model.
- **Email/notification digests** → additional `DigestSink` implementations.
