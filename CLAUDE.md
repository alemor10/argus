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
- [ ] ETF look-through concentration: resolve constituents → true aggregate
      single-name / theme exposure. Hardest data-engineering piece (joins, not cost).

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
