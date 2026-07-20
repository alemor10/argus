"""Scrub secrets from strings before they are stored or echoed.

Provider errors (httpx) embed the request URL, which for Finnhub carries
`?token=<api_key>` and for a Discord/Slack webhook IS the secret. Those error
strings flow into run notes, ticker/source errors, and the "not delivered"
echo — so any secret in them would land in the SQLite DB, the reports, or the
logs. This module is the one choke point that redacts them; apply it wherever
an untrusted error string crosses into storage or output.
"""

import re

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ?token=…  /  &api_key=…  /  key=…  query params (secret is the value)
    (re.compile(r'((?:token|api[_-]?key|key|secret)=)[^&\s"\'<>]+', re.IGNORECASE), r"\1REDACTED"),
    # Discord/Slack webhook URLs — the path IS the credential.
    (re.compile(r"(https://discord(?:app)?\.com/api/webhooks/)\S+", re.IGNORECASE), r"\1REDACTED"),
    (re.compile(r"(https://hooks\.slack\.com/services/)\S+", re.IGNORECASE), r"\1REDACTED"),
)


def redact(text: str | None) -> str:
    """Return `text` with known secret patterns replaced by REDACTED. Safe on
    None/empty (returns '')."""
    if not text:
        return ""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
