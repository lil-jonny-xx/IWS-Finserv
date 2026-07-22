#!/usr/bin/env python3
"""
Live broker transaction sync — keeps stock_transaction (and Dhan's external_cashflow)
current so cost-basis / XIRR / portfolio-return metrics stay accurate as NEW trades and
deposits happen, with no manual tradebook/ledger re-upload.

Per (entity, Indian broker) in equity_sync_worker.BROKER_ENTITY_MAP:
  • Zerodha / Angel One — append today's executed trades (their APIs only retain the
    current trading day, so this MUST run after market close).
  • Dhan — append trades AND ledger movements over a date window from the last imported
    date (Dhan exposes date-ranged history + a ledger report, so gaps self-heal).

Everything dedupes (trades on broker:trade_id; cashflows on source_ref), so re-runs and
overlapping windows are safe. ISINs are resolved from the same-run holdings snapshot so
the rows link to equity_holding for the metrics workers. Dry-run by default; --commit writes.
"""
import os
import sys
import logging
import argparse
import hashlib
from datetime import date, datetime, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

sys.path.insert(0, "/var/www/mis-portal")
load_dotenv("/var/www/mis-portal/.env", override=True)

from equity.equity_sync_worker import BROKER_ENTITY_MAP
from equity.holdings_source import cached_fetch_holdings
from workers.import_tradebooks_multi import get_or_create_security
from workers.import_ledgers import classify

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("broker_txn_sync")

TODAY = date.today()
FNO = ("FUT", "PUT", "CALL", "OPT")
DEFAULT_LOOKBACK = 30          # days, when no prior trade exists for a (entity,broker)


def _f(v):
    try:
        return abs(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _date(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v if isinstance(v, date) and not isinstance(v, datetime) else v.date()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d-%m-%Y",
                "%d/%m/%Y", "%d-%b-%Y", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    return None


def _side(v):
    s = str(v).strip().upper()
    return "BUY" if s.startswith("B") else "SELL" if s.startswith("S") else None


# ---- per-broker trade normalisers (defensive on key names) -----------------
def norm_zerodha(t):
    return {"symbol": t.get("tradingsymbol"), "isin": t.get("isin"),
            "date": _date(t.get("fill_timestamp") or t.get("exchange_timestamp") or t.get("order_timestamp")),
            "side": _side(t.get("transaction_type")), "qty": _f(t.get("quantity")),
            "price": _f(t.get("average_price") or t.get("price")),
            "trade_id": str(t.get("trade_id") or ""), "exchange": t.get("exchange")}


def norm_angel(t):
    # Angel's trade book is the CURRENT trading day only, and the fill timestamp it
    # returns is a bare time ("12:10:37") with no date — so a date parse yields None
    # and the row would be wrongly dropped as "missing date". Fall back to today (its
    # real trade date). Also: the per-fill id is `fillid`, not `tradeid` (which is
    # absent), so keying on tradeid produced empty ids that collapsed distinct fills
    # via the content-hash fallback.
    return {"symbol": t.get("tradingsymbol"), "isin": None,
            "date": _date(t.get("filltime") or t.get("exchangetime") or t.get("filldate")) or TODAY,
            "side": _side(t.get("transactiontype")),
            "qty": _f(t.get("fillsize") or t.get("filledshares")),
            "price": _f(t.get("fillprice")),
            "trade_id": str(t.get("fillid") or t.get("tradeid") or ""),
            "exchange": t.get("exchange")}


def norm_dhan(t):
    # Dhan's exchangeTradeId is uselessly "0"; build a stable per-fill key from the
    # order id + timestamp + qty + price (re-fetch yields the same key → dedupes).
    oid = t.get("orderId") or t.get("exchangeOrderId") or ""
    tid = f"{oid}-{t.get('exchangeTime') or ''}-{t.get('tradedQuantity') or ''}-{t.get('tradedPrice') or ''}" if oid else ""
    return {"symbol": t.get("customSymbol") or t.get("tradingSymbol"), "isin": t.get("isin"),
            "date": _date(t.get("exchangeTime") or t.get("createTime") or t.get("tradeDate") or t.get("updateTime")),
            "side": _side(t.get("transactionType")),
            "qty": _f(t.get("tradedQuantity") or t.get("quantity")),
            "price": _f(t.get("tradedPrice") or t.get("price")),
            "trade_id": tid, "exchange": t.get("exchangeSegment")}


NORMALISERS = {"zerodha": norm_zerodha, "angel_one": norm_angel, "dhan": norm_dhan}


def connect():
    return psycopg2.connect(host=os.getenv("DB_HOST", "localhost"),
                            database=os.getenv("DB_NAME", "mis_portal"),
                            user=os.getenv("DB_USER", "postgres"),
                            password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
                            cursor_factory=RealDictCursor)


def entity_map(cur):
    cur.execute("SELECT id, entity_name FROM entity")
    return {r["entity_name"]: r["id"] for r in cur.fetchall()}


def last_trade_date(cur, entity_id, broker):
    """Latest AUTHORITATIVE trade date for this account — the high-water mark the
    incremental fetch window starts from.

    Live-captured rows are excluded even though they carry source=<broker>: the
    order-update daemon writes them with source_ref '<broker>:live:<order_id>'.
    Counting them lets a single live fill close the window on an EARLIER day the API
    has not returned yet. Dhan's tradebook is T+1, so the sequence is routine: a fill
    on day 1 is missed live, the API does not expose it until day 2, and one live fill
    on day 2 would move this high-water mark past day 1 and strand it permanently —
    the sync would report "up to date" while the trade is simply gone.

    Re-fetching a day that already has live rows is safe by design: the API row lands
    under a different source_ref, and sync_trades then supersedes the live rows it
    covers (see the '{broker}:live:%' delete), so the trade is booked exactly once at
    its authoritative price. Snapshot and reconstructed rows carry their own `source`
    values and are already outside this filter.
    """
    cur.execute("""SELECT MAX(transaction_date) d FROM stock_transaction
                   WHERE entity_id = %s AND source = %s
                     AND COALESCE(source_ref, '') NOT LIKE %s""",
                (entity_id, broker, f"{broker}:live:%"))
    r = cur.fetchone()
    return r["d"] if r and r["d"] else None


def holdings_isin_map(broker_module, broker, entity_code):
    """tradingsymbol → isin from current holdings (for trades lacking ISIN). Uses the
    shared cached fetch so a recent price-worker pull is reused rather than a new call."""
    try:
        raw = cached_fetch_holdings(broker_module, broker, entity_code, max_age=300)
    except Exception as e:
        logger.warning(f"    holdings fetch failed (isin map unavailable): {e}")
        return {}
    m = {}
    for h in raw:
        sym = h.get("tradingsymbol") or h.get("tradingSymbol")
        isin = h.get("isin")
        if sym and isin:
            m[sym.upper()] = isin
    return m


def sync_trades(cur, broker_module, broker, entity_id, entity_code, commit, stats):
    norm = NORMALISERS[broker]
    if broker == "dhan":
        # Start the day AFTER the last imported trade: the backfilled day is already
        # fully covered, and Dhan's backfill source_ref is a content hash (no trade_id)
        # so it wouldn't dedupe against the API's trade_id — avoid re-fetching it.
        last = last_trade_date(cur, entity_id, broker)
        frm = (last + timedelta(days=1)) if last else (TODAY - timedelta(days=DEFAULT_LOOKBACK))
        if frm > TODAY:
            print("    trades: up to date"); return
        raw = broker_module.fetch_trades(entity_code, frm.strftime("%Y-%m-%d"), TODAY.strftime("%Y-%m-%d"))
    else:
        raw = broker_module.fetch_trades(entity_code)

    isin_map = holdings_isin_map(broker_module, broker, entity_code)
    ins = dup = skip = 0
    touched_secs, max_tdate = set(), None   # for snapshot supersede after the loop
    for t in raw:
        r = norm(t)
        if not r["isin"] and r["symbol"]:
            r["isin"] = isin_map.get(r["symbol"].upper())
        sym_u = (r["symbol"] or "").upper()
        if not (r["symbol"] and r["date"] and r["side"] and r["qty"] and r["price"]) \
                or any(h in sym_u for h in FNO):
            skip += 1
            continue
        tid = r["trade_id"] if r["trade_id"] and r["trade_id"] not in ("0", "None") else None
        # A broker trade_id is unique per ACCOUNT PER DAY, never globally — zerodha
        # 12479400 is BOTH HHR's BEL sell (2024-07-01) and DHR's SBIN sell (2026-02-20).
        # Keying on it alone made the dedup below drop whichever entity was imported
        # second, silently: three real HHR trades were lost that way, and the loss is
        # invisible afterwards because the row that would prove it was never inserted.
        # Scope by entity+date. The md5 branch already carries entity_id, so it stays
        # byte-identical — changing it would orphan the existing hash-keyed rows and
        # re-import every one of them as new. See db_migrate_source_ref_scope.py.
        sref = (f"{broker}:{entity_id}:{r['date']}:{tid}" if tid else
                f"{broker}:" + hashlib.md5(
                    f"{entity_id}|{r['symbol']}|{r['date']}|{r['side']}|{r['qty']}|{r['price']}"
                    .encode()).hexdigest()[:16])
        cur.execute("SELECT 1 FROM stock_transaction WHERE source_ref=%s", (sref,))
        if cur.fetchone():
            dup += 1
            continue
        amount = r["qty"] * r["price"]
        if commit:
            sec_id = get_or_create_security(cur, r["isin"], r["symbol"], r["exchange"], "INR", True)
            cur.execute("""INSERT INTO stock_transaction
                (entity_id, security_id, transaction_date, transaction_type, quantity, price,
                 amount, currency, exchange, source, source_ref, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'INR',%s,%s,%s,NOW())""",
                (entity_id, sec_id, r["date"], r["side"], r["qty"], r["price"], amount,
                 r["exchange"], broker, sref))
            touched_secs.add(sec_id)
            if max_tdate is None or r["date"] > max_tdate:
                max_tdate = r["date"]
        ins += 1

    # Real fills are authoritative: supersede the approximate snapshot-derived rows
    # they cover (the snapshot worker prices at LTP and dates by settlement-detection,
    # so a real fill has the correct date + price). Same policy as the manual
    # tradebook importers: opening seeds go unconditionally; detected trades only up
    # to the last real trade date, so a snapshot placeholder for a fill not yet
    # returned by the API survives until it is.
    superseded = 0
    if commit and touched_secs:
        secs = list(touched_secs)
        cur.execute("DELETE FROM stock_transaction WHERE source = 'snapshot_open' "
                    "AND entity_id = %s AND security_id = ANY(%s)", (entity_id, secs))
        superseded += cur.rowcount
        cur.execute("DELETE FROM stock_transaction WHERE source = 'snapshot' "
                    "AND entity_id = %s AND security_id = ANY(%s) AND transaction_date <= %s",
                    (entity_id, secs, max_tdate))
        superseded += cur.rowcount
        # Also supersede the real-time LIVE rows (WS + postback, source_ref '{broker}:live:{order_id}')
        # for the same securities/dates: the authoritative trade_id fill is the truth (exact price,
        # settled qty, correct date), and live + reconcile use different source_refs so they would
        # otherwise double-count. Bounded by max_tdate, so a live fill for a trade the API hasn't
        # returned yet (e.g. Dhan same-day, T+1 tradebook) survives until the reconcile catches it.
        cur.execute("DELETE FROM stock_transaction WHERE source_ref LIKE %s "
                    "AND entity_id = %s AND security_id = ANY(%s) AND transaction_date <= %s",
                    (f"{broker}:live:%", entity_id, secs, max_tdate))
        superseded += cur.rowcount

    stats["trades"] += ins
    extra = f", superseded {superseded} snapshot/live row(s)" if superseded else ""
    print(f"    trades: +{ins} new, {dup} dup, {skip} skipped (of {len(raw)}){extra}")


def sync_dhan_ledger(cur, broker_module, entity_id, entity_code, commit, stats):
    cur.execute("SELECT MAX(flow_date) d FROM external_cashflow WHERE entity_id=%s AND broker='dhan'",
                (entity_id,))
    r = cur.fetchone()
    frm = ((r["d"] + timedelta(days=1)) if r and r["d"] else (TODAY - timedelta(days=DEFAULT_LOOKBACK)))
    if frm > TODAY:
        print("    dhan ledger: up to date"); return
    try:
        rows = broker_module.fetch_ledger(entity_code, frm.strftime("%Y-%m-%d"), TODAY.strftime("%Y-%m-%d"))
    except Exception as e:
        logger.warning(f"    Dhan ledger fetch failed: {e}")
        return
    ins = 0
    for row in rows:
        text = row.get("narration") or row.get("description") or row.get("voucherdesc") or ""
        ftype = classify(text)
        if ftype not in ("DEPOSIT", "WITHDRAWAL", "DIVIDEND", "INTEREST"):
            continue
        d = _date(row.get("voucherdate") or row.get("date") or row.get("exchangeTime"))
        credit = _f(row.get("credit")) or 0
        debit = _f(row.get("debit")) or 0
        amt = -credit if ftype in ("DEPOSIT",) else (debit if ftype == "WITHDRAWAL" else credit)
        if not d or amt == 0:
            continue
        sref = f"dhan:{entity_id}:{d}:{ftype}:{abs(amt):.0f}:" + hashlib.md5(text.encode()).hexdigest()[:8]
        cur.execute("SELECT 1 FROM external_cashflow WHERE source_ref=%s", (sref,))
        if cur.fetchone():
            continue
        if commit:
            cur.execute("""INSERT INTO external_cashflow
                (entity_id, broker, flow_date, flow_type, amount_native, currency, source, source_ref, notes)
                VALUES (%s,'dhan',%s,%s,%s,'INR','dhan',%s,%s) ON CONFLICT (source_ref) DO NOTHING""",
                (entity_id, d, ftype, round(amt, 2), sref, text[:200]))
        ins += 1
    stats["cashflows"] += ins
    print(f"    dhan ledger: +{ins} new deposits/withdrawals")


def main():
    ap = argparse.ArgumentParser(description="Live broker trade + ledger sync.")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--broker", choices=["zerodha", "angel_one", "dhan"])
    args = ap.parse_args()

    conn = connect(); cur = conn.cursor()
    emap = entity_map(cur)
    stats = {"trades": 0, "cashflows": 0}
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'}\n")
    for entity_code, broker_module, broker in BROKER_ENTITY_MAP:
        if args.broker and broker != args.broker:
            continue
        entity_id = emap.get(entity_code)
        if not entity_id:
            continue
        print(f"[{entity_code} / {broker}]")
        try:
            sync_trades(cur, broker_module, broker, entity_id, entity_code, args.commit, stats)
            if broker == "dhan":
                sync_dhan_ledger(cur, broker_module, entity_id, entity_code, args.commit, stats)
            if args.commit:
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"    [{entity_code}/{broker}] FAILED: {e}")
    print(f"\n{stats}")
    print("committed." if args.commit else "dry-run — nothing written.")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
