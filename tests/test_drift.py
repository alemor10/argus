"""Watchlist run-over-run drift suffixes: sub-threshold movement is
information — quiet weeks show which way things lean, without ever touching
the Changes section's threshold semantics."""

from datetime import UTC, datetime

from argus.digest import _drift_suffix
from argus.fields import Field, Source
from argus.models import FieldValue, Snapshot

NOW = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)


def _baseline(**values) -> Snapshot:
    return Snapshot(
        ticker="NVDA",
        run_id=1,
        as_of=NOW,
        values={
            field: FieldValue(field=field, value=value, source=Source.YAHOO, fetched_at=NOW)
            for field, value in values.items()
        },
    )


def test_scale_free_fields_drift_in_percent():
    baseline = _baseline(**{Field.PRICE: 170.0})
    assert _drift_suffix(Field.PRICE, 181.25, baseline) == " (+6.6%)"


def test_ratios_drift_in_absolute_points():
    baseline = _baseline(**{Field.PE_FWD: 29.8})
    assert _drift_suffix(Field.PE_FWD, 31.2, baseline) == " (+1.40)"


def test_margins_drift_in_percentage_points():
    baseline = _baseline(**{Field.GROSS_MARGIN: 0.733})
    assert _drift_suffix(Field.GROSS_MARGIN, 0.741, baseline) == " (+0.8pp)"


def test_negative_drift_renders_signed():
    baseline = _baseline(**{Field.PRICE: 210.0})
    assert _drift_suffix(Field.PRICE, 205.71, baseline) == " (-2.0%)"


def test_unchanged_value_shows_nothing():
    baseline = _baseline(**{Field.PRICE: 205.71})
    assert _drift_suffix(Field.PRICE, 205.71, baseline) == ""


def test_no_baseline_or_no_baseline_value_shows_nothing():
    assert _drift_suffix(Field.PRICE, 205.71, None) == ""
    assert _drift_suffix(Field.PRICE, 205.71, _baseline()) == ""  # field absent in baseline


def test_text_and_date_fields_never_drift():
    baseline = _baseline(**{Field.PRICE: 100.0})
    assert _drift_suffix(Field.ANALYST_RATING, "buy", baseline) == ""
