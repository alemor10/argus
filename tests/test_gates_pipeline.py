"""Pipeline tests for gates.run_gates — the six fixed stages over one ticker's
observations. target_vs_price's own behavior is locked in test_gates.py; the
subject here is the pipeline: stage order, corroboration stamping,
corroboration-aware blame, primary resolution, and the postconditions."""

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from argus.fields import SPECS, Field, FieldSpec, QuarantineCode, Source
from argus.gates import DEFAULT_PROFILE, GateProfile, run_gates, target_vs_price
from argus.models import ParseFailure, RawObservation

NOW = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)


def _obs(field, *, num=None, text=None, day=None, source=Source.YAHOO, observed_at=None):
    return RawObservation(
        ticker="NTDOY",
        field=field,
        value_num=num,
        value_text=text,
        value_date=day,
        source=source,
        fetched_at=NOW,
        observed_at=observed_at,
    )


def _price(value, source=Source.YAHOO, observed_at=None):
    return _obs(Field.PRICE, num=value, source=source, observed_at=observed_at)


def _target(value, source=Source.YAHOO):
    return _obs(Field.ANALYST_TARGET_MEAN, num=value, source=source)


def _failure(field=Field.ANALYST_TARGET_MEAN, raw="N/A garbled", source=Source.YAHOO):
    return ParseFailure(ticker="NTDOY", field=field, raw=raw, source=source, fetched_at=NOW)


def _gate(raw, failures=(), profile=DEFAULT_PROFILE, as_of=NOW):
    return run_gates(profile, list(raw), list(failures), as_of)


def _one(out, field, source):
    """The single gated RawObservation for (field, source)."""
    matches = [
        g
        for g in out
        if isinstance(g.obs, RawObservation) and g.obs.field == field and g.obs.source == source
    ]
    assert len(matches) == 1, f"expected exactly one ({field}, {source}), got {len(matches)}"
    return matches[0]


def _codes(gated):
    return [hit.code for hit in gated.reasons]


# A profile in which the analyst target CAN be corroborated (SPECS gives it no
# cross-source tolerance) — for the contrived blame scenarios.
_TARGET_TOL_PROFILE = GateProfile(
    specs={
        **SPECS,
        Field.ANALYST_TARGET_MEAN: FieldSpec(
            "num", bounds=(0.0001, 10_000_000), cross_source_rel_tol=0.02
        ),
    },
    relational_checks=(target_vs_price,),
)


class TestNtdoyWalkthrough:
    """The founding case, end to end (ARCHITECTURE.md): agreeing prices are
    corroborated and untouched; the stale $35 target is quarantined by the
    relational gate, so '218% upside' is never computable."""

    def _run(self):
        return _gate([_price(10.97), _price(10.99, Source.FINNHUB), _target(35.0)])

    def test_agreeing_prices_are_accepted_and_corroborated(self):
        out = self._run()
        yahoo = _one(out, Field.PRICE, Source.YAHOO)
        finnhub = _one(out, Field.PRICE, Source.FINNHUB)
        assert yahoo.verdict == finnhub.verdict == "accepted"
        assert yahoo.corroborated_by == (Source.FINNHUB,)
        assert finnhub.corroborated_by == (Source.YAHOO,)

    def test_yahoo_price_is_primary(self):
        out = self._run()
        assert _one(out, Field.PRICE, Source.YAHOO).is_primary
        assert not _one(out, Field.PRICE, Source.FINNHUB).is_primary

    def test_target_quarantined_by_relational_gate(self):
        target = _one(self._run(), Field.ANALYST_TARGET_MEAN, Source.YAHOO)
        assert target.verdict == "quarantined"
        assert _codes(target) == [QuarantineCode.TARGET_PRICE_RATIO]
        assert "3.19" in target.reasons[0].detail
        assert not target.is_primary

    def test_price_is_untouched_by_the_blame(self):
        out = self._run()
        assert _one(out, Field.PRICE, Source.YAHOO).reasons == ()
        assert _one(out, Field.PRICE, Source.FINNHUB).reasons == ()


class TestCorroborationAwareBlame:
    """Blame policy, all four corroboration configurations of the two legs."""

    def test_price_corroborated_target_not_blames_target_only(self):
        out = _gate([_price(10.97), _price(10.99, Source.FINNHUB), _target(35.0)])
        assert _one(out, Field.ANALYST_TARGET_MEAN, Source.YAHOO).verdict == "quarantined"
        assert _one(out, Field.PRICE, Source.YAHOO).verdict == "accepted"
        assert _one(out, Field.PRICE, Source.FINNHUB).verdict == "accepted"

    def test_neither_leg_corroborated_blames_both(self):
        out = _gate([_price(10.97), _target(35.0)])
        price = _one(out, Field.PRICE, Source.YAHOO)
        target = _one(out, Field.ANALYST_TARGET_MEAN, Source.YAHOO)
        assert price.verdict == target.verdict == "quarantined"
        assert _codes(price) == [QuarantineCode.TARGET_PRICE_RATIO]
        assert _codes(target) == [QuarantineCode.TARGET_PRICE_RATIO]
        assert not any(g.is_primary for g in out)

    def test_target_corroborated_price_not_blames_price_only(self):
        # Contrived: two agreeing targets corroborate each other, the lone
        # price does not — so the price leg takes the blame.
        out = _gate(
            [_price(10.97), _target(35.0), _target(35.1, Source.FINNHUB)],
            profile=_TARGET_TOL_PROFILE,
        )
        price = _one(out, Field.PRICE, Source.YAHOO)
        target = _one(out, Field.ANALYST_TARGET_MEAN, Source.YAHOO)
        assert price.verdict == "quarantined"
        assert _codes(price) == [QuarantineCode.TARGET_PRICE_RATIO]
        assert target.verdict == "accepted"
        assert target.corroborated_by == (Source.FINNHUB,)
        assert target.is_primary

    def test_all_legs_corroborated_blames_every_observation_of_both(self):
        # Fault cannot be localized → every still-accepted observation of
        # every implicated field is quarantined.
        out = _gate(
            [
                _price(10.97),
                _price(10.99, Source.FINNHUB),
                _target(35.0),
                _target(35.1, Source.FINNHUB),
            ],
            profile=_TARGET_TOL_PROFILE,
        )
        for field in (Field.PRICE, Field.ANALYST_TARGET_MEAN):
            for source in (Source.YAHOO, Source.FINNHUB):
                gated = _one(out, field, source)
                assert gated.verdict == "quarantined"
                assert QuarantineCode.TARGET_PRICE_RATIO in _codes(gated)
        assert not any(g.is_primary for g in out)


class TestCrossSource:
    def test_disagreement_quarantines_both_sides(self):
        out = _gate([_price(10.97), _price(35.10, Source.FINNHUB)])
        yahoo = _one(out, Field.PRICE, Source.YAHOO)
        finnhub = _one(out, Field.PRICE, Source.FINNHUB)
        assert yahoo.verdict == finnhub.verdict == "quarantined"
        assert _codes(yahoo) == [QuarantineCode.CROSS_SOURCE_DISAGREEMENT]
        assert _codes(finnhub) == [QuarantineCode.CROSS_SOURCE_DISAGREEMENT]
        assert yahoo.corroborated_by == () and finnhub.corroborated_by == ()
        assert not any(g.is_primary for g in out)
        detail = yahoo.reasons[0].detail
        assert "yahoo" in detail and "finnhub" in detail

    def test_single_source_accepted_uncorroborated(self):
        gated = _gate([_price(10.97)])[0]
        assert gated.verdict == "accepted"
        assert gated.corroborated_by == ()
        assert gated.is_primary

    def test_field_without_tolerance_never_checks_or_corroborates(self):
        out = _gate(
            [
                _obs(Field.MARKET_CAP, num=1e9),
                _obs(Field.MARKET_CAP, num=5e9, source=Source.EDGAR),  # wildly apart — no tol
            ]
        )
        assert all(g.verdict == "accepted" for g in out)
        assert all(g.corroborated_by == () for g in out)
        assert _one(out, Field.MARKET_CAP, Source.YAHOO).is_primary
        assert not _one(out, Field.MARKET_CAP, Source.EDGAR).is_primary


class TestStaleness:
    def test_observed_five_days_ago_with_four_day_max_age_is_stale(self):
        gated = _gate([_price(10.97, observed_at=NOW - timedelta(days=5))])[0]
        assert gated.verdict == "quarantined"
        assert _codes(gated) == [QuarantineCode.STALE]

    def test_missing_observed_at_skips_the_check(self):
        assert _gate([_price(10.97, observed_at=None)])[0].verdict == "accepted"

    def test_fresh_observed_at_passes(self):
        assert _gate([_price(10.97, observed_at=NOW - timedelta(days=1))])[0].verdict == "accepted"

    def test_age_exactly_max_age_passes(self):
        assert _gate([_price(10.97, observed_at=NOW - timedelta(days=4))])[0].verdict == "accepted"

    def test_stale_observation_does_not_corroborate(self):
        out = _gate(
            [_price(10.97), _price(10.99, Source.FINNHUB, observed_at=NOW - timedelta(days=5))]
        )
        yahoo = _one(out, Field.PRICE, Source.YAHOO)
        finnhub = _one(out, Field.PRICE, Source.FINNHUB)
        assert _codes(finnhub) == [QuarantineCode.STALE]
        assert yahoo.verdict == "accepted"
        assert yahoo.corroborated_by == ()  # the cross-check never ran
        assert yahoo.is_primary


class TestUnary:
    def test_nan_is_non_finite(self):
        assert _codes(_gate([_price(math.nan)])[0]) == [QuarantineCode.NON_FINITE]

    def test_inf_is_non_finite(self):
        assert _codes(_gate([_price(math.inf)])[0]) == [QuarantineCode.NON_FINITE]

    def test_out_of_bounds_price(self):
        gated = _gate([_price(-5.0)])[0]
        assert gated.verdict == "quarantined"
        assert _codes(gated) == [QuarantineCode.OUT_OF_BOUNDS]

    def test_bounds_are_inclusive(self):
        assert _gate([_price(0.0001)])[0].verdict == "accepted"  # exactly lo
        assert _gate([_price(10_000_000.0)])[0].verdict == "accepted"  # exactly hi (BRK-A room)

    def test_earnings_date_in_past(self):
        gated = _gate([_obs(Field.NEXT_EARNINGS_DATE, day=date(2026, 7, 11))])[0]
        assert _codes(gated) == [QuarantineCode.DATE_IN_PAST]

    def test_earnings_today_and_future_pass(self):
        assert _gate([_obs(Field.NEXT_EARNINGS_DATE, day=NOW.date())])[0].verdict == "accepted"
        assert (
            _gate([_obs(Field.NEXT_EARNINGS_DATE, day=date(2026, 8, 20))])[0].verdict == "accepted"
        )

    def test_text_fields_have_no_unary_checks(self):
        assert _gate([_obs(Field.ANALYST_RATING, text="buy")])[0].verdict == "accepted"

    def test_unary_quarantine_does_not_reach_cross_source(self):
        # The out-of-bounds yahoo price is out before the pairwise check, so
        # the surviving finnhub price is accepted alone (uncorroborated) and
        # picks up primary as the next source in the price priority.
        out = _gate([_price(-5.0), _price(10.99, Source.FINNHUB)])
        bad = _one(out, Field.PRICE, Source.YAHOO)
        good = _one(out, Field.PRICE, Source.FINNHUB)
        assert _codes(bad) == [QuarantineCode.OUT_OF_BOUNDS]  # no disagreement hit stacked on
        assert good.verdict == "accepted"
        assert good.corroborated_by == ()
        assert good.is_primary


class TestParseBoundary:
    def test_parse_failure_passthrough(self):
        failure = _failure()
        out = _gate([], failures=[failure])
        assert len(out) == 1
        gated = out[0]
        assert gated.obs is failure
        assert gated.verdict == "quarantined"
        assert _codes(gated) == [QuarantineCode.UNPARSEABLE]
        assert "N/A garbled" in gated.reasons[0].detail  # raw wire text preserved
        assert not gated.is_primary

    def test_failures_follow_raws_in_input_order(self):
        first, second = _failure(), _failure(field=Field.PE_TTM, raw="—", source=Source.EDGAR)
        out = _gate([_price(10.97)], failures=[first, second])
        assert isinstance(out[0].obs, RawObservation)
        assert out[1].obs is first
        assert out[2].obs is second


class TestPostconditions:
    RAW = [
        _price(10.97),
        _price(10.99, Source.FINNHUB),
        _target(35.0),
        _obs(Field.ANALYST_RATING, text="buy"),
        _obs(Field.NEXT_EARNINGS_DATE, day=date(2026, 7, 1)),  # DATE_IN_PAST
        _obs(Field.MARKET_CAP, num=math.nan),  # NON_FINITE
    ]
    FAILURES = [_failure(field=Field.PEG, raw="?")]

    def test_every_input_is_represented_in_input_order(self):
        out = _gate(self.RAW, failures=self.FAILURES)
        assert len(out) == len(self.RAW) + len(self.FAILURES)
        assert [g.obs for g in out[: len(self.RAW)]] == self.RAW
        assert [g.obs for g in out[len(self.RAW) :]] == self.FAILURES

    def test_at_most_one_primary_per_field_and_it_is_accepted(self):
        out = _gate(self.RAW, failures=self.FAILURES)
        primaries: dict[Field, object] = {}
        for g in out:
            if g.is_primary:
                assert g.verdict == "accepted"
                assert g.obs.field not in primaries
                primaries[g.obs.field] = g
        # every field with any accepted, prioritized observation resolved one
        assert set(primaries) == {Field.PRICE, Field.ANALYST_RATING}

    def test_reasons_nonempty_iff_quarantined(self):
        for g in _gate(self.RAW, failures=self.FAILURES):
            assert (g.verdict == "quarantined") == bool(g.reasons)

    def test_source_outside_priority_is_never_primary(self):
        # MARKET_CAP priority is (yahoo,) — a lone edgar value is accepted
        # but cannot resolve as primary.
        gated = _gate([_obs(Field.MARKET_CAP, num=1e9, source=Source.EDGAR)])[0]
        assert gated.verdict == "accepted"
        assert not gated.is_primary

    def test_empty_input_yields_empty_output(self):
        assert _gate([]) == []

    def test_naive_as_of_raises(self):
        with pytest.raises(ValueError, match="naive"):
            _gate([_price(10.97)], as_of=datetime(2026, 7, 12, 14, 0))
