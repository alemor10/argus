"""Thesis checks — the bridge from a prose thesis to checkable data, PURE.

Argus never interprets the thesis line. The human declares falsifiable
conditions ("revenue_growth >= 20%") that operationalize their reasoning;
this module parses them (at the config boundary, failing loud on bad syntax)
and evaluates them against a gated snapshot. A breach is not a prediction —
it is current data reported against a line the human drew. See CLAUDE.md
(no forecasts, human decides) and ARCHITECTURE.md (Thesis drift).

Grammar:  <field> <op> <value>
  field   any Field name (numeric or the analyst_rating text field)
  op       >= <= > < == !=            (numeric fields)
           == != in not in            (text field: analyst_rating)
  value    a number, optionally with a trailing % (÷100 — write margins and
           growth as percents: "gross_margin >= 65%"); a bare word for text;
           or a [comma, list] for in / not in.
"""

import math
import re

from argus.fields import SPECS, Field
from argus.models import Snapshot, ThesisCheck, ThesisCheckResult

_NUMERIC_OPS = frozenset({">=", "<=", ">", "<", "==", "!="})
_TEXT_OPS = frozenset({"==", "!=", "in", "not_in"})
# Longest first so ">=" is found before ">".
_SYMBOL_OPS = (">=", "<=", "==", "!=", ">", "<")


def parse_thesis_check(raw: str) -> ThesisCheck:
    """Parse one condition string into a structured, validated ThesisCheck.
    Raises ValueError on anything malformed — thesis checks are validated at
    config load, so a typo fails the run loudly rather than silently never
    firing."""
    text = " ".join(raw.split())  # collapse whitespace
    field_tok, op, value_tok = _split(text, raw)
    field = _field(field_tok, raw)
    kind = SPECS[field].kind
    if kind == "date":
        raise ValueError(f"thesis checks are not supported on the date field {field}: {raw!r}")
    if kind == "num" and op not in _NUMERIC_OPS:
        raise ValueError(f"operator {op!r} is not valid on numeric field {field}: {raw!r}")
    if kind == "text" and op not in _TEXT_OPS:
        raise ValueError(f"operator {op!r} is not valid on text field {field}: {raw!r}")
    value = _value(value_tok, kind, op, raw)
    return ThesisCheck(field=field, op=op, value=value, raw=text)


def evaluate_thesis_checks(
    checks: tuple[ThesisCheck, ...], snapshot: Snapshot
) -> tuple[ThesisCheckResult, ...]:
    """Evaluate each check against the run's accepted values. A field with no
    accepted value (missing or quarantined) yields `undeterminable` — the
    thesis could not be verified this run, which the digest surfaces so an
    unverifiable check is never mistaken for a passing one."""
    results = []
    for check in checks:
        fv = snapshot.values.get(check.field)
        if fv is None:
            results.append(ThesisCheckResult(check=check, status="undeterminable"))
            continue
        holds = _holds(fv.value, check)
        results.append(
            ThesisCheckResult(
                check=check,
                status="holds" if holds else "breached",
                observed=fv.value if isinstance(fv.value, (int, float, str)) else None,
            )
        )
    return tuple(results)


def _split(text: str, raw: str) -> tuple[str, str, str]:
    word = re.match(r"^([a-z_]+)\s+(not in|in)\s+(.+)$", text)
    if word:
        op = "not_in" if word.group(2) == "not in" else "in"
        return word.group(1), op, word.group(3).strip()
    for sym in _SYMBOL_OPS:
        idx = text.find(sym)
        if idx > 0:  # field name precedes it; field tokens carry no symbols
            return text[:idx].strip(), sym, text[idx + len(sym) :].strip()
    raise ValueError(f"thesis check has no recognized operator: {raw!r}")


def _field(token: str, raw: str) -> Field:
    try:
        return Field(token)
    except ValueError as exc:
        valid = ", ".join(f.value for f in Field)
        raise ValueError(f"unknown field {token!r} in {raw!r} — valid fields: {valid}") from exc


def _value(token: str, kind: str, op: str, raw: str) -> float | str | tuple[str, ...]:
    if op in ("in", "not_in"):
        if not (token.startswith("[") and token.endswith("]")):
            raise ValueError(f"operator {op!r} needs a [list] value: {raw!r}")
        # Strip quotes per item — same as the scalar text path below. Without
        # this a quoted item keeps its quote and never matches an (unquoted)
        # rating, so a `not in ['sell']` downgrade guard would silently never
        # fire — the worst failure mode for the highest-signal event.
        items = tuple(x.strip().strip("'\"").lower() for x in token[1:-1].split(",") if x.strip())
        if not items:
            raise ValueError(f"empty list in {raw!r}")
        return items
    if kind == "num":
        scale, body = (0.01, token[:-1].strip()) if token.endswith("%") else (1.0, token)
        try:
            result = float(body) * scale
        except ValueError as exc:
            raise ValueError(f"expected a number in {raw!r}, got {token!r}") from exc
        if not math.isfinite(result):
            # A non-finite target (nan, or 1e400 overflowing to inf) makes
            # every comparison False → a permanent false breach. Reject it at
            # the fail-loud boundary, like the gates reject non-finite values.
            raise ValueError(f"thesis check target is not a finite number: {raw!r}")
        return result
    return token.strip().strip("'\"").lower()  # text field


def _holds(observed: object, check: ThesisCheck) -> bool:
    op, target = check.op, check.value
    if op in ("in", "not_in"):
        obs = observed.lower() if isinstance(observed, str) else observed
        inside = obs in target
        return inside if op == "in" else not inside
    if op in ("==", "!="):
        if isinstance(observed, str) and isinstance(target, str):
            equal = observed.lower() == target
        else:
            equal = observed == target
        return equal if op == "==" else not equal
    if not isinstance(observed, (int, float)) or not isinstance(target, (int, float)):
        return False  # defensive: parse already matched op to field kind
    return {
        ">=": observed >= target,
        "<=": observed <= target,
        ">": observed > target,
        "<": observed < target,
    }[op]
