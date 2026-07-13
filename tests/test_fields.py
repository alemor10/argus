"""The registry completeness tests: every Field must be fully specified —
adding a field without gate parameters is a test failure, not a silent gap."""

from argus.fields import SPECS, Field, Source


def test_every_field_has_a_spec():
    missing = [f for f in Field if f not in SPECS]
    assert not missing, f"fields without a FieldSpec: {missing}"


def test_no_spec_for_unknown_fields():
    assert set(SPECS) <= set(Field)


def test_bounds_are_ordered_and_only_on_numeric_fields():
    for field, spec in SPECS.items():
        if spec.bounds is not None:
            assert spec.kind == "num", f"{field}: bounds on non-numeric field"
            low, high = spec.bounds
            if low is not None and high is not None:
                assert low < high, f"{field}: bounds inverted"


def test_cross_source_tolerance_only_on_numeric_fields():
    for field, spec in SPECS.items():
        if spec.cross_source_rel_tol is not None:
            assert spec.kind == "num", f"{field}: tolerance on non-numeric field"
            assert 0 < spec.cross_source_rel_tol < 1


def test_not_in_past_only_on_date_fields():
    for field, spec in SPECS.items():
        if spec.not_in_past:
            assert spec.kind == "date", f"{field}: not_in_past on non-date field"
    assert SPECS[Field.NEXT_EARNINGS_DATE].not_in_past  # DATE_IN_PAST has an owner


def test_priority_is_nonempty_and_uses_known_sources():
    for field, spec in SPECS.items():
        assert spec.priority, f"{field}: empty priority"
        assert all(isinstance(s, Source) for s in spec.priority)
        assert len(set(spec.priority)) == len(spec.priority), f"{field}: duplicate priority"


def test_known_sanity_cases_fit_the_bounds():
    """The decision log's named false-positive risks stay impossible."""
    price_low, price_high = SPECS[Field.PRICE].bounds
    assert price_high > 800_000, "BRK-A-class prices must pass the unary gate"
    pe_low, _ = SPECS[Field.PE_FWD].bounds
    assert pe_low < 0, "negative forward P/E (expected-loss names) must pass"
