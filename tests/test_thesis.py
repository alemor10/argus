"""Thesis-drift detection: the parser (config fail-loud boundary), the pure
evaluator, drift-event emission, digest rendering, and store round-trip.

Design invariant under test: Argus never interprets the thesis prose — it only
reports whether the human's declared, checkable conditions still hold. Every
test here operates on structured conditions and gated values, never on text.
"""

from datetime import UTC, date, datetime

import pytest

from argus import changes
from argus.fields import Field, Source
from argus.models import (
    FieldValue,
    Snapshot,
    ThesisDrift,
    Thresholds,
    TickerContext,
)
from argus.thesis import evaluate_thesis_checks, parse_thesis_check

NOW = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)


def _snap(run_id=2, **values) -> Snapshot:
    return Snapshot(
        ticker="NVDA",
        run_id=run_id,
        as_of=NOW,
        values={
            f: FieldValue(field=f, value=v, source=Source.YAHOO, fetched_at=NOW)
            for f, v in values.items()
        },
    )


class TestParse:
    def test_percent_scales_to_fraction(self):
        c = parse_thesis_check("revenue_growth >= 20%")
        assert (c.field, c.op, c.value) == (Field.REVENUE_GROWTH, ">=", pytest.approx(0.20))

    def test_bare_fraction(self):
        assert parse_thesis_check("gross_margin >= 0.65").value == pytest.approx(0.65)

    def test_plain_number(self):
        assert parse_thesis_check("pe_fwd <= 25").value == 25.0

    def test_no_spaces_tolerated(self):
        c = parse_thesis_check("revenue_growth>=20%")
        assert (c.op, c.value) == (">=", pytest.approx(0.20))

    def test_all_numeric_operators(self):
        for op in (">=", "<=", ">", "<", "==", "!="):
            assert parse_thesis_check(f"pe_fwd {op} 25").op == op

    def test_text_equality(self):
        c = parse_thesis_check("analyst_rating == buy")
        assert (c.field, c.op, c.value) == (Field.ANALYST_RATING, "==", "buy")

    def test_text_in_list(self):
        c = parse_thesis_check("analyst_rating in [strong_buy, buy]")
        assert (c.op, c.value) == ("in", ("strong_buy", "buy"))

    def test_text_not_in_list(self):
        c = parse_thesis_check("analyst_rating not in [sell, underperform]")
        assert (c.op, c.value) == ("not_in", ("sell", "underperform"))

    def test_raw_is_normalized_and_preserved(self):
        assert parse_thesis_check("  revenue_growth   >=   20%  ").raw == "revenue_growth >= 20%"

    def test_quoted_list_items_are_unquoted(self):
        # Review finding: a quoted item kept its quote and never matched a
        # bare rating — a `not in ['sell']` guard would silently never fire.
        assert parse_thesis_check("analyst_rating in ['buy', \"strong_buy\"]").value == (
            "buy",
            "strong_buy",
        )
        check = parse_thesis_check("analyst_rating not in ['sell']")
        (r,) = evaluate_thesis_checks((check,), _snap(**{Field.ANALYST_RATING: "sell"}))
        assert r.status == "breached"  # the downgrade guard fires

    @pytest.mark.parametrize("bad", ["pe_fwd >= nan", "pe_fwd <= inf", "pe_fwd >= 1e400"])
    def test_non_finite_targets_are_rejected(self, bad):
        # Review finding: nan/inf targets made every comparison False → a
        # permanent false breach. Reject at the fail-loud boundary.
        with pytest.raises(ValueError):
            parse_thesis_check(bad)

    @pytest.mark.parametrize(
        "raw",
        [
            "foobar >= 5",  # unknown field
            "revenue_growth 20",  # no operator
            "analyst_rating >= 5",  # numeric op on text field
            "revenue_growth in [1, 2]",  # text op on numeric field
            "analyst_rating in buy",  # in without a list
            "revenue_growth >= abc",  # non-number value
            "next_earnings_date >= 2026",  # date field unsupported
            "analyst_rating in []",  # empty list
        ],
    )
    def test_malformed_checks_fail_loudly(self, raw):
        with pytest.raises(ValueError):
            parse_thesis_check(raw)


class TestEvaluate:
    def test_holds_and_breached(self):
        check = parse_thesis_check("revenue_growth >= 20%")
        (holds,) = evaluate_thesis_checks((check,), _snap(**{Field.REVENUE_GROWTH: 0.25}))
        (breach,) = evaluate_thesis_checks((check,), _snap(**{Field.REVENUE_GROWTH: 0.14}))
        assert holds.status == "holds" and holds.observed == pytest.approx(0.25)
        assert breach.status == "breached" and breach.observed == pytest.approx(0.14)

    def test_boundary_is_inclusive(self):
        check = parse_thesis_check("revenue_growth >= 20%")
        (r,) = evaluate_thesis_checks((check,), _snap(**{Field.REVENUE_GROWTH: 0.20}))
        assert r.status == "holds"

    def test_missing_field_is_undeterminable(self):
        check = parse_thesis_check("revenue_growth >= 20%")
        (r,) = evaluate_thesis_checks((check,), _snap())  # field absent
        assert r.status == "undeterminable" and r.observed is None

    def test_text_membership_case_insensitive(self):
        check = parse_thesis_check("analyst_rating in [strong_buy, buy]")
        (holds,) = evaluate_thesis_checks((check,), _snap(**{Field.ANALYST_RATING: "BUY"}))
        (breach,) = evaluate_thesis_checks((check,), _snap(**{Field.ANALYST_RATING: "hold"}))
        assert holds.status == "holds"
        assert breach.status == "breached"

    def test_not_in_holds_when_absent_from_list(self):
        check = parse_thesis_check("analyst_rating not in [sell, underperform]")
        (r,) = evaluate_thesis_checks((check,), _snap(**{Field.ANALYST_RATING: "buy"}))
        assert r.status == "holds"


class TestDriftEvents:
    def _detect(self, baseline, current, checks):
        ctx = TickerContext(ticker="NVDA", thesis="Supercycle.", thresholds=Thresholds(),
                            thesis_checks=tuple(parse_thesis_check(c) for c in checks))
        return changes.detect(
            baseline, current, ctx, [], date(2026, 7, 13), latest_accepted=lambda f: None
        )

    def test_breach_emits_thesis_drift_first_and_newly(self):
        events = self._detect(
            _snap(run_id=1, **{Field.REVENUE_GROWTH: 0.25}),  # held last run
            _snap(run_id=2, **{Field.REVENUE_GROWTH: 0.14}),  # breached now
            ["revenue_growth >= 20%"],
        )
        drift = [e for e in events if isinstance(e, ThesisDrift)]
        assert len(drift) == 1
        assert events[0] is drift[0]  # leads the canonical order
        assert drift[0].newly is True
        assert drift[0].check == "revenue_growth >= 20%"

    def test_continuing_breach_is_not_newly(self):
        events = self._detect(
            _snap(run_id=1, **{Field.REVENUE_GROWTH: 0.10}),  # already breached
            _snap(run_id=2, **{Field.REVENUE_GROWTH: 0.12}),  # still breached
            ["revenue_growth >= 20%"],
        )
        (drift,) = [e for e in events if isinstance(e, ThesisDrift)]
        assert drift.newly is False

    def test_holding_check_emits_nothing(self):
        events = self._detect(
            _snap(run_id=1, **{Field.REVENUE_GROWTH: 0.25}),
            _snap(run_id=2, **{Field.REVENUE_GROWTH: 0.30}),
            ["revenue_growth >= 20%"],
        )
        assert not [e for e in events if isinstance(e, ThesisDrift)]

    def test_undeterminable_check_emits_nothing(self):
        # A quarantined/missing field can't breach — it's surfaced in the
        # watchlist standing line, not as a drift event.
        events = self._detect(_snap(run_id=1), _snap(run_id=2), ["revenue_growth >= 20%"])
        assert not [e for e in events if isinstance(e, ThesisDrift)]


def test_config_parses_and_fails_loud(tmp_path):
    from argus.config import build_contexts, load_watch_config

    good = tmp_path / "w.yaml"
    good.write_text(
        'tickers:\n  - ticker: NVDA\n    thesis: "x"\n'
        '    thesis_checks:\n      - "revenue_growth >= 20%"\n      - "analyst_rating in [buy]"\n',
        encoding="utf-8",
    )
    (ctx,) = build_contexts(load_watch_config(good))
    assert len(ctx.thesis_checks) == 2
    assert ctx.thesis_checks[0].field is Field.REVENUE_GROWTH

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        'tickers:\n  - ticker: NVDA\n    thesis: "x"\n'
        '    thesis_checks:\n      - "nonsense !! field"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="NVDA: bad thesis check"):
        build_contexts(load_watch_config(bad))


def test_thesis_checks_round_trip_through_the_store(tmp_path):
    from argus.gates import GatedObservation
    from argus.models import RawObservation
    from argus.store import connect, migrate, queries, writer

    con = connect(tmp_path / "argus.db")
    migrate(con)
    con.execute(
        "INSERT INTO runs (kind, started_at, app_version, status, finished_at) "
        "VALUES ('watch', ?, 't', 'complete', ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    ctx = TickerContext(
        ticker="NVDA",
        thesis="Supercycle.",
        thresholds=Thresholds(),
        thesis_checks=(parse_thesis_check("revenue_growth >= 20%"),),
    )
    gated = GatedObservation(
        obs=RawObservation(
            ticker="NVDA", field=Field.REVENUE_GROWTH, value_num=0.14,
            source=Source.YAHOO, fetched_at=NOW,
        ),
        verdict="accepted",
        is_primary=True,
    )
    writer.write_ticker_result(
        con, run_id=1, context=ctx, gated=[gated], actions=[], source_health=[], status="ok"
    )
    report = queries.run_report(con, 1)
    rebuilt = report.tickers[0].context.thesis_checks
    assert len(rebuilt) == 1 and rebuilt[0].raw == "revenue_growth >= 20%"
    con.close()


def test_digest_renders_drift_and_standing():
    from argus.digest import render
    from argus.models import RunReport, TickerReport

    breached = _snap(run_id=2, **{Field.REVENUE_GROWTH: 0.14, Field.PRICE: 100.0})
    ctx = TickerContext(
        ticker="NVDA",
        thesis="Datacenter supercycle.",
        thresholds=Thresholds(),
        thesis_checks=(parse_thesis_check("revenue_growth >= 20%"),),
    )
    drift = ThesisDrift(
        ticker="NVDA", check="revenue_growth >= 20%", field=Field.REVENUE_GROWTH,
        observed=0.14, thesis="Datacenter supercycle.", newly=True,
    )
    report = RunReport(
        run_id=2, kind="watch", as_of=NOW, status="complete",
        tickers=(TickerReport(context=ctx, status="ok", snapshot=breached, events=(drift,)),),
    )
    text = render(report)
    assert "THESIS DRIFT" in text
    assert "revenue_growth >= 20%" in text
    assert "1/1 checks BREACHED" in text
