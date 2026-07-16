"""Well-known ETF rebalancing — constituent-change detection.

When a name is added to (or dropped from) a major index ETF, index funds are
forced to buy (or sell) it — knowing that a well-known ETF is rebalancing is
signal, and it works whether or not you hold the ETF. Argus watches a
configured set of ETFs, snapshots their membership, and reports the diff:
who entered, who left.

Data sources (verified live 2026-07-16), one adapter per issuer behind the
HoldingsSource protocol; `holdings_source_for` routes each ETF by a known
map:
  - SSGA / State Street SPDR — one uniform daily-holdings xlsx per fund
    (SPY, DIA, the eleven sector SPDRs, SDY, MDY, style/size funds).
  - Vanguard — the investor.vanguard.com portfolio-holding JSON per fund
    (VOO, VTI, VYM, VUG, …), full membership by ticker.
  - SEC N-PORT — the official monthly holdings filing, for funds whose issuer
    blocks headless requests (Schwab's SCHD, iShares). Never bot-blocked, but
    lagged (a filing lands weeks after its period) and CUSIP-keyed with no
    ticker. Because the rebalance only needs a STABLE identity plus a display
    name — not a ticker — the CUSIP→ticker join that the roadmap called the
    hardest piece is sidestepped entirely: diff on CUSIP, name the change by
    company. Fine for annually-reconstituted funds like SCHD (Dow Jones US
    Dividend 100, rebalanced each March); the lag is disclosed in the digest.
The issuer feeds are accepted eyes-open the same way yfinance and the TV
scanner are; N-PORT is official (needs a SEC contact-email User-Agent, like
EdgarSource). Holdings are CLAIMS: never gated, never observations, never a
delivery trigger by themselves; only the membership diff (an event) is.

Storage is a change-log, not a daily dump: a holdings snapshot is persisted
only when membership actually changes (the analyst-actions "first-seen"
philosophy). Most days nothing is stored and nothing is reported — silence
is a statement here too.
"""

import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import httpx

from argus.models import EtfHolding

_URL = (
    "https://www.ssga.com/us/en/intermediary/etfs/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-{etf}.xlsx"
)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class HoldingsError(RuntimeError):
    """The holdings feed failed wholesale (network, HTTP error, or an xlsx
    shape this module does not recognize). The digest reports the outage —
    silence is a statement — so this must raise, never guess at a changed
    contract."""


@runtime_checkable
class HoldingsSource(Protocol):
    """The ETF-holdings seam. iShares / Vanguard / SEC N-PORT slot in behind
    this later."""

    def fetch(self, etf: str) -> list[EtfHolding]: ...


class SsgaHoldingsSource:
    """One GET of an SSGA SPDR daily-holdings xlsx per ETF."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def fetch(self, etf: str) -> list[EtfHolding]:
        try:
            content = self._fetch_raw(etf)
        except Exception:  # free feeds hiccup: one inline retry, no framework
            try:
                content = self._fetch_raw(etf)
            except Exception as exc:
                raise HoldingsError(f"ssga: holdings fetch failed for {etf}: {exc}") from exc
        return self.parse(content, etf)

    def _fetch_raw(self, etf: str) -> bytes:
        """Network only. Recorded payloads in tests/fixtures/ssga/."""
        response = httpx.get(
            _URL.format(etf=etf.lower()),
            headers={"User-Agent": _USER_AGENT},
            timeout=self.timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "spreadsheet" not in content_type and "officedocument" not in content_type:
            # An HTML consent/404 shell instead of the xlsx — a changed
            # contract, not empty data. Fail loud.
            raise HoldingsError(f"ssga: {etf} returned {content_type or 'unknown'}, not an xlsx")
        return response.content

    def parse(self, content: bytes, etf: str) -> list[EtfHolding]:
        """Pure over the xlsx bytes. The sheet carries a fund-metadata block,
        then a header row (Name, Ticker, …, Weight, …), then constituents.
        A row without a real ticker (cash lines carry '-') is skipped; a
        sheet with no header or zero constituents raises — an empty holdings
        list would read as 'the fund holds nothing', a false statement."""
        import openpyxl

        try:
            workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
        except Exception as exc:
            raise HoldingsError(f"ssga: {etf} xlsx unreadable: {exc}") from exc
        rows = list(workbook.active.iter_rows(values_only=True))
        header_idx = next(
            (i for i, r in enumerate(rows) if r and any(_clean(c) == "Ticker" for c in r)),
            None,
        )
        if header_idx is None:
            raise HoldingsError(f"ssga: {etf} xlsx has no Ticker header — changed shape")
        header = [_clean(c) for c in rows[header_idx]]
        try:
            t_col = header.index("Ticker")
            w_col = header.index("Weight")
        except ValueError as exc:
            raise HoldingsError(f"ssga: {etf} xlsx missing Ticker/Weight columns") from exc
        n_col = header.index("Name") if "Name" in header else None

        holdings: list[EtfHolding] = []
        for row in rows[header_idx + 1 :]:
            if not row or t_col >= len(row):
                continue
            ticker = _normalize(row[t_col])
            if ticker is None:
                continue  # cash / derivative / footer line
            holdings.append(
                EtfHolding(
                    ticker=ticker,
                    weight=_num(row[w_col]) if w_col < len(row) else 0.0,
                    name=_clean(row[n_col]) if n_col is not None and n_col < len(row) else None,
                )
            )
        if not holdings:
            raise HoldingsError(f"ssga: {etf} xlsx parsed to zero constituents")
        return holdings


class VanguardHoldingsSource:
    """One GET of a Vanguard fund's portfolio-holding JSON per ETF."""

    _URL = (
        "https://investor.vanguard.com/investment-products/etfs/profile/api/"
        "{etf}/portfolio-holding/stock"
    )

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def fetch(self, etf: str) -> list[EtfHolding]:
        try:
            payload = self._fetch_raw(etf)
        except Exception:
            try:
                payload = self._fetch_raw(etf)
            except Exception as exc:
                raise HoldingsError(f"vanguard: holdings fetch failed for {etf}: {exc}") from exc
        return self.parse(payload, etf)

    def _fetch_raw(self, etf: str) -> object:
        response = httpx.get(
            self._URL.format(etf=etf.upper()),
            params={"start": 1, "count": 5000},  # one page for any fund
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=self.timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.json()

    def parse(self, payload: object, etf: str) -> list[EtfHolding]:
        """Pure. fund.entity[] carries one row per holding with a ticker and
        percentWeight (a string). Rows without a real ticker (cash, futures)
        are skipped; an unrecognized body or zero constituents raises."""
        if not isinstance(payload, dict):
            raise HoldingsError(f"vanguard: {etf} body is not JSON object")
        entities = payload.get("fund", {}).get("entity")
        if not isinstance(entities, list):
            raise HoldingsError(f"vanguard: {etf} body has no fund.entity list — changed shape")
        holdings: list[EtfHolding] = []
        for row in entities:
            if not isinstance(row, dict):
                continue
            ticker = _normalize(row.get("ticker"))
            if ticker is None:
                continue
            holdings.append(
                EtfHolding(
                    ticker=ticker,
                    weight=_num(row.get("percentWeight")),
                    name=_clean(row.get("longName")) or None,
                )
            )
        if not holdings:
            raise HoldingsError(f"vanguard: {etf} parsed to zero constituents")
        return holdings


class NportHoldingsSource:
    """SEC N-PORT holdings, for funds whose issuer blocks headless requests.

    Two GETs behind the fund's ticker: (1) the SEC fund-ticker→series map, so
    SCHD resolves to its series id, and (2) that series' latest NPORT-P filing,
    whose primary_doc.xml lists every holding as name + CUSIP + pctVal. Official
    and never bot-blocked, but ~monthly and lagged — disclosed, and acceptable
    for funds that reconstitute at most a few times a year. Requires a SEC
    contact email in the User-Agent, exactly like EdgarSource."""

    _MF_MAP_URL = "https://www.sec.gov/files/company_tickers_mf.json"
    _BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
    # CUSIP placeholders N-PORT uses for instruments without one — never a key.
    _CUSIP_PLACEHOLDERS = frozenset({"N/A", "000000000", "0", ""})

    def __init__(self, contact_email: str, timeout: float = 30.0) -> None:
        self.contact_email = contact_email
        self.timeout = timeout
        self._series_map: dict[str, str] | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {"User-Agent": f"argus/0.1 {self.contact_email}"}

    def fetch(self, etf: str) -> list[EtfHolding]:
        try:
            xml = self._fetch_raw(etf)
        except HoldingsError:
            raise  # a resolved-but-empty result is not a transient fault
        except Exception:  # SEC hiccups: one inline retry, no framework
            try:
                xml = self._fetch_raw(etf)
            except HoldingsError:
                raise
            except Exception as exc:
                raise HoldingsError(f"nport: holdings fetch failed for {etf}: {exc}") from exc
        return self.parse(xml, etf)

    def _fetch_raw(self, etf: str) -> str:
        """Network only: ticker → series id → latest NPORT-P primary_doc.xml."""
        series_id = self._series_for(etf)
        atom = httpx.get(
            self._BROWSE_URL,
            params={
                "action": "getcompany",
                "CIK": series_id,
                "type": "NPORT-P",
                "count": "1",
                "output": "atom",
            },
            headers=self._headers,
            timeout=self.timeout,
        )
        atom.raise_for_status()
        match = re.search(r"<filing-href>([^<]+)</filing-href>", atom.text)
        if match is None:
            raise HoldingsError(f"nport: no NPORT-P filing for {etf} (series {series_id})")
        folder = match.group(1).rsplit("/", 1)[0]
        doc = httpx.get(folder + "/primary_doc.xml", headers=self._headers, timeout=self.timeout)
        doc.raise_for_status()
        return doc.text

    def _series_for(self, etf: str) -> str:
        """Resolve the fund ticker to its SEC series id via the official
        mutual-fund/ETF map (fetched once, then cached on the instance)."""
        if self._series_map is None:
            resp = httpx.get(self._MF_MAP_URL, headers=self._headers, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
            fields = payload["fields"]
            sym_i, ser_i = fields.index("symbol"), fields.index("seriesId")
            self._series_map = {row[sym_i]: row[ser_i] for row in payload["data"]}
        series_id = self._series_map.get(etf.strip().upper())
        if not series_id:
            raise HoldingsError(f"nport: {etf} not in SEC fund-ticker map")
        return series_id

    def parse(self, xml: str, etf: str) -> list[EtfHolding]:
        """Pure over the N-PORT primary_doc.xml. Each invstOrSec is one holding
        (name, cusip, pctVal = percent of net assets); a row with neither a
        usable cusip nor a name is skipped; zero holdings raises — an empty
        list would read as 'the fund holds nothing', a false statement."""
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise HoldingsError(f"nport: {etf} xml unparseable: {exc}") from exc
        holdings: list[EtfHolding] = []
        for inv in root.iter():
            if not _local_tag(inv) == "invstOrSec":
                continue
            name = _xml_child(inv, "name")
            cusip = _xml_child(inv, "cusip")
            if cusip is not None and cusip.upper() in self._CUSIP_PLACEHOLDERS:
                cusip = None  # placeholder → fall back to the name as identity
            if cusip is None and name is None:
                continue  # nothing stable to key or label on
            holdings.append(
                EtfHolding(cusip=cusip, name=name, weight=_num(_xml_child(inv, "pctVal")))
            )
        if not holdings:
            raise HoldingsError(f"nport: {etf} parsed to zero holdings")
        return holdings


# Which source serves each ETF. Extend as sources are added; an ETF not here is
# skipped with a disclosed note (Argus never guesses a feed).
_SSGA_ETFS = frozenset(
    "SPY DIA XLK XLF XLE XLV XLI XLY XLP XLU XLB XLRE XLC "
    "SDY MDY SPLG SPYG SPYV SPSM XBI KRE XHB XRT XME XOP".split()
)
_VANGUARD_ETFS = frozenset(
    "VOO VTI VUG VTV VYM VIG VGT VNQ VO VB VBR VEA VWO VXUS VT BND".split()
)
# Funds the issuer blocks headless — served by SEC N-PORT (needs a contact
# email). SCHD is why this exists; the iShares core funds ride the same path.
_NPORT_ETFS = frozenset("SCHD IVV IWM IJR IJH IWD IWF IWB IWR".split())


def is_nport_etf(etf: str) -> bool:
    """Whether this ETF is served by SEC N-PORT (lagged, filing-based) rather
    than a live issuer feed — a pure, reproducible check the digest uses to
    disclose freshness on a rebalance line."""
    return etf.strip().upper() in _NPORT_ETFS


def holdings_source_for(etf: str, contact_email: str | None = None) -> HoldingsSource | None:
    """The source adapter that serves this ETF, or None when Argus has no feed
    for it (disclosed by the caller, never guessed). N-PORT funds need a SEC
    contact email; without one they read as unserved, since SEC refuses
    anonymous requests."""
    symbol = etf.strip().upper()
    if symbol in _SSGA_ETFS:
        return SsgaHoldingsSource()
    if symbol in _VANGUARD_ETFS:
        return VanguardHoldingsSource()
    if symbol in _NPORT_ETFS and contact_email:
        return NportHoldingsSource(contact_email)
    return None


def membership_diff(
    prev: Sequence[EtfHolding], curr: Sequence[EtfHolding]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(added, dropped) constituents between two membership snapshots — a pure
    set diff keyed on each holding's identity (`key`: ticker, else CUSIP) and
    rendered by its `label` (ticker, else company name), alphabetical for
    determinism. Issuer feeds key and label on the same ticker (unchanged
    behavior); N-PORT keys on CUSIP but names the change by company."""
    prev_map = {h.key: h for h in prev if h.key}
    curr_map = {h.key: h for h in curr if h.key}
    added = tuple(sorted(curr_map[k].label for k in curr_map.keys() - prev_map.keys()))
    dropped = tuple(sorted(prev_map[k].label for k in prev_map.keys() - curr_map.keys()))
    return added, dropped


def _local_tag(el: ET.Element) -> str:
    """The element's tag without its XML namespace ({...}invstOrSec → invstOrSec)."""
    return el.tag.rsplit("}", 1)[-1]


def _xml_child(el: ET.Element, local: str) -> str | None:
    """First direct child with this local name, its text stripped, or None."""
    for child in el:
        if _local_tag(child) == local:
            text = (child.text or "").strip()
            return text or None
    return None


def _clean(raw: object) -> str:
    return str(raw).strip() if raw is not None else ""


def _normalize(raw: object) -> str | None:
    """House symbology (dashes for share classes), or None for a non-ticker
    cell (cash lines carry '-', footers carry prose)."""
    text = _clean(raw).upper().replace(".", "-")
    if not text or text == "-" or " " in text or len(text) > 12:
        return None
    return text


def _num(raw: object) -> float:
    """Weights arrive as floats (SSGA xlsx) or strings (Vanguard JSON)."""
    if isinstance(raw, bool):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value == value and abs(value) != float("inf") else 0.0  # NaN/inf guard
