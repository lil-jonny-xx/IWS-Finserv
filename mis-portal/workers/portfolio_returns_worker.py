#!/usr/bin/env python3
"""
Portfolio-level money-weighted return (XIRR) per entity, from real external cash flows.

Uses external_cashflow deposits/withdrawals (see import_ledgers.py) as the dated
flows and the current brokerage portfolio value (equity_holding +
foreign_equity_holding + broker_cash) as the final inflow, then solves XIRR. This
is the true investor return: it reflects when money actually crossed the investor
boundary. Dividend/interest rows are NOT flows — they are credited inside the
broker account and therefore already live in the terminal value; they are only
totalled for the income_inr display column.

Sign convention (investor perspective): deposits negative (cash out of pocket),
withdrawals positive, current value positive.

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


def usd_inr(cur):
    cur.execute("SELECT rate FROM fx_rate WHERE from_currency='USD' AND to_currency='INR' ORDER BY rate_date DESC LIMIT 1")
    r = cur.fetchone()
    if r:
        return float(r["rate"])
    cur.execute("SELECT MAX(fx_rate) m FROM foreign_equity_holding WHERE currency='USD'")
    r = cur.fetchone()
    return float(r["m"]) if r and r["m"] else 83.0


def current_value(cur, entity_id, fx):
    """Current brokerage portfolio value in INR: equity + foreign equity + broker cash
    + brokerage-linked PMS (zerodha_pms)."""
    cur.execute("SELECT COALESCE(SUM(current_market_value),0) v FROM equity_holding WHERE entity_id=%s", (entity_id,))
    v = float(cur.fetchone()["v"])
    cur.execute("SELECT COALESCE(SUM(current_market_value),0) v FROM foreign_equity_holding WHERE entity_id=%s", (entity_id,))
    v += float(cur.fetchone()["v"])
    # broker_cash.balance is ALREADY in INR (refresh_broker_cash converts foreign
    # cash and stores the native amount separately in balance_native). Summing it
    # directly — multiplying by fx here double-converts USD/SGD cash by ~95x.
    cur.execute("SELECT balance FROM broker_cash WHERE entity_id=%s", (entity_id,))
    for row in cur.fetchall():
        v += float(row["balance"] or 0)
    # zerodha_pms is a PMS strategy run INSIDE the client's own Zerodha account — the
    # stock sits in their demat and the cash deposits flow through the Zerodha ledger
    # (so they ARE in external_cashflow). Include it so the XIRR value side matches the
    # flow side. nuvama_pms is a separate managed account whose deposits are NOT yet
    # ingested, so it's deliberately excluded (adding value with no flows would distort).
    cur.execute("SELECT COALESCE(SUM(market_value),0) v FROM pms_holding WHERE entity_id=%s AND source='zerodha_pms'", (entity_id,))
    v += float(cur.fetchone()["v"])
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
            if r["flow_type"] == "DEPOSIT":
                dep += amt; flows.append((r["flow_date"], amt))
            elif r["flow_type"] == "WITHDRAWAL":
                wd += amt; flows.append((r["flow_date"], amt))
            else:
                # DIVIDEND / INTEREST are credited INSIDE the broker account (these
                # rows come from broker ledgers), so the money is already part of the
                # terminal portfolio value (cash balance or reinvested holdings).
                # Counting them as investor inflows too double-counted the income and
                # inflated XIRR. Money-weighted return uses only flows that cross the
                # investor boundary; income is still totalled for display.
                inc += amt
        cv = current_value(cur, eid, fx)
        flows.append((TODAY, cv))
        rate = xirr(flows)
        net_invested = -dep - wd          # dep is negative (outflow); wd positive (inflow)
        xirr_pct = round(rate * 100, 4) if rate is not None else None
        # XIRR is only trustworthy when the ledger and the holdings describe the SAME
        # book. Reject it when they clearly don't: implausible rate, non-positive net
        # investment, value >> net invested (pre-ledger holdings transferred in), or
        # value far BELOW net invested. The last case isn't a real loss for a long-only
        # book — it means the holdings table is mismatched/incomplete vs the deposit
        # ledger (e.g. Rajani Corp's mislabelled Dhan import), which yields a spurious
        # deeply-negative XIRR. Keep XIRR only when value sits within [0.25x, 3x] of
        # net invested.
        plausible = (xirr_pct is not None and abs(xirr_pct) <= 300
                     and net_invested > 0
                     and 0.25 * net_invested <= cv <= 3 * net_invested)
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
