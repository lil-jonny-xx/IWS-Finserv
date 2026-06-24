#!/usr/bin/env python3
"""
Portfolio-level money-weighted return (XIRR) per entity, from real external cash flows.

Uses external_cashflow (deposits / withdrawals / dividends / interest — see
import_ledgers.py) as the dated flows and the current brokerage portfolio value
(equity_holding + foreign_equity_holding + broker_cash) as the final inflow, then
solves XIRR. This is the true investor return: it reflects when money actually went
in/out and counts dividends & interest as income — things per-holding price-return
metrics miss.

Sign convention (investor perspective): deposits negative (cash out of pocket),
withdrawals/dividends/interest positive, current value positive.

USD flows (Vested dividends/interest) are converted at the latest USD→INR rate; this
is an approximation for historical flows (timing dominates XIRR more than FX drift).

Writes one row per entity into portfolio_returns. Dry-run by default; --commit to write.
"""
import os
import sys
import argparse
from datetime import date

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

sys.path.insert(0, "/var/www/mis-portal")
from equity.finmath import xirr

load_dotenv("/var/www/mis-portal/.env", override=True)
TODAY = date.today()


def connect():
    return psycopg2.connect(host=os.getenv("DB_HOST", "localhost"),
                            database=os.getenv("DB_NAME", "mis_portal"),
                            user=os.getenv("DB_USER", "postgres"),
                            password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
                            cursor_factory=RealDictCursor)


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_returns (
            entity_id      INTEGER NOT NULL REFERENCES entity(id),
            as_of_date     DATE NOT NULL,
            xirr_pct       NUMERIC(18,4),
            deposits_inr   NUMERIC(18,2),
            withdrawals_inr NUMERIC(18,2),
            income_inr     NUMERIC(18,2),
            current_value_inr NUMERIC(18,2),
            coverage       VARCHAR(10),
            updated_at     TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (entity_id, as_of_date)
        );
    """)
    cur.execute("ALTER TABLE portfolio_returns ADD COLUMN IF NOT EXISTS coverage VARCHAR(10);")


def coverage_flag(cur, entity_id, earliest_flow):
    """'full' if the ledger's external flows start no later than the entity's oldest
    holding; else 'partial' — XIRR is unreliable when deposit history is incomplete."""
    cur.execute("SELECT MIN(first_invested_date) d FROM equity_holding WHERE entity_id=%s AND first_invested_date IS NOT NULL", (entity_id,))
    r = cur.fetchone()
    oldest = r["d"] if r else None
    if oldest is None or earliest_flow is None:
        return "partial"
    return "full" if earliest_flow <= oldest else "partial"


def usd_inr(cur):
    cur.execute("SELECT rate FROM fx_rate WHERE from_currency='USD' AND to_currency='INR' ORDER BY rate_date DESC LIMIT 1")
    r = cur.fetchone()
    if r:
        return float(r["rate"])
    cur.execute("SELECT MAX(fx_rate) m FROM foreign_equity_holding WHERE currency='USD'")
    r = cur.fetchone()
    return float(r["m"]) if r and r["m"] else 83.0


def current_value(cur, entity_id, fx):
    """Current brokerage portfolio value in INR: equity + foreign equity + broker cash."""
    cur.execute("SELECT COALESCE(SUM(current_market_value),0) v FROM equity_holding WHERE entity_id=%s", (entity_id,))
    v = float(cur.fetchone()["v"])
    cur.execute("SELECT COALESCE(SUM(current_market_value),0) v FROM foreign_equity_holding WHERE entity_id=%s", (entity_id,))
    v += float(cur.fetchone()["v"])
    cur.execute("SELECT broker, balance, currency FROM broker_cash WHERE entity_id=%s", (entity_id,))
    for row in cur.fetchall():
        bal = float(row["balance"] or 0)
        v += bal * (fx if (row["currency"] or "INR").upper() == "USD" else 1.0)
    return v


def main():
    ap = argparse.ArgumentParser(description="Per-entity portfolio XIRR from external cash flows.")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = connect(); cur = conn.cursor()
    ensure_table(cur)
    fx = usd_inr(cur)

    cur.execute("SELECT id, entity_name FROM entity ORDER BY id")
    entities = cur.fetchall()
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'}  (USD→INR={fx:.2f})\n")
    print(f"{'ENTITY':14}{'XIRR%':>8}{'deposits':>16}{'withdrawn':>15}{'income':>12}{'cur.value':>16}")
    for e in entities:
        eid = e["id"]
        cur.execute("""SELECT flow_date, flow_type, amount_native, currency FROM external_cashflow
                       WHERE entity_id=%s ORDER BY flow_date""", (eid,))
        rows = cur.fetchall()
        if not rows:
            continue
        flows = []
        dep = wd = inc = 0.0
        for r in rows:
            amt = float(r["amount_native"])
            if (r["currency"] or "INR").upper() == "USD":
                amt *= fx
            flows.append((r["flow_date"], amt))
            if r["flow_type"] == "DEPOSIT": dep += amt
            elif r["flow_type"] == "WITHDRAWAL": wd += amt
            else: inc += amt
        cv = current_value(cur, eid, fx)
        flows.append((TODAY, cv))
        rate = xirr(flows)
        net_invested = -dep - wd          # dep is negative (outflow); wd positive (inflow)
        xirr_pct = round(rate * 100, 4) if rate is not None else None
        # XIRR is only trustworthy when the ledger captures the capital behind the current
        # value: implausible rate, non-positive net investment, or value >> net invested
        # (pre-ledger holdings transferred in) all mean incomplete deposit history.
        plausible = (xirr_pct is not None and abs(xirr_pct) <= 300
                     and net_invested > 0 and cv <= 3 * net_invested)
        cov = "full" if plausible else "partial"
        if not plausible:
            xirr_pct = None
        print(f"{e['entity_name'][:13]:14}{(xirr_pct if xirr_pct is not None else 0):>8.1f}"
              f"{dep:>16,.0f}{wd:>15,.0f}{inc:>12,.0f}{cv:>16,.0f}  {cov}")
        if args.commit:
            cur.execute("""INSERT INTO portfolio_returns
                (entity_id, as_of_date, xirr_pct, deposits_inr, withdrawals_inr, income_inr, current_value_inr, coverage, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (entity_id, as_of_date) DO UPDATE SET
                  xirr_pct=EXCLUDED.xirr_pct, deposits_inr=EXCLUDED.deposits_inr,
                  withdrawals_inr=EXCLUDED.withdrawals_inr, income_inr=EXCLUDED.income_inr,
                  current_value_inr=EXCLUDED.current_value_inr, coverage=EXCLUDED.coverage, updated_at=NOW()""",
                (eid, TODAY, xirr_pct, round(dep, 2), round(wd, 2), round(inc, 2), round(cv, 2), cov))
    if args.commit:
        conn.commit()
    print("\ncommitted." if args.commit else "\ndry-run — nothing written.")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
