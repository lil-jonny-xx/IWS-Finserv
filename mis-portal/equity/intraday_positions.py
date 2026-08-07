#!/usr/bin/env python3
"""
Today's unsettled positions, carried alongside the settled holding.

A buy made today is not a holding yet. Each broker says so differently:

  Zerodha   absent from holdings(); sits in positions() until it settles, then
            arrives as `t1_quantity` the next session
  Dhan      same — absent from the holdings feed entirely (verified live)
  Angel One folds it straight into holdings `quantity`, so it looks settled at once

Left alone, the same trade reads three different ways depending on where it was
placed. This module normalises that: every broker's today-only activity lands in
`equity_holding.intraday_*`, and for Angel the quantity is SUBTRACTED back out of
the settled figure so `quantity` means the same thing everywhere — shares actually
settled in demat.

Nothing else changes. Daily snapshots, FIFO/XIRR metrics, the XLSX reports, the
ghost prune and the holdings-vs-ledger reconcile all keep reading `quantity` and
see exactly what they saw before; only the Equity page adds the intraday line.
"""
import logging
from datetime import date
from decimal import Decimal

logger = logging.getLogger(__name__)


def _resolve_isin(cur, entity_id, broker, symbol):
    """ISIN for a position. Broker position payloads omit it, and matching on name
    alone forks a second security row, so work from the most specific signal to the
    least: this account's own holding, then the same ticker held anywhere else, then
    the security master by name (exact, then as a leading word).

    Returning None here is expensive, not neutral: the caller inserts a placeholder
    row with a NULL ISIN, and once the position settles the broker feed delivers it
    WITH an ISIN — so the symbol shows twice until the ghost prune catches it a week
    later. Three symbols forked that way on 2026-07-22 (NCC, BEL, GODREJIND), each
    for a different reason, which is why there are four attempts and not one.
    """
    # '-EQ' is Angel's NSE series tag, not part of the instrument's identity.
    base = symbol[:-3] if symbol and symbol.upper().endswith("-EQ") else symbol

    # 1. This account already holds it — same entity, same broker, same ticker.
    cur.execute("SELECT isin FROM equity_holding WHERE entity_id=%s AND broker=%s "
                "AND UPPER(symbol)=UPPER(%s) AND isin IS NOT NULL LIMIT 1",
                (entity_id, broker, base))
    r = cur.fetchone()
    if r and r["isin"]:
        return r["isin"]

    # 2. Anyone else holds the same ticker. On the Indian exchanges a trading symbol
    #    maps to one instrument, so another entity's or broker's row is a reliable
    #    read — and it is the only one of these four that would have caught all three
    #    of the 2026-07-22 forks (BEL has no security_master name row at all, and
    #    GODREJIND's was created hours after the position was processed).
    cur.execute("SELECT isin FROM equity_holding WHERE UPPER(symbol)=UPPER(%s) "
                "AND isin IS NOT NULL GROUP BY isin ORDER BY count(*) DESC LIMIT 1",
                (base,))
    r = cur.fetchone()
    if r and r["isin"]:
        return r["isin"]

    # 3. Security master, exact name.
    cur.execute("SELECT isin FROM security_master WHERE UPPER(security_name)=UPPER(%s) "
                "AND isin IS NOT NULL LIMIT 1", (base,))
    r = cur.fetchone()
    if r and r["isin"]:
        return r["isin"]

    # 4. Security master, ticker as a leading word ('NCC' -> 'NCC LIMITED'). The
    #    trailing space is the word boundary that keeps 'BEL' off 'BELRISE
    #    INDUSTRIES'; an ambiguous match resolves to nothing rather than to a guess.
    cur.execute("SELECT DISTINCT isin FROM security_master WHERE security_name ILIKE %s "
                "AND isin IS NOT NULL LIMIT 2", (f"{base} %",))
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0]["isin"]
    return None


def sync_intraday_positions(conn, entity_map, broker_entity_map):
    """Refresh `intraday_*` on equity_holding for every Indian broker account.

    Returns (rows_written, accounts_covered). Never raises for a single broker: a
    failed positions call leaves that account's intraday columns cleared rather than
    stale, which is the safe direction — a missing line is obvious, a wrong one is not.
    """
    today = date.today()
    cur = conn.cursor()

    # Yesterday's unsettled shares have since become real holdings; leaving the line
    # up would double them on screen. Clear by DATE, not by absence from today's feed,
    # so a broker outage cannot strand a stale figure on the page.
    cur.execute("""UPDATE equity_holding
                      SET intraday_qty = NULL, intraday_avg_cost = NULL,
                          intraday_value = NULL, intraday_as_of = NULL
                    WHERE intraday_as_of IS NOT NULL AND intraday_as_of <> %s""", (today,))
    cleared_stale = cur.rowcount

    written = accounts = 0
    for entity_code, broker_module, broker_label in broker_entity_map:
        entity_id = entity_map.get(entity_code)
        if entity_id is None or not hasattr(broker_module, "fetch_positions"):
            continue
        try:
            positions = broker_module.fetch_positions(entity_code)
        except Exception as e:
            logger.warning(f"[{entity_code}/{broker_label}] positions fetch failed — {e}")
            conn.rollback()
            continue
        accounts += 1

        # Clear this account first so a position closed since the last run disappears.
        cur.execute("""UPDATE equity_holding
                          SET intraday_qty = NULL, intraday_avg_cost = NULL,
                              intraday_value = NULL, intraday_as_of = NULL
                        WHERE entity_id = %s AND broker = %s AND intraday_qty IS NOT NULL""",
                    (entity_id, broker_label))

        for p in positions:
            symbol = p["symbol"]
            qty    = Decimal(str(p["quantity"]))
            avg    = Decimal(str(p["avg_cost"] or 0))
            isin   = p.get("isin") or _resolve_isin(cur, entity_id, broker_label, symbol)

            if isin:
                cur.execute("SELECT id, quantity, avg_cost, current_price FROM equity_holding "
                            "WHERE entity_id=%s AND broker=%s AND isin=%s",
                            (entity_id, broker_label, isin))
            else:
                cur.execute("SELECT id, quantity, avg_cost, current_price FROM equity_holding "
                            "WHERE entity_id=%s AND broker=%s AND UPPER(symbol)=UPPER(%s)",
                            (entity_id, broker_label, symbol))
            row = cur.fetchone()

            # Price the line off the holding's own current price when there is one, so
            # the settled and settling legs of the same instrument never disagree.
            ltp = (row["current_price"] if row and row.get("current_price") else None) \
                or p.get("ltp") or avg
            value = (qty * Decimal(str(ltp))).quantize(Decimal("0.01"))

            if row:
                if p.get("already_in_holdings"):
                    # Angel counted these shares as settled. Take them back out so the
                    # settled figure means the same thing as it does for the others.
                    settled = Decimal(str(row["quantity"])) - qty
                    settled = settled if settled > 0 else Decimal("0")
                    settled_avg = Decimal(str(row["avg_cost"] or 0))
                    cur.execute("""UPDATE equity_holding
                                      SET quantity = %s,
                                          cost = %s,
                                          current_market_value = %s,
                                          intraday_qty = %s, intraday_avg_cost = %s,
                                          intraday_value = %s, intraday_as_of = %s
                                    WHERE id = %s""",
                                (settled,
                                 (settled * settled_avg).quantize(Decimal("0.01")),
                                 (settled * Decimal(str(ltp))).quantize(Decimal("0.01")),
                                 qty, avg, value, today, row["id"]))
                else:
                    cur.execute("""UPDATE equity_holding
                                      SET intraday_qty = %s, intraday_avg_cost = %s,
                                          intraday_value = %s, intraday_as_of = %s
                                    WHERE id = %s""",
                                (qty, avg, value, today, row["id"]))
            else:
                # Nothing settled in this instrument yet — a brand-new position. Carry
                # it as a zero-quantity row so the page has somewhere to show the line.
                # The ghost prune skips rows with an intraday leg (see holdings prune).
                cur.execute("""INSERT INTO equity_holding
                                 (entity_id, broker, symbol, isin, exchange, quantity,
                                  avg_cost, cost, current_price, current_market_value,
                                  currency, fx_rate, as_of_date,
                                  intraday_qty, intraday_avg_cost, intraday_value, intraday_as_of)
                               VALUES (%s,%s,%s,%s,%s,0,0,0,%s,0,'INR',1,%s,%s,%s,%s,%s)""",
                            (entity_id, broker_label, symbol, isin, p.get("exchange") or "NSE",
                             ltp, today, qty, avg, value, today))
            written += 1

    conn.commit()
    logger.info(f"Intraday positions: {written} line(s) across {accounts} account(s)"
                + (f", cleared {cleared_stale} stale" if cleared_stale else ""))
    return written, accounts
