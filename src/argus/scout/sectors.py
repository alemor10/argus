"""Canonical sector taxonomy — 11 buckets + Other.

Two vocabularies feed Argus: TradingView's ~20 quirky groups at screen time
and Yahoo's 11 sectors on company profiles at render time. Everything maps
onto ONE canonical set here so the sector cap, the grouped digest, and the
sector-leaders strip all agree. Mapping completeness is test-enforced
against the live-recorded TV vocabulary; unknown inputs land in OTHER —
visible, never dropped.
"""

CANONICAL_SECTORS: tuple[str, ...] = (
    "Technology",
    "Healthcare",
    "Financial Services",
    "Consumer Cyclical",
    "Consumer Defensive",
    "Industrials",
    "Energy",
    "Basic Materials",
    "Communication Services",
    "Real Estate",
    "Utilities",
)

OTHER = "Other"

# TradingView sector → canonical. Full vocabulary observed live 2026-07-14
# (~1,300-row scan). Note: TV files REITs under Finance and oilfield
# services under Industrial Services — close enough at the 11-bucket level.
_TRADINGVIEW_MAP: dict[str, str] = {
    "Technology Services": "Technology",
    "Electronic Technology": "Technology",
    "Finance": "Financial Services",
    "Health Technology": "Healthcare",
    "Health Services": "Healthcare",
    "Consumer Durables": "Consumer Cyclical",
    "Consumer Services": "Consumer Cyclical",
    "Retail Trade": "Consumer Cyclical",
    "Consumer Non-Durables": "Consumer Defensive",
    "Distribution Services": "Industrials",
    "Commercial Services": "Industrials",
    "Industrial Services": "Industrials",
    "Producer Manufacturing": "Industrials",
    "Transportation": "Industrials",
    "Process Industries": "Basic Materials",
    "Non-Energy Minerals": "Basic Materials",
    "Energy Minerals": "Energy",
    "Utilities": "Utilities",
    "Communications": "Communication Services",
    "Miscellaneous": OTHER,
}

# Yahoo's sector names ARE the canonical set — identity, with a guard.
_YAHOO_MAP: dict[str, str] = {sector: sector for sector in CANONICAL_SECTORS}


def canonical_sector(raw: str | None) -> str:
    """Map either vocabulary onto the canonical set; unknown/absent → Other."""
    if not raw:
        return OTHER
    cleaned = raw.strip()
    return _TRADINGVIEW_MAP.get(cleaned) or _YAHOO_MAP.get(cleaned) or OTHER
