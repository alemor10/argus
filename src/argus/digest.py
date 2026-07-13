"""Digest rendering (pure) and delivery sinks (the one IO seam for output).

Silence is a statement: a digest is written on every run that produced any
data, including a run with zero change events — "nothing changed" is
information, and degraded runs disclose their own degradation.
"""

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from argus.fields import SPECS, Field, Source
from argus.models import (
    AnalystAction,
    ChangeEvent,
    ConsensusShift,
    EarningsImminent,
    FieldQuarantined,
    FieldRecovered,
    PriceMove,
    QuarantineHit,
    RunReport,
    Snapshot,
    TargetMove,
    TickerReport,
)

# Human-facing field names. Adding a Field without a label falls back to the
# enum value — never a KeyError in the report path.
_FIELD_LABELS: dict[Field, str] = {
    Field.PRICE: "Price",
    Field.MARKET_CAP: "Market cap",
    Field.PE_TTM: "P/E (TTM)",
    Field.PE_FWD: "Fwd P/E",
    Field.PEG: "PEG",
    Field.GROSS_MARGIN: "Gross margin",
    Field.OPERATING_MARGIN: "Operating margin",
    Field.DEBT_TO_EQUITY: "Debt/equity",
    Field.NEXT_EARNINGS_DATE: "Next earnings",
    Field.ANALYST_RATING: "Analyst rating",
    Field.ANALYST_TARGET_MEAN: "Analyst target (mean)",
    Field.ANALYST_COUNT: "Analyst count",
}

# Deterministic sort keys: declaration order of the enums, not alphabetical —
# the watchlist reads price-first the way a human scans it.
_FIELD_ORDER = {f: i for i, f in enumerate(Field)}
_SOURCE_ORDER = {s: i for i, s in enumerate(Source)}


def render(report: RunReport) -> str:
    """RunReport → markdown.

    Rules (see ARCHITECTURE.md):
      - Tri-state per watched field, always one of:
          value with provenance   `Fwd P/E 31.2 (yahoo, 2026-07-12 14:03Z)`
          quarantined with reason `⚠ DATA QUARANTINED — target/price 3.19 …`
          no data with cause      `— no data (finnhub: HTTP 502)`
      - Headline "Changes" section renders the typed events (incl.
        FieldQuarantined — going dark is news); numeric moves print old_as_of.
      - Data-health section from run_sources (source down → which cross-checks
        were skipped); quarantine report table rendered from
        report.tickers[*].quarantines — EVERY quarantined observation,
        including those coexisting with an accepted primary; per-ticker
        thesis printed beside its changes.
      - Deterministic: tickers alphabetical, events by severity then kind —
        golden tests byte-compare the output.
    """
    tickers = sorted(report.tickers, key=lambda t: t.context.ticker)
    sections = (
        _header(report),
        _changes_section(tickers),
        _watchlist_section(tickers),
        _quarantine_section(tickers),
        _health_section(tickers),
        _footer(report),
    )
    return "\n\n".join("\n".join(_trimmed(section)) for section in sections) + "\n"


# --- Sections ---------------------------------------------------------------


def _header(report: RunReport) -> list[str]:
    lines = [f"# Argus {report.kind} digest — run {report.run_id} — {report.as_of.date().isoformat()}", ""]
    if report.status == "complete":
        lines.append("Status: complete.")
    elif report.status == "partial":
        lines.append(
            "Status: PARTIAL — some tickers or sources failed this run; "
            "degradation is detailed under Data health."
        )
    else:
        lines.append("Status: FAILED — this run produced no usable data.")
    return lines


def _changes_section(tickers: Sequence[TickerReport]) -> list[str]:
    lines = ["## Changes", ""]
    if not any(t.events for t in tickers):
        # Silence is a statement: zero events is information, stated plainly.
        lines += ["No changes since last run.", ""]
    else:
        for ticker in tickers:
            if not ticker.events:
                continue
            lines += [f"### {ticker.context.ticker}", ""]
            if ticker.context.thesis:
                lines += [f"_{ticker.context.thesis}_", ""]
            lines += [f"- {_event_line(event)}" for event in ticker.events]
            lines.append("")
    # status-based, not snapshot-based: queries.snapshot returns an EMPTY
    # (non-None) Snapshot for a failed ticker's run_tickers row, so a
    # snapshot-is-None guard never fires and a never-succeeded ticker would
    # read as "baseline established" forever.
    firsts = [
        t.context.ticker for t in tickers if t.baseline_run_id is None and t.status != "failed"
    ]
    if firsts:
        lines += [
            "Baseline established this run (no prior run to diff against): " + ", ".join(firsts) + ".",
            "",
        ]
    failures = [t for t in tickers if t.status == "failed"]
    if failures:
        lines.append(
            "Fetch failures (no data this run): "
            + ", ".join(f"{t.context.ticker} ({t.error or 'unknown error'})" for t in failures)
            + "."
        )
    return lines


def _watchlist_section(tickers: Sequence[TickerReport]) -> list[str]:
    lines = ["## Watchlist", ""]
    for ticker in tickers:
        lines += [f"### {ticker.context.ticker}", ""]
        if ticker.baseline is not None and ticker.snapshot is not None and ticker.snapshot.values:
            # Names the drift window once per ticker (baselines are
            # per-ticker, so it can differ across tickers after failures).
            # Skipped when nothing this run has a value to drift.
            lines += [f"_Δ vs {ticker.baseline.as_of.date().isoformat()}_", ""]
        if ticker.snapshot is None:
            # Never silently absent: a dead ticker is named, with its error.
            lines += [f"Fetch failed — no data this run ({ticker.error or 'unknown error'}).", ""]
            continue
        lines += [_field_line(field, ticker) for field in Field]
        lines.append("")
    return lines


def _quarantine_section(tickers: Sequence[TickerReport]) -> list[str]:
    rows: list[tuple[str, object]] = []
    for ticker in tickers:
        ordered = sorted(
            ticker.quarantines,
            key=lambda q: (_FIELD_ORDER[q.field], _SOURCE_ORDER[q.source], q.fetched_at),
        )
        rows += [(ticker.context.ticker, q) for q in ordered]
    if not rows:
        return ["No data quarantined this run."]
    lines = [
        "## Data quarantined",
        "",
        "| Ticker | Field | Source | Reasons | Fetched at |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, q in rows:
        reasons = _cell("; ".join(f"{hit.code.value}: {hit.detail}" for hit in q.reasons))
        lines.append(
            f"| {name} | {_label(q.field)} | {q.source.value} | {reasons} | {_ts(q.fetched_at)} |"
        )
    return lines


def _health_section(tickers: Sequence[TickerReport]) -> list[str]:
    lines = ["## Data health", ""]
    tallies: dict[Source, dict[str, int]] = {}
    first_error: dict[Source, str] = {}
    for ticker in tickers:  # already alphabetical → "first error" is deterministic
        for health in sorted(ticker.sources, key=lambda s: _SOURCE_ORDER[s.source]):
            tally = tallies.setdefault(health.source, {"ok": 0, "error": 0, "not_applicable": 0})
            tally[health.status] += 1
            if health.status == "error" and health.error and health.source not in first_error:
                first_error[health.source] = health.error
    if not tallies:
        lines.append("No source health recorded.")
    for source in Source:
        tally = tallies.get(source)
        if tally is None:
            if tallies:
                # A source with zero rows on a run that fetched anything was
                # never wired in (no API key / contact email) — permanent
                # degradation belongs in the digest, not just a CLI echo.
                lines.append(
                    f"- {source.value}: not configured — its cross-checks never ran"
                )
            continue
        parts = []
        if tally["ok"]:
            parts.append(f"{tally['ok']} ok")
        if tally["error"]:
            parts.append(f"{tally['error']} error" + ("s" if tally["error"] != 1 else ""))
        if tally["not_applicable"]:
            parts.append(f"{tally['not_applicable']} not applicable")
        line = f"- {source.value}: " + ", ".join(parts)
        if tally["error"]:
            if source in first_error:
                line += f" (first: {first_error[source]})"
            skipped = [
                _label(f).lower()
                for f in Field
                if SPECS[f].cross_source_rel_tol is not None
                and source in SPECS[f].priority
                and SPECS[f].priority[0] is not source
            ]
            if skipped:
                n = tally["error"]
                line += (
                    f" — {', '.join(skipped)} cross-checks skipped"
                    f" ({n} ticker{'s' if n != 1 else ''})"
                )
        lines.append(line)
    failed = [t for t in tickers if t.status == "failed"]
    if failed:
        lines += ["", "Failed tickers:"]
        lines += [f"- {t.context.ticker}: {t.error or 'unknown error'}" for t in failed]
    return lines


def _footer(report: RunReport) -> list[str]:
    # Self-identifying: `argus report --run N` output names its run.
    return ["---", "", f"Run {report.run_id} — regenerate with `argus report --run {report.run_id}`."]


# --- Lines ------------------------------------------------------------------


def _event_line(event: ChangeEvent) -> str:
    match event:
        case PriceMove(old=old, new=new, pct=pct, threshold=threshold, old_as_of=old_as_of):
            return (
                f"Price {old:.2f} → {new:.2f} ({pct:+.1f}%, threshold {threshold:.1f}%)"
                f" vs {old_as_of.date().isoformat()}"
            )
        case TargetMove(old=old, new=new, pct=pct, threshold=threshold, old_as_of=old_as_of):
            return (
                f"Analyst target {old:.2f} → {new:.2f} ({pct:+.1f}%, threshold {threshold:.1f}%)"
                f" vs {old_as_of.date().isoformat()}"
            )
        case ConsensusShift(old=old, new=new, direction=direction):
            return f"Consensus rating {old} → {new} ({direction})"
        case AnalystAction(firm=firm, action=action, from_grade=from_grade, to_grade=to_grade, action_date=when):
            grades = f"{from_grade} → {to_grade}" if from_grade else to_grade
            return f"Analyst action ({when.isoformat()}): {firm} {action} — {grades}"
        case EarningsImminent(earnings_date=earnings_date, days_until=days_until):
            return f"Earnings imminent: {earnings_date.isoformat()} ({_days_phrase(days_until)})"
        case FieldQuarantined(field=field, reasons=reasons):
            # Going dark is news, not a footnote.
            return f"⚠ {_label(field)} went dark — DATA QUARANTINED: {_details(reasons)}"
        case FieldRecovered(field=field):
            return f"✓ {_label(field)} recovered — accepted data resumed"
        case _:  # pragma: no cover — the union is closed
            raise AssertionError(f"unhandled event kind: {event!r}")


def _field_line(field: Field, ticker: TickerReport) -> str:
    """The tri-state: value with provenance, quarantined with reasons, or an
    explained absence. Absence of signal is never confusable with absence of
    data."""
    snapshot = ticker.snapshot
    assert snapshot is not None  # callers filter fetch failures first
    label = _label(field)
    fv = snapshot.values.get(field)
    if fv is not None:
        marks = "".join(f" ✓{s.value}" for s in sorted(fv.corroborated_by))
        drift = _drift_suffix(field, fv.value, ticker.baseline)
        return (
            f"- {label}: {_fmt_value(field, fv.value)}{drift} "
            f"({fv.source.value}, {_ts(fv.fetched_at)}){marks}"
        )
    hits = snapshot.quarantined.get(field)
    if hits:
        return f"- {label}: ⚠ DATA QUARANTINED — {_details(hits)}"
    return f"- {label}: — no data ({_no_data_cause(field, ticker)})"


# Scale-free fields drift in percent; ratios drift in absolute points;
# margins (stored as fractions, rendered as percents) drift in percentage
# points. Sub-threshold drift is information — quiet weeks should still show
# which way things are leaning.
_PCT_DRIFT_FIELDS = frozenset({Field.PRICE, Field.MARKET_CAP, Field.ANALYST_TARGET_MEAN})
_PP_DRIFT_FIELDS = frozenset({Field.GROSS_MARGIN, Field.OPERATING_MARGIN})


def _drift_suffix(field: Field, value: float | str | date, baseline: Snapshot | None) -> str:
    """Run-over-run drift for a numeric watchlist line, e.g. ' (+6.6%)'.
    Empty when there is no accepted baseline value for the field (first
    sighting, prior quarantine/outage — the Changes section owns gap
    stories) or when the drift rounds to zero (genuinely unchanged)."""
    if baseline is None or not isinstance(value, (int, float)):
        return ""
    old = baseline.values.get(field)
    if old is None or not isinstance(old.value, (int, float)):
        return ""
    if field in _PCT_DRIFT_FIELDS:
        if old.value == 0:
            return ""
        rendered = f"{(value - old.value) / old.value * 100:+.1f}%"
        zero = "+0.0%", "-0.0%"
    elif field in _PP_DRIFT_FIELDS:
        rendered = f"{(value - old.value) * 100:+.1f}pp"
        zero = "+0.0pp", "-0.0pp"
    elif field is Field.ANALYST_COUNT:
        rendered = f"{value - old.value:+.0f}"
        zero = ("+0", "-0")
    else:  # P/E, PEG, debt/equity — absolute points
        rendered = f"{value - old.value:+.2f}"
        zero = "+0.00", "-0.00"
    if rendered in zero:
        return ""
    return f" ({rendered})"


def _no_data_cause(field: Field, ticker: TickerReport) -> str:
    """Why is this field absent? Blame any down/inapplicable source in the
    field's priority chain; otherwise the sources were fine and the field
    simply wasn't provided."""
    health = {h.source: h for h in ticker.sources}
    causes = []
    for source in SPECS[field].priority:
        h = health.get(source)
        if h is None or h.status == "ok":
            continue
        causes.append(f"{source.value}: {h.error or ('not applicable' if h.status == 'not_applicable' else 'error')}")
    return "; ".join(causes) if causes else "not provided"


# --- Formatting helpers -----------------------------------------------------


def _label(field: Field) -> str:
    return _FIELD_LABELS.get(field, field.value.replace("_", " "))


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _details(hits: Sequence[QuarantineHit]) -> str:
    return "; ".join(hit.detail for hit in hits)


def _days_phrase(days: int) -> str:
    if days == 0:
        return "today"
    return f"in {days} day" + ("" if days == 1 else "s")


def _humanize_cap(value: float) -> str:
    """1.23T / 456.7B / 89.0M — three-ish significant figures, never raw."""

    def _render(scaled: float, suffix: str) -> str:
        return f"{scaled:.2f}{suffix}" if abs(scaled) < 10 else f"{scaled:.1f}{suffix}"

    magnitude = abs(value)
    units = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))
    for i, (divisor, suffix) in enumerate(units):
        if magnitude >= divisor:
            rendered = _render(value / divisor, suffix)
            if rendered.lstrip("-").startswith("1000.") and i > 0:
                # 999.96B rounds past its unit boundary — roll to 1.00T
                # rather than printing 1000.0B.
                bigger_divisor, bigger_suffix = units[i - 1]
                rendered = _render(value / bigger_divisor, bigger_suffix)
            return rendered
    return f"{value:.2f}"


def _fmt_value(field: Field, value: float | str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if field is Field.MARKET_CAP:
        return _humanize_cap(value)
    if field is Field.ANALYST_COUNT:
        return f"{value:.0f}"
    if field in (Field.GROSS_MARGIN, Field.OPERATING_MARGIN):
        return f"{value * 100:.1f}%"  # stored as a fraction; read as a percent
    return f"{value:.2f}"


def _cell(text: str) -> str:
    """Make arbitrary text safe inside a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _trimmed(lines: list[str]) -> list[str]:
    """Strip leading/trailing blank lines so section joins never double up
    (and no digest ever carries trailing-whitespace lines)."""
    start, end = 0, len(lines)
    while start < end and not lines[start]:
        start += 1
    while end > start and not lines[end - 1]:
        end -= 1
    return lines[start:end]


@runtime_checkable
class DigestSink(Protocol):
    """Where a rendered digest goes. FileDigestSink ships in v1; email or
    notification sinks are additional implementations, no engine changes."""

    def write(self, markdown: str, *, run_id: int, as_of: date) -> Path | None: ...


class FileDigestSink:
    def __init__(self, reports_dir: Path) -> None:
        self.reports_dir = reports_dir

    def write(self, markdown: str, *, run_id: int, as_of: date) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / f"digest-{as_of.isoformat()}-run{run_id}.md"
        path.write_text(markdown, encoding="utf-8")
        return path


class EmailDigestSink:
    """Delivers the digest as a plain-text email (markdown reads fine as
    text) via authenticated SMTP submission — port 465 implicit TLS or
    587 STARTTLS. Built for Gmail-with-app-password but any submission
    server works. Network/auth failures raise; CompositeSink turns them
    into a disclosed delivery failure rather than silence."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipient: str,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.recipient = recipient

    def write(self, markdown: str, *, run_id: int, as_of: date) -> None:
        import smtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["Subject"] = f"Argus digest — {as_of.isoformat()} — run {run_id}"
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content(markdown)

        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as smtp:
                smtp.login(self.username, self.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.username, self.password)
                smtp.send_message(message)
        return None


class DiscordDigestSink:
    """Posts a headline message (title, status, the Changes section) with the
    full digest attached as a .md file to a Discord webhook. The headline is
    distilled from the rendered markdown and capped well under Discord's
    2,000-character message limit; the attachment carries the whole report.
    HTTP failures raise; CompositeSink turns them into a disclosed delivery
    failure rather than silence."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def write(self, markdown: str, *, run_id: int, as_of: date) -> None:
        import httpx

        payload = {
            "content": _discord_headline(markdown),
            "allowed_mentions": {"parse": []},  # a digest must never ping anyone
        }
        response = httpx.post(
            self.webhook_url,
            data={"payload_json": json.dumps(payload)},
            files={
                "files[0]": (
                    f"digest-{as_of.isoformat()}-run{run_id}.md",
                    markdown.encode("utf-8"),
                    "text/markdown",
                )
            },
            timeout=30.0,
        )
        response.raise_for_status()
        return None


_DISCORD_HEADLINE_LINES = 20
_DISCORD_HEADLINE_CHARS = 1900  # hard API limit is 2000; leave headroom


def _discord_headline(markdown: str) -> str:
    """Title + status + the Changes section, truncated — the message is the
    hook, the attachment is the report."""
    lines = markdown.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else "Argus digest"
    status = next((line for line in lines if line.startswith("Status:")), "")
    changes: list[str] = []
    in_changes = False
    for line in lines:
        if line.startswith("## "):
            in_changes = line.strip() == "## Changes"
            continue
        if in_changes and line.strip():
            changes.append(line)
    shown = changes[:_DISCORD_HEADLINE_LINES]
    headline = "\n".join(part for part in (f"**{title}**", status, "", *shown) if part is not None)
    if len(changes) > len(shown):
        headline += "\n… (full digest attached)"
    if len(headline) > _DISCORD_HEADLINE_CHARS:
        headline = headline[:_DISCORD_HEADLINE_CHARS] + "\n… (truncated — full digest attached)"
    return headline.strip()


class DeliveryError(RuntimeError):
    """Some sink(s) failed AFTER the digest was rendered (and possibly after
    other sinks succeeded). Carries whatever path a successful file sink
    produced, so the caller can say 'written here, but not delivered'."""

    def __init__(self, message: str, digest_path: Path | None) -> None:
        super().__init__(message)
        self.digest_path = digest_path


class CompositeSink:
    """Fan a digest out to several sinks. Every sink is ATTEMPTED even when
    an earlier one fails — a broken mail server must not stop the file copy —
    then failures are raised together as DeliveryError. On a headless box an
    undelivered digest is an unseen digest; failure must be loud."""

    def __init__(self, *sinks: DigestSink) -> None:
        self.sinks = sinks

    def write(self, markdown: str, *, run_id: int, as_of: date) -> Path | None:
        path: Path | None = None
        failures: list[str] = []
        for sink in self.sinks:
            try:
                result = sink.write(markdown, run_id=run_id, as_of=as_of)
            except Exception as exc:
                failures.append(f"{type(sink).__name__}: {exc}")
                continue
            if path is None and result is not None:
                path = result
        if failures:
            raise DeliveryError("; ".join(failures), digest_path=path)
        return path
