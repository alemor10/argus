"""The canonical field registry.

Every field Argus watches is declared here, with its gate parameters. Adding a
field = one enum value + one SPECS entry (completeness is test-enforced).
This module imports nothing internal — everything else builds on it.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Literal


class Source(StrEnum):
    YAHOO = "yahoo"
    EDGAR = "edgar"
    FINNHUB = "finnhub"


class Field(StrEnum):
    PRICE = "price"
    MARKET_CAP = "market_cap"
    PE_TTM = "pe_ttm"
    PE_FWD = "pe_fwd"
    PEG = "peg"
    GROSS_MARGIN = "gross_margin"
    OPERATING_MARGIN = "operating_margin"
    ROE = "roe"
    DEBT_TO_EQUITY = "debt_to_equity"
    NEXT_EARNINGS_DATE = "next_earnings_date"
    ANALYST_RATING = "analyst_rating"
    ANALYST_TARGET_MEAN = "analyst_target_mean"
    ANALYST_COUNT = "analyst_count"


class QuarantineCode(StrEnum):
    NON_FINITE = "non_finite"
    OUT_OF_BOUNDS = "out_of_bounds"
    DATE_IN_PAST = "date_in_past"
    STALE = "stale"
    UNPARSEABLE = "unparseable"
    CROSS_SOURCE_DISAGREEMENT = "cross_source_disagreement"
    TARGET_PRICE_RATIO = "target_price_ratio"


FieldKind = Literal["num", "text", "date"]


@dataclass(frozen=True)
class FieldSpec:
    """Gate parameters for one field.

    Unary bounds are deliberately wide sanity rails, not judgment: they exist
    to catch the absurd (negative prices, non-finite ratios), never to encode
    opinions about what a reasonable P/E is. A false-positive machine trains
    the reader to skim the quarantine section, which must stay credible.
    Tighten empirically later — the observations table keeps the
    distributions forever.
    """

    kind: FieldKind
    bounds: tuple[float | None, float | None] | None = None
    cross_source_rel_tol: float | None = None  # None → no pairwise check
    max_age: timedelta | None = None  # staleness vs observed_at, when the source reports one
    not_in_past: bool = False  # date-kind only: DATE_IN_PAST when earlier than the run date
    priority: tuple[Source, ...] = (Source.YAHOO,)  # primary resolution order


# Cross-source tolerances: price 2% (same instant, same number); fundamentals
# 25% (TTM-vs-fiscal-window and taxonomy mismatches are legitimate — see the
# decision log in ARCHITECTURE.md).
_FUNDAMENTAL_TOL = 0.25

SPECS: dict[Field, FieldSpec] = {
    Field.PRICE: FieldSpec(
        "num",
        bounds=(0.0001, 10_000_000),  # BRK-A must pass
        cross_source_rel_tol=0.02,
        max_age=timedelta(days=4),  # catches cached quotes; generous for weekends
        priority=(Source.YAHOO, Source.FINNHUB),
    ),
    Field.MARKET_CAP: FieldSpec("num", bounds=(1e5, 1e14)),
    Field.PE_TTM: FieldSpec("num", bounds=(-10_000, 10_000)),  # negative earnings are real
    Field.PE_FWD: FieldSpec("num", bounds=(-10_000, 10_000)),
    Field.PEG: FieldSpec("num", bounds=(-1_000, 1_000)),
    Field.GROSS_MARGIN: FieldSpec(
        "num",
        bounds=(-10.0, 1.01),  # stored as a fraction; parse() normalizes
        cross_source_rel_tol=_FUNDAMENTAL_TOL,
        priority=(Source.YAHOO, Source.EDGAR),
    ),
    Field.OPERATING_MARGIN: FieldSpec(
        "num",
        bounds=(-10.0, 1.01),
        cross_source_rel_tol=_FUNDAMENTAL_TOL,
        priority=(Source.YAHOO, Source.EDGAR),
    ),
    Field.ROE: FieldSpec(
        "num",
        bounds=(-100.0, 100.0),  # a fraction; extreme leverage makes wild ROEs real
    ),
    Field.DEBT_TO_EQUITY: FieldSpec(
        "num",
        bounds=(-1_000, 1_000),  # negative equity happens
        cross_source_rel_tol=_FUNDAMENTAL_TOL,
        priority=(Source.YAHOO, Source.EDGAR),
    ),
    Field.NEXT_EARNINGS_DATE: FieldSpec("date", not_in_past=True),
    Field.ANALYST_RATING: FieldSpec("text"),
    Field.ANALYST_TARGET_MEAN: FieldSpec("num", bounds=(0.0001, 10_000_000)),
    Field.ANALYST_COUNT: FieldSpec("num", bounds=(1, 10_000)),
}
