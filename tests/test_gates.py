"""Gate tests. Only target_vs_price is implemented in the skeleton — it is the
founding case (NTDOY) and its behavior is locked here before the pipeline
lands. run_gates itself is implementation-phase."""

from datetime import UTC, datetime

from argus.fields import Field, QuarantineCode, Source
from argus.gates import DEFAULT_PROFILE, target_vs_price
from argus.models import FieldValue

NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)


def _value(field: Field, value: float, source: Source = Source.YAHOO) -> FieldValue:
    return FieldValue(field=field, value=value, source=source, fetched_at=NOW)


def _values(price: float | None = None, target: float | None = None):
    values = {}
    if price is not None:
        values[Field.PRICE] = _value(Field.PRICE, price)
    if target is not None:
        values[Field.ANALYST_TARGET_MEAN] = _value(Field.ANALYST_TARGET_MEAN, target)
    return values


class TestTargetVsPrice:
    def test_ntdoy_stale_target_trips_the_gate(self):
        """The founding case: $35 stale target vs $10.97 price → 3.19,
        outside [0.3, 3.0]. '218% upside' must be uncomputable."""
        violation = target_vs_price(_values(price=10.97, target=35.0))
        assert violation is not None
        assert violation.hit.code == QuarantineCode.TARGET_PRICE_RATIO
        assert "3.19" in violation.hit.detail
        assert set(violation.implicated) == {Field.ANALYST_TARGET_MEAN, Field.PRICE}

    def test_plausible_ratio_passes(self):
        assert target_vs_price(_values(price=100.0, target=120.0)) is None

    def test_boundary_ratios_pass(self):
        assert target_vs_price(_values(price=100.0, target=30.0)) is None  # exactly 0.3
        assert target_vs_price(_values(price=100.0, target=300.0)) is None  # exactly 3.0

    def test_deep_undershoot_also_trips(self):
        violation = target_vs_price(_values(price=100.0, target=10.0))
        assert violation is not None
        assert violation.hit.code == QuarantineCode.TARGET_PRICE_RATIO

    def test_missing_leg_is_not_a_violation(self):
        assert target_vs_price(_values(price=10.97)) is None
        assert target_vs_price(_values(target=35.0)) is None
        assert target_vs_price({}) is None


def test_default_profile_is_complete():
    assert target_vs_price in DEFAULT_PROFILE.relational_checks
    assert set(DEFAULT_PROFILE.specs) == set(Field)
