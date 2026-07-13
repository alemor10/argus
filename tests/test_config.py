import pytest
from pydantic import ValidationError

from argus.config import build_contexts, load_watch_config, resolve_paths, resolve_secrets

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
    assert paths.db == tmp_path / "argus.db"
    assert paths.reports == tmp_path / "reports"


def test_resolve_paths_explicit_overrides_win(tmp_path):
    db = tmp_path / "elsewhere" / "state.db"
    paths = resolve_paths(tmp_path, db=db)
    assert paths.db == db
    assert paths.watchlist == tmp_path / "watchlist.yaml"
