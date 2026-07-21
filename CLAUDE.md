# Argus — Personal Equity Monitor + Discovery Tool

Named for Argus Panoptes, the hundred-eyed watchman: some eyes always open, watches
and reports, **never acts**.

## What this is

A read-only tool with two capabilities on one fundamentals-and-quality engine:

1. **Monitor** (`argus watch`) — track a defined watchlist, detect what changed since
   the last run, produce a thesis-aware digest.
2. **Discover** (`argus scout`) — scan a broad universe for names *not* held that match
   defined criteria, and surface a shortlist with reasons. Proposes only.

## Hard constraints (non-negotiable)

- **Read-only.** No trading, no order execution, no P&L accounting, no auto-adding
  tickers to the watchlist. Argus informs; the human decides.
- **No self-generated price predictions.** Report data and changes, not forecasts.
- **No autonomous decision-making.** Runs on a schedule, reports, stops.
- **Data quality gates everywhere.** Bad data silently poisons output:
  - Cross-source sanity checks (Yahoo vs SEC EDGAR vs Finnhub free tier).
  - Reject/quarantine implausible values — never silently drop or silently pass them.
    Quarantined fields appear in the digest as "data quarantined," so absence of
    signal is distinguishable from absence of data.
  - Timestamp everything: every stored value carries `source` + `fetched_at`.
  - Real example from the Phase-0 spike: NTDOY showed a $35 analyst target vs a
    $10.97 price (stale pre-ADR-ratio-change value). A naive pipeline would report
    "218% upside." Plausibility bounds (e.g. target/price outside [0.3, 3.0] →
    quarantine) exist because of exactly this.

## Roadmap

### v1 — Core monitor (build first)
- [ ] Watchlist config (`watchlist.yaml`): ticker + one-line thesis + optional
      per-ticker alert thresholds
- [ ] Fetch layer: price, core valuation (P/E, forward P/E, PEG, market cap,
      margins, debt/equity), next earnings date, analyst ratings/targets
- [ ] Snapshot store (SQLite, append-only) — change detection = diff of last two
      snapshots per ticker
- [ ] Analyst *change* detection: rating upgrades/downgrades and target moves since
      last run
- [ ] Weekly digest (markdown) + event triggers: big price move, earnings imminent
- [ ] Quality gate module (see constraints above) — built alongside v1, not after

### Post-v1 — Intelligence layer
- [x] Thesis-drift detection (v1.4, 2026-07-14): the human attaches falsifiable
      `thesis_checks` when promoting a name ("revenue_growth >= 20%"); watch flags
      a breach against the stated line. Argus NEVER interprets the thesis prose —
      it reports data vs a human-drawn line (no forecasts, human decides). See
      ARCHITECTURE.md, Thesis drift.
- [x] Earnings results reporting (v1.6, 2026-07-15): when a quarter's results
      land since the last run, watch reports realized EPS vs the street estimate
      at report time ("EPS 1.05 vs 0.93 est (+12.9%)"). Realized data against a
      line third parties drew — never an Argus forecast. Event-shaped like
      analyst actions (first-seen set membership; first-run history is baseline,
      not news). See ARCHITECTURE.md, Change detection.
- [x] Macro watch + daily pulse (v1.7, 2026-07-15): `macro.yaml` series —
      market quotes (Yahoo: 10Y/3M yields, VIX, S&P) and economic releases
      (FRED, keyless: CPI/core-PCE YoY, unemployment, payrolls MoM, Fed funds)
      — levels + Δ in every watch digest, with human-drawn alert lines
      ("value >= 25"), absolute-move thresholds, and release-day prints as
      events. Daily cadence via `argus watch --deliver events-only`: file
      digest always written, Discord/email only when a run carries NEW
      information (the alarm-fatigue guard). Bellwether megacap earnings
      calendar as a claims-labeled context section (one Finnhub call). See
      ARCHITECTURE.md, Macro watch + daily pulse.
- [x] PDF-first delivery (v1.8, 2026-07-15): the PDF carries the WHOLE digest
      (watch page 1 = Macro + every change event + bellwethers; page 2 =
      watchlist, quarantine table, data health; then detail pages) and is what
      Discord/email deliver — the .md attaches only as the no-PDF fallback.
      Markdown remains the canonical on-disk record (bit-for-bit `report
      --run N`, golden byte-compares). See ARCHITECTURE.md, PDF-first delivery.
- [x] The Argus Daily (v1.9, 2026-07-16): the digest is a MAGAZINE issue —
      beside your desk and the macro dashboard, market-wide pages from one
      scan + one calendar call (movers, sector pulse, earnings wire, 52-week
      extremes; mechanical curation, claims-labeled). Tue–Sat issues post
      always; Monday is a quiet events-only pulse; crypto/commodities joined
      the macro strip. See ARCHITECTURE.md, The market wire.
- [x] The Sunday Edition (v1.10, 2026-07-16): `argus recap` — the week in one
      PDF, aggregated purely from the store (day-stamped events with standing
      reminders rolled up, macro week-over-week, shortlist churn, scorecard,
      week-ahead pins). Sunday posts ONE message: the Edition + the morning's
      scout PDF (scout runs `--deliver never`). See ARCHITECTURE.md, The
      Sunday Edition.
- [x] The visual issue + Radar (v1.11, 2026-07-16): masthead, macro stat-tile
      dashboard with 30-day sparklines, sector/mover diverging bar charts,
      colored earnings wire, page-floor overflow guard. Radar section: standing
      scout shortlist in every issue, mechanical crossings vs the market wire,
      and the `consider` tier (`argus consider TICKER` → gated daily tracking,
      `promote` graduates; consider.yaml is machine-managed). Fixes: calendar
      duplicate rows deduped (conservative surprise), honest "not consulted"
      health wording, same-day Δ label. See ARCHITECTURE.md, The Radar.
- [x] Featured stocks (v1.12, 2026-07-16): each Daily carries up to three
      "Worth reading about" cards — business summary, sector/size, claimed
      numbers — picked by disclosed mechanical rules (top mover each way,
      largest upcoming reporter). Claims-labeled; prose verbatim; ~3 extra
      fetches per issue; persisted in the wire blob.
- [x] Rich feature cards (v1.13, 2026-07-16): each card now carries the 52w
      range, valuation (fwd P/E, beta), quality (revenue+growth, margins, ROE,
      yield), the street's view (consensus, mean target — render-railed to
      [0.3,3]×price, the NTDOY rule applied to claims; analyst count), a
      sentence-trimmed summary, and a 1-year price strip (ungated, captioned).
- [x] ETF rebalancing (v1.14, 2026-07-16): watch well-known ETFs (SPY, DIA,
      the 11 sector SPDRs, Vanguard VOO/VTI/VYM/VUG) via issuer daily-holdings
      feeds; report constituent adds/drops when membership changes (forced-flow
      signal — an index add means index funds must buy). Change-log storage
      (blob only on change), reproducible diff, claims-labeled, feeds the
      delivery gate. Issuer feeds give tickers directly. See ARCHITECTURE.md,
      ETF rebalancing.
- [x] N-PORT holdings source (v1.16, 2026-07-16): funds whose issuer blocks
      headless requests (Schwab's SCHD, iShares core) are served from the
      official SEC N-PORT filing instead. Lagged (~monthly) and CUSIP-keyed
      with no ticker — but rebalance detection needs only a stable identity
      (CUSIP) + a display name (company), never a ticker, so the CUSIP→ticker
      join that made N-PORT "the hardest data-eng piece" is sidestepped, not
      solved. `EtfHolding` splits `key` (ticker→CUSIP) from `label`
      (ticker→name); the lag is disclosed on the rebalance line. Needs a SEC
      contact email. SCHD is now followable. See ARCHITECTURE.md, ETF
      rebalancing.
- [x] Insider buys (v1.15, 2026-07-16): Form 4 open-market purchases (code P)
      by officers/directors on watchlist + consider names AND the scout shortlist (Radar crossing) — realized filed
      data, event-shaped like analyst actions (first-seen; first-run history
      is baseline). Rides EdgarSource as a best-effort secondary channel;
      grants/options/sales filtered at the adapter. Dormant until names held.
      See ARCHITECTURE.md, Insider buys.
- [x] Scout breadth + visual shortlist (v1.17, 2026-07-19): a wider funnel
      (`top_n` 15→20, `max_per_sector` 3→4) surfaces more names, and the scout
      PDF paginates its front matter — page 1 the shortlist (a **New this
      week** callout foregrounding first-seen names, streak ≤ 1, + the compact
      proposals table that IS the long tail's home), page 2 the back matter
      (exclusions, scorecard, data health). Only the top 12 proposals by rank
      earn a full detail page; the rest read from the table. Three new charts,
      all backward/current-looking (no forecasts): a per-name **α-vs-SPY
      diverging-bars** scorecard chart, a per-name **peer valuation dot plot**
      (verified fwd P/E vs industry peers + median — the "cheap for its
      growth" thesis made visual), and a **rank-trajectory sparkline** (screen
      rank across recent proposed weeks). Markdown digest unchanged except the
      callout; goldens hold. See ARCHITECTURE.md, PDF-first delivery.
- [x] Sunday Edition, visual issue (v1.18, 2026-07-19): the weekly recap
      became a two-page magazine issue instead of a plain text page —
      masthead, a **macro scoreboard** (per-series tile: level + colored
      week-over-week move + the week's sparkline, biggest proportional move
      first), the week's events, then a discovery page (shortlist churn
      coloured entered-green/dropped-red + the scorecard's per-name α-vs-SPY
      bars) and the week ahead. Two data fixes: the macro Δ now falls back to
      the week's first run when a series has no prior-week baseline (the
      Jul-19 issue silently degraded to bare levels), carrying each run's
      value in `RecapMacroLine.path` for the sparkline; and unchanged daily
      macro re-prints (Fed-funds-effective, delta 0) roll into the suppressed
      count instead of drowning the events section. See ARCHITECTURE.md, The
      Sunday Edition.
- [x] Sector board + deterioration watch (v1.19, 2026-07-19): two non-quality
      lenses on the FULL market scan Argus already pulls (and today discards
      ~99% of). **Sector board** — top ~3 per canonical sector by within-sector
      forward-PEG, sanity floors only (drops the margin/ROE/leverage gates a
      bank/utility/REIT structurally can't meet), so every sector fills.
      **Deterioration watch** — names with weakening fundamentals (shrinking
      revenue, collapsing EPS, unprofitable ops, priced-for-gone-growth),
      reported as FACTS, never a forecast or trade signal (the read-only /
      no-forecast constraint holds — Argus informs, the human decides). Both
      are screener claims — never enriched, gated, or scored — so the
      conviction shortlist and its scorecard stay pure. New scout PDF page 3 +
      digest sections; `scout_candidates` gains `board`/`deterioration`
      statuses (schema v12). See ARCHITECTURE.md, Scout.
- [x] Scout buckets + reading cards (v1.20, 2026-07-20): renamed the three
      discovery sections to the human's language — **Conviction** (graded
      shortlist), **Worth watching** (sector board), **Under pressure**
      (deterioration) — and added **reading cards** for a curated few of the
      broader-lens names (up to 3 worth-watching leaders + 3 under-pressure),
      so they can be READ (business summary + claimed numbers + 1-yr price
      strip), not just scanned. Cards reuse the Daily's featured-card machinery
      (`fetch_feature_card` + a parameterized `_featured_page`); curation is
      `scout_card_subjects` (deterministic → `report --run N` reproduces).
      Watchlist stays in the Daily (scout is names you don't hold). No schema
      change.
- [x] Discord-safe publication (v1.21, 2026-07-20): the trustworthiness
      milestone, personal scope. **Security boundary** — redact() applied at
      the writer/CLI/engine persistence+output boundaries (not just inside
      providers); channels always CompositeSink-wrapped; sentinel-secret
      tests assert a fake token/webhook appears nowhere (SQLite dump, digests,
      RunOutcome, CLI output). **Publication lifecycle** (schema v13) —
      runs.publication_status walks collecting→assembled→artifact_committed→
      delivery_pending→delivered|delivery_failed (+file_only/artifact_failed);
      real published_at timestamps; diff/hook failures persisted; flock run
      lock serializes watch/scout/recap/deliver. **Immutable artifacts +
      outbox** (schema v14) — sha256+renderer recorded per file, atomic
      writes, delivery_outbox rows per channel attempt; `argus deliver` retries
      undelivered posts without re-collection (refuses hash-mismatched files,
      never double-posts); `argus report --run N` verifies the original and
      writes divergent regenerations to a -rerender file. **Honest language**
      — "Research shortlist" (was Conviction), "gate-accepted" (was verified),
      precise survivorship caption. See ARCHITECTURE.md, Discord-safe
      publication.
- [x] Evidence contract + honest scorecard (v1.22, 2026-07-21): the
      trustworthiness slice before the closed beta. **Fixed-horizon scorecard**
      (schema v15) — grades past proposals at 4/13/26/52-week horizons instead
      of "total return since proposed" (which quietly re-priced every run); a
      horizon return is locked once measured; one row per (run, name, matured
      horizon) + an entry sentinel; disjoint matured/pending/unpriceable buckets
      so "too young" never reads as "no data"; min-sample gate (n≥3) withholds a
      horizon's medians. Old age-cohort marks dropped by migration (log too young
      to matter). **Evidence contract** (`evidence.py`, pure) — a four-state
      backing label per metric (corroborated / single-source / claim-only /
      missing), screen-exit conditions read back from persisted reason strings
      (the human's thresholds as factual falsification lines), and factual data
      flags (near-boundary, claim-only/single-source core metrics, quarantine) —
      provenance and thresholds as fact, never a forecast or opinion. See
      ARCHITECTURE.md, Scout self-scoring. NEXT: stop building — run the closed
      beta (roadmap items 3–5 deferred).
- [ ] ETF look-through concentration (portfolio's true single-name exposure):
      needs holdings you own; distinct from the rebalancing feature above.

### Scout self-scoring — SHIPPED v1.5 (2026-07-14, "grade the grader")
- [x] Each scout run scores how its past proposals have done vs SPY — realized
      returns, forward log, no survivorship, never revised, reproducible from
      persisted marks. The market is the answer key; the engine never grades
      itself. See ARCHITECTURE.md, Scout self-scoring.

### Discovery module — SHIPPED as scout v1.1 (2026-07-13, free-screener path)
- [x] Universe screening: growth + valuation-adjusted-for-growth (PEG-style, never
      naive low-P/E), margin/balance-sheet health, value-trap exclusion,
      liquidity/market-cap floors (server-side at the screener)
- [x] Stricter quality gates than the monitor: post-enrichment eligibility —
      core fields must verify cleanly, verified PEG must honor the screen window
- [x] Data source: free TradingView scanner behind a `Screener` protocol (see
      the decided section below); paid EODHD remains the upgrade path
- [ ] Sector-underweight context (needs portfolio weights — post-v1.1)

## Data sources (validated in Phase-0 spike, 2026-07-12)

| Need | Source | Notes |
|---|---|---|
| Price, valuation, earnings dates, analyst data | Yahoo via `yfinance` | Free; covered ALL spike tickers incl. OTC ADRs (NTDOY/TCEHY/NSRGY), BRK-B, ETFs. `upgrades_downgrades` gives dated rating-change history. Unofficial API — expect breakage, design the fetch layer behind an adapter interface. |
| Fundamentals cross-check (US filers) | SEC EDGAR `companyfacts` | Free, official. Covers 20-F foreign filers (ASML) too. Symbology uses dashes (`BRK-B`). Needs a `User-Agent` header with contact email. NOT available for OTC ADRs or ETFs. |
| Price cross-check | Finnhub free tier | 60 req/min with free API key; ample at watchlist scale. Its `/calendar/earnings` also feeds the bellwether context section (one call, claims-labeled). (Stooq is dead/blocked — do not use.) |
| ETF holdings (rebalancing) | SSGA / SPDR daily-holdings xlsx (verified 2026-07-16) | Uniform per-fund endpoint, gives tickers directly (no CUSIP join). Unofficial issuer feed behind `etf.HoldingsSource`; iShares blocks headless, so SPDR is the starter issuer. |
| Macro economic series (CPI, jobs, rates) | FRED via keyless `fredgraph.csv` (verified 2026-07-15) | Unofficial-but-free chart endpoint, accepted eyes-open behind `sources/fred.py`; the keyed official API (free registration) is the upgrade path. Yahoo carries the market-quote macro series (^TNX/^IRX/^VIX/^GSPC…) through the existing adapter. |
| ETF full holdings (look-through) | SEC N-PORT filings | Free, monthly, all holdings with CUSIPs (VOO = 519 entries verified). Pain is trust→series→ticker mapping + CUSIP→ticker join (OpenFIGI free API). |
| Bulk fundamentals (discovery) | **TradingView scanner (free, unofficial) — decided 2026-07-13** | Screening a universe from Yahoo is rate-limit-abusive and fragile — don't. Scout ships on the TV scanner behind a `Screener` protocol; paid options below remain the upgrade path (July 2026 prices): |

### Discovery data decision — DECIDED 2026-07-13: free screener path

Scout v1.1 ships on the **TradingView scanner endpoint** (free, unofficial —
accepted in the same eyes-open way as yfinance, behind a `Screener` protocol;
verified working from the deployment box with all needed columns incl. PEG).
Screener values only *select* candidates; everything reported is re-fetched
and gated by the v1 stack. The paid options below remain the upgrade path
(EODHD most likely) if/when the free endpoint breaks or discovery needs
global/OTC coverage. Original research preserved for that day:

Three viable paths, all reusing the v1 engine to enrich + quality-gate the shortlist:

- **A. Finviz Elite hybrid — $25/mo** ($299.50/yr): server-side screener with PEG /
  margin / liquidity filters + CSV export URL a cron job can pull. Argus consumes the
  ~top-50 export, then enriches and gates each candidate per-ticker with the free v1
  stack. Cheapest, least build. Misses: preset filters only, US-listed only, no OTC.
- **B. EODHD Fundamentals Feed — $60/mo** ($50/mo annual): one JSON per ticker with
  ratios + margins + analyst ratings/targets + **ETF holdings**; 10k tickers/day quota.
  Global incl. OTC/ADRs. Choosing B also makes ETF look-through nearly free (skip
  N-PORT parsing entirely). Most likely end state given OTC ADR holdings.
- **C. FMP Starter — $22/mo** (annual billing): server-side screener endpoint +
  analyst data, 300 calls/min. US-only at this tier; true bulk endpoints gated to
  Ultimate ($149/mo).

Rejected: Tiingo (no analyst/ratios convenience), Alpha Vantage ($50, weaker value),
Polygon/Massive (no analyst data, fundamentals at $79 tier), Finnhub paid (modules
stack to $125+/mo; its **free** tier stays in use for price cross-checks). SEC EDGAR
`companyfacts.zip` (nightly ~1GB bulk, free) is the ground-truth verification layer
for whatever paid source is chosen — not a sole source (no ratios, no non-filers,
filing lag).

## Conventions

- **Architecture: see [ARCHITECTURE.md](ARCHITECTURE.md)** — module map, schema,
  gate pipeline, and the decision log. Changes to the shape of the system argue
  with that document first.

- **Package manager: `uv`** — `uv add <pkg>` to add dependencies, `uv run argus ...`
  to run, `uv run pytest` for tests. Never pip, never poetry, no manually-managed
  venvs.
- Python ≥ 3.12, `src/` layout.
- SQLite for state (single-user, single-machine — no server DB).
- Every external fetch goes through an adapter interface so a broken/replaced
  source is a one-module fix (free feeds break; this is priced in).
- Scheduled execution (cron/launchd) — Argus is a batch reporter, not a daemon.
