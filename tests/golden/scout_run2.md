# Argus scout digest — run 2 — 2026-07-13

Status: PARTIAL — some tickers or sources failed this run; degradation is detailed under Data health.

## Proposals

| # | Ticker | Streak | Price | PEG | Fwd P/E | Gross margin | Op margin | D/E |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CLEANCO | 2w | 100.00 | 0.92 | — | 55.0% | — | — |

Screen (screener claims, verified independently above):
- **CLEANCO** — peg 0.9 <= 1.5; gross_margin 55 >= 30; operating_margin 20 >= 8; eps_growth 25 >= 10; revenue_growth 15 >= 5; debt_to_equity 0.4 <= 1.5; eps_growth 25 > 0 and revenue_growth 15 > 0

## Excluded after enrichment

- THINCO (screen rank 2): core fields not verifiable — missing: P/E or PEG, margins
- DEADCO (screen rank 3): fetch failed: yahoo: HTTP 502 from upstream

_Exclusion is a data-quality verdict, not an investment one — these names passed the screen but their fundamentals could not be verified cleanly this run._

No data quarantined this run.

## Data health

- yahoo: 2 ok, 1 error (first: HTTP 502 from upstream)
- edgar: not configured — its cross-checks never ran
- finnhub: not configured — its cross-checks never ran

Failed tickers:
- DEADCO: yahoo: HTTP 502 from upstream

---

Argus proposes; the human decides. To start watching a name: `argus promote TICKER --thesis "why you believe it"`.

Run 2 — regenerate with `argus report --run 2`.
