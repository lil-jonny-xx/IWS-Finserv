#!/usr/bin/env python3
"""
Equity Price Worker — IWS MIS Portal

Fetches live LTP (last-traded price) for every row in equity_holding from the
appropriate broker API, then recomputes all derived metrics in-place.

Supported brokers: zerodha, angel_one, dhan
Credentials are stored in broker_api_credentials (JSONB) keyed per broker.

Runs every minute via a systemd timer, but exits immediately outside Indian
market hours (Mon–Fri 09:15–15:30 IST).

Cron fallback:
  * * * * * /var/www/.venv/bin/python /var/www/mis-portal/workers/equity_price_worker.py >> /var/log/mis-portal-equity-price.log 2>&1
"""

import os
import sys
import logging
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import zoneinfo

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # File persistence handled by systemd StandardOutput=append (see .service unit)
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

IST  = zoneinfo.ZoneInfo("Asia/Kolkata")
TWO  = Decimal("0.01")
FOUR = Decimal("0.0001")

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

MARKET_OPEN  = (9, 15)   # 09:15 IST
MARKET_CLOSE = (15, 30)  # 15:30 IST

EOD_SNAPSHOT_WINDOW_MINUTES = 5   # write history in last 5 min before close
BROKER_CASH_REFRESH_MIN = 30      # min minutes between broker-cash refreshes (cash moves slowly)


# ---------------------------------------------------------------------------
# Market hours guard
# ---------------------------------------------------------------------------
def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:          # Saturday / Sunday
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE


def is_eod_window() -> bool:
    """True in the last N minutes before market close — trigger daily snapshot."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    close_dt = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0)
    delta = (close_dt - now).total_seconds()
    return 0 <= delta <= EOD_SNAPSHOT_WINDOW_MINUTES * 60


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_db():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


def load_credentials(conn) -> dict:
    """Return {broker: cred_row} — one row per broker, preferring rows with a live access_token."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT broker, entity_id, credentials, access_token, token_expiry
        FROM broker_api_credentials
        WHERE is_active = TRUE
        ORDER BY broker,
                 CASE WHEN access_token IS NOT NULL AND access_token != '' THEN 0 ELSE 1 END
        """
    )
    rows = cur.fetchall()
    cur.close()
    result = {}
    for r in rows:
        broker = r["broker"]
        if broker not in result:
            result[broker] = r
    return result


def load_holdings(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT eh.id, eh.entity_id, eh.broker, eh.symbol, eh.exchange,
               eh.quantity, eh.cost, eh.prev_week_value, eh.first_invested_date,
               eh.isin, eh.angel_one_token
        FROM   equity_holding eh
        ORDER  BY eh.broker, eh.symbol
        """
    )
    rows = cur.fetchall()
    cur.close()
    return list(rows)


# ---------------------------------------------------------------------------
# Intraday holdings refresh (quantity / avg cost / new positions)
# ---------------------------------------------------------------------------
def refresh_holdings_light(conn):
    """Refresh quantity / avg cost and pick up new positions for Indian brokers.

    The 60s price loop only reprices EXISTING quantities, so a same-day buy or sell
    doesn't reach `equity_holding` until the twice-daily full sync. This closes that
    gap cheaply: it re-pulls live holdings (same `fetch_holdings`+`normalise` path as
    the full sync) and writes ONLY the columns that move when you trade —
    `quantity`, `avg_cost`, `cost` — plus inserts genuinely new positions. The heavy
    per-holding recompute (history lookups, ledger XIRR/CAGR, FX, daily snapshot)
    stays on the full sync; price / market value / P&L are refreshed immediately
    after by the normal price loop, now on the current quantities.

    Deliberately does NOT delete positions absent from the feed — same as the full
    sync — so a flaky/empty broker read can never wipe holdings (empty feed = skip).
    Domestic (INR) brokers only; foreign holdings have their own path. Disable via
    HOLDINGS_LIGHT_REFRESH_DISABLED=1 if a broker gets unhappy with the extra calls.
    """
    if os.getenv("HOLDINGS_LIGHT_REFRESH_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return

    try:
        from equity.equity_sync_worker import (
            load_entity_map, BROKER_ENTITY_MAP, _asset_class_for, classify_sector,
        )
        from equity.holdings_source import cached_fetch_holdings
    except Exception as e:
        logger.warning(f"holdings light-refresh: import failed — {e}")
        return

    try:
        emap = load_entity_map(conn)
        _asset_class_for(conn, "", "")   # prime the override cache once (no per-row DB hit)
    except Exception as e:
        logger.warning(f"holdings light-refresh: setup failed — {e}")
        return

    today = date.today()
    total_new = total_upd = 0

    for entity_code, broker_module, broker_label in BROKER_ENTITY_MAP:
        eid = emap.get(entity_code)
        if eid is None:
            continue
        try:
            # Authoritative fetcher: fetch fresh AND repopulate the shared cache so the
            # hourly snapshot and daily reconcile can reuse this pull instead of re-hitting
            # the broker.
            raw      = cached_fetch_holdings(broker_module, broker_label, entity_code, refresh=True)
            holdings = broker_module.normalise(eid, entity_code, raw)
        except NotImplementedError:
            continue
        except Exception as e:
            logger.warning(f"[{entity_code}/{broker_label}] holdings light-refresh fetch failed — {e}")
            conn.rollback()
            continue
        if not holdings:
            continue   # empty / flaky read — never wipe on a blank

        cur = conn.cursor()
        try:
            for h in holdings:
                qty, avg, cost = float(h.quantity), float(h.avg_cost), float(h.cost)
                isin = (h.isin or "").strip() or None

                updated = 0
                if isin:
                    # Natural key (entity, broker, ISIN); adopt any relabelled symbol.
                    cur.execute(
                        """
                        UPDATE equity_holding
                           SET quantity=%s, avg_cost=%s, cost=%s, symbol=%s,
                               exchange=COALESCE(%s, exchange), updated_at=NOW()
                         WHERE entity_id=%s AND broker=%s AND isin=%s
                        """,
                        (qty, avg, cost, h.symbol, h.exchange, eid, broker_label, isin),
                    )
                    updated = cur.rowcount
                if not updated:
                    # Legacy / null-ISIN rows: match on symbol, backfill the ISIN.
                    cur.execute(
                        """
                        UPDATE equity_holding
                           SET quantity=%s, avg_cost=%s, cost=%s,
                               isin=COALESCE(isin, %s), updated_at=NOW()
                         WHERE entity_id=%s AND broker=%s AND symbol=%s
                        """,
                        (qty, avg, cost, isin, eid, broker_label, h.symbol),
                    )
                    updated = cur.rowcount
                if updated:
                    total_upd += updated
                    continue

                # Genuinely new position — minimal INR row; the full sync enriches
                # metrics/FX later, the price loop sets live price/MV this same tick.
                # A stock first seen today was bought today, so its "Since" date
                # (first_invested_date) is today. The full sync preserves it
                # (fetch_first_invested_date reads it back), and CAGR stays NULL until
                # 1yr elapses (compute_metrics' ≥1yr guard), so no bogus 0-year figure.
                price = float(h.current_price) if h.current_price is not None else None
                mv    = (qty * price) if price is not None else None
                cur.execute(
                    """
                    INSERT INTO equity_holding
                        (entity_id, broker, symbol, isin, exchange, sector, asset_class,
                         quantity, avg_cost, cost, current_price, current_market_value,
                         currency, fx_rate, as_of_date, first_invested_date, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'INR',1,%s,%s,NOW())
                    ON CONFLICT (entity_id, broker, isin) DO NOTHING
                    """,
                    (eid, broker_label, h.symbol, isin, h.exchange,
                     classify_sector(h.symbol, isin or ""),
                     _asset_class_for(conn, h.symbol, isin or ""),
                     qty, avg, cost, price, mv, today, today),
                )
                total_new += cur.rowcount
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"[{entity_code}/{broker_label}] holdings light-refresh upsert failed — {e}")
        finally:
            cur.close()

    if total_new or total_upd:
        logger.info(f"Holdings light-refresh: {total_upd} updated, {total_new} new position(s)")


# ---------------------------------------------------------------------------
# Broker adapters
# ---------------------------------------------------------------------------
class ZerodhaAdapter:
    """Zerodha Kite Connect — needs api_key + access_token (refreshed daily)."""

    def __init__(self, cred: dict):
        from kiteconnect import KiteConnect
        api_key      = cred["credentials"].get("api_key", "")
        access_token = cred.get("access_token") or cred["credentials"].get("access_token", "")
        if not api_key or not access_token:
            raise ValueError("Zerodha: api_key and access_token required")
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)

    def get_ltp(self, holdings: list[dict]) -> dict[str, float]:
        """
        holdings: list of {symbol, exchange} dicts.
        Returns {symbol: ltp}.
        """
        instruments = []
        sym_map = {}
        for h in holdings:
            exchange = (h.get("exchange") or "NSE").upper()
            key = f"{exchange}:{h['symbol']}"
            instruments.append(key)
            sym_map[key] = h["symbol"]

        if not instruments:
            return {}

        data = self.kite.ltp(instruments)
        return {sym_map[k]: v["last_price"] for k, v in data.items() if k in sym_map}


class AngelOneAdapter:
    """Angel One SmartAPI — needs client_id, api_key, totp_secret, password."""

    def __init__(self, cred: dict):
        from SmartApi import SmartConnect
        import pyotp
        c = cred["credentials"]
        required = ("client_id", "api_key", "password", "totp_secret")
        missing = [k for k in required if not c.get(k)]
        if missing:
            raise ValueError(f"Angel One: missing credentials: {missing}")

        self.smart = SmartConnect(api_key=c["api_key"])
        totp  = pyotp.TOTP(c["totp_secret"]).now()
        self.smart.generateSession(c["client_id"], c["password"], totp)

    def get_ltp(self, holdings: list[dict]) -> dict[str, float]:
        prices = {}
        for h in holdings:
            token = (h.get("angel_one_token") or "").strip()
            if not token:
                logger.debug(f"Angel One: no symboltoken for {h['symbol']} — skipping LTP")
                continue
            try:
                exchange = (h.get("exchange") or "NSE").upper()
                resp = self.smart.ltpData(exchange, h["symbol"], token)
                if resp and resp.get("status") and resp.get("data"):
                    prices[h["symbol"]] = float(resp["data"]["ltp"])
            except Exception as e:
                logger.warning(f"Angel One LTP error for {h['symbol']}: {e}")
        return prices


class DhanAdapter:
    """Dhan HQ — live LTP read from the holdings feed.

    Dhan's ``get_holdings()`` already returns ``lastTradedPrice`` for every held
    security, so LTP is read straight from there. (The old per-symbol
    ``get_market_feed_ltp`` call was removed in dhanhq 2.x.) Tokens are managed
    per entity in .env by ``equity.brokers.dhan`` and auto-refreshed via TOTP on
    auth failure, so an expired token self-heals instead of silently freezing
    prices. Refresh happens lazily (only when a fetch fails), not every cycle.

    The DB ``cred`` row is unused — Dhan tokens/entities live in .env.
    """

    def __init__(self, cred: dict | None = None):
        from equity.brokers import dhan as dhan_broker
        self._dhan = dhan_broker

    def _fetch_holdings(self, entity_code: str) -> list[dict]:
        """Fetch holdings; on failure refresh the token once and retry."""
        try:
            return self._dhan.fetch_holdings(entity_code)
        except Exception as e:
            logger.warning(
                f"Dhan[{entity_code}]: holdings fetch failed ({e}); "
                f"refreshing token and retrying once"
            )
            self._dhan.refresh_access_token(entity_code)
            return self._dhan.fetch_holdings(entity_code)

    def get_ltp(self, holdings: list[dict]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for entity_code in self._dhan.SUPPORTED_ENTITIES:
            try:
                raw = self._fetch_holdings(entity_code)
            except Exception as e:
                logger.error(f"Dhan[{entity_code}]: LTP unavailable — {e}")
                continue
            for h in raw:
                sym = h.get("tradingSymbol")
                ltp = h.get("lastTradedPrice") or h.get("closingPrice")
                if not sym or not ltp:
                    continue
                try:
                    prices[sym] = float(ltp)
                except (TypeError, ValueError):
                    continue
        return prices


ADAPTER_MAP = {
    "zerodha":   ZerodhaAdapter,
    "angel_one": AngelOneAdapter,
    "dhan":      DhanAdapter,
}


def build_adapter(broker: str, cred: dict):
    cls = ADAPTER_MAP.get(broker)
    if not cls:
        raise ValueError(f"Unknown broker: {broker}")
    return cls(cred)


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------
def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def compute_metrics(holding: dict, ltp: float) -> dict:
    qty   = _d(holding["quantity"])
    cost  = _d(holding["cost"])
    prev  = _d(holding["prev_week_value"]) if holding.get("prev_week_value") is not None else None
    price = Decimal(str(ltp))

    cmv            = (qty * price).quantize(TWO, ROUND_HALF_UP)
    weekly_change  = (cmv - prev).quantize(TWO, ROUND_HALF_UP) if prev is not None else None
    pnl_inception  = (cmv - cost).quantize(TWO, ROUND_HALF_UP)

    returns_inception_pct = (
        (pnl_inception / cost * 100).quantize(TWO, ROUND_HALF_UP) if cost else None
    )

    # CAGR inception (requires at least 1 year to be meaningful; tiny years → huge exponent → overflow)
    cagr = None
    if holding.get("first_invested_date") and cost and cost > 0 and cmv > 0:
        years = (date.today() - holding["first_invested_date"]).days / 365.25
        if years >= 1.0:
            try:
                cagr = (
                    ((cmv / cost) ** Decimal(str(1 / years)) - 1) * 100
                ).quantize(TWO, ROUND_HALF_UP)
            except Exception:
                cagr = None

    return {
        "current_price":         float(price),
        "current_market_value":  float(cmv),
        "weekly_change":         float(weekly_change) if weekly_change is not None else None,
        "pnl_inception":         float(pnl_inception),
        "returns_inception_pct": float(returns_inception_pct) if returns_inception_pct is not None else None,
        "cagr_inception_pct":    float(cagr) if cagr is not None else None,
        "updated_at":            datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------------------
# DB update
# ---------------------------------------------------------------------------
def update_holdings_batch(conn, updates: list[dict]):
    """Bulk-update equity_holding rows with new prices + metrics."""
    if not updates:
        return
    cur = conn.cursor()
    cur.executemany(
        """
        UPDATE equity_holding SET
            current_price         = %(current_price)s,
            current_market_value  = %(current_market_value)s,
            weekly_change         = %(weekly_change)s,
            pnl_inception         = %(pnl_inception)s,
            returns_inception_pct = %(returns_inception_pct)s,
            cagr_inception_pct    = %(cagr_inception_pct)s,
            updated_at            = %(updated_at)s
        WHERE id = %(id)s
        """,
        updates,
    )
    conn.commit()
    cur.close()


def mark_broker_synced(conn, broker: str):
    cur = conn.cursor()
    cur.execute(
        "UPDATE broker_api_credentials SET last_synced_at = NOW() WHERE broker = %s",
        (broker,),
    )
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# EOD snapshot
# ---------------------------------------------------------------------------
def write_eod_snapshot(conn, holdings: list[dict], ltp_map: dict[str, float]):
    """
    Write a row to equity_holding_history for each holding that has a price.
    Only one snapshot per (entity, broker, symbol, snapshot_date).
    """
    today = date.today()
    cur = conn.cursor()
    inserted = 0
    for h in holdings:
        ltp = ltp_map.get(h["symbol"])
        if ltp is None:
            continue
        qty   = _d(h["quantity"])
        cost  = _d(h["cost"])
        price = Decimal(str(ltp))
        cmv   = qty * price
        pnl   = cmv - cost
        try:
            cur.execute(
                """
                INSERT INTO equity_holding_history
                    (entity_id, broker, symbol, isin, snapshot_date,
                     quantity, close_price, market_value, cost, pnl)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    h["entity_id"], h["broker"], h["symbol"], h.get("isin") or None, today,
                    float(qty), float(price), float(cmv), float(cost), float(pnl),
                ),
            )
            inserted += cur.rowcount
        except Exception as e:
            logger.warning(f"EOD snapshot error for {h['symbol']}: {e}")
    conn.commit()
    cur.close()
    if inserted:
        logger.info(f"EOD snapshot: wrote {inserted} rows for {today}")


# ---------------------------------------------------------------------------
# Update exposure_pct — needs full entity totals after all prices refreshed
# ---------------------------------------------------------------------------
def update_exposure_pct(conn):
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE equity_holding eh
        SET    exposure_pct = ROUND(
                   eh.current_market_value
                   / NULLIF(totals.total_cmv, 0) * 100, 2
               )
        FROM (
            SELECT entity_id, SUM(current_market_value) AS total_cmv
            FROM   equity_holding
            WHERE  current_market_value IS NOT NULL
            GROUP  BY entity_id
        ) totals
        WHERE eh.entity_id = totals.entity_id
          AND eh.current_market_value IS NOT NULL
        """
    )
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# PMS price refresh — pms_holding rows sourced from a broker (e.g. Rajani Corp's
# Zerodha PMS). Equity prices and cash come live from the broker module (same
# source as the daily sync), so quantities/ISINs stay as last synced and only
# price/value/weight move intraday.
# ---------------------------------------------------------------------------
def update_pms_prices(conn):
    try:
        from equity.equity_sync_worker import PMS_BROKER_MAP
    except Exception as e:
        logger.warning(f"PMS: could not load PMS_BROKER_MAP — skipping ({e})")
        return

    if not PMS_BROKER_MAP:
        return

    cur = conn.cursor()
    cur.execute("SELECT id, entity_name FROM entity")
    emap = {r["entity_name"]: r["id"] for r in cur.fetchall()}

    total_updated = 0
    for entity_code, broker_module, broker_label, pms_source in PMS_BROKER_MAP:
        eid = emap.get(entity_code)
        if eid is None:
            continue

        cur.execute(
            "SELECT id, security_name, quantity FROM pms_holding "
            "WHERE entity_id = %s AND source = %s AND holding_type = 'equity'",
            (eid, pms_source),
        )
        rows = cur.fetchall()
        if not rows:
            continue

        try:
            raw = broker_module.fetch_holdings(entity_code)
        except Exception as e:
            logger.error(f"PMS[{entity_code}/{broker_label}]: holdings fetch failed — {e}")
            continue

        ltp = {}
        for h in raw:
            sym, price = h.get("tradingsymbol"), h.get("last_price")
            if sym and price:
                ltp[sym] = Decimal(str(price))

        cash = Decimal("0")
        if hasattr(broker_module, "fetch_cash_balance"):
            try:
                cash = broker_module.fetch_cash_balance(entity_code) or Decimal("0")
            except Exception as e:
                logger.warning(f"PMS[{entity_code}/{broker_label}]: cash fetch failed — {e}")

        new_vals = {}          # row_id -> (price, market_value)
        equity_total = Decimal("0")
        for r in rows:
            price = ltp.get(r["security_name"])
            if price is None:
                continue
            mv = (_d(r["quantity"]) * price).quantize(TWO, ROUND_HALF_UP)
            new_vals[r["id"]] = (price, mv)
            equity_total += mv

        if not new_vals:
            logger.warning(f"PMS[{entity_code}/{broker_label}]: no matching prices")
            continue

        total_value = equity_total + cash

        def _wt(mv):
            return float((mv / total_value * 100).quantize(FOUR, ROUND_HALF_UP)) if total_value > 0 else None

        for rid, (price, mv) in new_vals.items():
            cur.execute(
                "UPDATE pms_holding SET current_price = %s, market_value = %s, "
                "weight_pct = %s, updated_at = NOW() WHERE id = %s",
                (float(price), float(mv), _wt(mv), rid),
            )
            total_updated += 1

        if cash > 0:
            cur.execute(
                "UPDATE pms_holding SET market_value = %s, cost = %s, weight_pct = %s, "
                "updated_at = NOW() WHERE entity_id = %s AND source = %s AND holding_type = 'cash'",
                (float(cash), float(cash), _wt(cash), eid, pms_source),
            )

    conn.commit()
    cur.close()
    if total_updated:
        logger.info(f"PMS: updated {total_updated} holding price(s)")


# ---------------------------------------------------------------------------
# Nuvama PMS price refresh — these positions are scraped (no broker account),
# so we price them by ISIN: ISIN → NSE/BSE symbol (Dhan master) → Zerodha LTP.
# Quantities and cash stay as last scraped (no broker access to the Nuvama
# account); only equity price/value/weight move intraday. Dormant until the
# scrape populates ISINs (pms_holding.isin is NULL until then).
# ---------------------------------------------------------------------------
def update_nuvama_pms_prices(conn, creds: dict, source: str = "nuvama_pms"):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, entity_id, security_name, isin, quantity FROM pms_holding "
        "WHERE source = %s AND holding_type = 'equity' AND isin IS NOT NULL",
        (source,),
    )
    rows = cur.fetchall()
    if not rows:
        cur.close()
        return   # no ISINs captured yet — nothing to price

    zcred = creds.get("zerodha")
    if not zcred:
        logger.warning(f"{source}: no Zerodha credentials for LTP — skipping")
        cur.close()
        return

    try:
        from equity import isin_lookup
        adapter = build_adapter("zerodha", zcred)
    except Exception as e:
        logger.error(f"{source}: LTP setup failed — {e}")
        cur.close()
        return

    # Resolve each row's ISIN → EXCHANGE:SYMBOL.
    row_sym, instruments = {}, []
    for r in rows:
        mapped = isin_lookup.symbol_for_isin(r["isin"])
        if not mapped:
            logger.warning(f"{source}: no symbol for ISIN {r['isin']} ({r['security_name']})")
            continue
        exch, sym = mapped.split(":", 1)
        row_sym[r["id"]] = sym
        instruments.append({"symbol": sym, "exchange": exch})

    if not instruments:
        cur.close()
        return

    try:
        ltp = adapter.get_ltp(instruments)   # {symbol: price}
    except Exception as e:
        logger.error(f"{source}: LTP fetch failed — {e}")
        cur.close()
        return

    # Existing scraped cash per entity (kept as-is; broker can't see Nuvama cash).
    cur.execute(
        "SELECT entity_id, COALESCE(SUM(market_value),0) AS cash FROM pms_holding "
        "WHERE source = %s AND holding_type = 'cash' GROUP BY entity_id",
        (source,),
    )
    cash_by_entity = {r["entity_id"]: _d(r["cash"]) for r in cur.fetchall()}

    # New equity market values, then recompute weights per entity (equity + cash).
    new_vals, equity_total_by_entity = {}, {}
    for r in rows:
        sym = row_sym.get(r["id"])
        price = ltp.get(sym) if sym else None
        if price is None:
            continue
        mv = (_d(r["quantity"]) * Decimal(str(price))).quantize(TWO, ROUND_HALF_UP)
        new_vals[r["id"]] = (r["entity_id"], Decimal(str(price)), mv)
        equity_total_by_entity[r["entity_id"]] = equity_total_by_entity.get(r["entity_id"], Decimal("0")) + mv

    updated = 0
    for rid, (eid, price, mv) in new_vals.items():
        total = equity_total_by_entity.get(eid, Decimal("0")) + cash_by_entity.get(eid, Decimal("0"))
        wt = float((mv / total * 100).quantize(FOUR, ROUND_HALF_UP)) if total > 0 else None
        cur.execute(
            "UPDATE pms_holding SET current_price = %s, market_value = %s, "
            "weight_pct = %s, updated_at = NOW() WHERE id = %s",
            (float(price), float(mv), wt, rid),
        )
        updated += 1

    conn.commit()
    cur.close()
    if updated:
        logger.info(f"{source}: updated {updated}/{len(rows)} holding price(s) via ISIN→LTP")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    if not is_market_open():
        logger.debug("Outside market hours — exiting.")
        return

    conn = get_db()
    try:
        creds    = load_credentials(conn)

        # Pull live quantities / avg cost / new positions first, so the price loop
        # below values the CURRENT holdings (a same-day buy/sell shows immediately
        # instead of waiting for the twice-daily full sync).
        refresh_holdings_light(conn)

        holdings = load_holdings(conn)

        if not holdings:
            logger.info("No equity holdings in DB yet — nothing to update.")
            return

        # Group holdings by broker
        by_broker: dict[str, list[dict]] = {}
        for h in holdings:
            by_broker.setdefault(h["broker"], []).append(h)

        all_ltp: dict[str, float] = {}   # symbol → price (across brokers)
        updates: list[dict] = []

        for broker, broker_holdings in by_broker.items():
            cred = creds.get(broker)
            if not cred:
                logger.warning(f"No credentials for broker '{broker}' — skipping.")
                continue

            try:
                adapter = build_adapter(broker, cred)
                ltp_map = adapter.get_ltp(broker_holdings)
                logger.info(f"{broker}: fetched {len(ltp_map)}/{len(broker_holdings)} prices")
            except Exception as e:
                logger.error(f"{broker}: adapter error — {e}")
                continue

            if not ltp_map:
                logger.error(
                    f"{broker}: zero prices returned for {len(broker_holdings)} "
                    f"holding(s) — NOT marking synced (stale token / API failure?)"
                )
                continue

            all_ltp.update(ltp_map)

            missing = 0
            for h in broker_holdings:
                ltp = ltp_map.get(h["symbol"])
                if ltp is None:
                    missing += 1
                    logger.warning(f"No price for {h['symbol']} ({broker})")
                    continue
                metrics = compute_metrics(h, ltp)
                metrics["id"] = h["id"]
                updates.append(metrics)
            if missing:
                logger.warning(
                    f"{broker}: {missing}/{len(broker_holdings)} holding(s) had no price"
                )

            mark_broker_synced(conn, broker)

        if updates:
            update_holdings_batch(conn, updates)
            update_exposure_pct(conn)
            logger.info(f"Updated {len(updates)} holdings.")

        # PMS holdings (e.g. Rajani Corp's Zerodha PMS) live in pms_holding and
        # aren't part of the equity_holding batch above — refresh them too.
        update_pms_prices(conn)

        # Scraped PMS (Nuvama) — priced by ISIN→symbol→LTP; dormant until the
        # scrape populates ISINs.
        update_nuvama_pms_prices(conn, creds)

        # Refresh per-broker cash balances (shown on the Equity page). Cash barely
        # moves intraday, so throttle to ~every 30 min instead of every loop — this
        # avoids hammering broker funds endpoints (e.g. Angel One RMS, whose token
        # can be invalidated intraday) with a per-minute failing call. The daily
        # sync still refreshes unconditionally.
        try:
            cur = conn.cursor()
            cur.execute("SELECT MAX(updated_at) AS last FROM broker_cash")
            row = cur.fetchone()
            cur.close()
            last = row["last"] if row else None
            stale = last is None or (
                datetime.now(last.tzinfo) - last
            ) >= timedelta(minutes=BROKER_CASH_REFRESH_MIN)
            if stale:
                from equity.equity_sync_worker import refresh_broker_cash
                refresh_broker_cash(conn)
        except Exception as e:
            logger.warning(f"broker cash refresh failed — {e}")

        if is_eod_window():
            write_eod_snapshot(conn, holdings, all_ltp)

    finally:
        conn.close()


if __name__ == "__main__":
    run()
