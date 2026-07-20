"""Sentinel-secret regression tests: a fake token/webhook in an error string
must never survive redaction into a stored note, report, or echo."""

from argus.redact import redact

_TOKEN = "sk_live_ABC123secretXYZ"
_WEBHOOK = "https://discord.com/api/webhooks/123456789/verysecretwebhooktoken"


def test_token_query_param_is_redacted():
    err = f"httpx.HTTPStatusError: GET https://finnhub.io/api/v1/quote?symbol=AAPL&token={_TOKEN} 429"
    out = redact(err)
    assert _TOKEN not in out
    assert "token=REDACTED" in out
    assert "symbol=AAPL" in out  # non-secret context preserved


def test_discord_webhook_url_is_redacted():
    err = f"ConnectError: POST {_WEBHOOK} failed"
    out = redact(err)
    assert "verysecretwebhooktoken" not in out
    assert "webhooks/REDACTED" in out


def test_api_key_variants_and_slack_are_redacted():
    for probe in (
        "api_key=SUPERSECRET",
        "apikey=SUPERSECRET",
        "key=SUPERSECRET",
        "https://hooks.slack.com/services/T00/B00/SUPERSECRET",
    ):
        assert "SUPERSECRET" not in redact(f"error: {probe} boom")


def test_none_and_clean_text_are_safe():
    assert redact(None) == ""
    assert redact("plain error, no secrets") == "plain error, no secrets"


def test_delivery_failure_string_is_redacted_at_the_sink():
    """CompositeSink must scrub a webhook that a failing sink leaks via its
    exception — the DeliveryError message is echoed and can be stored."""
    from argus.digest import CompositeSink, DeliveryError

    class _LeakySink:
        def write(self, markdown, *, run_id, as_of, attachments=()):
            raise RuntimeError(f"POST {_WEBHOOK} -> 401")

    import pytest

    with pytest.raises(DeliveryError) as excinfo:
        CompositeSink(_LeakySink()).write("x", run_id=1, as_of=__import__("datetime").date(2026, 7, 20))
    assert "verysecretwebhooktoken" not in str(excinfo.value)
    assert "REDACTED" in str(excinfo.value)
