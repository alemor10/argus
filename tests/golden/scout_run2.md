# Argus scout digest — run 2 — 2026-07-13

Status: PARTIAL — some tickers or sources failed this run; degradation is detailed under Data health.

## Proposals

_No new names cleared the screen this week — the shortlist held._

| # | Ticker | Sector | Streak | Price | Fwd P/E | Gross margin | Op margin | ROE | D/E |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CLEANCO | Technology | 2w | 100.00 | 14.80 | 55.0% | — | — | — |

_'#' is the global screen rank; gaps are names excluded after enrichment or sector-capped._

Screen (screener claims, verified independently above):
- **CLEANCO** — fwd P/E 15.0 ≤ 25; rev growth +15.0% ≥ 10%; gross margin 55.0% ≥ 40%; op margin 20.0% ≥ 12%; ROE 25.0% ≥ 15%; D/E 0.40 ≤ 1; EPS trend +25.0% > -30% · vs industry median fwd P/E 15 (Widgets, n=3)

## Scorecard — how past proposals have done vs SPY

| First proposed | Names | Median return | SPY | Median α | Beat SPY |
| --- | --- | --- | --- | --- | --- |
| ≤ 1 week | 1 | +10.0% | +4.0% | +6.0% | 1/1 |

**Overall:** 1 names ever proposed — median α +6.0%, 1/1 beat SPY.

_Total return incl. dividends (adjusted close), every proposal counted from its first appearance (no survivorship), never revised. The market is the answer key — Argus never grades itself._

## Excluded after enrichment

- DEADCO (screen rank 2): fetch failed: yahoo: HTTP 502 from upstream
- THINCO (screen rank 3): core fields not verifiable — missing: forward or trailing P/E, margins

_Exclusion is a data-quality verdict, not an investment one — these names passed the screen but their fundamentals could not be verified cleanly this run._

No data quarantined this run.

## Data health

- yahoo: 2 ok, 1 error (first: HTTP 502 from upstream)
- edgar: not consulted — no key configured or nothing required it
- finnhub: not consulted — no key configured or nothing required it

Failed tickers:
- DEADCO: yahoo: HTTP 502 from upstream

---

Argus proposes; the human decides. To start watching a name: `argus promote TICKER --thesis "why you believe it"`.

Run 2 — regenerate with `argus report --run 2`.
