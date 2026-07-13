"""Email delivery: the sink, the composite fan-out, config resolution, and
the exit-code contract — an undelivered digest on a headless box is an unseen
digest, so delivery failure must be loud (exit 1), unlike partial data."""

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from argus import engine
from argus.config import resolve_email_config
from argus.digest import CompositeSink, DeliveryError, EmailDigestSink, FileDigestSink
from argus.gates import DEFAULT_PROFILE

AS_OF = date(2026, 7, 13)


class _FakeSMTP:
    """Stands in for smtplib.SMTP_SSL / SMTP; records the interaction."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.logged_in = None
        self.sent = []
        self.started_tls = False
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.logged_in = (username, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeSMTP.instances = []


def _sink(**overrides) -> EmailDigestSink:
    kwargs = dict(
        host="smtp.gmail.com",
        port=465,
        username="me@gmail.com",
        password="app-password",
        sender="me@gmail.com",
        recipient="me@gmail.com",
    )
    kwargs.update(overrides)
    return EmailDigestSink(**kwargs)


class TestEmailDigestSink:
    def test_sends_over_implicit_tls_on_465(self):
        with patch("smtplib.SMTP_SSL", _FakeSMTP):
            result = _sink().write("# digest body", run_id=7, as_of=AS_OF)
        assert result is None  # pathless sink
        smtp = _FakeSMTP.instances[0]
        assert (smtp.host, smtp.port) == ("smtp.gmail.com", 465)
        assert smtp.logged_in == ("me@gmail.com", "app-password")
        [message] = smtp.sent
        assert message["Subject"] == "Argus digest — 2026-07-13 — run 7"
        assert message["To"] == "me@gmail.com"
        assert "# digest body" in message.get_content()

    def test_starttls_on_587(self):
        with patch("smtplib.SMTP", _FakeSMTP):
            _sink(port=587).write("body", run_id=1, as_of=AS_OF)
        smtp = _FakeSMTP.instances[0]
        assert smtp.started_tls
        assert smtp.sent


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestDiscordDigestSink:
    DIGEST = (
        "# Argus watch digest — run 9 — 2026-07-13\n\n"
        "Status: complete.\n\n"
        "## Changes\n\n### NVDA\n\n_thesis line_\n\n- Price 170.00 → 181.25 (+6.6%)\n\n"
        "## Watchlist\n\n### NVDA\n\n- Price: 181.25\n"
    )

    def _post(self, captured):
        def fake_post(url, *, data, files, timeout):
            captured.update(url=url, data=data, files=files, timeout=timeout)
            return _FakeResponse()

        return fake_post

    def test_headline_plus_full_digest_attachment(self):
        from argus.digest import DiscordDigestSink

        captured = {}
        with patch("httpx.post", self._post(captured)):
            result = DiscordDigestSink("https://discord.example/webhook").write(
                self.DIGEST, run_id=9, as_of=AS_OF
            )
        assert result is None  # pathless sink
        payload = __import__("json").loads(captured["data"]["payload_json"])
        assert "Argus watch digest — run 9" in payload["content"]
        assert "Status: complete." in payload["content"]
        assert "+6.6%" in payload["content"]  # the Changes section is the hook
        assert "Watchlist" not in payload["content"]  # detail stays in the attachment
        assert payload["allowed_mentions"] == {"parse": []}
        filename, content, mime = captured["files"]["files[0]"]
        assert filename == "digest-2026-07-13-run9.md"
        assert content == self.DIGEST.encode("utf-8")

    def test_headline_respects_discord_message_limit(self):
        from argus.digest import DiscordDigestSink, _discord_headline

        bullets = "\n".join(f"- change number {i} with some padding text" for i in range(200))
        digest = f"# Argus watch digest — run 9 — 2026-07-13\n\nStatus: complete.\n\n## Changes\n\n{bullets}\n"
        headline = _discord_headline(digest)
        assert len(headline) <= 2000
        assert "full digest attached" in headline

        captured = {}
        with patch("httpx.post", self._post(captured)):
            DiscordDigestSink("https://discord.example/webhook").write(
                digest, run_id=9, as_of=AS_OF
            )
        payload = __import__("json").loads(captured["data"]["payload_json"])
        assert len(payload["content"]) <= 2000

    def test_http_error_raises(self):
        from argus.digest import DiscordDigestSink

        with patch("httpx.post", return_value=_FakeResponse(status_code=404)):
            with pytest.raises(RuntimeError, match="HTTP 404"):
                DiscordDigestSink("https://discord.example/webhook").write(
                    self.DIGEST, run_id=9, as_of=AS_OF
                )


class _BoomSink:
    def write(self, markdown, *, run_id, as_of):
        raise ConnectionError("mail server down")


class TestCompositeSink:
    def test_returns_the_file_path_when_all_succeed(self, tmp_path):
        with patch("smtplib.SMTP_SSL", _FakeSMTP):
            path = CompositeSink(FileDigestSink(tmp_path), _sink()).write(
                "body", run_id=3, as_of=AS_OF
            )
        assert isinstance(path, Path) and path.exists()
        assert _FakeSMTP.instances[0].sent

    def test_file_copy_survives_a_dead_mail_server(self, tmp_path):
        with pytest.raises(DeliveryError) as excinfo:
            CompositeSink(FileDigestSink(tmp_path), _BoomSink()).write(
                "body", run_id=3, as_of=AS_OF
            )
        assert excinfo.value.digest_path is not None
        assert excinfo.value.digest_path.exists()  # every sink was attempted
        assert "mail server down" in str(excinfo.value)

    def test_all_sinks_attempted_even_when_the_first_fails(self, tmp_path):
        with pytest.raises(DeliveryError) as excinfo:
            CompositeSink(_BoomSink(), FileDigestSink(tmp_path)).write(
                "body", run_id=3, as_of=AS_OF
            )
        assert excinfo.value.digest_path is not None  # file sink still ran


class TestResolveEmailConfig:
    def test_off_when_recipient_unset(self, monkeypatch):
        monkeypatch.delenv("ARGUS_EMAIL_TO", raising=False)
        assert resolve_email_config() is None

    def test_half_configured_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("ARGUS_EMAIL_TO", "me@gmail.com")
        monkeypatch.delenv("ARGUS_SMTP_USER", raising=False)
        monkeypatch.delenv("ARGUS_SMTP_PASSWORD", raising=False)
        with pytest.raises(ValueError, match="half-configured"):
            resolve_email_config()

    def test_gmail_defaults(self, monkeypatch):
        monkeypatch.setenv("ARGUS_EMAIL_TO", "me@gmail.com")
        monkeypatch.setenv("ARGUS_SMTP_USER", "me@gmail.com")
        monkeypatch.setenv("ARGUS_SMTP_PASSWORD", "app-password")
        monkeypatch.delenv("ARGUS_SMTP_HOST", raising=False)
        monkeypatch.delenv("ARGUS_SMTP_PORT", raising=False)
        monkeypatch.delenv("ARGUS_EMAIL_FROM", raising=False)
        config = resolve_email_config()
        assert (config.host, config.port) == ("smtp.gmail.com", 465)
        assert config.sender == "me@gmail.com"


def test_engine_discloses_delivery_failure_instead_of_crashing(tmp_path):
    """DeliveryError from the sink becomes RunOutcome.delivery_error with the
    surviving file path — never an exception out of engine.run, never
    silence."""
    from argus.store import connect, migrate

    con = connect(tmp_path / "argus.db")
    migrate(con)
    try:
        outcome = engine.run(
            [],  # empty watchlist: vacuously complete run, digest still attempted
            con=con,
            sources=[],
            profile=DEFAULT_PROFILE,
            sink=CompositeSink(FileDigestSink(tmp_path / "reports"), _BoomSink()),
            as_of=datetime(2026, 7, 13, 14, 0, tzinfo=UTC),
            today=AS_OF,
            app_version="delivery-test",
        )
    finally:
        con.close()
    assert outcome.status == "complete"
    assert outcome.delivery_error is not None
    assert "mail server down" in outcome.delivery_error
    assert outcome.digest_path is not None and outcome.digest_path.exists()
