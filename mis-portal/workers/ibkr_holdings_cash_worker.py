#!/usr/bin/env python3
"""
IBKR Holdings + Cash + Trades Worker — ONE Flex call per token per day.

Fetches open positions, cash balance AND executed trades (last 365 days) for every
IBKR-configured entity from a SINGLE Flex statement per login, deliberately paced to
stay clear of the Web Service throttle that locked the DHR token out on 2026-06-24.

Why this is safe / why it's one call:
  • As of 2026-06-26 the daily QUERY_ID bundles Open Positions + Cash Report + Trades,
    so the statement is fetched EXACTLY ONCE per login (ibkr.fetch_all) and all three
    sections are parsed out of that single XML — never the back-to-back same-query
    regeneration that escalates 1001 → token-wide throttle → 1025 lockout.
  • Trades are upserted into equity_trade_ledger FIRST, so the holdings sync's
    ledger_metrics() recomputes XIRR / first-invested off the fresh ledger in the
    same run. Cash and holdings are then upserted from the one fetch.

This worker runs regardless of IBKR_SYNC_PAUSED — it IS the deliberate single-call
path; the pause flag keeps the unattended nightly equity sync from making a SECOND
IBKR call the same day. It does nothing quietly when no IBKR_*_FLEX_TOKEN is set.

  /var/www/.venv/bin/python workers/ibkr_holdings_cash_worker.py            # dry-run
  /var/www/.venv/bin/python workers/ibkr_holdings_cash_worker.py --commit

Run with IBKR_FLEX_SEND_RETRIES=1 (the cron entry does) so a throttled response (1001)
costs exactly ONE SendRequest with no retry — we don't probe token health, we take our
one shot for the day; 1025 already bails immediately. Statements are disk-cached, so a
throttled day transparently reuses the last good statement.
"""
import os
import sys
import argparse
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

from equity import fx
from equity.brokers import ibkr
from equity.equity_sync_worker import load_entity_map, sync_entity_broker
# Reuse the inception backfill's trade parsing + ledger upsert (deduped by tradeID)
# so the daily path and the backfill agree on the ledger schema exactly.
from equity.ibkr_backfill_inception import _parse_trade, upsert_ledger

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

TWO = Decimal("0.01")


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def ibkr_entities(emap: dict) -> list[tuple[str, int]]:
    """(entity_name, entity_id) for every entity with at least one IBKR Flex token set."""
    out = []
    for code, eid in emap.items():
        try:
            ibkr._account_prefixes(code)   # raises KeyError if no token configured
            out.append((code, eid))
        except KeyError:
            continue
    return out


def upsert_cash(conn, entity_id: int, entity_code: str, bal: Decimal, commit: bool):
    """Upsert IBKR cash (already parsed from the single statement) into broker_cash,
    converting the account base currency to INR."""
    bal = bal or Decimal("0")
    ccy = (ibkr.cash_currency(entity_code) or "INR").upper()
    today = date.today()

    if ccy == "INR":
        inr_bal, native_bal, rate = bal, None, Decimal("1")
    else:
        rate = fx.get_rate(conn, ccy, today)
        if rate is None:
            logger.warning(f"  [{entity_code}/ibkr] cash FX skip — no {ccy} rate")
            return None
        inr_bal, native_bal = (bal * rate).quantize(TWO, ROUND_HALF_UP), bal

    logger.info(f"  [{entity_code}/ibkr] cash {ccy} {bal} → ₹{inr_bal}")
    if commit:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO broker_cash
                (entity_id, broker, balance, currency, fx_rate, balance_native, as_of_date, updated_at)
            VALUES (%s, 'ibkr', %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (entity_id, broker) DO UPDATE SET
                balance        = EXCLUDED.balance,
                currency       = EXCLUDED.currency,
                fx_rate        = EXCLUDED.fx_rate,
                balance_native = EXCLUDED.balance_native,
                as_of_date     = EXCLUDED.as_of_date,
                updated_at     = NOW()
            """,
            (entity_id, float(inr_bal), ccy, float(rate),
             float(native_bal) if native_bal is not None else None, today),
        )
        conn.commit(); cur.close()
    return inr_bal


def sync_cash_currencies(conn, entity_id: int, entity_code: str, commit: bool):
    """Persist the per-currency cash breakdown behind the consolidated broker_cash row.

    Reads the native per-currency balances from the SAME statement fetch_all just pulled
    (ibkr.cash_by_currency reuses the cache — no extra Flex hit), converts each to INR,
    upserts one broker_cash_currency row per currency, and DELETES any currency for this
    (entity, ibkr) that is no longer present — so a swept / closed currency drops off the
    portal. Called only on a FULL successful fetch (see main's partial guard)."""
    breakdown = ibkr.cash_by_currency(entity_code)   # {ccy: native Decimal}
    today = date.today()
    kept: list[str] = []
    for ccy, native in sorted(breakdown.items(), key=lambda kv: -abs(kv[1])):
        ccy = ccy.upper()
        if ccy == "INR":
            inr, rate = native, Decimal("1")
        else:
            rate = fx.get_rate(conn, ccy, today)
            if rate is None:
                logger.warning(f"  [{entity_code}/ibkr] {ccy} breakdown skip — no FX rate")
                continue
            inr = (native * rate).quantize(TWO, ROUND_HALF_UP)
        kept.append(ccy)
        logger.info(f"    [{entity_code}/ibkr] {ccy} {native} → ₹{inr}")
        if commit:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO broker_cash_currency
                    (entity_id, broker, currency, balance_native, balance_inr, fx_rate,
                     as_of_date, updated_at)
                VALUES (%s, 'ibkr', %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (entity_id, broker, currency) DO UPDATE SET
                    balance_native = EXCLUDED.balance_native,
                    balance_inr    = EXCLUDED.balance_inr,
                    fx_rate        = EXCLUDED.fx_rate,
                    as_of_date     = EXCLUDED.as_of_date,
                    updated_at     = NOW()
                """,
                (entity_id, ccy, float(native), float(inr), float(rate), today),
            )
            conn.commit(); cur.close()

    # Snapshot semantics: drop any currency this entity no longer holds.
    if commit:
        cur = conn.cursor()
        if kept:
            cur.execute(
                "DELETE FROM broker_cash_currency "
                "WHERE entity_id=%s AND broker='ibkr' AND currency <> ALL(%s) RETURNING currency",
                (entity_id, kept),
            )
        else:
            cur.execute(
                "DELETE FROM broker_cash_currency "
                "WHERE entity_id=%s AND broker='ibkr' RETURNING currency",
                (entity_id,),
            )
        dropped = [r["currency"] for r in cur.fetchall()]
        conn.commit(); cur.close()
        if dropped:
            logger.info(f"  [{entity_code}/ibkr] dropped stale cash currencies: {dropped}")
    logger.info(f"  [{entity_code}/ibkr] cash currencies: {kept or 'none'}")


def prune_stale_holdings(conn, entity_id: int, entity_code: str, snap: date, commit: bool):
    """Remove IBKR holdings that were NOT in today's snapshot (i.e. sold / closed).

    sync_entity_broker upserts every current position with as_of_date = snap, but never
    deletes positions that vanished from the statement, so a sold-off stock (e.g. URNU)
    lingers. On a FULL successful fetch we delete any ibkr row for this entity whose
    as_of_date is older than today's snapshot — but ONLY if the snapshot actually wrote
    rows today, so a zero-holding fetch never wipes the last good set by mistake."""
    if not commit:
        return
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM foreign_equity_holding h
        WHERE h.entity_id=%s AND h.broker='ibkr' AND h.as_of_date < %s
          AND EXISTS (SELECT 1 FROM foreign_equity_holding h2
                      WHERE h2.entity_id=h.entity_id AND h2.broker='ibkr'
                        AND h2.as_of_date=%s)
        RETURNING symbol
        """,
        (entity_id, snap, snap),
    )
    gone = [r["symbol"] for r in cur.fetchall()]
    conn.commit(); cur.close()
    if gone:
        logger.info(f"  [{entity_code}/ibkr] pruned sold/closed holdings: {gone}")


def upsert_trades(conn, entity_id: int, entity_code: str, trades: list, commit: bool) -> int:
    """Parse the statement's Trade rows into ledger rows and upsert them into
    equity_trade_ledger (deduped by IBKR tradeID). Done BEFORE the holdings sync so
    ledger_metrics() recomputes XIRR / first-invested off the fresh trades."""
    rows = [r for r in (_parse_trade(t) for t in trades) if r]
    if not rows:
        logger.info(f"  [{entity_code}/ibkr] no trades in statement")
        return 0
    if not commit:
        logger.info(f"  [{entity_code}/ibkr] {len(rows)} trades parsed (dry-run, not written)")
        return 0
    cur = conn.cursor()
    n = upsert_ledger(cur, entity_id, rows)
    conn.commit(); cur.close()
    logger.info(f"  [{entity_code}/ibkr] ledger: {n} new trade(s) ({len(rows) - n} dup(s) ignored)")
    return n


def main():
    ap = argparse.ArgumentParser(
        description="One-call IBKR holdings + cash + trades refresh (single Flex statement).")
    ap.add_argument("--commit", action="store_true", help="write to DB (default: dry-run)")
    args = ap.parse_args()

    if os.environ.get("IBKR_SYNC_PAUSED", "").strip().lower() in ("1", "true", "yes"):
        logger.info("Note: IBKR_SYNC_PAUSED is set — the daily sync stays paused; "
                    "this paced worker still runs (it's the deliberate path).")

    conn = connect()
    targets = ibkr_entities(load_entity_map(conn))
    if not targets:
        logger.info("No IBKR Flex token configured (IBKR_*_FLEX_TOKEN) — nothing to do.")
        conn.close(); return

    mode = "COMMIT" if args.commit else "DRY-RUN"
    logger.info(f"{mode} — IBKR holdings+cash for: {[c for c, _ in targets]}")

    for code, eid in targets:
        # ONE statement hit per login → positions, cash AND trades parsed from the same XML.
        try:
            positions, cash, trades, failures = ibkr.fetch_all(code)
        except Exception as e:
            logger.error(f"  [{code}/ibkr] fetch failed — {e}")
            continue
        logger.info(f"  [{code}/ibkr] {len(positions)} positions, cash {cash}, {len(trades)} trades")

        # cash/holdings/trades for this entity aggregate across all its logins, so a partial
        # fetch would overwrite the aggregate with an undercount. Skip the write and keep
        # the last good values; the failed login just needs more quiet time (or a fresh token).
        if failures:
            logger.warning(f"  [{code}/ibkr] PARTIAL fetch (failed logins: {failures}) — "
                           f"NOT writing, to avoid corrupting the aggregate. Retry later.")
            # A persistent auth/config failure (expired/blocked token, bad query) won't
            # heal with time and silently freezes this entity's foreign holdings — alert a
            # human. Transient throttles (auth_code None) are expected noise; stay quiet.
            dead = [f for f in failures if f.get("auth_code")]
            if dead:
                try:
                    from alert import send_alert
                    body = "\n".join(
                        f"  {f['prefix']}: Flex code {f['auth_code']} — {f.get('msg', '')}"
                        for f in dead)
                    send_alert(
                        f"IBKR Flex token needs attention ({code})",
                        f"IBKR Flex could not authenticate for entity {code}. These logins need a "
                        f"re-issued token / fixed query — foreign holdings will stay frozen until "
                        f"fixed:\n\n{body}")
                except Exception as ae:
                    logger.error(f"  [{code}/ibkr] could not send token-death alert — {ae}")
            continue

        if not args.commit:
            upsert_trades(conn, eid, code, trades, commit=False)
            upsert_cash(conn, eid, code, cash, commit=False)   # logs the would-be INR value
            sync_cash_currencies(conn, eid, code, commit=False)  # logs the per-currency split
            continue

        # Trades FIRST: refresh the ledger so the holdings sync's ledger_metrics() picks up
        # the latest trades when it recomputes XIRR / first-invested below.
        try:
            upsert_trades(conn, eid, code, trades, commit=True)
        except Exception as e:
            logger.error(f"  [{code}/ibkr] TRADES upsert failed — {e}")

        # Holdings upsert reuses the full sync path (FX, ledger metrics, history snapshot)
        # but is fed the positions we ALREADY pulled — no second fetch, no extra Flex hit.
        snap = date.today()
        try:
            n = sync_entity_broker(conn, eid, code, ibkr, "ibkr", snap,
                                   foreign=True, raw=positions)
            logger.info(f"  [{code}/ibkr] holdings upserted: {n}")
            # Snapshot semantics: drop holdings that vanished from the statement (sold).
            prune_stale_holdings(conn, eid, code, snap, commit=True)
        except Exception as e:
            logger.error(f"  [{code}/ibkr] HOLDINGS upsert failed — {e}")
        # Cash comes straight from the statement we already parsed — no extra call.
        try:
            upsert_cash(conn, eid, code, cash, commit=True)
            sync_cash_currencies(conn, eid, code, commit=True)
        except Exception as e:
            logger.error(f"  [{code}/ibkr] CASH upsert failed — {e}")

    conn.close()
    logger.info("done." if args.commit else "dry-run — nothing written.")


if __name__ == "__main__":
    main()
