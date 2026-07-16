from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from argus.fields import Field, QuarantineCode, Source
from argus.models import (
    CHANGE_EVENT_ADAPTER,
    EarningsReported,
    EarningsResultRecord,
    FieldValue,
    GatedObservation,
    ParseFailure,
    PriceMove,
    QuarantineHit,
    RawObservation,
    require_aware,
)

NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)


def _price_obs(**overrides):
    kwargs = dict(
        ticker="NVDA",
        field=Field.PRICE,
        value_num=181.25,
        source=Source.YAHOO,
        fetched_at=NOW,
    )
    kwargs.update(overrides)
    return RawObservation(**kwargs)


class TestRawObservation:
    def test_valid_numeric_observation(self):
        obs = _price_obs()
        assert obs.value == 181.25

    def test_provenance_is_mandatory(self):
        with pytest.raises(ValidationError):
            RawObservation(ticker="NVDA", field=Field.PRICE, value_num=1.0, source=Source.YAHOO)

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValidationError):
            _price_obs(fetched_at=datetime(2026, 7, 12, 14, 0))

    def test_value_kind_must_match_field_spec(self):
        with pytest.raises(ValidationError, match="expected value_num"):
            _price_obs(value_num=None, value_text="181.25")

    def test_exactly_one_value_required(self):
        with pytest.raises(ValidationError, match="exactly one value"):
            _price_obs(value_text="also set")
        with pytest.raises(ValidationError, match="exactly one value"):
            _price_obs(value_num=None)

    def test_date_field_takes_value_date(self):
        obs = _price_obs(field=Field.NEXT_EARNINGS_DATE, value_num=None, value_date=date(2026, 8, 20))
        assert obs.value == date(2026, 8, 20)

    def test_frozen(self):
        with pytest.raises(ValidationError):
            _price_obs().value_num = 1.0


class TestGatedObservation:
    def test_quarantined_requires_reasons(self):
        with pytest.raises(ValidationError, match="reasons"):
            GatedObservation(obs=_price_obs(), verdict="quarantined")

    def test_accepted_forbids_reasons(self):
        hit = QuarantineHit(code=QuarantineCode.OUT_OF_BOUNDS, detail="x")
        with pytest.raises(ValidationError, match="reasons"):
            GatedObservation(obs=_price_obs(), verdict="accepted", reasons=(hit,))

    def test_quarantined_cannot_be_primary(self):
        hit = QuarantineHit(code=QuarantineCode.OUT_OF_BOUNDS, detail="x")
        with pytest.raises(ValidationError, match="primary"):
            GatedObservation(obs=_price_obs(), verdict="quarantined", reasons=(hit,), is_primary=True)

    def test_accepted_primary_ok(self):
        gated = GatedObservation(
            obs=_price_obs(), verdict="accepted", corroborated_by=(Source.FINNHUB,), is_primary=True
        )
        assert gated.is_primary


class TestParseFailurePayload:
    """Hard rule 2: sent-but-unreadable data reaches the store as UNPARSEABLE
    quarantine, never a silent absence — even for num/date fields."""

    FAILURE = ParseFailure(
        ticker="NTDOY",
        field=Field.ANALYST_TARGET_MEAN,  # a num-kind field: the hard case
        raw="N/A garbled",
        source=Source.YAHOO,
        fetched_at=NOW,
    )

    def test_unparseable_quarantine_is_representable(self):
        gated = GatedObservation(
            obs=self.FAILURE,
            verdict="quarantined",
            reasons=(QuarantineHit(code=QuarantineCode.UNPARSEABLE, detail="raw: 'N/A garbled'"),),
        )
        assert isinstance(gated.obs, ParseFailure)
        assert gated.obs.raw == "N/A garbled"

    def test_parse_failure_cannot_be_accepted(self):
        with pytest.raises(ValidationError, match="UNPARSEABLE"):
            GatedObservation(obs=self.FAILURE, verdict="accepted")

    def test_parse_failure_requires_unparseable_reason(self):
        wrong = QuarantineHit(code=QuarantineCode.OUT_OF_BOUNDS, detail="x")
        with pytest.raises(ValidationError, match="UNPARSEABLE"):
            GatedObservation(obs=self.FAILURE, verdict="quarantined", reasons=(wrong,))


class TestFieldValueHydration:
    """SQL hands back TEXT; FieldValue must coerce to the declared kind so the
    diff engine never sees a latent string."""

    def test_iso_date_string_becomes_date(self):
        fv = FieldValue(
            field=Field.NEXT_EARNINGS_DATE, value="2026-08-20", source=Source.YAHOO, fetched_at=NOW
        )
        assert fv.value == date(2026, 8, 20)

    def test_numeric_string_becomes_float(self):
        fv = FieldValue(field=Field.PRICE, value="181.25", source=Source.YAHOO, fetched_at=NOW)
        assert fv.value == 181.25

    def test_garbage_for_num_field_is_an_error(self):
        with pytest.raises(ValidationError, match="num-kind"):
            FieldValue(field=Field.PRICE, value="N/A", source=Source.YAHOO, fetched_at=NOW)

    def test_non_string_for_text_field_is_an_error(self):
        with pytest.raises(ValidationError, match="text-kind"):
            FieldValue(field=Field.ANALYST_RATING, value=3.0, source=Source.YAHOO, fetched_at=NOW)


def test_require_aware_rejects_naive_datetimes():
    assert require_aware(NOW) is NOW
    with pytest.raises(ValueError, match="naive"):
        require_aware(datetime(2026, 7, 12, 14, 0))


class TestChangeEventRoundTrip:
    def test_discriminated_union_survives_json(self):
        """The change_events table stores model_dump_json payloads; they must
        come back as the same typed event."""
        event = PriceMove(
            ticker="NVDA", old=170.0, new=181.25, pct=6.6, threshold=5.0, old_as_of=NOW
        )
        payload = event.model_dump_json()
        restored = CHANGE_EVENT_ADAPTER.validate_json(payload)
        assert restored == event
        assert isinstance(restored, PriceMove)

    def test_earnings_reported_survives_json(self):
        event = EarningsReported(
            ticker="NVDA", quarter_end=date(2026, 6, 30),
            eps_actual=1.05, eps_estimate=0.93, surprise_pct=12.9,
        )
        restored = CHANGE_EVENT_ADAPTER.validate_json(event.model_dump_json())
        assert restored == event
        assert isinstance(restored, EarningsReported)


class TestEarningsResultRecord:
    def test_non_finite_eps_is_rejected_at_construction(self):
        """NaN binds as NULL in SQLite and would violate the table's NOT NULL
        mid-transaction — reject it at the model boundary instead."""
        for bad in (float("nan"), float("inf")):
            with pytest.raises(ValidationError):
                EarningsResultRecord(
                    ticker="NVDA", quarter_end=date(2026, 6, 30), eps_actual=bad,
                    source=Source.YAHOO, fetched_at=NOW,
                )
        with pytest.raises(ValidationError):
            EarningsResultRecord(
                ticker="NVDA", quarter_end=date(2026, 6, 30), eps_actual=1.0,
                eps_estimate=float("nan"), source=Source.YAHOO, fetched_at=NOW,
            )
