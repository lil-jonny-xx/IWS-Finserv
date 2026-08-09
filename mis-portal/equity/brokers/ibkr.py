"""
Interactive Brokers wrapper — Flex Web Service (automated, no gateway needed).

Currency : USD (per-position currency is read from the statement; the account
           base currency, used for cash, defaults to USD).
Auth     : A Flex Web Service token + a saved Flex Query that includes the
           "Open Positions" (and optionally "Cash Report") sections. The daily
           sync pulls the generated statement over HTTPS — no TWS / IB Gateway
           session required.

Setup (one-time, in IBKR Client Portal → Settings → Account Settings):
  1. Reporting → Flex Web Service → enable, generate a Token (valid ~1 year).
  2. Reporting → Flex Queries → create an *Activity* Flex Query with the
     "Open Positions" section (Summary level) and "Cash Report" section.
     Format: XML. Note the Query ID.

Env vars (per entity, replace {CODE} with the entity code e.g. DHR):
  IBKR_{CODE}_FLEX_TOKEN      — Flex Web Service token
  IBKR_{CODE}_QUERY_ID        — saved Flex Query ID. As of 2026-06-26 this query
                                bundles Open Positions + Cash Report + Trades (365d),
                                so fetch_all() gets holdings, cash AND the trade ledger
                                from a single SendRequest per login (one call/day).
  IBKR_{CODE}_TRADES_QUERY_ID — optional; trades-only query for the inception backfill
                                (>1yr history via fd/td windows). Not used by the daily
                                path now that QUERY_ID carries 365d of trades.
  IBKR_{CODE}_BASE_CURRENCY   — optional; account base currency for cash (default USD)
"""
import logging
import os
import random
import time
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path

import requests

from equity.models import EquityHolding

logger = logging.getLogger(__name__)

SUPPORTED_ENTITIES: list[str] = []   # configured via .env per entity
CURRENCY = "USD"
_LABEL   = "ibkr"

_SEND_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
_GET_URL  = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"
_VERSION  = "3"

# Flex Web Service rate limits (per IBKR docs): 1 request/second AND 10
# requests/minute, PER TOKEN. Exceeding returns 1018 ("too many requests from
# this token"); bursts can also trip a transient 1001 ("statement could not be
# generated"), and 1009 means the server is busy / 1019 "in progress" — all
# retryable. We pace EVERY Flex request (SendRequest + GetStatement polls) through
# _flex_get(), which holds a randomized minimum gap between calls so we stay well
# under the 10/min ceiling and never hit the service with a perfectly periodic
# cadence (which can itself trip the server-side throttle). The gap is tunable via
# .env for ad-hoc backfills.
# 1025 ("too many failed attempts") is a hard cooldown LOCKOUT — never retry it;
# retrying only deepens it. The transient/throttle codes below get a SMALL number
# of retries with a LONG randomized wait: rapid sub-minute retries are precisely
# what escalate a transient 1001 into a token-wide throttle and then a 1025
# (learned the hard way — the cure for 1001 is silence, not more requests).
# Transient "try again shortly" codes: server busy (1009), rate (1018), still
# generating (1019), and the various "data not ready yet" codes (1004 incomplete,
# 1005 settlement, 1006 FIFO P/L, 1007 MTM P/L, 1008 MTM+FIFO) — the last group
# shows up when we pull soon after the close, before IBKR's overnight batch has
# baked realised/settlement sub-sections. All are safe to retry with a LONG wait.
_RETRYABLE_SEND_CODES = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019"}
# Persistent auth/config failures — a human must re-issue the token or fix the
# query. NEVER retried (waiting can't fix them) and worth an ALERT: a dead token
# silently freezes foreign holdings (this is exactly what happened to the DHR
# tokens). 1010 legacy, 1011 inactive, 1012 expired, 1013 IP-restricted,
# 1014 invalid query, 1015 invalid token, 1016 invalid account, 1020 invalid request.
_AUTH_FAIL_CODES = {"1010", "1011", "1012", "1013", "1014", "1015", "1016", "1020"}
_SEND_RETRIES = int(os.environ.get("IBKR_FLEX_SEND_RETRIES", "2"))           # total attempts (1 retry)
_SEND_BACKOFF = float(os.environ.get("IBKR_FLEX_THROTTLE_WAIT_SEC", "90"))   # base s between throttle retries (× attempt)


class FlexAuthError(RuntimeError):
    """A persistent auth/config Flex failure (token expired/invalid/blocked, bad
    query/account). Carries the numeric `.code` so callers can alert a human instead
    of retrying — retrying never fixes these and only risks a 1025 cooldown."""
    def __init__(self, code: str, msg: str):
        super().__init__(f"IBKR Flex auth/config error {code}: {msg}")
        self.code = code
        self.flex_msg = msg

# Randomized minimum gap between consecutive Flex requests. Defaults give a
# 7–13s spacing (≈ 4.6–8.6 req/min, comfortably under the 10/min limit).
_FLEX_MIN_GAP = float(os.environ.get("IBKR_FLEX_MIN_GAP_SEC", "7"))   # base seconds
_FLEX_JITTER  = float(os.environ.get("IBKR_FLEX_JITTER_SEC", "6"))    # random 0..this, added
_last_req_ts  = 0.0   # monotonic timestamp of the last Flex request (module-wide pacing)

# In-process cache of generated statements. Holdings AND cash come from the SAME Flex
# query (Open Positions + Cash Report), so fetch_holdings() + fetch_cash_balance() would
# otherwise fire TWO SendRequests for one query back-to-back — exactly the rapid
# same-query regeneration that escalates a transient 1001 into a token-wide throttle and
# then a 1025 lockout. Caching the parsed statement briefly lets the second call reuse the
# first's result with zero extra requests. A Flex statement is a point-in-time daily
# snapshot, so reuse within the TTL is correct.
_STMT_CACHE: dict = {}
_STMT_CACHE_TTL = float(os.environ.get("IBKR_FLEX_STMT_CACHE_SEC", "600"))   # seconds

# PERSISTENT (on-disk) fallback. A Flex statement is a daily snapshot, so when a live
# fetch is throttled (1001/1025) the last statement we successfully pulled is still a far
# better answer than nothing/very-stale DB values. Every successful daily fetch is saved
# here; on a live failure we fall back to the most recent saved copy if it's within
# _STMT_DISK_MAX_AGE. Only the daily query (no date range) is disk-cached — backfill date
# windows are one-off and must not be reused.
_STMT_DISK_DIR = Path(os.environ.get(
    "IBKR_STMT_CACHE_DIR", "/var/www/mis-portal/.ibkr_statements"))
_STMT_DISK_MAX_AGE = float(os.environ.get("IBKR_STMT_CACHE_MAX_AGE_DAYS", "7")) * 86400

# Refetch FLOOR — the minimum age an on-disk daily statement must reach before we
# regenerate it. A Flex activity statement is a once-daily post-EOD snapshot, so a
# second pull within a few hours yields ~identical data; skipping it is both correct
# and the single best defence against the rapid same-query regeneration that escalates
# 1001 → token-wide throttle → 1025. The two scheduled runs (pre-open ET + post-close
# ET) are ≥9h apart, so this never suppresses a legitimate run — only reruns, double
# fires, holiday re-triggers, and ad-hoc manual pulls. Set to 0 to disable.
_FLEX_MIN_REFETCH = float(os.environ.get("IBKR_FLEX_MIN_REFETCH_SEC", str(6 * 3600)))   # 6h


def _disk_cache_path(acct_prefix: str, query_id: str) -> "Path":
    return _STMT_DISK_DIR / f"{acct_prefix}_{query_id}.xml"


def _save_statement_disk(acct_prefix: str, query_id: str, stmt: ET.Element) -> None:
    try:
        _STMT_DISK_DIR.mkdir(parents=True, exist_ok=True)
        _disk_cache_path(acct_prefix, query_id).write_bytes(ET.tostring(stmt))
    except Exception as e:
        logger.warning(f"[{acct_prefix}] could not save IBKR statement to disk: {e}")


def _load_statement_disk(acct_prefix: str, query_id: str):
    """Return (stmt, age_seconds) from the on-disk copy if it exists and is within
    _STMT_DISK_MAX_AGE; else None."""
    path = _disk_cache_path(acct_prefix, query_id)
    try:
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > _STMT_DISK_MAX_AGE:
            logger.warning(f"[{acct_prefix}] on-disk IBKR statement is {age/86400:.1f}d old "
                           f"(> {_STMT_DISK_MAX_AGE/86400:.0f}d cap) — not using it")
            return None
        return ET.fromstring(path.read_bytes()), age
    except Exception as e:
        logger.warning(f"[{acct_prefix}] could not read cached IBKR statement: {e}")
        return None


def _flex_get(url: str, params: dict, timeout: int = 60) -> "requests.Response":
    """Paced GET against the Flex Web Service.

    Blocks until at least a randomized _FLEX_MIN_GAP .. (_FLEX_MIN_GAP+_FLEX_JITTER)
    seconds have elapsed since the previous Flex request, keeping us under IBKR's
    1/sec & 10/min per-token limit and avoiding a perfectly periodic request
    pattern that can still trip the 1001/1018 throttle.
    """
    global _last_req_ts
    gap  = _FLEX_MIN_GAP + random.uniform(0, _FLEX_JITTER)
    wait = _last_req_ts + gap - time.monotonic()
    if wait > 0:
        logger.debug(f"IBKR Flex pacing: sleeping {wait:.1f}s before next request")
        time.sleep(wait)
    try:
        return requests.get(url, params=params, timeout=timeout)
    finally:
        _last_req_ts = time.monotonic()

# entity_name (DB) → env var prefix when they differ (matches the other adapters)
_ENV_PREFIX = {
    "Rajani Corp": "RAJANIRCORP",
}


def _resolve_prefix(entity_code: str) -> str:
    return _ENV_PREFIX.get(entity_code, entity_code)


def _get(acct_prefix: str, key: str, required: bool = True, default: str = "") -> str:
    """Read IBKR_{acct_prefix}_{key} from the environment."""
    val = os.environ.get(f"IBKR_{acct_prefix}_{key}", default)
    if required and not val:
        raise KeyError(f"IBKR_{acct_prefix}_{key} not set in .env")
    return val


def _env(entity_code: str, key: str, required: bool = True, default: str = "") -> str:
    """Back-compat shim: read a key from the entity's PRIMARY login only."""
    return _get(_resolve_prefix(entity_code), key, required, default)


def _account_prefixes(entity_code: str) -> list[str]:
    """Env-var prefixes for every IBKR login that feeds this entity.

    A single portal entity (e.g. DHR) can aggregate more than one IBKR login: the
    primary prefix (IBKR_DHR_*) plus numbered extras (IBKR_DHR_2_*, IBKR_DHR_3_*,
    ...). Use the extras when one master login covers some accounts but another
    account is logged in separately — each login needs its own token/query, yet
    all their holdings/trades roll up under the one entity.
    """
    base = _resolve_prefix(entity_code)
    prefixes: list[str] = []
    if os.environ.get(f"IBKR_{base}_FLEX_TOKEN"):
        prefixes.append(base)
    n = 2
    while os.environ.get(f"IBKR_{base}_{n}_FLEX_TOKEN"):
        prefixes.append(f"{base}_{n}")
        n += 1
    if not prefixes:
        raise KeyError(f"IBKR_{base}_FLEX_TOKEN not set in .env")
    return prefixes


def cash_currency(entity_code: str) -> str:
    return _env(entity_code, "BASE_CURRENCY", required=False, default=CURRENCY) or CURRENCY


# ---------------------------------------------------------------------------
# Flex Web Service — two-step fetch
# ---------------------------------------------------------------------------

def _fetch_statement_xml(
    acct_prefix: str,
    query_id: str = "",
    from_date: "date | None" = None,
    to_date: "date | None" = None,
) -> ET.Element:
    query_id = query_id or _get(acct_prefix, "QUERY_ID")
    cache_key = (acct_prefix, query_id,
                 from_date.isoformat() if from_date else None,
                 to_date.isoformat() if to_date else None)
    cached = _STMT_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _STMT_CACHE_TTL:
        logger.info(f"[{acct_prefix}] IBKR Flex statement cache hit (q={query_id}) — "
                    f"reusing, no new SendRequest")
        return cached[1]

    daily = from_date is None and to_date is None   # only the daily query is disk-cached

    # Proactive refetch floor: if we already have a recent on-disk daily statement,
    # reuse it instead of regenerating. The data hasn't changed (daily snapshot), and
    # this is what keeps double-fires / holiday re-triggers / manual pulls from turning
    # into the rapid same-query regeneration that trips 1025. Date-ranged (backfill)
    # queries are one-off and never reused. See _FLEX_MIN_REFETCH.
    if daily and _FLEX_MIN_REFETCH > 0:
        fresh = _load_statement_disk(acct_prefix, query_id)
        if fresh is not None and fresh[1] < _FLEX_MIN_REFETCH:
            stmt, age = fresh
            logger.info(f"[{acct_prefix}] IBKR Flex: on-disk statement only {age/60:.0f} min old "
                        f"(< {_FLEX_MIN_REFETCH/60:.0f} min refetch floor) — reusing, no SendRequest "
                        f"(q={query_id})")
            _STMT_CACHE[cache_key] = (time.monotonic(), stmt)
            return stmt

    try:
        stmt = _fetch_statement_xml_live(acct_prefix, query_id, from_date, to_date)
    except Exception as e:
        # Throttled (1001/1025) or otherwise unavailable: fall back to the last statement
        # we successfully saved for this query, if it's still fresh enough. A day-old
        # snapshot beats blanking out holdings/cash. Re-raise if there's nothing to use.
        if daily:
            fallback = _load_statement_disk(acct_prefix, query_id)
            if fallback is not None:
                stmt, age = fallback
                logger.warning(f"[{acct_prefix}] live Flex fetch failed ({e}); USING CACHED "
                               f"on-disk statement from {age/3600:.1f}h ago (q={query_id})")
                _STMT_CACHE[cache_key] = (time.monotonic(), stmt)
                return stmt
        raise

    _STMT_CACHE[cache_key] = (time.monotonic(), stmt)
    if daily:
        _save_statement_disk(acct_prefix, query_id, stmt)
    return stmt


def _fetch_statement_xml_live(
    acct_prefix: str,
    query_id: str,
    from_date: "date | None" = None,
    to_date: "date | None" = None,
) -> ET.Element:
    """Live two-step Flex fetch (SendRequest → poll GetStatement). Raises on failure."""
    token = _get(acct_prefix, "FLEX_TOKEN")

    # Step 1 — request statement generation, get a reference code.
    # fd/td override the saved query's date period (yyyymmdd, max 365-day span).
    params = {"t": token, "q": query_id, "v": _VERSION}
    if from_date and to_date:
        params["fd"] = from_date.strftime("%Y%m%d")
        params["td"] = to_date.strftime("%Y%m%d")

    # IBKR paces statement generation: regenerating the same query in quick
    # succession returns Status=Fail / ErrorCode 1001 ("could not be generated at
    # this time, try again shortly"). That's transient — back off and retry.
    root = None
    for attempt in range(_SEND_RETRIES):
        resp = _flex_get(_SEND_URL, params, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        status = (root.findtext("Status") or "").strip()
        if status == "Success":
            break
        code = (root.findtext("ErrorCode") or "").strip()
        msg  = (root.findtext("ErrorMessage") or "").strip()
        if code == "1025":
            # Hard cooldown lockout — retrying extends it. Bail immediately.
            raise RuntimeError(
                f"[{acct_prefix}] IBKR Flex 1025 (too many failed attempts): token is in a "
                f"cooldown LOCKOUT — stop all requests and let it rest (do NOT retry). {msg}")
        if code in _AUTH_FAIL_CODES:
            # Dead/blocked token, or bad query/account — a human must fix it. Surface a
            # typed error so the worker alerts instead of silently freezing holdings.
            raise FlexAuthError(code, f"[{acct_prefix}] {msg}")
        retryable = code in _RETRYABLE_SEND_CODES or "try again" in msg.lower()
        if retryable and attempt < _SEND_RETRIES - 1:
            # Linear backoff with jitter, on top of the per-request pacing gap.
            wait = _SEND_BACKOFF * (attempt + 1) + random.uniform(0, _FLEX_JITTER)
            logger.info(f"[{acct_prefix}] IBKR Flex SendRequest pacing "
                        f"({code} {msg}); retry {attempt + 1}/{_SEND_RETRIES - 1} in {wait:.0f}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"[{acct_prefix}] IBKR Flex SendRequest failed: {code} {msg}")

    reference_code = (root.findtext("ReferenceCode") or "").strip()
    get_url        = (root.findtext("Url") or _GET_URL).strip()
    if not reference_code:
        raise RuntimeError(f"[{acct_prefix}] IBKR Flex: no ReferenceCode returned")

    # Step 2 — poll for the generated statement (IBKR may report "in progress").
    last_msg = ""
    for attempt in range(6):
        # _flex_get already enforces a randomized gap (≥7s), which doubles as the
        # wait for the statement to finish generating — no extra fixed sleep needed.
        r = _flex_get(
            get_url,
            {"t": token, "q": reference_code, "v": _VERSION},
            timeout=60,
        )
        r.raise_for_status()
        stmt = ET.fromstring(r.content)

        # Ready statements are rooted at <FlexQueryResponse>. A still-generating
        # or errored response comes back as <FlexStatementResponse> with a Status.
        if stmt.tag == "FlexQueryResponse":
            logger.info(f"[{acct_prefix}] IBKR Flex statement retrieved (attempt {attempt + 1})")
            return stmt

        last_msg = f"{stmt.findtext('ErrorCode')} {stmt.findtext('ErrorMessage')}"
        logger.info(f"[{acct_prefix}] IBKR Flex not ready ({last_msg}); retrying")

    raise RuntimeError(f"[{acct_prefix}] IBKR Flex statement not ready after retries: {last_msg}")


# ---------------------------------------------------------------------------
# Holdings fetch
# ---------------------------------------------------------------------------

def fetch_holdings(entity_code: str) -> list[dict]:
    """
    Open positions from the Flex statement, as attribute dicts.

    Pulls every IBKR login that feeds this entity (primary + numbered extras) and
    concatenates the rows; the same symbol can appear once per (sub)account, so
    normalise() sums them. Relevant OpenPosition attributes:
      symbol, isin, currency, position, costBasisPrice, markPrice,
      listingExchange, assetCategory, levelOfDetail
    """
    prefixes = _account_prefixes(entity_code)
    positions: list[dict] = []
    for p in prefixes:
        root = _fetch_statement_xml(p)
        positions += [
            el.attrib
            for el in root.iter("OpenPosition")
            # SUMMARY rows aggregate the LOT rows — keep only SUMMARY to avoid
            # double-counting when the query is configured at lot level.
            if el.attrib.get("levelOfDetail", "SUMMARY") != "LOT"
        ]
    logger.info(f"[{entity_code}] IBKR: fetched {len(positions)} open positions "
                f"across {len(prefixes)} login(s)")
    return positions


def fetch_trades(
    entity_code: str,
    from_date: "date | None" = None,
    to_date: "date | None" = None,
) -> list[dict]:
    """
    All executed trades from the *Trades* Flex query, as attribute dicts.

    Uses a SEPARATE saved query per login — IBKR_{PREFIX}_TRADES_QUERY_ID — that
    has the "Trades" section enabled (the daily QUERY_ID is positions/cash only).
    Used by the one-time inception backfill (equity/ibkr_backfill_inception.py).
    Pulls every login feeding this entity and concatenates; ledger dedup is by
    `tradeID`, so trades from different accounts never collide.

    The Flex Web Service caps a single statement at 365 days, but DOES accept an
    fd/td date-range override (yyyymmdd) on SendRequest — so pass from_date/to_date
    to pull an arbitrary ~1-year window without editing the saved query. For an
    account older than a year, call once per year-window; the ledger upsert dedups
    by `tradeID`, so overlapping/re-imported trades are harmless.

    Relevant Trade attributes:
      symbol, isin, currency, tradeID, tradeDate (YYYYMMDD), buySell,
      quantity (signed: sells negative), tradePrice, ibCommission (negative),
      netCash (signed cash impact incl. commission)
    """
    trades: list[dict] = []
    for p in _account_prefixes(entity_code):
        query_id = _get(p, "TRADES_QUERY_ID")
        root = _fetch_statement_xml(p, query_id, from_date, to_date)
        trades += [el.attrib for el in root.iter("Trade")]
    logger.info(f"[{entity_code}] IBKR: fetched {len(trades)} trades")
    return trades


def fetch_cash_balance(entity_code: str) -> Decimal:
    """
    Ending cash from the Flex Cash Report, in the account base currency
    (BASE_SUMMARY row). Sums the BASE_SUMMARY row of every (sub)account across all
    logins feeding this entity. Returns 0 if no query has a Cash Report section.
    """
    total = Decimal("0")
    for p in _account_prefixes(entity_code):
        root = _fetch_statement_xml(p)
        total += _cash_from_root(root)
    return total


def _positions_from_root(root: ET.Element) -> list[dict]:
    # SUMMARY rows aggregate the LOT rows — keep only SUMMARY to avoid double-counting.
    return [el.attrib for el in root.iter("OpenPosition")
            if el.attrib.get("levelOfDetail", "SUMMARY") != "LOT"]


def _cash_from_root(root: ET.Element) -> Decimal:
    total = Decimal("0")
    for el in root.iter("CashReportCurrency"):
        if el.attrib.get("currency") == "BASE_SUMMARY":
            try:
                total += Decimal(str(el.attrib.get("endingCash") or "0"))
            except Exception:
                continue
    return total


def _asof_from_root(root: ET.Element) -> "date | None":
    """The date this statement's positions and cash are actually AS OF — its period
    end (`toDate`), NOT the day we happened to read it.

    This matters because a throttled live fetch silently falls back to the last
    on-disk statement (see _fetch_statement_xml), which can be several days old. If
    the caller stamps `date.today()` on that data, a stale position reads as current
    everywhere downstream — the portal, the reports, and the staleness monitor, for
    which as_of_date is the ONLY staleness signal.

    A login's statement can carry several FlexStatement elements (one per account,
    all generated together); they share a period, so max() just picks that period end.
    Returns None if the statement carries no parseable toDate, leaving the decision
    to the caller."""
    ends: list[date] = []
    for el in root.iter("FlexStatement"):
        raw = (el.attrib.get("toDate") or "").strip()
        if not raw:
            continue
        try:
            # Flex writes YYYYMMDD; some query configurations emit YYYY-MM-DD.
            ends.append(date.fromisoformat(raw) if "-" in raw
                        else date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8])))
        except Exception:
            continue
    return max(ends) if ends else None


def _cash_by_ccy_from_root(root: ET.Element) -> "dict[str, Decimal]":
    """Ending cash per NATIVE currency (AED / USD / GBP …) from the Cash Report.

    The report carries two levels of detail per (sub)account: levelOfDetail='Currency'
    rows hold the native per-currency balances, while 'BaseCurrency' rows hold the
    BASE_SUMMARY rollup that _cash_from_root sums. We keep ONLY the 'Currency' rows so
    the per-currency breakdown never double-counts the base rollup, and we skip zero
    balances (a currency swept to 0 simply isn't reported)."""
    out: "dict[str, Decimal]" = {}
    for el in root.iter("CashReportCurrency"):
        ccy = el.attrib.get("currency")
        if not ccy or ccy == "BASE_SUMMARY":
            continue
        if el.attrib.get("levelOfDetail") != "Currency":
            continue
        try:
            amt = Decimal(str(el.attrib.get("endingCash") or "0"))
        except Exception:
            continue
        if amt == 0:
            continue
        out[ccy] = out.get(ccy, Decimal("0")) + amt
    return out


def cash_by_currency(entity_code: str) -> "dict[str, Decimal]":
    """Per-currency ending cash for an entity, summed across all its logins.

    Reuses the statement already fetched by fetch_all/fetch_positions_and_cash (the
    in-process + disk cache), so this adds NO extra Flex request when called right
    after the daily fetch.

    Not every login's Flex query has the Cash Report's per-currency ('Currency') level
    of detail enabled — some emit only the 'BaseCurrency' rollup (BASE_SUMMARY). For
    such a login we fall back to attributing its base-summary total to that login's own
    base currency (IBKR_<prefix>_BASE_CURRENCY, default USD), so the breakdown still sums
    to the same figure as the consolidated broker_cash row. Logins that DO emit per-
    currency rows use them verbatim."""
    out: "dict[str, Decimal]" = {}
    for p in _account_prefixes(entity_code):
        try:
            root = _fetch_statement_xml(p)
        except Exception as e:
            logger.error(f"[{p}] IBKR per-currency cash read failed — {e}")
            continue
        per = _cash_by_ccy_from_root(root)
        if not per:
            # No per-currency detail in this login's query → book the base total under
            # the login's base currency (exact for single-currency accounts).
            base_total = _cash_from_root(root)
            if base_total != 0:
                ccy = (_get(p, "BASE_CURRENCY", required=False, default=CURRENCY)
                       or CURRENCY).upper()
                per = {ccy: base_total}
        for ccy, amt in per.items():
            out[ccy] = out.get(ccy, Decimal("0")) + amt
    return out


def _trades_from_root(root: ET.Element) -> list[dict]:
    return [el.attrib for el in root.iter("Trade")]


def fetch_all(entity_code: str) -> "tuple[list[dict], Decimal, list[dict], list[str], 'date | None']":
    """Open positions, ending cash AND executed trades from ONE Flex statement per login.

    The daily QUERY_ID now bundles the Trades section (last 365 days) alongside Open
    Positions + Cash Report, so a single SendRequest per login yields all three — never
    the back-to-back same-query regeneration that escalates 1001 → token-wide throttle →
    1025 lockout. This is the "make one call a day, get everything" path.

    Returns (positions, cash_in_base_currency, trades, failed_prefixes, as_of). One login
    failing does NOT discard the logins that succeeded, but the caller MUST treat a
    non-empty failed_prefixes as a PARTIAL result: positions/cash/trades for an entity
    aggregate across all its logins, so persisting a partial run would understate them.

    `as_of` is the OLDEST statement period end across the logins that answered — an
    entity's figures are an aggregate, so they are only as fresh as their stalest part.
    None when no login produced a parseable date. Callers must persist this rather than
    date.today(); a throttled fetch quietly serves a days-old on-disk statement, and
    stamping it with today's date hides that from every consumer downstream.
    """
    prefixes = _account_prefixes(entity_code)
    positions: list[dict] = []
    trades: list[dict] = []
    cash = Decimal("0")
    asofs: list[date] = []
    # Each failure carries the login prefix and, when known, the numeric Flex code
    # (auth_code set only for persistent auth/config failures) so the caller can tell a
    # dead token apart from a transient throttle and alert accordingly.
    failures: list[dict] = []
    for p in prefixes:
        try:
            root = _fetch_statement_xml(p)
        except FlexAuthError as e:
            logger.error(f"[{p}] IBKR fetch failed (auth/config) — {e}")
            failures.append({"prefix": p, "auth_code": e.code, "msg": e.flex_msg})
            continue
        except Exception as e:
            logger.error(f"[{p}] IBKR fetch failed — {e}")
            failures.append({"prefix": p, "auth_code": None, "msg": str(e)})
            continue
        positions += _positions_from_root(root)
        cash      += _cash_from_root(root)
        trades    += _trades_from_root(root)
        a = _asof_from_root(root)
        if a:
            asofs.append(a)
            stale = (date.today() - a).days
            if stale > 1:
                logger.warning(f"[{p}] IBKR statement is as of {a} ({stale}d old) — "
                               f"positions/cash will be recorded with THAT date, not today's")
    as_of = min(asofs) if asofs else None
    logger.info(f"[{entity_code}] IBKR: {len(positions)} positions + cash {cash} + "
                f"{len(trades)} trades from {len(prefixes) - len(failures)}/{len(prefixes)} "
                f"login(s)" + (f", as of {as_of}" if as_of else "")
                + (f"; FAILED: {[f['prefix'] for f in failures]}" if failures else ""))
    return positions, cash, trades, failures, as_of


def fetch_positions_and_cash(entity_code: str) -> "tuple[list[dict], Decimal, list[str]]":
    """Open positions AND ending cash from ONE Flex statement per login.

    Holdings and cash both come from the same query (Open Positions + Cash Report), so
    this pulls the statement a single time and parses both sections out of it — exactly
    one SendRequest per login, never the back-to-back same-query regeneration that trips
    the 1001 throttle.

    Returns (positions, cash_in_base_currency, failed_prefixes). One login failing does
    NOT discard the logins that succeeded, but the caller must treat a non-empty
    failed_prefixes as a PARTIAL result: positions/cash for this entity are an aggregate
    across all its logins, so persisting a partial run would understate cash/holdings.
    """
    prefixes = _account_prefixes(entity_code)
    positions: list[dict] = []
    cash = Decimal("0")
    failures: list[str] = []
    for p in prefixes:
        try:
            root = _fetch_statement_xml(p)
        except Exception as e:
            logger.error(f"[{p}] IBKR fetch failed — {e}")
            failures.append(p)
            continue
        positions += _positions_from_root(root)
        cash      += _cash_from_root(root)
    logger.info(f"[{entity_code}] IBKR: {len(positions)} positions + cash {cash} "
                f"from {len(prefixes) - len(failures)}/{len(prefixes)} login(s)"
                + (f"; FAILED: {failures}" if failures else ""))
    return positions, cash, failures


# ---------------------------------------------------------------------------
# Normalise to EquityHolding (native currency; converted to INR later)
# ---------------------------------------------------------------------------

def normalise(entity_id: int, entity_code: str, raw: list[dict]) -> list[EquityHolding]:
    # equity_holding is keyed (entity_id, broker, symbol), so the same symbol held
    # in more than one (sub)account must be merged here — otherwise the upsert
    # would overwrite rather than sum. Aggregate quantity and total cost (the cost
    # basis is summed; avg_cost is recomputed as a quantity-weighted average).
    agg: dict[str, dict] = {}
    for h in raw:
        try:
            qty = Decimal(str(h.get("position") or "0"))
            avg = Decimal(str(h.get("costBasisPrice") or "0"))
            ltp = Decimal(str(h.get("markPrice") or h.get("costBasisPrice") or "0"))
        except Exception:
            continue

        if qty <= 0:
            continue

        symbol = h.get("symbol", "") or h.get("isin", "")
        a = agg.get(symbol)
        if a is None:
            agg[symbol] = {
                "isin":     h.get("isin", "") or "",
                "exchange": h.get("listingExchange", "") or "NASDAQ",
                "qty":      qty,
                "cost":     qty * avg,
                "ltp":      ltp,
                "currency": (h.get("currency") or CURRENCY).upper(),
            }
        else:
            a["qty"]  += qty
            a["cost"] += qty * avg
            if ltp and not a["ltp"]:
                a["ltp"] = ltp

    result = []
    for symbol, a in agg.items():
        qty = a["qty"]
        if qty <= 0:
            continue
        avg = (a["cost"] / qty) if qty else Decimal("0")
        result.append(EquityHolding(
            entity_id            = entity_id,
            broker               = _LABEL,
            symbol               = symbol,
            isin                 = a["isin"],
            exchange             = a["exchange"],
            quantity             = qty,
            avg_cost             = avg.quantize(Decimal("0.0001")),
            cost                 = a["cost"].quantize(Decimal("0.01")),
            current_price        = a["ltp"],
            current_market_value = (qty * a["ltp"]).quantize(Decimal("0.01")),
            currency             = a["currency"],
        ))
    return result
