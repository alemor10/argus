"""Quality gates. PURE — no network, no DB, no clock reads.

The only constructor of GatedObservation in the codebase. Pipeline order is
fixed; each stage sees the survivors of the previous one:

  1. parse boundary   ParseFailure → UNPARSEABLE quarantine (raw text preserved)
  2. unary            plausibility bounds from FieldSpec (NON_FINITE,
                      OUT_OF_BOUNDS, DATE_IN_PAST)
  3. staleness        as_of − observed_at > max_age → STALE; skipped when the
                      source reports no timestamp (gate on evidence, not guesses)
  4. cross-source     ≥2 accepted observations + a tolerance: within → all
                      accepted, stamped corroborated_by; beyond → quarantine
                      ALL disagreeing sides (with n=2 picking a winner is a
                      coin flip dressed as data)
  5. relational       GateProfile.relational_checks over the accepted values,
                      with corroboration-aware blame (see below)
  6. resolution       first source in FieldSpec.priority among accepted
                      becomes is_primary — stamped here, frozen at write time
"""

import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations

from argus.fields import SPECS, Field, FieldSpec, QuarantineCode, Source
from argus.models import (
    FieldValue,
    GatedObservation,
    ParseFailure,
    QuarantineHit,
    RawObservation,
    RelationalViolation,
    require_aware,
)

RelationalCheck = Callable[[Mapping[Field, FieldValue]], RelationalViolation | None]


@dataclass(frozen=True)
class GateProfile:
    """A named, swappable gate bundle. scout's stricter gates become a second
    profile value — any further abstraction gets extracted then, from two
    real examples, not zero."""

    specs: Mapping[Field, FieldSpec]
    relational_checks: tuple[RelationalCheck, ...]


_TARGET_PRICE_RATIO_BOUNDS = (0.3, 3.0)


def target_vs_price(values: Mapping[Field, FieldValue]) -> RelationalViolation | None:
    """The NTDOY gate: a $35 stale analyst target against a $10.97 price must
    quarantine, so '218% upside' is never computable. Implicates both legs;
    the pipeline assigns blame from corroboration evidence."""
    target = values.get(Field.ANALYST_TARGET_MEAN)
    price = values.get(Field.PRICE)
    if target is None or price is None:
        return None
    if not isinstance(target.value, (int, float)) or not isinstance(price.value, (int, float)):
        return None
    if price.value <= 0:  # unary gate territory; don't double-report
        return None
    low, high = _TARGET_PRICE_RATIO_BOUNDS
    ratio = target.value / price.value
    if low <= ratio <= high:
        return None
    return RelationalViolation(
        hit=QuarantineHit(
            code=QuarantineCode.TARGET_PRICE_RATIO,
            detail=(
                f"target {target.value:.2f} ({target.source}) / price {price.value:.2f} "
                f"({price.source}) = {ratio:.2f} outside [{low}, {high}]"
            ),
        ),
        implicated=(Field.ANALYST_TARGET_MEAN, Field.PRICE),
    )


DEFAULT_PROFILE = GateProfile(specs=SPECS, relational_checks=(target_vs_price,))


def run_gates(
    profile: GateProfile,
    raw: Sequence[RawObservation],
    parse_failures: Sequence[ParseFailure],
    as_of: datetime,
) -> list[GatedObservation]:
    """Gate one ticker's observations through the six stages above.

    Every input is represented in the output — accepted or quarantined with
    reasons — nothing is ever dropped. Relational blame policy: when a
    violation implicates several fields, quarantine only the uncorroborated
    leg(s) if at least one other leg is corroborated; quarantine ALL
    implicated legs when corroboration cannot localize fault. (A statically
    blamed gate accepts the bad value in exactly the scenario where the
    cross-check source is down.)

    `as_of` MUST be timezone-aware UTC (guard with models.require_aware) —
    the staleness comparison against observed_at raises TypeError otherwise.
    Each ParseFailure becomes a quarantined GatedObservation whose obs IS the
    ParseFailure (UNPARSEABLE reason; the writer persists its raw text in
    value_text).

    Postconditions (test-enforced):
      - len(output) == len(raw) + len(parse_failures)
      - at most one is_primary per (ticker, field), and it is accepted
      - reasons non-empty iff quarantined
    """
    require_aware(as_of)

    # Mutable working records — GatedObservation is frozen and constructed
    # exactly once, at the very end. Verdict is implicit throughout:
    # quarantined iff reasons is non-empty.
    pending = [_Pending(obs) for obs in raw]

    # 2. unary plausibility (NON_FINITE, OUT_OF_BOUNDS, DATE_IN_PAST)
    for p in pending:
        hit = _unary_hit(profile.specs[p.obs.field], p.obs, as_of)
        if hit is not None:
            p.reasons.append(hit)

    # 3. staleness — only survivors of unary; once quarantined at a stage, an
    # observation does not participate in later stages
    for p in pending:
        if p.accepted:
            hit = _staleness_hit(profile.specs[p.obs.field], p.obs, as_of)
            if hit is not None:
                p.reasons.append(hit)

    # 4. cross-source agreement (quarantine all sides / stamp corroborated_by)
    _cross_source_stage(profile, pending)

    # 5. relational cross-field, corroboration-aware blame
    _relational_stage(profile, pending)

    # 6. primary resolution — first source in spec.priority among accepted
    for field, group in _accepted_by_field(pending).items():
        primary = _would_be_primary(profile.specs[field].priority, group)
        if primary is not None:
            primary.is_primary = True

    gated = [
        GatedObservation(
            obs=p.obs,
            verdict="accepted" if p.accepted else "quarantined",
            reasons=tuple(p.reasons),
            corroborated_by=p.corroborated_by,
            is_primary=p.is_primary,
        )
        for p in pending
    ]
    # 1. parse boundary — always quarantined, appended after the raws so every
    # input is represented in input order
    gated.extend(_quarantine_parse_failure(f) for f in parse_failures)
    return gated


# --- pipeline internals ------------------------------------------------------


class _Pending:
    """One RawObservation moving through the stages. Plain and mutable — the
    frozen GatedObservation is built from this once, after resolution."""

    __slots__ = ("obs", "reasons", "corroborated_by", "is_primary")

    def __init__(self, obs: RawObservation) -> None:
        self.obs = obs
        self.reasons: list[QuarantineHit] = []
        self.corroborated_by: tuple[Source, ...] = ()
        self.is_primary = False

    @property
    def accepted(self) -> bool:
        return not self.reasons


def _accepted_by_field(pending: Sequence[_Pending]) -> dict[Field, list[_Pending]]:
    by_field: defaultdict[Field, list[_Pending]] = defaultdict(list)
    for p in pending:
        if p.accepted:
            by_field[p.obs.field].append(p)
    return dict(by_field)


def _unary_hit(spec: FieldSpec, obs: RawObservation, as_of: datetime) -> QuarantineHit | None:
    """Stage 2: per-observation plausibility from the FieldSpec. Bounds are
    inclusive; text fields have no unary checks."""
    if spec.kind == "num":
        v = obs.value_num
        if v is None:
            return None
        if not math.isfinite(v):
            return QuarantineHit(
                code=QuarantineCode.NON_FINITE,
                detail=f"{obs.field} {v} ({obs.source}) is not finite",
            )
        if spec.bounds is not None:
            low, high = spec.bounds
            if (low is not None and v < low) or (high is not None and v > high):
                return QuarantineHit(
                    code=QuarantineCode.OUT_OF_BOUNDS,
                    detail=f"{obs.field} {v:g} ({obs.source}) outside [{low}, {high}]",
                )
    elif spec.kind == "date":
        if spec.not_in_past and obs.value_date is not None and obs.value_date < as_of.date():
            return QuarantineHit(
                code=QuarantineCode.DATE_IN_PAST,
                detail=(
                    f"{obs.field} {obs.value_date.isoformat()} ({obs.source}) "
                    f"is before run date {as_of.date().isoformat()}"
                ),
            )
    return None


def _staleness_hit(spec: FieldSpec, obs: RawObservation, as_of: datetime) -> QuarantineHit | None:
    """Stage 3: only when the source reported its own data timestamp — gate on
    evidence, not guesses."""
    if spec.max_age is None or obs.observed_at is None:
        return None
    age = as_of - obs.observed_at
    if age <= spec.max_age:
        return None
    return QuarantineHit(
        code=QuarantineCode.STALE,
        detail=(
            f"{obs.field} ({obs.source}) observed {obs.observed_at.isoformat()}, "
            f"age {age} exceeds max {spec.max_age}"
        ),
    )


def _rel_spread(a: float, b: float) -> float:
    """Pairwise relative spread |a−b| / mean(|a|,|b|); zero when both values
    are zero (identical values trivially agree)."""
    mean = (abs(a) + abs(b)) / 2
    return abs(a - b) / mean if mean else 0.0


def _cross_source_stage(profile: GateProfile, pending: Sequence[_Pending]) -> None:
    """Stage 4: fields with a tolerance and ≥2 accepted observations. ANY pair
    beyond tolerance → quarantine ALL sides (with n=2 picking a winner is a
    coin flip dressed as data); all within → stamp each with the OTHER
    agreeing sources."""
    for field, group in _accepted_by_field(pending).items():
        spec = profile.specs[field]
        if spec.cross_source_rel_tol is None or spec.kind != "num" or len(group) < 2:
            continue
        worst: tuple[float, _Pending, _Pending] | None = None
        for a, b in combinations(group, 2):
            spread = _rel_spread(a.obs.value_num, b.obs.value_num)  # type: ignore[arg-type]
            if worst is None or spread > worst[0]:
                worst = (spread, a, b)
        spread, a, b = worst  # len(group) >= 2 guarantees at least one pair
        if spread > spec.cross_source_rel_tol:
            hit = QuarantineHit(
                code=QuarantineCode.CROSS_SOURCE_DISAGREEMENT,
                detail=(
                    f"{a.obs.source} {a.obs.value_num:.2f} vs "
                    f"{b.obs.source} {b.obs.value_num:.2f}, spread {spread:.0%}"
                ),
            )
            for p in group:
                p.reasons.append(hit)
        else:
            sources = sorted({p.obs.source for p in group})
            for p in group:
                p.corroborated_by = tuple(s for s in sources if s != p.obs.source)


def _relational_stage(profile: GateProfile, pending: Sequence[_Pending]) -> None:
    """Stage 5: checks run over the would-be-resolved accepted values. Blame is
    corroboration-aware: quarantine only the uncorroborated leg(s) when at
    least one other leg is corroborated; when fault cannot be localized (all
    corroborated or none), quarantine every implicated leg. Blame lands on
    EVERY observation of a blamed field that was accepted entering this stage."""
    accepted = _accepted_by_field(pending)
    values: dict[Field, FieldValue] = {}
    for field, group in accepted.items():
        primary = _would_be_primary(profile.specs[field].priority, group)
        if primary is None:
            continue  # no prioritized source accepted → nothing resolvable to check
        values[field] = FieldValue(
            field=field,
            value=primary.obs.value,
            source=primary.obs.source,
            fetched_at=primary.obs.fetched_at,
            corroborated_by=primary.corroborated_by,
        )
    violations = [v for check in profile.relational_checks if (v := check(values)) is not None]
    for violation in violations:
        implicated = [f for f in violation.implicated if f in values]
        uncorroborated = [f for f in implicated if not values[f].corroborated_by]
        blamed = uncorroborated if 0 < len(uncorroborated) < len(implicated) else implicated
        for field in blamed:
            for p in accepted[field]:
                p.reasons.append(violation.hit)


def _would_be_primary(
    priority: tuple[Source, ...], group: Sequence[_Pending]
) -> _Pending | None:
    """First accepted observation whose source appears earliest in the
    priority tuple; None when no accepted source is prioritized."""
    for source in priority:
        for p in group:
            if p.obs.source == source:
                return p
    return None


def _quarantine_parse_failure(failure: ParseFailure) -> GatedObservation:
    """Stage 1: sent-but-unreadable is evidence, not absence. The raw wire
    text rides in the detail here and persists in value_text at write time."""
    return GatedObservation(
        obs=failure,
        verdict="quarantined",
        reasons=(
            QuarantineHit(
                code=QuarantineCode.UNPARSEABLE,
                detail=f"{failure.field} from {failure.source}: could not parse {failure.raw!r}",
            ),
        ),
    )
