"""Domain types. All frozen — observations are immutable facts.

The central type-level rule: adapters produce RawObservation, which has no
verdict field at all. Only gates.py constructs GatedObservation, and the store
writer accepts nothing else — ungated data is unrepresentable downstream.
"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field as PydanticField,
    TypeAdapter,
    model_validator,
)

from argus.fields import SPECS, Field, QuarantineCode, Source

_KIND_TO_COLUMN = {"num": "value_num", "text": "value_text", "date": "value_date"}
_VALUE_COLUMNS = ("value_num", "value_text", "value_date")


def require_aware(dt: datetime) -> datetime:
    """Guard for the clock seams (engine.run as_of, writer timestamps,
    gates staleness): reject naive datetimes at the boundary instead of
    TypeError-ing mid-pipeline when compared against observed_at."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime at a clock seam — pass datetime.now(UTC)")
    return dt


class RawObservation(BaseModel):
    """One value, from one source, at one moment. The atom of everything."""

    model_config = ConfigDict(frozen=True)

    ticker: str = PydanticField(min_length=1)
    field: Field
    value_num: float | None = None
    value_text: str | None = None
    value_date: date | None = None
    source: Source
    fetched_at: AwareDatetime  # stamped by the ADAPTER at fetch time, not the engine
    observed_at: AwareDatetime | None = None  # source-reported data timestamp, when available

    @model_validator(mode="after")
    def _exactly_one_value_of_declared_kind(self) -> "RawObservation":
        set_columns = [c for c in _VALUE_COLUMNS if getattr(self, c) is not None]
        if len(set_columns) != 1:
            raise ValueError(f"exactly one value column must be set, got {set_columns or 'none'}")
        expected = _KIND_TO_COLUMN[SPECS[self.field].kind]
        if set_columns[0] != expected:
            raise ValueError(
                f"{self.field} is kind {SPECS[self.field].kind!r}; expected {expected}, got {set_columns[0]}"
            )
        return self

    @property
    def value(self) -> float | str | date:
        for column in _VALUE_COLUMNS:
            v = getattr(self, column)
            if v is not None:
                return v
        raise AssertionError("unreachable: validator guarantees one value")


class ParseFailure(BaseModel):
    """The source sent something for this field and we could not parse it.

    Becomes an UNPARSEABLE quarantine row (raw wire text preserved) — never a
    silent absence.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    field: Field
    raw: str
    source: Source
    fetched_at: AwareDatetime


class QuarantineHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: QuarantineCode
    detail: str  # human-readable, rendered in the digest verbatim


class GatedObservation(BaseModel):
    """An observation after the quality gate. Only gates.py constructs this.

    The payload is either a RawObservation or a ParseFailure — the latter is
    always quarantined with an UNPARSEABLE reason and persists with its raw
    wire text in value_text (whatever the field's declared kind), so
    sent-but-unreadable data reaches the store instead of vanishing.
    """

    model_config = ConfigDict(frozen=True)

    obs: RawObservation | ParseFailure
    verdict: Literal["accepted", "quarantined"]
    reasons: tuple[QuarantineHit, ...] = ()
    corroborated_by: tuple[Source, ...] = ()  # other sources that agreed (accepted only)
    is_primary: bool = False  # the resolved value for (ticker, field) this run

    @model_validator(mode="after")
    def _consistent(self) -> "GatedObservation":
        if (self.verdict == "quarantined") != bool(self.reasons):
            raise ValueError("reasons must be non-empty iff quarantined")
        if self.is_primary and self.verdict != "accepted":
            raise ValueError("a quarantined observation cannot be primary")
        if isinstance(self.obs, ParseFailure) and not any(
            hit.code == QuarantineCode.UNPARSEABLE for hit in self.reasons
        ):
            raise ValueError("a ParseFailure payload must be quarantined with an UNPARSEABLE reason")
        return self


class FieldValue(BaseModel):
    """A resolved, accepted value with provenance intact.

    Hydration-safe: SQLite hands back TEXT for dates, and a bare union would
    silently keep '2026-08-20' as str — so values are coerced to the field's
    declared kind here, and a kind mismatch is a ValidationError, not a latent
    string in the diff engine.
    """

    model_config = ConfigDict(frozen=True)

    field: Field
    value: float | str | date
    source: Source
    fetched_at: AwareDatetime
    observed_at: AwareDatetime | None = None  # source-reported data timestamp, when available
    corroborated_by: tuple[Source, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _coerce_value_to_kind(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        field, value = data.get("field"), data.get("value")
        if field is None or value is None:
            return data
        data = dict(data)
        kind = SPECS[Field(field)].kind
        if kind == "num":
            try:
                data["value"] = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} is num-kind; cannot coerce {value!r}") from exc
        elif kind == "date" and isinstance(value, str):
            data["value"] = date.fromisoformat(value)
        elif kind == "date" and not isinstance(value, date):
            raise ValueError(f"{field} is date-kind; got {type(value).__name__}")
        elif kind == "text" and not isinstance(value, str):
            raise ValueError(f"{field} is text-kind; got {type(value).__name__}")
        return data


class Snapshot(BaseModel):
    """What Argus believes about one ticker after one run. Hydrated from SQL.

    Tri-state by construction:
      - field in `values`               → usable signal (primary accepted)
      - field in `quarantined`          → data existed, gates rejected all of it
      - field absent from both          → no source offered it
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    run_id: int
    as_of: AwareDatetime
    values: dict[Field, FieldValue] = {}
    quarantined: dict[Field, tuple[QuarantineHit, ...]] = {}


class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")  # typo'd override keys must error

    price_move_pct: float = 5.0
    target_move_pct: float = 10.0
    earnings_within_days: int = 7


ThesisOp = Literal[">=", "<=", ">", "<", "==", "!=", "in", "not_in"]


class ThesisCheck(BaseModel):
    """A falsifiable condition the human attached to a thesis — the line that,
    if crossed, means "reconsider". Argus never interprets the thesis prose;
    it only reports whether these human-declared, checkable conditions still
    hold. Constructed by thesis.parse_thesis_check (config is the fail-loud
    boundary), never from free text at run time."""

    model_config = ConfigDict(frozen=True)

    field: Field
    op: ThesisOp
    value: float | str | tuple[str, ...]
    raw: str  # the original "revenue_growth >= 20%" — rendered verbatim


class ThesisCheckResult(BaseModel):
    """One check evaluated against a snapshot. `undeterminable` means the
    field had no accepted value this run (missing or quarantined) — the
    thesis could not be verified, which is itself worth showing."""

    model_config = ConfigDict(frozen=True)

    check: ThesisCheck
    status: Literal["holds", "breached", "undeterminable"]
    observed: float | str | None = None


class MacroSpec(BaseModel):
    """How one macro series is watched and rendered. Persisted WHOLE in
    run_tickers.macro (JSON) so `argus report --run N` reproduces the Macro
    section bit-for-bit even after macro.yaml changes — the presence of a
    MacroSpec on a TickerContext IS the macro role.

    `alert_when` lines are alert-WHEN-TRUE ("value >= 25" pages when VIX is
    at or above 25) — the OPPOSITE sense of thesis checks, which breach when
    a condition STOPS holding; changes.py inverts locally and deliberately.
    `sanity` is a render-time plausibility band (a human-drawn "check units"
    line — e.g. a silent ×10 regime change in a yield quote), never a gate.
    """

    model_config = ConfigDict(frozen=True)

    label: str = PydanticField(min_length=1)
    unit: str = ""  # rendered suffix: "%", "k", ""
    decimals: int = PydanticField(default=2, ge=0, le=6)
    source: Source = Source.YAHOO  # the ONE source fetched for this series
    transform: Literal["level", "yoy_pct", "mom_change"] = "level"  # FRED-side derivation
    alert_move: float | None = PydanticField(default=None, gt=0, allow_inf_nan=False)
    alert_on_release: bool = False  # econ prints: a new period IS the alert
    sanity: tuple[float, float] | None = None
    alert_when: tuple[ThesisCheck, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> "MacroSpec":
        if self.source not in (Source.YAHOO, Source.FRED):
            raise ValueError(f"macro series can only come from yahoo or fred, not {self.source}")
        if self.sanity is not None and not self.sanity[0] < self.sanity[1]:
            raise ValueError(f"sanity bounds must be [low, high], got {list(self.sanity)}")
        if self.transform != "level" and self.source is not Source.FRED:
            raise ValueError("transforms only apply to fred series")
        return self

    @property
    def value_field(self) -> Field:
        """Where this series' number lives: market quotes are PRICE, econ
        prints are ECON_VALUE."""
        return Field.ECON_VALUE if self.source is Source.FRED else Field.PRICE


class TickerContext(BaseModel):
    """What the engine operates on — NOT "a watchlist entry".

    `watch` builds these from watchlist.yaml; `scout` will build them from a
    screener feed and reuse the identical enrich → gate → persist pipeline.
    A context carrying a MacroSpec is a macro series: fetched from its one
    source, diffed by the macro rules, rendered in the Macro section.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str = PydanticField(min_length=1)
    thesis: str | None = None
    thresholds: Thresholds = Thresholds()
    thesis_checks: tuple[ThesisCheck, ...] = ()
    macro: MacroSpec | None = None
    # "consider" = the Radar's middle rung: human-added (argus consider),
    # tracked through the full fetch→gate pipeline daily, but not yet a
    # committed position thesis — promote graduates it. Argus never adds
    # names to either tier on its own.
    tier: Literal["watch", "consider"] = "watch"


class AnalystActionRecord(BaseModel):
    """Event-shaped source data: one dated per-firm rating action.

    Stored in its own table (not as a level observation) with
    INSERT OR IGNORE on the natural key + first_seen_run_id.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    action_date: date
    firm: str
    action: str  # up | down | init | reiterate | main (as reported by the source)
    from_grade: str | None = None
    to_grade: str
    source: Source
    fetched_at: AwareDatetime


class EarningsResultRecord(BaseModel):
    """Event-shaped source data: one REPORTED quarter — the realized EPS
    beside the street's estimate at report time. A scheduled future quarter
    has no actual and is not a result; only actual-bearing rows become
    records. Stored in its own table (analyst_actions precedent) with
    INSERT OR IGNORE on the natural key + first_seen_run_id, so "reported
    since last run" is a set-membership fact, crash-correct by construction.
    Non-finite EPS values are rejected at construction — NaN binds as NULL
    in SQLite and would violate the table's NOT NULL, rolling back the
    whole ticker."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    quarter_end: date  # fiscal quarter end — the natural key with ticker
    eps_actual: float = PydanticField(allow_inf_nan=False)
    eps_estimate: float | None = PydanticField(default=None, allow_inf_nan=False)
    source: Source
    fetched_at: AwareDatetime


class CompanyProfile(BaseModel):
    """Descriptive identity — what the business IS. Provenance-stamped like
    everything else, but not gate-material: there are no plausibility bounds
    on prose. Reports render it; the diff engine ignores it."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    employees: int | None = None
    summary: str | None = None
    source: Source
    fetched_at: AwareDatetime


class RelationalViolation(BaseModel):
    """A relational gate tripped. The pipeline — not the check — assigns blame
    among the implicated fields using corroboration evidence."""

    model_config = ConfigDict(frozen=True)

    hit: QuarantineHit
    implicated: tuple[Field, ...]


# --- Change events: a discriminated union so the renderer pattern-matches
# --- exhaustively and the change_events table round-trips them losslessly.


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str


class PriceMove(_Event):
    kind: Literal["price_move"] = "price_move"
    old: float
    new: float
    pct: float
    threshold: float
    old_as_of: AwareDatetime  # baseline provenance — honest across gaps


class TargetMove(_Event):
    kind: Literal["target_move"] = "target_move"
    old: float
    new: float
    pct: float
    threshold: float
    old_as_of: AwareDatetime


class ConsensusShift(_Event):
    kind: Literal["consensus_shift"] = "consensus_shift"
    old: str
    new: str
    # "unclear" when either grade is off the known scale — the shift is still
    # reported (suppressing it would be a silent drop), just not ranked.
    direction: Literal["up", "down", "unclear"]


class AnalystAction(_Event):
    kind: Literal["analyst_action"] = "analyst_action"
    firm: str
    action: str
    from_grade: str | None = None
    to_grade: str
    action_date: date


class EarningsReported(_Event):
    """A quarter's results landed since the last run: realized EPS beside the
    street's estimate at report time. Realized data reported against a line
    third parties drew — never Argus's own forecast. `surprise_pct` is
    computed from the two stored facts at detect time ((actual − estimate) /
    |estimate| · 100); None when there is no estimate to be surprised
    against, or the estimate is zero (the division is undefined)."""

    kind: Literal["earnings_reported"] = "earnings_reported"
    quarter_end: date
    eps_actual: float
    eps_estimate: float | None = None
    surprise_pct: float | None = None


class EarningsImminent(_Event):
    """State event: re-fires every run inside the window (suppression's
    failure mode is silence). `newly` is False only when the SAME date was
    already inside the window at the baseline run — so event-gated daily
    delivery reminds once, not seven mornings straight, while the weekly
    full digest still shows the standing reminder. Default True keeps old
    persisted payloads round-tripping."""

    kind: Literal["earnings_imminent"] = "earnings_imminent"
    earnings_date: date
    days_until: int
    newly: bool = True


class FieldQuarantined(_Event):
    kind: Literal["field_quarantined"] = "field_quarantined"
    field: Field
    reasons: tuple[QuarantineHit, ...]


class FieldRecovered(_Event):
    kind: Literal["field_recovered"] = "field_recovered"
    field: Field


class ThesisDrift(_Event):
    """A human-declared thesis condition is BREACHED — the data crossed the
    line the human said would make them reconsider. The highest-signal event
    a monitor can emit; never a prediction, only current data vs a stated
    line. `newly` distinguishes a fresh breach from one continuing since the
    last run."""

    kind: Literal["thesis_drift"] = "thesis_drift"
    check: str  # the raw condition, e.g. "revenue_growth >= 20%"
    field: Field
    observed: float | str
    thesis: str | None = None
    newly: bool = True


class MacroLineCrossed(_Event):
    """A human-drawn alert-when-true line on a macro series is currently TRUE
    ("value >= 25" with VIX at 25.4). Fires every run while crossed —
    suppression's failure mode is silence; `newly` is False when it was
    already crossed at baseline, so event-gated delivery reminds once.
    Carries label/unit/decimals so the payload self-renders (the
    PriceMove-freezes-threshold precedent)."""

    kind: Literal["macro_line_crossed"] = "macro_line_crossed"
    label: str
    check: str  # the raw line, rendered verbatim
    observed: float
    unit: str = ""
    decimals: int = 2
    newly: bool = True


class MacroPrint(_Event):
    """A NEW period landed for an economic series — release day is the news
    (CPI day, jobs Friday). prev_value/delta compare against the prior
    accepted print when one exists."""

    kind: Literal["macro_print"] = "macro_print"
    label: str
    period: date  # the new print's period (the series' observed_at date)
    value: float
    prev_value: float | None = None
    delta: float | None = None
    unit: str = ""
    decimals: int = 2


class MacroShift(_Event):
    """A macro series moved at least `alert_move` in its OWN units since the
    last accepted baseline — absolute, never percent (a 15bp yield move is
    the unit of interest, not "+3.3%")."""

    kind: Literal["macro_shift"] = "macro_shift"
    label: str
    old: float
    new: float
    delta: float
    unit: str = ""
    decimals: int = 2
    threshold: float
    old_as_of: AwareDatetime


ChangeEvent = Annotated[
    ThesisDrift
    | MacroLineCrossed
    | MacroPrint
    | MacroShift
    | PriceMove
    | TargetMove
    | ConsensusShift
    | AnalystAction
    | EarningsReported
    | EarningsImminent
    | FieldQuarantined
    | FieldRecovered,
    PydanticField(discriminator="kind"),
]

CHANGE_EVENT_ADAPTER: TypeAdapter[ChangeEvent] = TypeAdapter(ChangeEvent)


# --- Digest inputs, assembled entirely from SQL by store.queries.run_report.


class SourceHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: Source
    status: Literal["ok", "error", "not_applicable"]
    error: str | None = None
    latency_ms: int | None = None


class QuarantinedObservation(BaseModel):
    """One quarantined observation for the digest's quarantine table —
    including those coexisting with an accepted primary from another source
    (Snapshot.quarantined only carries fields that went fully dark)."""

    model_config = ConfigDict(frozen=True)

    field: Field
    source: Source
    fetched_at: AwareDatetime
    reasons: tuple[QuarantineHit, ...]


class TickerReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: TickerContext
    status: Literal["ok", "partial", "failed"]
    snapshot: Snapshot | None = None
    baseline: Snapshot | None = None  # the diffed-against snapshot, for watchlist drift
    profile: CompanyProfile | None = None  # latest known business identity
    events: tuple[ChangeEvent, ...] = ()
    quarantines: tuple[QuarantinedObservation, ...] = ()  # EVERY quarantined obs this run
    sources: tuple[SourceHealth, ...] = ()
    baseline_run_id: int | None = None
    baseline_as_of: AwareDatetime | None = None
    error: str | None = None


class ScoutCandidateRecord(BaseModel):
    """Write-side scout row: one screened candidate's fate this run.
    Screener numbers ride along as labeled claims only. A 'leader' is the
    best passer of a sector with no shortlist representation — shown for
    category coverage, never enriched, never proposed."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    rank: int  # global rank among all screen passers
    status: Literal["proposed", "excluded", "leader"]
    sector: str = "Other"  # canonical (scout.sectors)
    exclusion_reason: str | None = None
    screen_reasons: dict[str, str]
    screener_metrics: dict[str, float | str | None]
    peer_context: dict | None = None  # {industry, n, median_fwd_pe, peers:[...]} — claims

    @model_validator(mode="after")
    def _reason_iff_excluded(self) -> "ScoutCandidateRecord":
        if (self.status == "excluded") != (self.exclusion_reason is not None):
            raise ValueError("exclusion_reason must be set iff excluded")
        return self


class ScoutProposal(ScoutCandidateRecord):
    """Report-side scout row: the record plus its derived streak —
    consecutive scout runs (up to and including this one) it was proposed."""

    streak: int = 0


class BellwetherEarning(BaseModel):
    """One megacap earnings-calendar row — CLAIMS-labeled context from a
    single unofficial source (Finnhub), never gated, never an observation.
    Persisted per run only so `report --run N` reproduces the section."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    report_date: date
    hour: str = ""  # bmo | amc | "" as reported
    eps_estimate: float | None = None
    eps_actual: float | None = None
    revenue_estimate: float | None = None
    revenue_actual: float | None = None


# --- The market wire: the magazine issue's market-wide pages (claims-labeled,
# --- single unofficial sources, curated by mechanical rules in market.py,
# --- persisted whole per run so the issue reproduces bit-for-bit).


class Mover(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    company: str | None = None
    sector: str = "Other"
    close: float
    change_pct: float


class SectorPulse(BaseModel):
    model_config = ConfigDict(frozen=True)

    sector: str
    median_change_pct: float
    n: int


class Extreme(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    company: str | None = None
    close: float
    kind: str  # "high" | "low"


class EarningsWireEntry(BellwetherEarning):
    """A calendar row with the scan's market cap riding along (claims)."""

    market_cap: float | None = None


class FeatureCard(BaseModel):
    """One featured stock — the Daily's reading material. Selection is
    mechanical (see market.select_features); the business summary is the
    source's prose rendered verbatim (the company_profiles precedent — not
    gate-material), and every number on the card is a labeled claim."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    why: str  # the disclosed mechanical reason it was featured
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    employees: int | None = None
    summary: str | None = None
    close: float | None = None
    change_pct: float | None = None
    market_cap: float | None = None
    fwd_pe: float | None = None
    pe_ttm: float | None = None
    revenue: float | None = None  # TTM, dollars
    revenue_growth_pct: float | None = None
    gross_margin_pct: float | None = None
    operating_margin_pct: float | None = None
    roe_pct: float | None = None
    dividend_yield_pct: float | None = None
    beta: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    analyst_rating: str | None = None
    analyst_target: float | None = None  # shown only inside the sanity rail vs price
    analyst_count: int | None = None


class MarketWire(BaseModel):
    """One issue's market pages — movers, sector pulse, earnings wire,
    52-week extremes, featured cards. Absent (None on RunReport) for quiet
    pulses and old runs; the digest renders the sections only when present."""

    model_config = ConfigDict(frozen=True)

    universe: int  # scanned rows behind the curation
    gainers: tuple[Mover, ...] = ()
    losers: tuple[Mover, ...] = ()
    sectors: tuple[SectorPulse, ...] = ()
    highs: tuple[Extreme, ...] = ()
    lows: tuple[Extreme, ...] = ()
    earnings_reported: tuple[EarningsWireEntry, ...] = ()
    earnings_upcoming: tuple[EarningsWireEntry, ...] = ()
    earnings_more_upcoming: int = 0  # large-cap reporters beyond the shown cap
    features: tuple[FeatureCard, ...] = ()  # the issue's reading material (claims)


class ScorecardMark(BaseModel):
    """One name's realized standing as of a scoring run — total return since
    scout first proposed it, vs SPY over the same window. Immutable once
    written; the market is the answer key."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    first_proposed_at: date
    weeks_out: int
    name_return: float  # fraction
    spy_return: float

    @property
    def alpha(self) -> float:
        return self.name_return - self.spy_return


class ScorecardCohort(BaseModel):
    """Names grouped by how long ago scout first proposed them."""

    model_config = ConfigDict(frozen=True)

    label: str
    n: int
    median_return: float
    median_spy: float
    median_alpha: float
    beat_spy: int  # count with alpha > 0


class Scorecard(BaseModel):
    """The scout self-scoring summary rendered in the digest — derived
    deterministically from the run's persisted marks, so it reproduces."""

    model_config = ConfigDict(frozen=True)

    as_of: date
    cohorts: tuple[ScorecardCohort, ...] = ()
    overall_n: int = 0
    overall_median_alpha: float = 0.0
    overall_beat_spy: int = 0
    unpriceable: int = 0  # proposed names whose history could not be fetched


class RecapEvent(BaseModel):
    """One event of the week, stamped with the day it fired."""

    model_config = ConfigDict(frozen=True)

    day: date
    ticker: str
    event: ChangeEvent


class RecapMacroLine(BaseModel):
    """One macro series, week over week — current level vs the last accepted
    value before the window opened."""

    model_config = ConfigDict(frozen=True)

    label: str
    unit: str = ""
    decimals: int = 2
    current: float
    week_ago: float | None = None
    delta: float | None = None


class RecapReport(BaseModel):
    """The Sunday Edition's input — aggregated entirely from the store
    (append-only pays off: the week is already persisted), except the
    week-ahead calendar, which is fetched at print time and labeled so."""

    model_config = ConfigDict(frozen=True)

    week_ending: date
    watch_runs: int = 0
    events: tuple[RecapEvent, ...] = ()
    standing_suppressed: int = 0  # re-fired standing states rolled up, not listed
    macro: tuple[RecapMacroLine, ...] = ()
    scout_run_id: int | None = None  # the week's scout run (delivery naming rides it)
    proposals: tuple[ScoutProposal, ...] = ()
    entered: tuple[str, ...] = ()  # shortlist churn vs the prior scout run
    dropped: tuple[str, ...] = ()
    scorecard: Scorecard | None = None
    week_ahead: tuple[BellwetherEarning, ...] = ()  # pinned names only (print-time claims)
    week_ahead_note: str | None = None


class RunReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: int
    kind: Literal["watch", "scout"]
    as_of: AwareDatetime
    status: Literal["complete", "partial", "failed"]
    notes: str | None = None  # e.g. "screener unavailable: …" — rendered in the header
    tickers: tuple[TickerReport, ...] = ()
    scout: tuple[ScoutProposal, ...] = ()  # populated for kind='scout' only
    scorecard: Scorecard | None = None  # scout self-scoring, when there is history
    bellwethers: tuple[BellwetherEarning, ...] = ()  # pre-v1.9 runs' market context (claims)
    market: MarketWire | None = None  # the magazine issue's market pages (claims)
    radar: tuple[ScoutProposal, ...] = ()  # latest scout shortlist (watch runs; claims+gated)
