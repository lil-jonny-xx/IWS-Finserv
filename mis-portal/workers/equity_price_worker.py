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
        logging.FileHandler("/var/log/mis-portal-equity-price.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
TWO = Decimal("0.01")

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

MARKET_OPEN  = (9, 15)   # 09:15 IST
MARKET_CLOSE = (15, 30)  # 15:30 IST

EOD_SNAPSHOT_WINDOW_MINUTES = 5   # write history in last 5 min before close


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
    """Return {broker: cred_row} for all active credentials."""
    cur = conn.cursor()
    cur.execute(
        "SELECT broker, credentials, access_token, token_expiry FROM broker_api_credentials WHERE is_active = TRUE"
    )
    rows = cur.fetchall()
    cur.close()
    return {r["broker"]: r for r in rows}


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
    """Dhan HQ — needs access_token and client_id."""

    def __init__(self, cred: dict):
        from dhanhq import dhanhq
        from dhanhq.dhan_context import DhanContext
        c = cred["credentials"]
        access_token = cred.get("access_token") or c.get("access_token", "")
        client_id    = c.get("client_id", "")
        if not access_token or not client_id:
            raise ValueError("Dhan: access_token and client_id required")
        self.dhan = dhanhq(DhanContext(client_id, access_token))

    def get_ltp(self, holdings: list[dict]) -> dict[str, float]:
        prices = {}
        for h in holdings:
            try:
                exchange = (h.get("exchange") or "NSE").upper()
                resp = self.dhan.get_market_feed_ltp(exchange, h["symbol"])
                if resp and resp.get("status") == "success":
                    prices[h["symbol"]] = float(resp["data"]["last_price"])
            except Exception as e:
                logger.warning(f"Dhan LTP error for {h['symbol']}: {e}")
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
                    (entity_id, broker, symbol, snapshot_date,
                     quantity, close_price, market_value, cost, pnl)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    h["entity_id"], h["broker"], h["symbol"], today,
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
# Main
# ---------------------------------------------------------------------------
def run():
    if not is_market_open():
        logger.debug("Outside market hours — exiting.")
        return

    conn = get_db()
    try:
        creds    = load_credentials(conn)
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

            all_ltp.update(ltp_map)

            for h in broker_holdings:
                ltp = ltp_map.get(h["symbol"])
                if ltp is None:
                    logger.debug(f"No price for {h['symbol']} ({broker})")
                    continue
                metrics = compute_metrics(h, ltp)
                metrics["id"] = h["id"]
                updates.append(metrics)

            mark_broker_synced(conn, broker)

        if updates:
            update_holdings_batch(conn, updates)
            update_exposure_pct(conn)
            logger.info(f"Updated {len(updates)} holdings.")

        if is_eod_window():
            write_eod_snapshot(conn, holdings, all_ltp)

    finally:
        conn.close()


if __name__ == "__main__":
    run()
