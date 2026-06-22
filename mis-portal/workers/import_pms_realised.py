#!/usr/bin/env python3
"""
PMS realised-gains importer — load a broker PMS realised capital-gains statement
(CSV or Excel) into pms_realised, so PMS realised gains show on the Realised Gains
page (group='PMS').

  python -m workers.import_pms_realised --source nuvama_pms --entity DHR --file realised.csv
  python -m workers.import_pms_realised --source zerodha_pms --entity IWS --file rg.xlsx --dry-run

Behaviour:
  • Columns are matched fuzzily (security/isin/qty/buy-date/sale-date/buy-value/
    sale-value/pnl), so most statement exports work without renaming.
  • P&L is taken from the statement if present, else computed as sale − purchase.
  • Rows are de-duplicated on source_ref (source + statement row id, or a synthetic
    key) — re-importing the same statement is a no-op.
  • --dry-run parses and reports without writing anything.

All amounts are INR (Indian PMS — no FX conversion).
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

CANDIDATES = {
    "security":  ["security", "securityname", "scrip", "scripname", "stock", "instrument", "name", "company"],
    "isin":      ["isin", "isincode"],
    "qty":       ["quantity", "qty", "units", "shares"],
    "buy_date":  ["buydate", "purchasedate", "acquisitiondate", "dateofpurchase", "buy"],
    "sale_date": ["saledate", "selldate", "dateofsale", "exitdate", "sell", "date"],
    "buy_value": ["buyvalue", "purchasevalue", "purchaseamount", "cost", "costofacquisition", "acquisitioncost", "buyamount"],
    "sale_value":["salevalue", "sellvalue", "saleamount", "saleproceeds", "proceeds", "sellamount", "redemptionvalue"],
    "pnl":       ["pnl", "pl", "realisedgain", "realizedgain", "realisedpl", "gainloss", "netgain", "profit", "shorttermgain", "longtermgain"],
    "ref":       ["transactionid", "txnid", "id", "ref", "referenceno", "slno", "srno"],
}


def _norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _resolve_columns(df):
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


def _num(v):
    if pd.isna(v):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def main():
    ap = argparse.ArgumentParser(description="Import a PMS realised-gains statement into pms_realised.")
    ap.add_argument("--source", required=True, choices=["nuvama_pms", "zerodha_pms", "other"])
    ap.add_argument("--entity", required=True, help="entity code (entity_name), e.g. DHR")
    ap.add_argument("--file", required=True, help="statement .csv or .xlsx")
    ap.add_argument("--dry-run", action="store_true", help="parse & report; write nothing")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        sys.exit(f"File not found: {args.file}")
    df = (pd.read_excel(args.file) if args.file.lower().endswith((".xlsx", ".xls"))
          else pd.read_csv(args.file))
    cols = _resolve_columns(df)
    if "security" not in cols or "sale_date" not in cols:
        sys.exit(f"Need at least security + sale-date columns. Found: {list(df.columns)}")
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

    cur.execute("SELECT to_regclass('public.pms_realised') AS t")
    table_exists = cur.fetchone()["t"] is not None
    if not table_exists:
        if not args.dry_run:
            sys.exit("Table pms_realised is missing — run: python -m workers.db_migrate_pms_realised")
        print("(dry-run: pms_realised does not exist yet — skipping duplicate check)")

    inserted = skipped = dupes = 0
    for _, r in df.iterrows():
        security  = str(r[cols["security"]]).strip() if cols.get("security") else None
        sale_date = _parse_date(r[cols["sale_date"]])
        isin      = str(r[cols["isin"]]).strip() if cols.get("isin") and not pd.isna(r[cols["isin"]]) else None
        qty       = _num(r[cols["qty"]]) if cols.get("qty") else None
        buy_date  = _parse_date(r[cols["buy_date"]]) if cols.get("buy_date") else None
        buy_val   = _num(r[cols["buy_value"]]) if cols.get("buy_value") else None
        sale_val  = _num(r[cols["sale_value"]]) if cols.get("sale_value") else None
        pnl       = _num(r[cols["pnl"]]) if cols.get("pnl") else None
        if pnl is None and buy_val is not None and sale_val is not None:
            pnl = sale_val - buy_val
        if not (security and sale_date and pnl is not None):
            skipped += 1
            continue

        ref = str(r[cols["ref"]]).strip() if cols.get("ref") and not pd.isna(r[cols["ref"]]) else None
        source_ref = ref or hashlib.md5(
            f"{entity_id}|{security}|{sale_date}|{qty}|{pnl}".encode()).hexdigest()[:16]

        if table_exists:
            cur.execute("SELECT 1 FROM pms_realised WHERE source = %s AND source_ref = %s",
                        (args.source, source_ref))
            if cur.fetchone():
                dupes += 1
                continue

        if args.dry_run:
            print(f"  + {sale_date} {security[:28]:28s} cost={buy_val or 0:,.0f} "
                  f"sale={sale_val or 0:,.0f} pnl={pnl:,.0f}")
            inserted += 1
            continue
        cur.execute("""
            INSERT INTO pms_realised
                (entity_id, source, security_name, isin, quantity, buy_date, sale_date,
                 purchase_amount, sale_amount, pnl, source_ref, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        """, (entity_id, args.source, security, isin, qty, buy_date, sale_date,
              buy_val, sale_val, pnl, source_ref))
        inserted += 1

    if not args.dry_run:
        conn.commit()
    cur.close(); conn.close()
    verb = "would insert" if args.dry_run else "inserted"
    print(f"\nDone: {verb} {inserted}, skipped {skipped} (incomplete), {dupes} duplicates.")


if __name__ == "__main__":
    main()
