#!/usr/bin/env python3
"""
Tradebook importer — load a broker tradebook export (CSV or Excel) into
stock_transaction, so equity realised gains can be computed.

  python -m workers.import_tradebook --broker zerodha --entity DHR --file trades.csv
  python -m workers.import_tradebook --broker dhan    --entity HHR --file tb.xlsx --dry-run

Behaviour:
  • Columns are matched fuzzily (symbol/isin/date/side/qty/price/trade-id), so most
    broker exports work without renaming.
  • A security_master row (security_type='EQUITY') is auto-created for any stock not
    already present (matched by ISIN, else symbol) — realised matching is by symbol/ISIN.
  • Rows are de-duplicated on source_ref (broker + trade/order id, or a synthetic key).
  • --dry-run parses and reports without writing anything.
"""
import os
import sys
import argparse
import hashlib
from datetime import datetime
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

# Fuzzy column candidates (lower-cased, spaces/underscores ignored on match).
CANDIDATES = {
    "symbol":   ["symbol", "tradingsymbol", "scrip", "scripname", "stock", "instrument", "security", "name"],
    "isin":     ["isin", "isincode"],
    "date":     ["tradedate", "date", "orderexecutiontime", "time", "tradetime", "transactiondate"],
    "side":     ["tradetype", "transactiontype", "type", "side", "buysell", "bs", "b/s"],
    "qty":      ["quantity", "qty", "filledqty", "tradedqty", "tradeqty"],
    "price":    ["price", "tradeprice", "avgprice", "averageprice", "executedprice"],
    "trade_id": ["tradeid", "orderid", "tradenumber", "orderno", "tradeno"],
    "exchange": ["exchange", "exch", "exchangesegment"],
}


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _resolve_columns(df: pd.DataFrame) -> dict:
    norm_map = {_norm(c): c for c in df.columns}
    resolved = {}
    for field, cands in CANDIDATES.items():
        for cand in cands:
            if cand in norm_map:
                resolved[field] = norm_map[cand]
                break
    return resolved


def _parse_date(v):
    if pd.isna(v):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.date()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(v).strip()[:len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(v).date()
    except Exception:
        return None


def _side(v):
    s = str(v).strip().lower()
    if s.startswith("b"):
        return "BUY"
    if s.startswith("s"):
        return "SELL"
    return None


def _get_or_create_security(cur, isin, symbol, exchange, dry):
    """Return security_master id for a stock, creating it if absent (matched by ISIN then symbol)."""
    if isin:
        cur.execute("SELECT id FROM security_master WHERE isin = %s", (isin,))
        row = cur.fetchone()
        if row:
            return row["id"]
    cur.execute("SELECT id FROM security_master WHERE security_name = %s AND security_type = 'EQUITY'", (symbol,))
    row = cur.fetchone()
    if row:
        return row["id"]
    if dry:
        return f"<new:{symbol or isin}>"
    cur.execute("""
        INSERT INTO security_master (isin, security_name, security_type, asset_class, currency, exchange, created_at)
        VALUES (%s, %s, 'EQUITY', 'Equity', 'INR', %s, NOW())
        RETURNING id
    """, (isin or None, symbol, exchange))
    return cur.fetchone()["id"]


def main():
    ap = argparse.ArgumentParser(description="Import a broker tradebook into stock_transaction.")
    ap.add_argument("--broker", required=True, choices=["zerodha", "angel_one", "dhan", "other"])
    ap.add_argument("--entity", required=True, help="entity code (entity_name), e.g. DHR")
    ap.add_argument("--file", required=True, help="tradebook .csv or .xlsx")
    ap.add_argument("--dry-run", action="store_true", help="parse & report; write nothing")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        sys.exit(f"File not found: {args.file}")
    df = (pd.read_excel(args.file) if args.file.lower().endswith((".xlsx", ".xls"))
          else pd.read_csv(args.file))
    cols = _resolve_columns(df)
    missing = [f for f in ("symbol", "date", "side", "qty", "price") if f not in cols]
    if missing:
        sys.exit(f"Could not find required columns {missing}. Found: {list(df.columns)}")
    print(f"Column mapping: {cols}")

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"), password=os.getenv("DB_PASSWORD", ""),
        cursor_factory=RealDictCursor,
    )
    cur = conn.cursor()
    cur.execute("SELECT id FROM entity WHERE entity_name = %s", (args.entity,))
    ent = cur.fetchone()
    if not ent:
        sys.exit(f"Unknown entity code: {args.entity}")
    entity_id = ent["id"]

    inserted = skipped = dupes = 0
    for _, r in df.iterrows():
        symbol = str(r[cols["symbol"]]).strip() if cols.get("symbol") else None
        isin   = str(r[cols["isin"]]).strip() if cols.get("isin") and not pd.isna(r[cols["isin"]]) else None
        tdate  = _parse_date(r[cols["date"]])
        side   = _side(r[cols["side"]])
        try:
            qty   = abs(float(r[cols["qty"]]))
            price = abs(float(r[cols["price"]]))
        except (ValueError, TypeError):
            qty = price = None
        if not (symbol and tdate and side and qty and price):
            skipped += 1
            continue
        exch   = str(r[cols["exchange"]]).strip() if cols.get("exchange") and not pd.isna(r[cols["exchange"]]) else None
        tid    = str(r[cols["trade_id"]]).strip() if cols.get("trade_id") and not pd.isna(r[cols["trade_id"]]) else None
        amount = qty * price
        source_ref = f"{args.broker}:" + (tid or hashlib.md5(
            f"{entity_id}|{symbol}|{tdate}|{side}|{qty}|{price}".encode()).hexdigest()[:16])

        cur.execute("SELECT 1 FROM stock_transaction WHERE source_ref = %s", (source_ref,))
        if cur.fetchone():
            dupes += 1
            continue

        sec_id = _get_or_create_security(cur, isin, symbol, exch, args.dry_run)
        if args.dry_run:
            print(f"  + {tdate} {side:4s} {symbol:14s} qty={qty:g} @ {price:g} = {amount:,.0f}  sec={sec_id}")
            inserted += 1
            continue
        cur.execute("""
            INSERT INTO stock_transaction
                (entity_id, security_id, transaction_date, transaction_type, quantity, price,
                 amount, amount_inr, currency, exchange, source, source_ref, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'INR',%s,%s,%s,NOW())
        """, (entity_id, sec_id, tdate, side, qty, price, amount, amount, exch, args.broker, source_ref))
        inserted += 1

    if not args.dry_run:
        conn.commit()
    cur.close(); conn.close()
    verb = "would insert" if args.dry_run else "inserted"
    print(f"\nDone: {verb} {inserted}, skipped {skipped} (incomplete rows), {dupes} duplicates.")


if __name__ == "__main__":
    main()
