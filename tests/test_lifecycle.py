"""Publication lifecycle contract: collection and publication are tracked
separately; every phase transition is persisted; a failure at any phase
leaves a recorded, recoverable state; overlapping runs are refused."""

from datetime import UTC, datetime

import pytest

from argus import engine
from argus.digest import CompositeSink, DeliveryError
from argus.fields import Field, Source
from argus.gates import DEFAULT_PROFILE
from argus.locking import LockHeldError, run_lock
from argus.models import RawObservation, TickerContext
from argus.sources.base import FetchResult
from argus.store import connect, migrate, writer

RUN_AT = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


class _Source:
    source_id = Source.YAHOO

    def covers(self, ticker):
        return True

    def fetch(self, ticker):
        return FetchResult(
            observations=(
                RawObservation(
                    ticker=ticker, field=Field.PRICE, value_num=100.0,
                    source=Source.YAHOO, fetched_at=RUN_AT,
                ),
            )
        )


class _FileSink:
    def __init__(self, fail=False):
        self.fail = fail
        self.writes = 0

    def write(self, markdown, *, run_id, as_of, attachments=()):
        if self.fail:
            raise OSError("disk full")
        self.writes += 1
        return None


class _Channel:
    def __init__(self, fail=False):
        self.fail = fail
        self.writes = 0

    def write(self, markdown, *, run_id, as_of, attachments=()):
        if self.fail:
            raise RuntimeError("HTTP 502 from webhook")
        self.writes += 1
        return None


@pytest.fixture()
def con(tmp_path):
    con = connect(tmp_path / "argus.db")
    migrate(con)
    yield con
    con.close()


def _publication(con, run_id):
    row = con.execute(
        "SELECT publication_status, publication_error, published_at "
        "FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return row["publication_status"], row["publication_error"], row["published_at"]


def _run(con, *, sink=None, gated_sink=None, gate_channels=True):
    return engine.run(
        [TickerContext(ticker="NVDA")],
        con=con,
        sources=[_Source()],
        profile=DEFAULT_PROFILE,
        sink=sink if sink is not None else _FileSink(),
        as_of=RUN_AT,
        today=RUN_AT.date(),
        app_version="lifecycle-test",
        gated_sink=gated_sink,
        gate_channels=gate_channels,
    )


def test_file_only_when_no_channels(con):
    outcome = _run(con)
    status, error, at = _publication(con, outcome.run_id)
    assert status == "file_only"
    assert error is None
    assert at == RUN_AT.isoformat()  # deterministic default now() = as_of


def test_delivered_when_channels_succeed(con):
    channel = _Channel()
    outcome = _run(con, gated_sink=CompositeSink(channel), gate_channels=False)
    status, error, _ = _publication(con, outcome.run_id)
    assert status == "delivered"
    assert error is None
    assert channel.writes == 1
    assert outcome.delivery_error is None


def test_delivery_failed_is_persisted_with_safe_cause(con):
    outcome = _run(con, gated_sink=CompositeSink(_Channel(fail=True)), gate_channels=False)
    status, error, _ = _publication(con, outcome.run_id)
    assert status == "delivery_failed"
    assert error is not None and "502" in error
    assert outcome.delivery_error is not None  # exit-code path still fires


def test_artifact_failed_when_file_write_dies(con):
    """A file-sink failure is a recorded lifecycle state, not a crash — and
    channels are NOT attempted (never deliver what did not commit)."""
    channel = _Channel()
    outcome = _run(
        con, sink=_FileSink(fail=True), gated_sink=CompositeSink(channel), gate_channels=False
    )
    status, error, _ = _publication(con, outcome.run_id)
    assert status == "artifact_failed"
    assert error is not None and "disk full" in error
    assert channel.writes == 0
    assert outcome.delivery_error is not None


def test_events_only_skip_rests_at_file_only(con):
    """First run carries no new events: the gate skips channels, the file
    record is the whole publication, disclosed in the digest note."""
    channel = _Channel()
    outcome = _run(con, gated_sink=CompositeSink(channel), gate_channels=True)
    status, _, _ = _publication(con, outcome.run_id)
    assert status == "file_only"
    assert channel.writes == 0
    note = con.execute(
        "SELECT notes FROM runs WHERE run_id = ?", (outcome.run_id,)
    ).fetchone()["notes"]
    assert "delivery skipped" in note


def test_failed_watch_run_rests_at_assembled(con):
    """A watch run whose every fetch died produces no digest: the lifecycle
    honestly shows it stopped at 'assembled' (collected, never published)."""

    class _DeadSource:
        source_id = Source.YAHOO

        def covers(self, ticker):
            return True

        def fetch(self, ticker):
            raise RuntimeError("HTTP 502")

    sink = _FileSink()
    outcome = engine.run(
        [TickerContext(ticker="NVDA")],
        con=con,
        sources=[_DeadSource()],
        profile=DEFAULT_PROFILE,
        sink=sink,
        as_of=RUN_AT,
        today=RUN_AT.date(),
        app_version="lifecycle-test",
    )
    assert outcome.status == "failed"
    status, _, _ = _publication(con, outcome.run_id)
    assert status == "assembled"
    assert sink.writes == 0


def test_before_digest_crash_is_disclosed_not_fatal(con):
    def exploding_hook(con_, run_id):
        raise RuntimeError("hook exploded")

    sink = _FileSink()
    outcome = engine.run(
        [TickerContext(ticker="NVDA")],
        con=con,
        sources=[_Source()],
        profile=DEFAULT_PROFILE,
        sink=sink,
        as_of=RUN_AT,
        today=RUN_AT.date(),
        app_version="lifecycle-test",
        before_digest=exploding_hook,
    )
    assert sink.writes == 1  # the digest still landed
    note = con.execute(
        "SELECT notes FROM runs WHERE run_id = ?", (outcome.run_id,)
    ).fetchone()["notes"]
    assert "pre-digest step failed" in note and "hook exploded" in note


def test_diff_failure_cause_is_persisted(con, monkeypatch):
    """A diff-phase exception degrades the run to partial AND records why —
    previously the cause was swallowed entirely."""
    from argus import changes

    def exploding_detect(*args, **kwargs):
        raise RuntimeError("diff exploded")

    monkeypatch.setattr(changes, "detect", exploding_detect)
    outcome = _run(con)
    assert outcome.status == "partial"
    note = con.execute(
        "SELECT notes FROM runs WHERE run_id = ?", (outcome.run_id,)
    ).fetchone()["notes"]
    assert "diff failed for NVDA" in note and "diff exploded" in note


def test_publication_timestamps_use_injected_now(con):
    later = datetime(2026, 7, 20, 15, 30, tzinfo=UTC)
    outcome = _run_with_now(con, later)
    _, _, at = _publication(con, outcome.run_id)
    assert at == later.isoformat()


def _run_with_now(con, instant):
    return engine.run(
        [TickerContext(ticker="NVDA")],
        con=con,
        sources=[_Source()],
        profile=DEFAULT_PROFILE,
        sink=_FileSink(),
        as_of=RUN_AT,
        today=RUN_AT.date(),
        app_version="lifecycle-test",
        now=lambda: instant,
    )


def test_mark_publication_redacts_error(con):
    run_id = writer.begin_run(con, kind="watch", started_at=RUN_AT, app_version="t")
    writer.mark_publication(
        con, run_id=run_id, status="delivery_failed", at=RUN_AT,
        error="POST https://discord.com/api/webhooks/1/SECRETPART failed",
    )
    _, error, _ = _publication(con, run_id)
    assert "SECRETPART" not in error
    assert "REDACTED" in error


def test_begin_run_starts_lifecycle_at_collecting(con):
    run_id = writer.begin_run(con, kind="watch", started_at=RUN_AT, app_version="t")
    status, _, _ = _publication(con, run_id)
    assert status == "collecting"


def test_migration_is_idempotent_and_adds_columns(tmp_path):
    con = connect(tmp_path / "fresh.db")
    migrate(con)
    migrate(con)  # double-migrate must be a no-op, never a duplicate-column error
    columns = {row["name"] for row in con.execute("PRAGMA table_info(runs)")}
    assert {"publication_status", "publication_error", "published_at"} <= columns
    con.close()


# --- overlap protection --------------------------------------------------------


def test_run_lock_refuses_overlap(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(LockHeldError, match="another argus run is in progress"):
            with run_lock(tmp_path):
                pass  # pragma: no cover


def test_run_lock_releases_on_exit_and_on_error(tmp_path):
    with run_lock(tmp_path):
        pass
    with pytest.raises(ValueError):
        with run_lock(tmp_path):
            raise ValueError("boom")
    with run_lock(tmp_path):  # reacquirable after both exits
        pass
