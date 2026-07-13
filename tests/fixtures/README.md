# Recorded source payloads

Raw wire payloads captured from real sources, one directory per adapter
(`yahoo/`, `edgar/`, `finnhub/`). Adapter tests exercise `parse()` over these
only — never the network.

Pathological payloads are **perishable** — capture them the day they are
observed, because upstream corrects them. Already captured:

- `yahoo/NTDOY-2026-07-12.json` — the founding case, recorded live while the
  pathology existed: stale pre-ADR-ratio-change analyst target ($35.00)
  against the post-change price ($10.97). A naive pipeline reports "218%
  upside" from this exact payload; the relational gate must quarantine the
  target. Includes the `upgrades_downgrades` rows that feed analyst_actions.

Synthetic fixtures are allowed where a live capture adds nothing (an API-key
gate, or a reduced sample of a huge file) — they carry a `_note` key labeling
them synthetic and mirror the real wire shape exactly:

- `finnhub/NVDA-quote.json` — synthetic `/quote` response in Finnhub's
  documented shape (c/d/dp/h/l/o/pc/t); a live capture needs an API key.
- `edgar/company-tickers-synthetic.json` — reduced sample of the SEC
  ticker→CIK mapping (`company_tickers.json`); deliberately excludes OTC
  ADRs and ETFs so `covers()` False-cases are testable.
- `edgar/companyfacts-ACME-synthetic.json` — reduced companyfacts payload
  with clean ratio math (gross margin 0.40, operating margin 0.20,
  debt/equity 1.20 for FY2024) plus 10-Q rows that must NOT leak into the
  annual calculations.

Remaining fixtures (healthy-ticker Yahoo payloads, a real companyfacts
capture) can be added as they are recorded.
