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
- [ ] ETF look-through concentration: resolve constituents → true aggregate
      single-name / theme exposure. Hardest data-engineering piece (joins, not cost).

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
| Price cross-check | Finnhub free tier | 60 req/min with free API key; ample at watchlist scale. (Stooq is dead/blocked — do not use.) |
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
