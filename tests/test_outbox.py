"""Immutable artifacts + delivery outbox contract: every written file gets a
hash record; every channel attempt gets an outbox row; `argus deliver` retries
WITHOUT re-collecting, refuses hash-mismatched files, and never double-posts;
`argus report --run N` verifies the original instead of overwriting it."""

import hashlib
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from argus import engine
from argus.cli import app
from argus.digest import CompositeSink, FileDigestSink
from argus.fields import Field, Source
from argus.gates import DEFAULT_PROFILE
from argus.models import RawObservation, TickerContext
from argus.sources.base import FetchResult
from argus.store import connect, migrate, queries

runner = CliRunner()

RUN_AT = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)

DELIVERY_VARS = (
    "ARGUS_EMAIL_TO",
    "ARGUS_SMTP_USER",
    "ARGUS_SMTP_PASSWORD",
    "ARGUS_DISCORD_WEBHOOK",
)


@pytest.fixture(autouse=True)
def _no_delivery_env(monkeypatch):
    for var in DELIVERY_VARS:
        monkeypatch.delenv(var, raising=False)


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


class _Channel:
    channel_name = "discord"
    fingerprint = "abc123def456"

    def __init__(self, fail=False):
        self.fail = fail
        self.writes = 0

    def write(self, markdown, *, run_id, as_of, attachments=()):
        if self.fail:
            raise RuntimeError("HTTP 502 from webhook")
        self.writes += 1
        return None


def _engine_run(con, tmp_path, *, channel=None):
    return engine.run(
        [TickerContext(ticker="NVDA")],
        con=con,
        sources=[_Source()],
        profile=DEFAULT_PROFILE,
        sink=FileDigestSink(tmp_path / "reports"),
        as_of=RUN_AT,
        today=RUN_AT.date(),
        app_version="outbox-test",
        gated_sink=CompositeSink(channel) if channel is not None else None,
        gate_channels=False,
    )


@pytest.fixture()
def con(tmp_path):
    con = connect(tmp_path / "argus.db")
    migrate(con)
    yield con
    con.close()


def test_engine_records_md_artifact_with_hash(con, tmp_path):
    outcome = _engine_run(con, tmp_path)
    [artifact] = queries.artifacts_for(con, run_id=outcome.run_id)
    assert artifact["kind"] == "md"
    assert artifact["original"] == 1
    assert artifact["renderer"] == "outbox-test"
    on_disk = (tmp_path / "reports" / artifact["filename"]).read_bytes()
    assert hashlib.sha256(on_disk).hexdigest() == artifact["sha256"]
    assert artifact["bytes"] == len(on_disk)


def test_channel_attempt_lands_in_outbox(con, tmp_path):
    ok = _engine_run(con, tmp_path, channel=_Channel())
    rows = con.execute(
        "SELECT * FROM delivery_outbox WHERE run_id = ?", (ok.run_id,)
    ).fetchall()
    [row] = rows
    assert row["channel"] == "discord"
    assert row["fingerprint"] == "abc123def456"
    assert row["delivered_at"] is not None
    assert row["attempts"] == 1
    assert queries.undelivered_outbox(con, run_id=ok.run_id) == []


def test_failed_delivery_stays_in_outbox_with_safe_error(con, tmp_path):
    failed = _engine_run(con, tmp_path, channel=_Channel(fail=True))
    [row] = queries.undelivered_outbox(con, run_id=failed.run_id)
    assert row["delivered_at"] is None
    assert row["attempts"] == 1
    assert "502" in row["last_error"]
    assert row["next_retry_at"] is not None


# --- argus deliver (CLI, end to end) ------------------------------------------


def _seed_failed_delivery(tmp_path, monkeypatch):
    """A real watch run whose Discord post failed: file written, outbox row
    undelivered. Returns the run_id."""
    monkeypatch.setenv("ARGUS_DISCORD_WEBHOOK", "https://discord.com/api/webhooks/1/xyz")
    runner.invoke(app, ["init", "--root", str(tmp_path)])

    import httpx

    def dead_post(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", dead_post)
    result = runner.invoke(app, ["watch", "--root", str(tmp_path)])
    assert result.exit_code == 1  # undelivered = nonzero
    assert "NOT delivered" in result.output
    return 1


def test_deliver_retries_from_outbox_without_recollection(tmp_path, monkeypatch):
    run_id = _seed_failed_delivery(tmp_path, monkeypatch)

    import httpx

    posts = []

    class _OK:
        status_code = 200

        def raise_for_status(self):
            return None

    def good_post(url, **kwargs):
        posts.append(url)
        return _OK()

    monkeypatch.setattr(httpx, "post", good_post)
    result = runner.invoke(app, ["deliver", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "delivered" in result.output
    assert len(posts) == 1  # exactly one Discord POST — no re-collection, no dupes

    con = connect(tmp_path / "argus.db")
    try:
        assert queries.undelivered_outbox(con) == []
        row = con.execute(
            "SELECT publication_status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row["publication_status"] == "delivered"
    finally:
        con.close()

    # Idempotence: a second deliver finds nothing and posts nothing.
    posts.clear()
    again = runner.invoke(app, ["deliver", "--root", str(tmp_path)])
    assert again.exit_code == 0
    assert "Nothing undelivered" in again.output
    assert posts == []


def test_deliver_refuses_hash_mismatched_artifact(tmp_path, monkeypatch):
    _seed_failed_delivery(tmp_path, monkeypatch)
    # Tamper with the on-disk digest after the run recorded its hash.
    [md] = list((tmp_path / "reports").glob("digest-*.md"))
    md.write_text(md.read_text() + "\ntampered\n")

    import httpx

    posts = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posts.append(a) or None)
    result = runner.invoke(app, ["deliver", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "no longer matches" in result.output
    assert posts == []  # refused BEFORE any network attempt


def test_deliver_with_no_outbox_is_a_clean_noop(tmp_path):
    runner.invoke(app, ["init", "--root", str(tmp_path)])
    runner.invoke(app, ["watch", "--root", str(tmp_path)])  # no channels configured
    result = runner.invoke(app, ["deliver", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "Nothing undelivered" in result.output


# --- report --run verification -------------------------------------------------


def test_report_verifies_original_and_leaves_it_untouched(tmp_path):
    runner.invoke(app, ["init", "--root", str(tmp_path)])
    runner.invoke(app, ["watch", "--root", str(tmp_path)])
    [md] = list((tmp_path / "reports").glob("digest-*.md"))
    before = md.read_bytes()
    mtime = md.stat().st_mtime_ns

    result = runner.invoke(app, ["report", "--run", "1", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "bit-for-bit" in result.output and "verified" in result.output
    assert md.read_bytes() == before
    assert md.stat().st_mtime_ns == mtime  # genuinely untouched, not rewritten


def test_report_divergence_writes_rerender_never_overwrites(tmp_path):
    runner.invoke(app, ["init", "--root", str(tmp_path)])
    runner.invoke(app, ["watch", "--root", str(tmp_path)])
    con = connect(tmp_path / "argus.db")
    try:
        # Simulate a renderer upgrade: the recorded original hash no longer
        # matches what today's renderer produces.
        con.execute("UPDATE artifacts SET sha256 = 'stale' WHERE kind = 'md'")
        con.commit()
    finally:
        con.close()
    [md] = list((tmp_path / "reports").glob("digest-*run1.md"))
    before = md.read_bytes()

    result = runner.invoke(app, ["report", "--run", "1", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "DIFFERENTLY" in result.output and "untouched" in result.output
    assert md.read_bytes() == before  # the original is sacred
    rerenders = list((tmp_path / "reports").glob("digest-*-rerender.md"))
    assert len(rerenders) == 1

    con = connect(tmp_path / "argus.db")
    try:
        rows = queries.artifacts_for(con, run_id=1)
        assert any(a["original"] == 0 for a in rows)  # regeneration recorded separately
    finally:
        con.close()
