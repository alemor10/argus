"""Canonical sector taxonomy: both source vocabularies must map completely —
an unmapped TV sector silently landing in Other would quietly weaken the
concentration cap for that whole group."""

from argus.scout.sectors import (
    CANONICAL_SECTORS,
    OTHER,
    _TRADINGVIEW_MAP,
    canonical_sector,
)

# The full TradingView sector vocabulary, observed live 2026-07-14 across a
# ~1,300-row production scan. If TV adds a sector, this test names it.
TV_VOCABULARY = [
    "Commercial Services",
    "Communications",
    "Consumer Durables",
    "Consumer Non-Durables",
    "Consumer Services",
    "Distribution Services",
    "Electronic Technology",
    "Energy Minerals",
    "Finance",
    "Health Services",
    "Health Technology",
    "Industrial Services",
    "Miscellaneous",
    "Non-Energy Minerals",
    "Process Industries",
    "Producer Manufacturing",
    "Retail Trade",
    "Technology Services",
    "Transportation",
    "Utilities",
]


def test_every_observed_tv_sector_is_mapped():
    unmapped = [s for s in TV_VOCABULARY if s not in _TRADINGVIEW_MAP]
    assert not unmapped, f"TV sectors falling through to Other: {unmapped}"


def test_mapping_targets_are_canonical():
    targets = set(_TRADINGVIEW_MAP.values())
    assert targets <= set(CANONICAL_SECTORS) | {OTHER}


def test_yahoo_sectors_are_identity():
    for sector in CANONICAL_SECTORS:
        assert canonical_sector(sector) == sector


def test_unknown_and_absent_land_in_other_visibly():
    assert canonical_sector("Quantum Widgets") == OTHER
    assert canonical_sector(None) == OTHER
    assert canonical_sector("  ") == OTHER


def test_transport_lives_under_industrials():
    """The user's 'transport' category — sector-level it is Industrials; the
    finer industry ('Marine Shipping') stays visible per name."""
    assert canonical_sector("Transportation") == "Industrials"
