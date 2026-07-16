import pytest
from pydantic import ValidationError

from argus.config import (
    build_contexts,
    build_macro_contexts,
    ensure_no_overlap,
    load_macro_config,
    load_macro_config_text,
    load_watch_config,
    resolve_paths,
    resolve_secrets,
)
from argus.fields import Field, Source

WATCHLIST = """\
defaults:
  price_move_pct: 5.0
  target_move_pct: 10.0
  earnings_within_days: 7

tickers:
  - ticker: NVDA
    thesis: "Datacenter capex supercycle; CUDA moat."
    thresholds: { price_move_pct: 8.0 }
  - ticker: NTDOY
    thesis: "Switch 2 cycle + IP monetization."
"""


def _load(tmp_path, text=WATCHLIST):
    path = tmp_path / "watchlist.yaml"
    path.write_text(text, encoding="utf-8")
    return load_watch_config(path)


def test_per_ticker_overrides_merge_over_defaults(tmp_path):
    contexts = build_contexts(_load(tmp_path))
    by_ticker = {c.ticker: c for c in contexts}
    assert by_ticker["NVDA"].thresholds.price_move_pct == 8.0
    assert by_ticker["NVDA"].thresholds.target_move_pct == 10.0  # untouched default
    assert by_ticker["NTDOY"].thresholds.price_move_pct == 5.0
    assert by_ticker["NTDOY"].thesis == "Switch 2 cycle + IP monetization."


def test_unknown_threshold_key_fails_loudly(tmp_path):
    bad = WATCHLIST.replace("price_move_pct: 8.0", "price_mov_pct: 8.0")  # typo
    with pytest.raises(ValidationError):
        build_contexts(_load(tmp_path, bad))


def test_ticker_is_required(tmp_path):
    with pytest.raises(ValidationError):
        _load(tmp_path, "tickers:\n  - thesis: 'no ticker'\n")


def test_empty_watchlist_is_valid_and_empty(tmp_path):
    assert build_contexts(_load(tmp_path, "")) == []


def test_duplicate_tickers_fail_loudly(tmp_path):
    duplicated = WATCHLIST + "  - ticker: NVDA\n    thesis: 'again'\n"
    with pytest.raises(ValueError, match="duplicate ticker.*NVDA"):
        build_contexts(_load(tmp_path, duplicated))


def test_resolve_secrets_reads_env_and_defaults_to_none(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("ARGUS_CONTACT_EMAIL", raising=False)
    assert resolve_secrets().finnhub_api_key is None
    monkeypatch.setenv("FINNHUB_API_KEY", "k123")
    monkeypatch.setenv("ARGUS_CONTACT_EMAIL", "me@example.com")
    secrets = resolve_secrets()
    assert secrets.finnhub_api_key == "k123"
    assert secrets.edgar_contact_email == "me@example.com"


def test_resolve_paths_defaults_under_root(tmp_path):
    paths = resolve_paths(tmp_path)
    assert paths.watchlist == tmp_path / "watchlist.yaml"
    assert paths.macro == tmp_path / "macro.yaml"
    assert paths.db == tmp_path / "argus.db"
    assert paths.reports == tmp_path / "reports"


MACRO = """\
series:
  - symbol: "^VIX"
    label: "VIX"
    alert_move: 3.0
    alert_when: ["value >= 25"]
  - symbol: "CPIAUCSL"
    source: fred
    transform: yoy_pct
    label: "CPI inflation (YoY)"
    unit: "%"
    alert_when: ["value >= 4"]
"""


class TestMacroConfig:
    def _contexts(self, text=MACRO):
        return build_macro_contexts(load_macro_config_text(text))

    def test_market_and_econ_series_build_macro_contexts(self):
        vix, cpi = self._contexts()
        assert vix.ticker == "^VIX" and vix.macro is not None
        assert vix.macro.source is Source.YAHOO
        assert vix.macro.alert_move == 3.0
        assert vix.macro.alert_on_release is False  # market default
        assert cpi.macro.source is Source.FRED
        assert cpi.macro.transform == "yoy_pct"
        assert cpi.macro.alert_on_release is True  # a new print IS the alert

    def test_value_alias_maps_to_the_series_field_but_renders_human(self):
        vix, cpi = self._contexts()
        [vix_line] = vix.macro.alert_when
        assert vix_line.field is Field.PRICE
        assert vix_line.raw == "value >= 25"  # the human's spelling survives
        [cpi_line] = cpi.macro.alert_when
        assert cpi_line.field is Field.ECON_VALUE
        assert cpi_line.raw == "value >= 4"

    def test_line_naming_a_foreign_field_fails_loudly(self):
        bad = MACRO.replace('alert_when: ["value >= 25"]', 'alert_when: ["gross_margin >= 25"]')
        with pytest.raises(ValueError, match="watch the series' value"):
            self._contexts(bad)

    def test_price_line_on_an_econ_series_fails_loudly(self):
        bad = MACRO.replace('alert_when: ["value >= 4"]', 'alert_when: ["price >= 4"]')
        with pytest.raises(ValueError, match="CPIAUCSL"):
            self._contexts(bad)

    def test_duplicate_symbols_fail_loudly(self):
        dup = MACRO + '  - symbol: "^vix"\n    label: "VIX again"\n'
        with pytest.raises(ValueError, match="duplicate macro series"):
            self._contexts(dup)

    def test_unknown_key_fails_loudly(self):
        with pytest.raises(ValidationError):
            self._contexts(MACRO.replace("alert_move:", "alert_mov:"))

    def test_transform_on_a_market_series_fails_loudly(self):
        bad = MACRO.replace('symbol: "^VIX"\n    label: "VIX"', 'symbol: "^VIX"\n    transform: yoy_pct\n    label: "VIX"')
        with pytest.raises(ValueError, match="transforms only apply to fred"):
            self._contexts(bad)

    def test_bad_sanity_fails_loudly(self):
        bad = MACRO.replace('label: "VIX"', 'label: "VIX"\n    sanity: [30, 10]')
        with pytest.raises(ValueError, match="low, high"):
            self._contexts(bad)

    def test_missing_file_is_feature_off(self, tmp_path):
        config = load_macro_config(tmp_path / "macro.yaml")
        assert config.series == ()
        assert build_macro_contexts(config) == []

    def test_watchlist_overlap_is_refused(self, tmp_path):
        watch = build_contexts(_load(tmp_path))  # NVDA + NTDOY
        macro = build_macro_contexts(
            load_macro_config_text('series:\n  - symbol: "nvda"\n    label: "NVDA?!"\n')
        )
        with pytest.raises(ValueError, match="both watchlist.yaml and macro.yaml"):
            ensure_no_overlap(watch, macro)

    def test_disjoint_lists_concatenate_watch_first(self, tmp_path):
        watch = build_contexts(_load(tmp_path))
        macro = self._contexts()
        merged = ensure_no_overlap(watch, macro)
        assert [c.ticker for c in merged] == ["NVDA", "NTDOY", "^VIX", "CPIAUCSL"]


def test_resolve_paths_explicit_overrides_win(tmp_path):
    db = tmp_path / "elsewhere" / "state.db"
    paths = resolve_paths(tmp_path, db=db)
    assert paths.db == db
    assert paths.watchlist == tmp_path / "watchlist.yaml"
