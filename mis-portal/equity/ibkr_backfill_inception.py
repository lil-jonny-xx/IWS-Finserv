#!/usr/bin/env python3
"""
One-time inception backfill for IBKR holdings: trade ledger + Since + XIRR + CAGR.

Per configured IBKR entity (IBKR_{CODE}_FLEX_TOKEN + IBKR_{CODE}_TRADES_QUERY_ID):

  1. Pull every trade from the Trades Flex query and upsert it into
     equity_trade_ledger (native currency; deduped by IBKR tradeID).
  2. For each current IBKR holding in equity_holding, compute from the ledger:
       - first_invested_date  = earliest BUY date            -> "Since" + CAGR
       - xirr_inception_pct   = money-weighted return (native, ledger flows +
                                current market value as the final inflow)
       - cagr_inception_pct   = (MV / cost)^(1/years) - 1     (shown immediately;
                                the nightly sync recomputes it thereafter)
     and UPDATE those columns on the holding.

After this runs, the nightly equity sync keeps XIRR fresh by re-reading the
ledger (see ledger_metrics() in equity_sync_worker.py) — no daily re-pull.

Idempotent. The Flex Web Service caps a statement at 365 days and takes no date
parameters, so the script asks at runtime how many years of history to pull and
then walks one ~365-day window at a time, pausing before each so you can set the
saved Trades query's Custom Date Range to the window it prints. Trades are deduped
by tradeID, so overlapping/re-runs are harmless.

Run (prompts for number of years):
  /var/www/.venv/bin/python -m equity.ibkr_backfill_inception
  /var/www/.venv/bin/python -m equity.ibkr_backfill_inception --entity DHR
  /var/www/.venv/bin/python -m equity.ibkr_backfill_inception --years 3       # skip prompt
  /var/www/.venv/bin/python -m equity.ibkr_backfill_inception --years 1 --assume-ready
  /var/www/.venv/bin/python -m equity.ibkr_backfill_inception --dry-run       # no writes
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

import psycopg2
import psycopg2.extras

from equity import finmath
from equity.brokers import ibkr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BROKER = "ibkr"
FOUR   = Decimal("0.0001")

# entity_name (DB) -> env var prefix when they differ (matches the adapters).
_ENV_PREFIX = {"Rajani Corp": "RAJANIRCORP"}


def get_conn():
    return psycopg2.connect(
        host           = os.environ["DB_HOST"],
        dbname         = os.environ["DB_NAME"],
        user           = os.environ["DB_USER"],
        password       = os.environ["DB_PASSWORD"],
        cursor_factory = psycopg2.extras.RealDictCursor,
    )


def load_entity_map(conn) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute("SELECT id, entity_name FROM entity ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    return {r["entity_name"]: r["id"] for r in rows}


def configured_entities(emap: dict) -> list[str]:
    """Entity codes that have an IBKR token + a Trades query id in .env."""
    found = []
    for code in emap:
        prefix = _ENV_PREFIX.get(code, code)
        if os.environ.get(f"IBKR_{prefix}_FLEX_TOKEN") \
                and os.environ.get(f"IBKR_{prefix}_TRADES_QUERY_ID"):
            found.append(code)
    return found


def prompt_years() -> int:
    """Ask the operator how many years of history to pull (one ~365-day Flex
    window per year, since the Flex Web Service caps a statement at 365 days)."""
    while True:
        try:
            raw = input("\nHow many years of trade history to backfill from inception? "
                        "(IBKR caps each pull at ~1 year, so this many windows) > ").strip()
        except EOFError:
            return 1
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
        print("  Please enter a whole number >= 1.")


def year_windows(years: int, today: date) -> list[tuple[date, date]]:
    """`years` non-overlapping ~365-day windows from today backwards (newest first)."""
    windows = []
    end = today
    for _ in range(years):
        start = end - timedelta(days=364)   # 365 inclusive days = at IBKR's cap
        windows.append((start, end))
        end = start - timedelta(days=1)
    return windows


# ---------------------------------------------------------------------------
# Trade parsing -> ledger rows
# ---------------------------------------------------------------------------

def _to_decimal(val, default="0") -> Decimal:
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal(default)


def _parse_trade(t: dict) -> dict | None:
    """One IBKR Flex Trade attrib dict -> a ledger row dict (or None to skip)."""
    raw_date = (t.get("tradeDate") or "").strip()
    if not raw_date:
        return None
    try:
        trade_date = datetime.strptime(raw_date, "%Y%m%d").date()
    except ValueError:
        try:
            trade_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
        except ValueError:
            logger.warning(f"  unparseable tradeDate {raw_date!r} — skipping")
            return None

    side = (t.get("buySell") or "").strip().upper()
    if side not in ("BUY", "SELL"):
        # Fall back to the sign of quantity if buySell is absent.
        side = "BUY" if _to_decimal(t.get("quantity")) >= 0 else "SELL"

    qty_signed = _to_decimal(t.get("quantity"))
    price      = _to_decimal(t.get("tradePrice"))
    commission = _to_decimal(t.get("ibCommission"))   # negative in IBKR data

    # Prefer IBKR's own signed net cash (proceeds - cost - commission); otherwise
    # derive it: -qty_signed * price + commission (buy qty>0 -> outflow<0).
    net_cash = t.get("netCash")
    if net_cash not in (None, ""):
        cash_flow = _to_decimal(net_cash)
    else:
        cash_flow = (qty_signed * price * Decimal("-1")) + commission

    return {
        "symbol":           t.get("symbol", "") or t.get("isin", ""),
        "isin":             t.get("isin", "") or None,
        "trade_date":       trade_date,
        "side":             side,
        "quantity":         abs(qty_signed),
        "price_native":     price,
        "fees_native":      abs(commission),
        "currency":         (t.get("currency") or "USD").upper(),
        "cash_flow_native": cash_flow,
        "source":           "ibkr_flex",
        "external_id":      t.get("tradeID") or None,
    }


def upsert_ledger(cur, entity_id: int, rows: list[dict]) -> int:
    inserted = 0
    for r in rows:
        cur.execute(
            """
            INSERT INTO equity_trade_ledger
                (entity_id, broker, symbol, isin, trade_date, side, quantity,
                 price_native, fees_native, currency, cash_flow_native, source, external_id)
            VALUES
                (%(entity_id)s, %(broker)s, %(symbol)s, %(isin)s, %(trade_date)s, %(side)s,
                 %(quantity)s, %(price_native)s, %(fees_native)s, %(currency)s,
                 %(cash_flow_native)s, %(source)s, %(external_id)s)
            ON CONFLICT (broker, external_id) WHERE external_id IS NOT NULL
            DO NOTHING
            """,
            {"entity_id": entity_id, "broker": BROKER, **r},
        )
        inserted += cur.rowcount
    return inserted


# ---------------------------------------------------------------------------
# Metrics from the ledger
# ---------------------------------------------------------------------------

def compute_holding_metrics(cur, entity_id: int, symbol: str, today: date):
    """(first_invested_date, xirr_pct) from this holding's ledger, or (None, None)."""
    cur.execute(
        """
        SELECT trade_date, side, cash_flow_native
        FROM   equity_trade_ledger
        WHERE  entity_id = %s AND broker = %s AND symbol = %s
        ORDER  BY trade_date
        """,
        (entity_id, BROKER, symbol),
    )
    rows = cur.fetchall()
    if not rows:
        return None, None

    buys  = [r["trade_date"] for r in rows if r["side"] == "BUY"]
    first = min(buys) if buys else min(r["trade_date"] for r in rows)
    flows = [(r["trade_date"], float(r["cash_flow_native"])) for r in rows]
    return first, flows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", help="only this entity code (e.g. DHR)")
    ap.add_argument("--years", type=int, help="number of ~1-year windows to pull "
                    "(skips the runtime prompt)")
    ap.add_argument("--assume-ready", action="store_true",
                    help="don't pause between windows (saved query range already correct)")
    ap.add_argument("--dry-run", action="store_true", help="compute but do not write")
    args = ap.parse_args()

    today = date.today()
    conn  = get_conn()
    emap  = load_entity_map(conn)

    entities = configured_entities(emap)
    if args.entity:
        entities = [e for e in entities if e == args.entity]
    if not entities:
        logger.error("No IBKR entities with FLEX_TOKEN + TRADES_QUERY_ID configured "
                     "(check .env / --entity).")
        return

    years   = args.years if args.years and args.years >= 1 else prompt_years()
    windows = year_windows(years, today)
    logger.info(f"Pulling {years} year-window(s): "
                f"{windows[-1][0]} .. {windows[0][1]}")

    grand_holdings = 0
    for code in entities:
        entity_id = emap[code]
        logger.info(f"=== IBKR inception backfill: {code} (entity {entity_id}) ===")

        # 1) Trades -> ledger, one ~365-day window at a time (Flex cap).
        cur = conn.cursor()
        total_added = 0
        for i, (start, end) in enumerate(windows, 1):
            print(f"\n  [{code}] Window {i}/{len(windows)}: {start} .. {end}")
            if len(windows) > 1 and not args.assume_ready:
                try:
                    input(f"    In IBKR, set the Trades Flex query for {code} to a Custom "
                          f"Date Range of {start} .. {end}, then press Enter to pull "
                          f"(Ctrl-C to stop): ")
                except (EOFError, KeyboardInterrupt):
                    print("    aborted by user")
                    break
            try:
                raw = ibkr.fetch_trades(code)
            except Exception as e:
                logger.error(f"[{code}] trade fetch failed for window {i}: {e}")
                continue
            rows = [r for r in (_parse_trade(t) for t in raw) if r]
            logger.info(f"[{code}] window {i}: parsed {len(rows)} trades from {len(raw)} rows")
            if not args.dry_run and rows:
                total_added += upsert_ledger(cur, entity_id, rows)
                conn.commit()
        if not args.dry_run:
            logger.info(f"[{code}] ledger: {total_added} new rows (dups ignored)")

        # 2) Per-holding metrics
        cur.execute(
            """
            SELECT symbol, isin, cost, current_market_value,
                   current_market_value_native
            FROM   equity_holding
            WHERE  entity_id = %s AND broker = %s
            """,
            (entity_id, BROKER),
        )
        holdings = cur.fetchall()
        if not holdings:
            logger.warning(f"[{code}] no IBKR holdings in equity_holding yet — run the "
                           f"equity sync first so positions exist, then re-run this.")
            cur.close()
            continue

        for h in holdings:
            symbol = h["symbol"]
            first, flows = compute_holding_metrics(cur, entity_id, symbol, today)
            if not flows:
                logger.info(f"  [{code}] {symbol}: no ledger trades — skipping")
                continue

            mv_native = h.get("current_market_value_native")
            xirr_pct = None
            if mv_native is not None:
                rate = finmath.xirr(flows + [(today, float(mv_native))])
                if rate is not None:
                    xirr_pct = Decimal(str(round(rate * 100, 4)))
            else:
                logger.warning(f"  [{code}] {symbol}: no native market value — "
                               f"XIRR skipped (run equity sync to populate it)")

            # CAGR (ratio is currency-independent — INR cost/MV give the same).
            cagr_pct = None
            cost = h["cost"] or Decimal("0")
            mv   = h["current_market_value"] or Decimal("0")
            if first and cost > 0:
                years = (today - first).days / 365.25
                ratio = float(mv / cost)
                if years >= 0.08 and ratio > 0:
                    cagr_pct = Decimal(str(round((ratio ** (1.0 / years) - 1.0) * 100, 4)))

            logger.info(f"  [{code}] {symbol}: since={first} "
                        f"xirr={xirr_pct}% cagr={cagr_pct}%")

            if not args.dry_run:
                cur.execute(
                    """
                    UPDATE equity_holding
                    SET    first_invested_date = %s,
                           xirr_inception_pct  = %s,
                           cagr_inception_pct  = COALESCE(%s, cagr_inception_pct),
                           updated_at          = NOW()
                    WHERE  entity_id = %s AND broker = %s AND symbol = %s
                    """,
                    (first, xirr_pct, cagr_pct, entity_id, BROKER, symbol),
                )
            grand_holdings += 1

        if not args.dry_run:
            conn.commit()
        cur.close()

    conn.close()
    logger.info(f"=== Done. Backfilled {grand_holdings} holdings "
                f"across {len(entities)} entity(ies)."
                f"{' (dry run — nothing written)' if args.dry_run else ''}")


if __name__ == "__main__":
    main()
