"""Path resolution and watchlist config.

Everything lives under one project directory by default (watchlist.yaml,
argus.db, reports/), overridable per path or via ARGUS_HOME. The engine never
sees "the watchlist" — this module turns it into list[TickerContext], which is
exactly what scout will construct differently later.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field as PydanticField

from argus.models import Thresholds, TickerContext

DEFAULT_WATCHLIST = "watchlist.yaml"
DEFAULT_DB = "argus.db"
DEFAULT_REPORTS = "reports"


@dataclass(frozen=True)
class Paths:
    root: Path
    watchlist: Path
    db: Path
    reports: Path


def resolve_paths(
    root: Path | None = None,
    *,
    watchlist: Path | None = None,
    db: Path | None = None,
    reports: Path | None = None,
) -> Paths:
    """Project-dir defaults; explicit paths win; ARGUS_HOME beats cwd."""
    base = (root or Path(os.environ.get("ARGUS_HOME", "."))).resolve()
    return Paths(
        root=base,
        watchlist=(watchlist or base / DEFAULT_WATCHLIST).resolve(),
        db=(db or base / DEFAULT_DB).resolve(),
        reports=(reports or base / DEFAULT_REPORTS).resolve(),
    )


class WatchlistEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = PydanticField(min_length=1)
    thesis: str | None = None
    thresholds: dict[str, int | float] = {}  # partial overrides; keys validated on merge


class WatchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    defaults: Thresholds = Thresholds()
    tickers: tuple[WatchlistEntry, ...] = ()


def load_watch_config(path: Path) -> WatchConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
        contexts.append(
            TickerContext(ticker=entry.ticker, thesis=entry.thesis, thresholds=Thresholds(**merged))
        )
    return contexts


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
