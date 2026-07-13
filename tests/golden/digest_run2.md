# Argus watch digest — run 2 — 2026-07-13

Status: PARTIAL — some tickers or sources failed this run; degradation is detailed under Data health.

## Changes

### NTDOY

_Switch 2 cycle + IP monetization._

- ⚠ Analyst target (mean) went dark — DATA QUARANTINED: target 35.00 (yahoo) / price 10.97 (yahoo) = 3.19 outside [0.3, 3.0]

### NVDA

_Datacenter capex supercycle; CUDA moat._

- Price 170.00 → 181.25 (+6.6%, threshold 5.0%) vs 2026-07-06
- Consensus rating buy → hold (down)
- Analyst action (2026-07-12): Morgan Stanley down — Overweight → Equal-Weight
- Earnings imminent: 2026-07-17 (in 4 days)

Fetch failures (no data this run): DEADCO (yahoo: HTTP 502 from upstream; finnhub: HTTP 502 from upstream).

## Watchlist

### DEADCO

- Price: — no data (yahoo: HTTP 502 from upstream; finnhub: HTTP 502 from upstream)
- Market cap: — no data (yahoo: HTTP 502 from upstream)
- P/E (TTM): — no data (yahoo: HTTP 502 from upstream)
- Fwd P/E: — no data (yahoo: HTTP 502 from upstream)
- PEG: — no data (yahoo: HTTP 502 from upstream)
- Gross margin: — no data (yahoo: HTTP 502 from upstream)
- Operating margin: — no data (yahoo: HTTP 502 from upstream)
- Debt/equity: — no data (yahoo: HTTP 502 from upstream)
- Next earnings: — no data (yahoo: HTTP 502 from upstream)
- Analyst rating: — no data (yahoo: HTTP 502 from upstream)
- Analyst target (mean): — no data (yahoo: HTTP 502 from upstream)
- Analyst count: — no data (yahoo: HTTP 502 from upstream)

### NTDOY

- Price: 10.97 (yahoo, 2026-07-13 14:00Z) ✓finnhub
- Market cap: — no data (not provided)
- P/E (TTM): — no data (not provided)
- Fwd P/E: — no data (not provided)
- PEG: — no data (not provided)
- Gross margin: — no data (not provided)
- Operating margin: — no data (not provided)
- Debt/equity: — no data (not provided)
- Next earnings: — no data (not provided)
- Analyst rating: buy (yahoo, 2026-07-13 14:00Z)
- Analyst target (mean): ⚠ DATA QUARANTINED — target 35.00 (yahoo) / price 10.97 (yahoo) = 3.19 outside [0.3, 3.0]
- Analyst count: — no data (not provided)

### NVDA

- Price: 181.25 (yahoo, 2026-07-13 14:00Z)
- Market cap: — no data (not provided)
- P/E (TTM): — no data (not provided)
- Fwd P/E: 31.20 (yahoo, 2026-07-13 14:00Z)
- PEG: — no data (not provided)
- Gross margin: — no data (not provided)
- Operating margin: — no data (not provided)
- Debt/equity: — no data (not provided)
- Next earnings: 2026-07-17 (yahoo, 2026-07-13 14:00Z)
- Analyst rating: hold (yahoo, 2026-07-13 14:00Z)
- Analyst target (mean): 205.00 (yahoo, 2026-07-13 14:00Z)
- Analyst count: — no data (not provided)

## Data quarantined

| Ticker | Field | Source | Reasons | Fetched at |
| --- | --- | --- | --- | --- |
| NTDOY | Analyst target (mean) | yahoo | target_price_ratio: target 35.00 (yahoo) / price 10.97 (yahoo) = 3.19 outside [0.3, 3.0] | 2026-07-13 14:00Z |

## Data health

- yahoo: 2 ok, 1 error (first: HTTP 502 from upstream)
- edgar: not configured — its cross-checks never ran
- finnhub: 1 ok, 2 errors (first: HTTP 502 from upstream) — price cross-checks skipped (2 tickers)

Failed tickers:
- DEADCO: yahoo: HTTP 502 from upstream; finnhub: HTTP 502 from upstream

---

Run 2 — regenerate with `argus report --run 2`.
