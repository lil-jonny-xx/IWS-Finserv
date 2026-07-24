#!/usr/bin/env python3
"""
Equity Snapshot Worker — intraday position snapshots + trade detection.

Runs at market open (~09:15 IST), hourly, and at close (~15:30 IST). Each tick it
pulls every entity's LIVE positions from its Indian brokers (the same
`fetch_holdings` + `normalise` path the daily sync uses, so quantities are real,
not the once-a-day DB copy), writes them to `equity_position_snapshot`, then
diffs against the previous tick:

  • quantity dropped  → a SELL of the difference, priced at this tick
  • quantity rose / new symbol → a BUY of the difference, priced at this tick

Detected trades are written into `stock_transaction` (source='snapshot') so they
flow straight into Realised Gains (which reads that table) and the Equity page's
"Traded today" panel. On an account's very first snapshot we also seed an opening
BUY per holding at its broker avg cost (source='snapshot_open') so a later sell
nets to a correct realised P&L instead of booking the whole proceeds as gain.

Only regular Indian equity brokers (equity_sync_worker.BROKER_ENTITY_MAP) are
snapshotted: foreign brokers have their own trade ledger and PMS accounts their
own realised feed, so routing their sells through stock_transaction would be
wrong.

Schedule (systemd): mis-portal-equity-snapshot.timer — Mon–Fri at open, hourly,
and close. The worker self-guards market hours, so exact firing is not required.

Run manually:  python -m workers.equity_snapshot_worker
"""
import logging
import os
import sys
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

from equity.equity_sync_worker import (  # noqa: E402
    get_conn, load_entity_map, BROKER_ENTITY_MAP,
)
from equity.holdings_source import cached_fetch_holdings  # noqa: E402
from workers.import_tradebook import _get_or_create_security  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# Snapshot window — slightly wider than the trading session so the open and close
# ticks still land if the timer fires a few minutes early/late.
WINDOW_OPEN  = (9, 0)    # 09:00 IST
WINDOW_CLOSE = (15, 45)  # 15:45 IST
OPEN_UNTIL   = (9, 20)   # ticks up to 09:20 IST are the "open" snapshot
CLOSE_FROM   = (15, 25)  # ticks from 15:25 IST are the "close" snapshot

# Quantities below this are treated as flat (float/decimal dust from broker feeds).
QTY_EPS = Decimal("0.0001")


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def market_state():
    """Return (is_open, snapshot_kind) for the current IST time, or (False, None)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False, None
    t = (now.hour, now.minute)
    if not (WINDOW_OPEN <= t < WINDOW_CLOSE):
        return False, None
    if t < OPEN_UNTIL:
        return True, "open"
    if t >= CLOSE_FROM:
        return True, "close"
    return True, "hourly"


# ---------------------------------------------------------------------------
# Snapshot + diff
# ---------------------------------------------------------------------------
def load_prev_snapshot(cur, entity_id: int, broker: str, before_ts: datetime) -> dict:
    """Positions at the most recent tick strictly before `before_ts`.

    Returns {symbol: {"qty": Decimal, "price": Decimal|None, "isin": str|None}}.
    Empty dict means this is the account's first-ever snapshot (drives seeding).
    """
    cur.execute(
        """
        SELECT symbol, isin, quantity, price
        FROM   equity_position_snapshot
        WHERE  entity_id = %s AND broker = %s
          AND  captured_at = (
                SELECT MAX(captured_at) FROM equity_position_snapshot
                WHERE entity_id = %s AND broker = %s AND captured_at < %s
              )
        """,
        (entity_id, broker, entity_id, broker, before_ts),
    )
    return {
        r["symbol"]: {"qty": _d(r["quantity"]), "price": r["price"], "isin": r["isin"]}
        for r in cur.fetchall()
    }


def write_snapshot(cur, entity_id, broker, holdings, captured_at, kind):
    """Insert one snapshot row per current holding for this tick."""
    for h in holdings:
        qty   = _d(h.quantity)
        price = _d(h.current_price) if getattr(h, "current_price", None) is not None else None
        mv    = (qty * price) if price is not None else None
        cur.execute(
            """
            INSERT INTO equity_position_snapshot
                (entity_id, broker, symbol, isin, captured_at, snapshot_kind,
                 quantity, price, market_value, avg_cost)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (entity_id, broker, symbol, captured_at) DO NOTHING
            """,
            (
                entity_id, broker, h.symbol, getattr(h, "isin", None) or None,
                captured_at, kind, qty,
                price, mv, _d(h.avg_cost),
            ),
        )


def _synthetic_exists(cur, source_ref: str) -> bool:
    cur.execute("SELECT 1 FROM stock_transaction WHERE source_ref = %s", (source_ref,))
    return cur.fetchone() is not None


# Snapshot detection is a FALLBACK. The authoritative record is the real broker fill
# pulled by broker_txn_sync_worker (correct trade date + fill price). A delivery trade
# settles T+1, so the snapshot only sees it the next morning — by which point the real
# fill (recorded the evening it executed) already exists. Detecting it again here would
# duplicate that fill (and at the wrong LTP-priced date), so if a real fill for this
# security exists within the recent window we skip the snapshot row. When the real sync
# is down (token/cron outage), no recent real fill exists and detection resumes.
REAL_TRADE_LOOKBACK_DAYS = 6


def _real_trade_recent(cur, entity_id, security_id, isin, symbol, today) -> bool:
    """True if a real (non-snapshot) broker fill for THIS instrument was recorded
    recently — the authoritative record a snapshot must not duplicate.

    Matches the instrument THREE ways, not just by security_id, because a same-day
    delivery buy is booked by the broker's *trades* API before the stock reaches the
    *holdings* feed (delivery settles T+1). The Zerodha/Angel trades feed carries no
    ISIN, and the holdings-based ISIN backfill is still blind to a brand-new position,
    so that real fill can land on a DIFFERENT security row — typically a NULL-ISIN one
    named after the raw trading symbol (e.g. 'NCC') rather than the ISIN-resolved
    canonical row ('NCC LIMITED') the snapshot sees the next morning. Keying the guard
    only on security_id then misses the fill and re-books the settled buy as a phantom
    (seen 2026-07-23: NCC + BHARAT ELECTRONICS). So we also match by shared ISIN and by
    broker symbol == security_name to catch the fill wherever it landed. A later orphan
    merge collapses the duplicate row, but by then the phantom is already written."""
    isin = (isin or "").strip() or None
    sym  = (symbol or "").strip().upper() or None
    cur.execute(
        """
        SELECT 1 FROM stock_transaction st
        JOIN   security_master sm ON sm.id = st.security_id
        WHERE  st.entity_id = %s
          AND  st.source NOT IN ('snapshot', 'snapshot_open')
          AND  st.transaction_date >= %s
          AND  (
                 st.security_id = %s
              OR (%s IS NOT NULL AND sm.isin = %s)
              OR (%s IS NOT NULL AND UPPER(sm.security_name) = %s)
              )
        LIMIT 1
        """,
        (entity_id, today - timedelta(days=REAL_TRADE_LOOKBACK_DAYS),
         security_id, isin, isin, sym, sym),
    )
    return cur.fetchone() is not None


def _insert_trade(cur, entity_id, security_id, tdate, side, qty, price,
                  source, source_ref, exchange, broker):
    # broker is stored so the metrics worker can scope a snapshot-derived trade to
    # the right broker account (an ISIN held at two brokers by one entity must not
    # cross-contaminate). Imported/API rows carry the broker as their `source`;
    # snapshot + manual rows carry the broker column instead.
    amount = float(qty) * float(price)
    cur.execute(
        """
        INSERT INTO stock_transaction
            (entity_id, security_id, transaction_date, transaction_type, quantity, price,
             amount, amount_inr, currency, exchange, source, source_ref, broker, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'INR',%s,%s,%s,%s,NOW())
        """,
        (entity_id, security_id, tdate, side, float(qty), float(price),
         amount, amount, exchange, source, source_ref, broker),
    )


def _has_existing_buys(cur, entity_id, security_id) -> bool:
    """True if this (entity, security) already has BUY history in stock_transaction
    (from a real tradebook import or a prior seed). Seeding on top of real history
    would double the cost basis, so we defer to whatever is already there."""
    cur.execute(
        "SELECT 1 FROM stock_transaction "
        "WHERE entity_id = %s AND security_id = %s AND transaction_type = 'BUY' LIMIT 1",
        (entity_id, security_id),
    )
    return cur.fetchone() is not None


def seed_opening_buys(cur, entity_id, broker, holdings, today):
    """First-ever snapshot for this account: record each held position as an opening
    BUY at its broker avg cost so Realised Gains has a cost basis for later sells.
    Idempotent per (entity, symbol), and skipped when real buy history already
    exists for the security (avoids double-counting against imported tradebooks)."""
    seeded = 0
    for h in holdings:
        qty = _d(h.quantity)
        if qty <= QTY_EPS:
            continue
        avg = _d(h.avg_cost)
        if avg <= 0:
            continue  # no usable cost basis — skip rather than seed a zero-cost buy
        symbol = h.symbol
        source_ref = f"snapshot_open:{entity_id}|{symbol}"
        if _synthetic_exists(cur, source_ref):
            continue
        exch   = getattr(h, "exchange", None) or None
        sec_id = _get_or_create_security(cur, getattr(h, "isin", None) or None, symbol, exch, False)
        if _has_existing_buys(cur, entity_id, sec_id):
            continue
        tdate  = getattr(h, "first_invested_date", None) or today
        _insert_trade(cur, entity_id, sec_id, tdate, "BUY", qty, avg,
                      "snapshot_open", source_ref, exch, broker)
        seeded += 1
    return seeded


def _identity(isin, symbol) -> str:
    """Stable position identity for the diff: ISIN when the broker provides one,
    else the (upper-cased) symbol string.

    Keying the tick-to-tick diff on this — NOT on the raw symbol string — means an
    overnight symbol relabel that keeps the same ISIN nets to zero instead of being
    misread as a full exit + fresh entry (a phantom SELL + BUY that fabricates a
    realised gain). Seen in the wild: SGBAUG28V -> SGBAUG28V-GB (sovereign gold bond
    suffix appearing) and TRANSWORLD -> TRANSWORLD-BE (trade-to-trade segment
    reclassification). Both are pure relabels — same ISIN, same quantity, no trade."""
    isin = (isin or "").strip()
    return f"ISIN:{isin}" if isin else f"SYM:{(symbol or '').strip().upper()}"


def detect_trades(cur, entity_id, broker, holdings, prev, captured_at, today):
    """Diff current holdings vs the previous tick; write BUY/SELL rows. Returns
    (buys, sells).

    Positions are matched by ISIN-first identity (see _identity), so a symbol
    relabel of an unchanged holding produces no trade. Quantities are aggregated per
    identity, which also nets a dual-listed same-ISIN position reported on two
    broker lines."""
    ts_iso = captured_at.isoformat()
    buys = sells = 0

    # Aggregate current holdings by stable identity; keep a representative holding
    # per identity for price / symbol / exchange attribution of any trade row.
    curr: dict = {}
    for h in holdings:
        key = _identity(getattr(h, "isin", None), h.symbol)
        slot = curr.get(key)
        if slot is None:
            curr[key] = {"qty": _d(h.quantity), "h": h}
        else:
            slot["qty"] += _d(h.quantity)

    # Aggregate the previous snapshot (keyed by symbol on disk) the same way.
    prevk: dict = {}
    for sym, d in prev.items():
        key = _identity(d.get("isin"), sym)
        slot = prevk.get(key)
        if slot is None:
            prevk[key] = {"qty": d["qty"], "price": d.get("price"),
                          "isin": d.get("isin"), "symbol": sym}
        else:
            slot["qty"] += d["qty"]

    for key in set(curr) | set(prevk):
        c = curr.get(key)
        p = prevk.get(key)
        curr_qty = c["qty"] if c else Decimal("0")
        prev_qty = p["qty"] if p else Decimal("0")
        delta = curr_qty - prev_qty
        if abs(delta) <= QTY_EPS:
            continue

        h = c["h"] if c else None
        if delta > 0:
            side, qty = "BUY", delta
            price = _d(h.current_price) if (h and getattr(h, "current_price", None) is not None) else None
        else:
            side, qty = "SELL", -delta
            # Still-held reductions price at this tick; a fully-exited position has no
            # fresh price, so fall back to its last snapshot price.
            price = _d(h.current_price) if (h and getattr(h, "current_price", None) is not None) \
                else _d(p.get("price") if p else None)
        exch   = (getattr(h, "exchange", None) or None) if h else None
        isin   = (getattr(h, "isin", None) if h else None) or (p["isin"] if p else None) or None
        symbol = (h.symbol if h else None) or (p["symbol"] if p else key)

        if price is None or price <= 0:
            logger.warning(f"[{entity_id}/{broker}] {symbol}: no price for {side} — skipping trade row")
            continue

        # source_ref is keyed on the stable identity (not the symbol) so a later
        # relabel cannot re-open an already-recorded trade under a new string.
        source_ref = f"snapshot:{entity_id}|{key}|{ts_iso}|{side}"
        if _synthetic_exists(cur, source_ref):
            continue
        sec_id = _get_or_create_security(cur, isin, symbol, exch, False)
        # Fallback only: if the real broker feed already recorded a fill for this
        # instrument recently, it's the authoritative source — don't duplicate it with
        # an approximate snapshot row. Matched by security_id OR ISIN OR symbol so a
        # fill sitting on a soon-to-be-merged duplicate security still counts (see
        # _real_trade_recent).
        if _real_trade_recent(cur, entity_id, sec_id, isin, symbol, today):
            continue
        _insert_trade(cur, entity_id, sec_id, today, side, qty, price,
                      "snapshot", source_ref, exch, broker)
        if side == "BUY":
            buys += 1
        else:
            sells += 1

    return buys, sells


def _broker_has_buy(cur, entity_id, security_id, broker) -> bool:
    """True if this (entity, security) already has BUY history FOR THIS BROKER —
    an imported tradebook (source=broker) or a manual/snapshot row tagged to it.
    Broker-aware (unlike _has_existing_buys) so a stock held at two brokers by one
    entity can be seeded once per broker."""
    cur.execute(
        """SELECT 1 FROM stock_transaction
           WHERE entity_id=%s AND security_id=%s AND transaction_type='BUY'
             AND (source=%s OR (source IN ('manual','snapshot','snapshot_open') AND broker=%s))
           LIMIT 1""",
        (entity_id, security_id, broker, broker),
    )
    return cur.fetchone() is not None


def seed_held_without_history(conn, apply: bool) -> int:
    """Safety net for full automation: seed an opening BUY for any currently-held
    Indian-equity position that has NO reconstructing history for its broker.

    The live tick only seeds on an account's first-ever snapshot; a position held
    before that tick (or across a worker outage) can slip through and, being static,
    is never picked up by the qty-diff either. This backfills those so every holding
    reconstructs without a manual import. Idempotent (broker-aware BUY guard) and
    re-runnable. Reads holdings from the equity_holding table (avg_cost/qty already
    broker-fed), so it does not hit any broker API."""
    cur = conn.cursor()
    cur.execute("""
        SELECT eh.entity_id, eh.broker, eh.symbol, eh.isin, eh.exchange,
               eh.quantity, eh.avg_cost, eh.first_invested_date, e.entity_name
        FROM equity_holding eh JOIN entity e ON e.id=eh.entity_id
        WHERE eh.broker IN ('zerodha','angel_one','dhan')
          AND eh.quantity > 0 AND eh.avg_cost > 0
        ORDER BY e.entity_name, eh.broker, eh.symbol
    """)
    rows = cur.fetchall()
    today = date.today()
    seeded = 0
    for r in rows:
        sec_id = _get_or_create_security(cur, r["isin"] or None, r["symbol"], r["exchange"] or None, False)
        if _broker_has_buy(cur, r["entity_id"], sec_id, r["broker"]):
            continue
        # broker in the ref so a two-broker holding seeds once per broker
        source_ref = f"snapshot_open:{r['entity_id']}|{r['broker']}|{r['symbol']}"
        if _synthetic_exists(cur, source_ref):
            continue
        tdate = r["first_invested_date"] or today
        print(f"   seed {r['entity_name']:6} {r['broker']:9} {r['symbol']:14} "
              f"qty={_d(r['quantity'])} @ {_d(r['avg_cost'])}  ({tdate})")
        if apply:
            _insert_trade(cur, r["entity_id"], sec_id, tdate, "BUY",
                          _d(r["quantity"]), _d(r["avg_cost"]),
                          "snapshot_open", source_ref, r["exchange"] or None, r["broker"])
        seeded += 1
    if apply:
        conn.commit()
    cur.close()
    return seeded


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    is_open, kind = market_state()
    if not is_open:
        logger.debug("Outside snapshot window — exiting.")
        return

    captured_at = datetime.now(timezone.utc)
    today       = date.today()
    conn = get_conn()
    try:
        emap = load_entity_map(conn)
        total_snap = total_seed = total_buy = total_sell = 0

        for entity_code, broker_module, broker_label in BROKER_ENTITY_MAP:
            entity_id = emap.get(entity_code)
            if entity_id is None:
                logger.error(f"Entity '{entity_code}' not in DB — skipping")
                continue
            try:
                # Reuse the price worker's recent pull (<=90s) instead of re-hitting the
                # broker; falls through to a direct fetch if the cache is stale/unavailable.
                raw = cached_fetch_holdings(broker_module, broker_label, entity_code, max_age=90)
                holdings = broker_module.normalise(entity_id, entity_code, raw)
            except NotImplementedError:
                continue
            except Exception as e:
                logger.error(f"[{entity_code}/{broker_label}] holdings fetch failed: {e}")
                conn.rollback()
                continue

            if not holdings:
                logger.info(f"[{entity_code}/{broker_label}] no holdings this tick")
                continue

            cur = conn.cursor()
            try:
                prev = load_prev_snapshot(cur, entity_id, broker_label, captured_at)
                write_snapshot(cur, entity_id, broker_label, holdings, captured_at, kind)
                total_snap += len(holdings)

                if not prev:
                    total_seed += seed_opening_buys(cur, entity_id, broker_label, holdings, today)
                else:
                    b, s = detect_trades(cur, entity_id, broker_label, holdings,
                                         prev, captured_at, today)
                    total_buy += b
                    total_sell += s
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"[{entity_code}/{broker_label}] snapshot/diff failed: {e}")
            finally:
                cur.close()

        logger.info(
            f"Snapshot ({kind}): {total_snap} positions, seeded {total_seed}, "
            f"detected {total_buy} buys / {total_sell} sells"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Equity snapshot worker + seed backfill.")
    ap.add_argument("--seed-held", action="store_true",
                    help="backfill opening seeds for held positions lacking history (see seed_held_without_history)")
    ap.add_argument("--apply", action="store_true", help="with --seed-held: write (default dry-run)")
    args = ap.parse_args()
    if args.seed_held:
        conn = get_conn()
        try:
            print(f"{'APPLY' if args.apply else 'DRY-RUN'} — seeding held positions without history:")
            n = seed_held_without_history(conn, args.apply)
            print(f"\n{'seeded' if args.apply else 'would seed'} {n} position(s)."
                  + ("" if args.apply else " Re-run with --apply."))
        finally:
            conn.close()
    else:
        run()
