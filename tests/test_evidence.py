"""The evidence contract — the four-state backing label, the screen-exit
conditions read back from persisted reason strings, and the factual data flags.

Every assertion here guards the hard constraint: these outputs are provenance
and thresholds restated as fact, never a forecast or a judgement. The flags say
"D/E 0.95 is within 5% of the 1.0 ceiling", never "this is risky".
"""

from datetime import UTC, datetime

from argus import evidence
from argus.fields import Field, Source
from argus.models import FieldValue, Snapshot

AT = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


def _snapshot(values=None, quarantined=None) -> Snapshot:
    return Snapshot(
        ticker="AAA",
        run_id=1,
        as_of=AT,
        values=values or {},
        quarantined=quarantined or {},
    )


def _fv(field, value, *, corroborated=()) -> FieldValue:
    return FieldValue(
        field=field, value=value, source=Source.YAHOO, fetched_at=AT,
        corroborated_by=tuple(corroborated),
    )


# --- evidence_state ----------------------------------------------------------


class TestEvidenceState:
    def test_corroborated_when_a_second_source_agreed(self):
        snap = _snapshot({Field.PRICE: _fv(Field.PRICE, 100.0, corroborated=(Source.FINNHUB,))})
        assert evidence.evidence_state(Field.PRICE, snap, {}) == "corroborated"

    def test_single_source_when_accepted_without_corroboration(self):
        snap = _snapshot({Field.PRICE: _fv(Field.PRICE, 100.0)})
        assert evidence.evidence_state(Field.PRICE, snap, {}) == "single-source"

    def test_claim_only_when_screener_claimed_but_gates_did_not_confirm(self):
        snap = _snapshot()  # nothing accepted
        metrics = {"roe_pct": 22.0}
        assert evidence.evidence_state(Field.ROE, snap, metrics) == "claim-only"

    def test_missing_when_no_accepted_value_and_no_claim(self):
        assert evidence.evidence_state(Field.ROE, _snapshot(), {}) == "missing"
        assert evidence.evidence_state(Field.ROE, _snapshot(), {"roe_pct": None}) == "missing"


# --- screen_exit_conditions --------------------------------------------------


class TestScreenExit:
    def test_reads_thresholds_back_from_reason_strings(self):
        reasons = {
            "forward_pe": "fwd P/E 15.0 ≤ 25",
            "revenue_growth": "rev growth +15.0% ≥ 10%",
            "gross_margin": "gross margin 55.0% ≥ 40%",
            "operating_margin": "op margin 20.0% ≥ 12%",
            "roe": "ROE 25.0% ≥ 15%",
            "debt_to_equity": "D/E 0.40 ≤ 1",
            "value_trap": "EPS trend +25.0% > -30%",
        }
        assert evidence.screen_exit_conditions(reasons) == [
            "forward P/E above 25",
            "revenue growth below 10%",
            "gross margin below 40%",
            "operating margin below 12%",
            "ROE below 15%",
            "D/E above 1",
            "EPS trend at or below -30%",
        ]

    def test_empty_when_no_reasons(self):
        assert evidence.screen_exit_conditions({}) == []


# --- data_flags --------------------------------------------------------------


class TestDataFlags:
    def test_no_flags_for_a_cleanly_corroborated_name(self):
        # Comfortably inside every boundary, price cross-checked, nothing claimed.
        reasons = {"forward_pe": "fwd P/E 15.0 ≤ 25", "roe": "ROE 25.0% ≥ 15%"}
        snap = _snapshot(
            {
                Field.PRICE: _fv(Field.PRICE, 100.0, corroborated=(Source.FINNHUB,)),
                Field.PE_FWD: _fv(Field.PE_FWD, 15.0, corroborated=(Source.FINNHUB,)),
                Field.ROE: _fv(Field.ROE, 0.25),
            }
        )
        assert evidence.data_flags(reasons, {}, snap) == []

    def test_near_ceiling_is_a_proximity_fact(self):
        reasons = {"forward_pe": "fwd P/E 24.3 ≤ 25"}
        snap = _snapshot({Field.PE_FWD: _fv(Field.PE_FWD, 24.3)})
        flags = evidence.data_flags(reasons, {}, snap)
        assert any("forward P/E 24.3 is within" in f and "25 screen ceiling" in f for f in flags)

    def test_near_floor_scales_a_fraction_to_percent(self):
        # Verified gross margin 0.41 (=41.0%) sits just above the 40% floor.
        reasons = {"gross_margin": "gross margin 41.0% ≥ 40%"}
        snap = _snapshot({Field.GROSS_MARGIN: _fv(Field.GROSS_MARGIN, 0.41)})
        flags = evidence.data_flags(reasons, {}, snap)
        assert any("gross margin 41.0% is within" in f and "40% screen floor" in f for f in flags)

    def test_claim_only_core_metric_is_flagged(self):
        reasons = {"roe": "ROE 22.0% ≥ 15%"}
        snap = _snapshot()  # ROE not accepted
        flags = evidence.data_flags(reasons, {"roe_pct": 22.0}, snap)
        assert any(f.startswith("screener-claimed only") and "ROE" in f for f in flags)

    def test_single_source_price_is_flagged(self):
        snap = _snapshot({Field.PRICE: _fv(Field.PRICE, 100.0)})  # no corroboration
        flags = evidence.data_flags({}, {}, snap)
        assert any("price is single-source" in f for f in flags)

    def test_quarantined_core_field_is_flagged(self):
        from argus.fields import QuarantineCode
        from argus.models import QuarantineHit

        hit = (QuarantineHit(code=QuarantineCode.OUT_OF_BOUNDS, detail="out of band"),)
        snap = _snapshot(quarantined={Field.PE_FWD: hit})
        flags = evidence.data_flags({}, {}, snap)
        assert any(f.startswith("quarantined this run") and "forward P/E" in f for f in flags)

    def test_no_snapshot_yields_no_crash(self):
        assert evidence.data_flags({"roe": "ROE 22.0% ≥ 15%"}, {}, None) == []
