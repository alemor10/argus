"""Sentinel-secret integration tests — the security boundary's contract.

A fake Finnhub token and Discord webhook are injected into failure paths at
every layer (source fetch, delivery sink, run notes). The sentinels must then
be findable NOWHERE: not in any SQLite row (full dump), not in the rendered
digest, not in RunOutcome fields, not in CLI output. Sanitization is asserted
at the persistence/output boundaries, not just inside individual providers —
a NEW leaky provider added tomorrow is still covered.
"""

from datetime import UTC, date, datetime

import pytest

from argus import engine
from argus.digest import CompositeSink, DeliveryError, render
from argus.fields import Field, Source
from argus.gates import DEFAULT_PROFILE
from argus.models import RawObservation, TickerContext
from argus.sources.base import FetchResult
from argus.store import connect, migrate, queries, writer

TOKEN = "sk_sentinel_FINNHUB_TOKEN_a1b2c3"
WEBHOOK = "https://discord.com/api/webhooks/1234567890/sentinel_WEBHOOK_SECRET_x9y8z7"
SENTINELS = (TOKEN, "sentinel_WEBHOOK_SECRET_x9y8z7")

RUN_AT = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


class _LeakySource:
    """A provider whose failure embeds secret-bearing URLs — simulating a raw
    httpx error escaping an adapter without provider-side redaction."""

    source_id = Source.FINNHUB

    def covers(self, ticker: str) -> bool:
        return True

    def fetch(self, ticker: str) -> FetchResult:
        raise RuntimeError(
            f"HTTPStatusError: GET https://finnhub.io/api/v1/quote?symbol={ticker}"
            f"&token={TOKEN} 429; also POST {WEBHOOK} failed"
        )


class _HealthySource:
    source_id = Source.YAHOO

    def covers(self, ticker: str) -> bool:
        return True

    def fetch(self, ticker: str) -> FetchResult:
        return FetchResult(
            observations=(
                RawObservation(
                    ticker=ticker, field=Field.PRICE, value_num=100.0,
                    source=Source.YAHOO, fetched_at=RUN_AT,
                ),
            )
        )


class _LeakyChannel:
    """A delivery channel whose failure embeds the webhook URL — exactly what
    a real httpx.HTTPStatusError from a Discord webhook POST looks like."""

    def write(self, markdown, *, run_id, as_of, attachments=()):
        raise RuntimeError(f"Client error '401 Unauthorized' for url '{WEBHOOK}'")


class _FileStub:
    def __init__(self):
        self.markdown = None

    def write(self, markdown, *, run_id, as_of, attachments=()):
        self.markdown = markdown
        return None


@pytest.fixture()
def con(tmp_path):
    con = connect(tmp_path / "argus.db")
    migrate(con)
    yield con
    con.close()


def _db_dump(con) -> str:
    return "\n".join(con.iterdump())


def _run_leaky(con):
    # The ALWAYS-policy shape: channels composed with the file sink. The
    # channel fails with a webhook-bearing message on every write.
    sink = _FileStub()
    outcome = engine.run(
        [TickerContext(ticker="NVDA")],
        con=con,
        sources=[_HealthySource(), _LeakySource()],
        profile=DEFAULT_PROFILE,
        sink=CompositeSink(sink, _LeakyChannel()),
        as_of=RUN_AT,
        today=RUN_AT.date(),
        app_version="sentinel-test",
    )
    return outcome, sink


def test_sentinel_never_reaches_sqlite(con):
    _run_leaky(con)
    dump = _db_dump(con)
    for sentinel in SENTINELS:
        assert sentinel not in dump
    # ...while the non-secret context (source name) survives for debugging.
    assert "REDACTED" in dump


def test_sentinel_never_reaches_the_rendered_digest(con):
    outcome, sink = _run_leaky(con)
    assert sink.markdown is not None
    for sentinel in SENTINELS:
        assert sentinel not in sink.markdown
    regenerated = render(queries.run_report(con, outcome.run_id))
    for sentinel in SENTINELS:
        assert sentinel not in regenerated


def test_sentinel_never_reaches_run_outcome(con):
    outcome, _ = _run_leaky(con)
    for field in (outcome.delivery_error, outcome.attachment_error):
        for sentinel in SENTINELS:
            assert sentinel not in (field or "")


def test_run_notes_are_redacted_at_the_writer_boundary(con):
    """A note appended by ANY future caller with raw provider text is scrubbed
    at persistence — the boundary holds even for code that forgets redact()."""
    run_id = writer.begin_run(con, kind="watch", started_at=RUN_AT, app_version="t")
    writer.finish_run(con, run_id=run_id, status="complete", finished_at=RUN_AT)
    writer.append_run_note(
        con, run_id=run_id, note=f"calendar unavailable: GET ...?token={TOKEN} 429"
    )
    dump = _db_dump(con)
    assert TOKEN not in dump
    assert "token=REDACTED" in dump


def test_finish_run_notes_are_redacted(con):
    run_id = writer.begin_run(con, kind="scout", started_at=RUN_AT, app_version="t")
    writer.finish_run(
        con, run_id=run_id, status="failed", finished_at=RUN_AT,
        notes=f"screener unavailable: {WEBHOOK} refused",
    )
    dump = _db_dump(con)
    assert "sentinel_WEBHOOK_SECRET_x9y8z7" not in dump


def test_channel_failure_is_caught_and_redacted_not_raised(con):
    """A failing channel must surface as a redacted DeliveryError inside
    RunOutcome — never propagate as a raw traceback (which would print the
    webhook URL to stderr)."""
    outcome, _ = _run_leaky(con)
    assert outcome.delivery_error is not None
    assert "REDACTED" in outcome.delivery_error
    for sentinel in SENTINELS:
        assert sentinel not in outcome.delivery_error


def test_events_only_single_channel_is_composite_wrapped():
    """The _compose_sinks regression: under events-only with exactly one
    channel the gated sink must be CompositeSink-wrapped (the redaction
    boundary), never the bare channel."""
    from argus.cli import DeliverPolicy, _compose_sinks

    file_stub = _FileStub()
    _, gated, gate = _compose_sinks(file_stub, [_LeakyChannel()], DeliverPolicy.EVENTS_ONLY)
    assert isinstance(gated, CompositeSink)
    assert gate is True
    with pytest.raises(DeliveryError) as excinfo:  # not RuntimeError: wrapped
        gated.write("x", run_id=1, as_of=date(2026, 7, 20))
    for sentinel in SENTINELS:
        assert sentinel not in str(excinfo.value)


def test_composite_sink_redacts_every_channel_failure():
    with pytest.raises(DeliveryError) as excinfo:
        CompositeSink(_LeakyChannel(), _LeakyChannel()).write(
            "x", run_id=1, as_of=date(2026, 7, 20)
        )
    message = str(excinfo.value)
    for sentinel in SENTINELS:
        assert sentinel not in message
    assert message.count("REDACTED") >= 2
