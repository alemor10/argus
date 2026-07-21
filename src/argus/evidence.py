"""The evidence contract — PURE.

Every number Argus shows for a scout candidate carries a claim to trust, and
this module makes that claim explicit and non-subjective. Three honest,
data-derived outputs, none of them a forecast or an opinion:

1. **Evidence state** per field — how strong the backing is:
   - `corroborated` — gate-accepted AND a second source agreed
   - `single-source` — gate-accepted, one source, no cross-check
   - `claim-only` — the gates did NOT confirm it, but the screener claimed a
     value (so the screen leaned on an unverified number)
   - `missing` — no accepted value and no screener claim

2. **Screen-exit conditions** — the factual lines that, if crossed, drop a name
   from the screen. These are the human-set screen thresholds restated as
   falsification conditions (the negation of each pass rule). Identical for
   every proposed name because they are the ONE screen definition — so they are
   a screen-level fact, shown once, never a per-name opinion.

3. **Data flags** — factual, per-name observations about the evidence itself:
   a metric sitting near a screen boundary, a core metric that is claim-only or
   single-source, a quarantined field. Statements of fact ("D/E 0.94 is within
   6% of the 1.0 screen ceiling"), never a judgement ("this is risky"). The
   hard constraint holds: Argus reports data vs the human's stated lines; it
   never interprets, forecasts, or recommends.

Everything here is reconstructed from data already persisted on the proposal
(screen_reasons, screener_metrics) and its enrichment snapshot, so
`report --run N` reproduces it exactly — the thresholds come from the immutable
per-name reason strings, never from the live config (which could drift).
"""

import math
import re
from typing import Literal, NamedTuple

from argus.fields import Field
from argus.models import Snapshot

EvidenceState = Literal["corroborated", "single-source", "claim-only", "missing"]

# Field -> the screener_metrics key whose claim nominated it. Used to tell
# `claim-only` (screener had a number the gates didn't confirm) from `missing`.
SCREEN_CLAIM_KEYS: dict[Field, str] = {
    Field.PE_FWD: "fwd_pe",
    Field.REVENUE_GROWTH: "revenue_growth_ttm_pct",
    Field.GROSS_MARGIN: "gross_margin_pct",
    Field.OPERATING_MARGIN: "operating_margin_pct",
    Field.ROE: "roe_pct",
    Field.DEBT_TO_EQUITY: "debt_to_equity",
}


class _Rule(NamedTuple):
    """One screen rule's render metadata: how to phrase its exit condition, and
    (when it maps to a gate-accepted field) how to compare the verified value to
    the boundary for a near-threshold flag."""

    exit_clause: str  # "forward P/E above {t}"
    field: Field | None  # the gate-accepted field to compare, or None
    direction: Literal["ceiling", "floor"]
    is_pct: bool  # verified value is a fraction to scale ×100 before comparing
    label: str  # "forward P/E"


# Keyed by screen_reasons key, in the fixed reporting order (the same order
# scout.criteria emits them) so exit conditions and flags are deterministic.
_RULES: dict[str, _Rule] = {
    "forward_pe": _Rule("forward P/E above {t}", Field.PE_FWD, "ceiling", False, "forward P/E"),
    "revenue_growth": _Rule(
        "revenue growth below {t}", Field.REVENUE_GROWTH, "floor", True, "revenue growth"
    ),
    "gross_margin": _Rule(
        "gross margin below {t}", Field.GROSS_MARGIN, "floor", True, "gross margin"
    ),
    "operating_margin": _Rule(
        "operating margin below {t}", Field.OPERATING_MARGIN, "floor", True, "operating margin"
    ),
    "roe": _Rule("ROE below {t}", Field.ROE, "floor", True, "ROE"),
    "debt_to_equity": _Rule("D/E above {t}", Field.DEBT_TO_EQUITY, "ceiling", False, "D/E"),
    "value_trap": _Rule("EPS trend at or below {t}", None, "floor", False, "EPS trend"),
}

# How near a boundary counts as "near" — 8% of the threshold, on the passing
# side. A factual proximity statement, not a judgement about the name.
_NEAR_FRACTION = 0.08


def evidence_state(
    field: Field,
    snapshot: Snapshot | None,
    screener_metrics: dict[str, object],
) -> EvidenceState:
    """The four-state backing label for one field. Quarantine is NOT one of the
    four — a quarantined field is handled by the caller (it has its own louder
    treatment); this answers only accepted-vs-absent."""
    fv = snapshot.values.get(field) if snapshot is not None else None
    if fv is not None:
        return "corroborated" if fv.corroborated_by else "single-source"
    key = SCREEN_CLAIM_KEYS.get(field)
    if key is not None and _finite(screener_metrics.get(key)):
        return "claim-only"
    return "missing"


def screen_exit_conditions(screen_reasons: dict[str, str]) -> list[str]:
    """The factual lines that drop a name from the screen — the negation of each
    pass rule, with the human-set threshold read back verbatim from the
    persisted reason string. Deterministic order; empty when no reasons are
    present (a non-proposed row)."""
    out: list[str] = []
    for key, rule in _RULES.items():
        reason = screen_reasons.get(key)
        if not reason:
            continue
        token = _threshold_token(reason)
        if token is not None:
            out.append(rule.exit_clause.format(t=token))
    return out


def data_flags(
    screen_reasons: dict[str, str],
    screener_metrics: dict[str, object],
    snapshot: Snapshot | None,
) -> list[str]:
    """Factual, per-name observations about the evidence — never a judgement.
    Ordered most-informative first: a core metric the gates could not confirm,
    a quarantined field, the price anchor with no cross-check, then any verified
    metric sitting near a screen boundary."""
    flags: list[str] = []

    # 1. Core screen metrics the gates did NOT confirm — the screen leaned on an
    #    unverified claim. (Enrichment excludes a name whose CORE fields fail, so
    #    these are the secondary metrics that can still be claim-only.)
    claim_only = [
        rule.label
        for key, rule in _RULES.items()
        if rule.field is not None
        and evidence_state(rule.field, snapshot, screener_metrics) == "claim-only"
    ]
    if claim_only:
        flags.append(
            "screener-claimed only (gates did not confirm): " + ", ".join(claim_only)
        )

    # 2. Quarantined core fields — data existed but the gates rejected all of it.
    if snapshot is not None:
        quarantined = [
            _RULES[k].label
            for k in _RULES
            if _RULES[k].field is not None and _RULES[k].field in snapshot.quarantined
        ]
        if quarantined:
            flags.append("quarantined this run: " + ", ".join(quarantined))

    # 3. The price anchor with no independent cross-check this run.
    if snapshot is not None:
        price = snapshot.values.get(Field.PRICE)
        if price is not None and not price.corroborated_by:
            flags.append("price is single-source (no cross-check this run)")

    # 4. Verified metrics sitting near a screen boundary — a proximity fact.
    for key, rule in _RULES.items():
        if rule.field is None or snapshot is None:
            continue
        verified = _verified_num(snapshot, rule.field)
        threshold = _threshold_number(screen_reasons.get(key))
        if verified is None or threshold is None or threshold == 0:
            continue
        scaled = verified * (100.0 if rule.is_pct else 1.0)
        near = _near_threshold(scaled, threshold, rule.direction)
        if near is not None:
            edge = "ceiling" if rule.direction == "ceiling" else "floor"
            value = f"{scaled:.1f}%" if rule.is_pct else f"{scaled:.1f}"
            bound = _threshold_token(screen_reasons[key]) or f"{threshold:g}"
            flags.append(
                f"{rule.label} {value} is within {near:.0%} of the {bound} screen {edge}"
            )

    return flags


def _near_threshold(scaled: float, threshold: float, direction: str) -> float | None:
    """The proximity fraction if `scaled` sits within _NEAR_FRACTION of the
    boundary on the PASSING side, else None. A ceiling passes below it, a floor
    above it; a value on the failing side never reaches here (the name passed
    the screen), but the guard is written so it simply returns None if it did."""
    if direction == "ceiling":
        if scaled > threshold:
            return None
        gap = (threshold - scaled) / abs(threshold)
    else:  # floor
        if scaled < threshold:
            return None
        gap = (scaled - threshold) / abs(threshold)
    return gap if gap <= _NEAR_FRACTION else None


_NUMBER = re.compile(r"[-+]?\d*\.?\d+%?")


def _threshold_token(reason: str) -> str | None:
    """The threshold as it should READ — the last number (with its % if the
    metric is a percent) in the persisted reason string, e.g. 'fwd P/E 15.0 ≤
    25' → '25', 'rev growth +15.0% ≥ 10%' → '10%'. The reason strings put the
    threshold last, after the comparison operator."""
    matches = _NUMBER.findall(reason or "")
    return matches[-1] if matches else None


def _threshold_number(reason: str | None) -> float | None:
    """The threshold as a float for comparison math — the token with any % or
    sign parsed off."""
    token = _threshold_token(reason or "")
    if token is None:
        return None
    try:
        return float(token.rstrip("%"))
    except ValueError:
        return None


def _verified_num(snapshot: Snapshot | None, field: Field) -> float | None:
    if snapshot is None:
        return None
    fv = snapshot.values.get(field)
    if fv is None:
        return None
    try:
        number = float(fv.value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
