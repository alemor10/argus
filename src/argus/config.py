"""Path resolution and watchlist config.

Everything lives under one project directory by default (watchlist.yaml,
argus.db, reports/), overridable per path or via ARGUS_HOME. The engine never
sees "the watchlist" — this module turns it into list[TickerContext], which is
exactly what scout will construct differently later.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field as PydanticField

from argus.fields import Field, Source
from argus.models import MacroSpec, ThesisCheck, Thresholds, TickerContext
from argus.thesis import parse_thesis_check

DEFAULT_WATCHLIST = "watchlist.yaml"
DEFAULT_SCOUT = "scout.yaml"
DEFAULT_MACRO = "macro.yaml"
DEFAULT_CONSIDER = "consider.yaml"
DEFAULT_DB = "argus.db"
DEFAULT_REPORTS = "reports"


@dataclass(frozen=True)
class Paths:
    root: Path
    watchlist: Path
    scout: Path
    macro: Path
    consider: Path
    db: Path
    reports: Path


def resolve_paths(
    root: Path | None = None,
    *,
    watchlist: Path | None = None,
    scout: Path | None = None,
    macro: Path | None = None,
    consider: Path | None = None,
    db: Path | None = None,
    reports: Path | None = None,
) -> Paths:
    """Project-dir defaults; explicit paths win; ARGUS_HOME beats cwd."""
    base = (root or Path(os.environ.get("ARGUS_HOME", "."))).resolve()
    return Paths(
        root=base,
        watchlist=(watchlist or base / DEFAULT_WATCHLIST).resolve(),
        scout=(scout or base / DEFAULT_SCOUT).resolve(),
        macro=(macro or base / DEFAULT_MACRO).resolve(),
        consider=(consider or base / DEFAULT_CONSIDER).resolve(),
        db=(db or base / DEFAULT_DB).resolve(),
        reports=(reports or base / DEFAULT_REPORTS).resolve(),
    )


class WatchlistEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = PydanticField(min_length=1)
    thesis: str | None = None
    thresholds: dict[str, int | float] = {}  # partial overrides; keys validated on merge
    thesis_checks: tuple[str, ...] = ()  # falsifiable conditions, parsed at build


class WatchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    defaults: Thresholds = Thresholds()
    tickers: tuple[WatchlistEntry, ...] = ()


def load_watch_config(path: Path) -> WatchConfig:
    return load_watch_config_text(path.read_text(encoding="utf-8"))


def load_watch_config_text(text: str) -> WatchConfig:
    """Parse watchlist YAML from a string — promote validates its edit
    round-trips BEFORE writing the file."""
    raw = yaml.safe_load(text) or {}
    return WatchConfig.model_validate(raw)


def build_contexts(config: WatchConfig) -> list[TickerContext]:
    """Merge per-ticker threshold overrides over defaults. Config is the
    fail-loudly boundary: unknown override keys error (Thresholds forbids
    extras) and duplicate tickers error here rather than as an
    IntegrityError mid-run."""
    contexts = []
    seen: set[str] = set()
    for entry in config.tickers:
        if entry.ticker in seen:
            raise ValueError(f"duplicate ticker in watchlist: {entry.ticker}")
        seen.add(entry.ticker)
        merged = config.defaults.model_dump() | entry.thresholds
        try:
            checks = tuple(parse_thesis_check(raw) for raw in entry.thesis_checks)
        except ValueError as exc:
            raise ValueError(f"{entry.ticker}: bad thesis check — {exc}") from exc
        contexts.append(
            TickerContext(
                ticker=entry.ticker,
                thesis=entry.thesis,
                thresholds=Thresholds(**merged),
                thesis_checks=checks,
            )
        )
    return contexts


class ConsiderConfig(BaseModel):
    """consider.yaml — the Radar's middle rung. MACHINE-managed (unlike the
    human-owned watchlist): `argus consider` appends, `argus promote` removes
    on graduation. Names here are tracked through the full fetch→gate
    pipeline daily with no thesis required; Argus never adds one itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tickers: tuple[str, ...] = ()


def load_consider(path: Path) -> ConsiderConfig:
    if not path.exists():
        return ConsiderConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ConsiderConfig.model_validate(raw)


def build_consider_contexts(
    config: ConsiderConfig, watch: list[TickerContext]
) -> list[TickerContext]:
    """consider.yaml → consider-tier contexts (default thresholds, no thesis).
    A name on both lists errors — the run_tickers primary key allows one row
    per ticker, and 'promote' is the sanctioned move between tiers."""
    watched = {ctx.ticker.upper() for ctx in watch}
    contexts: list[TickerContext] = []
    seen: set[str] = set()
    for raw in config.tickers:
        symbol = raw.strip().upper()
        if not symbol:
            continue
        if symbol in seen:
            raise ValueError(f"duplicate ticker in consider.yaml: {symbol}")
        if symbol in watched:
            raise ValueError(
                f"{symbol} is on both watchlist.yaml and consider.yaml — "
                "promote removed it from consider; delete the leftover entry"
            )
        seen.add(symbol)
        contexts.append(TickerContext(ticker=symbol, tier="consider"))
    return contexts


class MacroSeriesEntry(BaseModel):
    """One macro.yaml series. Config is the fail-loudly boundary: unknown
    keys, bad transforms, and malformed alert lines all error here, never
    mid-run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = PydanticField(min_length=1)
    label: str = PydanticField(min_length=1)
    source: Literal["yahoo", "fred"] = "yahoo"
    transform: Literal["level", "yoy_pct", "mom_change"] = "level"
    unit: str = ""
    decimals: int = PydanticField(default=2, ge=0, le=6)
    alert_move: float | None = PydanticField(default=None, gt=0)
    alert_on_release: bool | None = None  # None → fred defaults on, yahoo off
    sanity: tuple[float, float] | None = None
    alert_when: tuple[str, ...] = ()


class MacroConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    series: tuple[MacroSeriesEntry, ...] = ()
    bellwethers: tuple[str, ...] = ()  # megacap earnings-context list (claims-labeled)
    etfs: tuple[str, ...] = ()  # well-known ETFs to watch for rebalancing (SSGA/SPDR feed)


def load_macro_config(path: Path) -> MacroConfig:
    """Missing file → feature off (empty config); present file → strict."""
    if not path.exists():
        return MacroConfig()
    return load_macro_config_text(path.read_text(encoding="utf-8"))


def load_macro_config_text(text: str) -> MacroConfig:
    raw = yaml.safe_load(text) or {}
    return MacroConfig.model_validate(raw)


# `value` is the human-facing token for "this series' number" — mapped to the
# concrete field (PRICE for market quotes, ECON_VALUE for econ prints) so the
# thesis-check grammar is reused without leaking storage names into yaml.
_VALUE_ALIAS = re.compile(r"^value\b")


def _parse_alert_line(raw: str, value_field: Field, symbol: str) -> ThesisCheck:
    text = " ".join(raw.split())
    rewritten = _VALUE_ALIAS.sub(value_field.value, text, count=1)
    check = parse_thesis_check(rewritten)
    if check.field is not value_field:
        raise ValueError(
            f"{symbol}: macro alert lines watch the series' value — "
            f'write "value >= …", got {raw!r}'
        )
    # Render the HUMAN's spelling everywhere (digest, events), not the
    # storage field name the alias resolved to.
    return check.model_copy(update={"raw": text})


def build_macro_contexts(config: MacroConfig) -> list[TickerContext]:
    """macro.yaml → macro-role TickerContexts. Duplicate symbols error here
    (they would violate the run_tickers primary key mid-run); every alert
    line parses now or the run refuses to start."""
    contexts: list[TickerContext] = []
    seen: set[str] = set()
    for entry in config.series:
        symbol = entry.symbol.strip()
        if symbol.upper() in seen:
            raise ValueError(f"duplicate macro series: {symbol}")
        seen.add(symbol.upper())
        source = Source(entry.source)
        value_field = Field.ECON_VALUE if source is Source.FRED else Field.PRICE
        try:
            lines = tuple(
                _parse_alert_line(raw, value_field, symbol) for raw in entry.alert_when
            )
            spec = MacroSpec(
                label=entry.label,
                unit=entry.unit,
                decimals=entry.decimals,
                source=source,
                transform=entry.transform,
                alert_move=entry.alert_move,
                alert_on_release=(
                    entry.alert_on_release
                    if entry.alert_on_release is not None
                    else source is Source.FRED
                ),
                sanity=entry.sanity,
                alert_when=lines,
            )
        except ValueError as exc:
            raise ValueError(f"macro series {symbol}: {exc}") from exc
        contexts.append(TickerContext(ticker=symbol, macro=spec))
    return contexts


def ensure_no_overlap(
    watch: list[TickerContext], macro: list[TickerContext]
) -> list[TickerContext]:
    """watch + macro, refusing shared symbols — a ticker on both lists would
    double-insert the run_tickers primary key and kill the whole run."""
    watched = {ctx.ticker.upper() for ctx in watch}
    collisions = sorted(ctx.ticker for ctx in macro if ctx.ticker.upper() in watched)
    if collisions:
        raise ValueError(
            "these symbols are on both watchlist.yaml and macro.yaml — remove one: "
            + ", ".join(collisions)
        )
    return [*watch, *macro]


@dataclass(frozen=True)
class Secrets:
    """Source credentials from the environment — never CLI flags, never the
    watchlist. None → that source is omitted at wiring time and the digest
    discloses the degradation (partial-failure policy)."""

    finnhub_api_key: str | None
    edgar_contact_email: str | None


def resolve_secrets() -> Secrets:
    return Secrets(
        finnhub_api_key=os.environ.get("FINNHUB_API_KEY") or None,
        edgar_contact_email=os.environ.get("ARGUS_CONTACT_EMAIL") or None,
    )


def pdf_enabled() -> bool:
    """PDF report attachment, on by default; ARGUS_PDF=0 turns it off."""
    return os.environ.get("ARGUS_PDF", "1").strip().lower() not in ("0", "false", "no")


def resolve_discord_webhook() -> str | None:
    """ARGUS_DISCORD_WEBHOOK turns Discord delivery on. A single value —
    nothing to half-configure."""
    return os.environ.get("ARGUS_DISCORD_WEBHOOK") or None


@dataclass(frozen=True)
class EmailConfig:
    """SMTP submission settings for the email digest sink. Presence of
    ARGUS_EMAIL_TO turns email delivery on; the credentials must then be
    complete — a half-configured channel fails loudly at the config
    boundary, never as a silently-skipped delivery."""

    host: str
    port: int
    username: str
    password: str
    sender: str
    recipient: str


def resolve_email_config() -> EmailConfig | None:
    recipient = os.environ.get("ARGUS_EMAIL_TO") or None
    if recipient is None:
        return None
    username = os.environ.get("ARGUS_SMTP_USER") or None
    password = os.environ.get("ARGUS_SMTP_PASSWORD") or None
    if username is None or password is None:
        raise ValueError(
            "ARGUS_EMAIL_TO is set but ARGUS_SMTP_USER/ARGUS_SMTP_PASSWORD are not — "
            "email delivery is half-configured"
        )
    return EmailConfig(
        host=os.environ.get("ARGUS_SMTP_HOST", "smtp.gmail.com"),
        port=int(os.environ.get("ARGUS_SMTP_PORT", "465")),
        username=username,
        password=password,
        sender=os.environ.get("ARGUS_EMAIL_FROM", username),
        recipient=recipient,
    )
