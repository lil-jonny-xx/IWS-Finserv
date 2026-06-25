#!/usr/bin/env python3
"""
Multi-broker tradebook importer — load Zerodha / Dhan / Angel One / Vested
tradebook exports into stock_transaction (so equity realised gains & cost basis
can be computed), with layout-aware parsing for each broker's real file format.

Single file:
  python -m workers.import_tradebooks_multi --broker zerodha --entity HHR \
      --kind tradebook --file "/path/HHR tradebook-IT0219-EQ Zerodha FY 24-25.csv"

Batch (recommended — explicit per-file attribution, immune to mislabelled filenames):
  python -m workers.import_tradebooks_multi --batch manifest.csv [--commit]

Defaults to DRY-RUN: parses, de-dupes, and prints a per-symbol reconciliation of the
tradebook's net position vs the entity's current equity_holding. Pass --commit to write.

Why layout-aware (vs the older import_tradebook.py): Angel One trade sheets put the
table header ~row 34, Dhan ledgers have a metadata preamble, Vested is multi-sheet USD,
and Dhan/Angel One carry no ISIN. A naive read_csv/read_excel mis-parses all of these.
"""
import os
import sys
import csv
import argparse
import hashlib
from datetime import datetime

import openpyxl
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

from equity.symbol_bridge import build_name_isin_map

load_dotenv("/var/www/mis-portal/.env", override=True)

FNO_HINTS = ("FUT", "PUT", "CALL", "OPT")


def _f(v):
    """Parse a float from messy cell values; '' / None / '-' -> None."""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(v):
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, (datetime,)):
        return v.date()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%d-%b-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- parsers
# Each parser yields normalised dict rows:
#   symbol, isin, date, side(BUY/SELL), qty, price, brokerage, stt, other,
#   currency, exchange, trade_id, skipped_reason(None|str)

def parse_zerodha_tradebook(path):
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            side = "BUY" if str(r.get("trade_type", "")).lower().startswith("b") else \
                   "SELL" if str(r.get("trade_type", "")).lower().startswith("s") else None
            yield {
                "symbol": (r.get("symbol") or "").strip(),
                "isin": (r.get("isin") or "").strip() or None,
                "date": _date(r.get("trade_date") or r.get("order_execution_time")),
                "side": side,
                "qty": _f(r.get("quantity")),
                "price": _f(r.get("price")),
                "brokerage": None, "stt": None, "other": None,
                "currency": "INR",
                "exchange": (r.get("exchange") or "").strip() or None,
                "trade_id": (str(r.get("trade_id")) or "").strip() or None,
                "skipped_reason": None,
            }


def _parse_dhan_global_txn_report(path):
    """Dhan 'Global Transaction Report': a preamble (Name/UCC/…) then per-bill rows with
    SEPARATE Buy Qty./Buy Value/Sell Qty./Sell Value columns — a single row can hold both a
    buy and a sell (intraday), so it yields up to two records. No ISIN (names bridge to
    holdings). Price = value/qty; the row's charges attach to the buy leg (else the sell)."""
    rows = list(csv.reader(open(path, newline="", encoding="utf-8", errors="replace")))
    def _joined(row):
        return " | ".join(str(c).strip().lower() for c in row if c is not None)
    hdr_idx = next((i for i, row in enumerate(rows)
                    if "scrip name" in _joined(row) and "buy qty" in _joined(row)), None)
    if hdr_idx is None:
        return
    idx = {str(c).strip(): i for i, c in enumerate(rows[hdr_idx])}
    def g(row, key):
        i = idx.get(key)
        return row[i] if (i is not None and i < len(row)) else None
    def gdate(v):
        try:
            return datetime.strptime(str(v).strip()[:11], "%d %b %Y").date()
        except (ValueError, TypeError):
            return _date(v)
    for row in rows[hdr_idx + 1:]:
        name = (g(row, "Scrip Name") or "").strip()
        d = gdate(g(row, "Date"))
        if not name or d is None:                       # blank line / footer (Net P&L, NOTE)
            continue
        exch = (g(row, "Exchange") or "").strip() or None
        buy_qty, buy_val = _f(g(row, "Buy Qty.")), _f(g(row, "Buy Value"))
        sell_qty, sell_val = _f(g(row, "Sell Qty.")), _f(g(row, "Sell Value"))
        other = sum(x for x in (_f(g(row, c)) for c in
                    ("GST", "SEBI Fees", "Stamp Duty", "Txn. Charges", "Oth. Charges")) if x) or None
        brokerage, stt = _f(g(row, "Brokerage")), _f(g(row, "STT"))
        charged = False
        if buy_qty and buy_val:
            yield {"symbol": name, "isin": None, "date": d, "side": "BUY",
                   "qty": buy_qty, "price": round(buy_val / buy_qty, 4),
                   "brokerage": brokerage, "stt": stt, "other": other,
                   "currency": "INR", "exchange": exch, "trade_id": None, "skipped_reason": None}
            charged = True
        if sell_qty and sell_val:
            yield {"symbol": name, "isin": None, "date": d, "side": "SELL",
                   "qty": sell_qty, "price": round(sell_val / sell_qty, 4),
                   "brokerage": None if charged else brokerage,
                   "stt": None if charged else stt, "other": None if charged else other,
                   "currency": "INR", "exchange": exch, "trade_id": None, "skipped_reason": None}


def parse_dhan_tradebook(path):
    # Dhan has two export layouts. The "Global Transaction Report" has a preamble and
    # per-bill Buy/Sell qty+value columns; the older trade book is flat Name/Segment/
    # Buy-Sell/Quantity rows. Detect and route.
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        head = fh.read(4096).lower()
    if "global transction report" in head or "scrip name" in head:
        yield from _parse_dhan_global_txn_report(path)
        return
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            name = (r.get("Name") or "").strip()
            seg = (r.get("Segment") or "").strip()
            if seg.lower() != "equity" or any(h in name.upper() for h in FNO_HINTS):
                yield {"symbol": name, "skipped_reason": "non-equity/F&O", **_blank()}
                continue
            bs = (r.get("Buy/Sell") or "").strip().upper()
            yield {
                "symbol": name,                  # company name; resolved to security by name
                "isin": None,
                "date": _date(r.get("Date")),
                "side": "BUY" if bs == "BUY" else "SELL" if bs == "SELL" else None,
                "qty": _f(r.get("Quantity/Lot")),
                "price": _f(r.get("Trade Price")),
                "brokerage": None, "stt": None, "other": None,
                "currency": "INR",
                "exchange": (r.get("Exchange") or "").strip() or None,
                "trade_id": None,
                "skipped_reason": None,
            }


def _find_header(rows, tokens):
    """Return index of the first row containing all given lowercase tokens."""
    for i, row in enumerate(rows):
        cells = [str(c).strip().lower() for c in row if c is not None]
        joined = " | ".join(cells)
        if all(t in joined for t in tokens):
            return i
    return None


def _xlsx_rows(path, sheet):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def parse_angel_one_tradebook(path):
    rows = _xlsx_rows(path, "TradesAndCharges")
    h = _find_header(rows, ["scrip", "buy/sell", "quantity"])
    if h is None:
        raise ValueError("Angel One: could not locate trades header (Scrip/Contract…)")
    hdr = [str(c).strip() if c is not None else "" for c in rows[h]]
    idx = {name: i for i, name in enumerate(hdr)}

    def g(row, col):
        return row[idx[col]] if col in idx and idx[col] < len(row) else None

    for row in rows[h + 1:]:
        if row is None or all(c is None for c in row):
            continue
        scrip = g(row, "Scrip/Contract")
        if not scrip:
            continue
        bs = str(g(row, "Buy/Sell") or "").strip().upper()
        side = "BUY" if bs == "BUY" else "SELL" if bs == "SELL" else None
        price = _f(g(row, "Buy Price")) if side == "BUY" else _f(g(row, "Sell Price"))
        charges = sum(x for x in (_f(g(row, "GST")), _f(g(row, "Sebi Tax")),
                                  _f(g(row, "Exchange Turnover Charges")), _f(g(row, "Stamp Duty")),
                                  _f(g(row, "Other Charges")), _f(g(row, "IPFT Charges"))) if x) or None
        yield {
            "symbol": str(scrip).strip(),
            "isin": None,
            "date": _date(g(row, "Date")),
            "side": side,
            "qty": _f(g(row, "Quantity")),
            "price": price,
            "brokerage": _f(g(row, "Brokerage")),
            "stt": _f(g(row, "STT")),
            "other": charges,
            "currency": "INR",
            "exchange": str(g(row, "Exchange") or "").strip() or None,
            "trade_id": str(g(row, "Trade ID") or "").strip() or None,
            "skipped_reason": None,
        }


def parse_vested_tradebook(path):
    rows = _xlsx_rows(path, "Trades")
    h = _find_header(rows, ["ticker", "activity", "quantity"])
    if h is None:
        raise ValueError("Vested: could not locate Trades header")
    hdr = [str(c).strip() if c is not None else "" for c in rows[h]]
    idx = {name: i for i, name in enumerate(hdr)}

    def g(row, col):
        return row[idx[col]] if col in idx and idx[col] < len(row) else None

    for row in rows[h + 1:]:
        if row is None or all(c is None for c in row):
            continue
        ticker = g(row, "Ticker")
        if not ticker:
            continue
        act = str(g(row, "Activity") or "").strip().upper()
        yield {
            "symbol": str(ticker).strip(),
            "isin": None,
            "date": _date(g(row, "Date")),
            "side": "BUY" if act == "BUY" else "SELL" if act == "SELL" else None,
            "qty": _f(g(row, "Quantity")),
            "price": _f(g(row, "Price Per Share (in USD)")),
            "brokerage": _f(g(row, "Commission Charges (in USD)")),
            "stt": None, "other": None,
            "currency": "USD",
            "exchange": "US",
            "trade_id": None,
            "skipped_reason": None,
        }


def _blank():
    return {"isin": None, "date": None, "side": None, "qty": None, "price": None,
            "brokerage": None, "stt": None, "other": None, "currency": "INR",
            "exchange": None, "trade_id": None}


PARSERS = {
    "zerodha": parse_zerodha_tradebook,
    "dhan": parse_dhan_tradebook,
    "angel_one": parse_angel_one_tradebook,
    "vested": parse_vested_tradebook,
}


# ---------------------------------------------------------------- DB
def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=RealDictCursor,
    )


def entity_id(cur, code):
    cur.execute("SELECT id FROM entity WHERE entity_name = %s", (code,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"Unknown entity code: {code}")
    return row["id"]


def get_or_create_security(cur, isin, symbol, exchange, currency, commit):
    if isin:
        cur.execute("SELECT id FROM security_master WHERE isin = %s", (isin,))
        row = cur.fetchone()
        if row:
            return row["id"]
    cur.execute("SELECT id FROM security_master WHERE security_name = %s AND security_type='EQUITY'", (symbol,))
    row = cur.fetchone()
    if row:
        return row["id"]
    if not commit:
        return None
    cur.execute("""
        INSERT INTO security_master (isin, security_name, security_type, asset_class, currency, exchange, created_at)
        VALUES (%s,%s,'EQUITY','Equity',%s,%s,NOW()) RETURNING id
    """, (isin or None, symbol, currency, exchange))
    return cur.fetchone()["id"]


def net_by_name(broker, paths):
    """Net traded quantity per security name across a broker's tradebook files.
    De-duplicates identical trades the same way the importer does (the Dhan exports
    overlap in date range), so net positions aren't inflated by double-counting."""
    net, seen = {}, set()
    for p in paths:
        for r in PARSERS[broker](p):
            if r.get("skipped_reason") or not (r["symbol"] and r["side"] and r["qty"] and r["date"]):
                continue
            key = (r["symbol"], r["date"], r["side"], r["qty"], r["price"])
            if key in seen:
                continue
            seen.add(key)
            net[r["symbol"]] = net.get(r["symbol"], 0) + (r["qty"] if r["side"] == "BUY" else -r["qty"])
    return net


def build_bridge(cur, broker, ent_id, paths):
    """Resolve Angel One / Dhan tradebook names -> ISIN via the entity's holdings."""
    cur.execute("""SELECT symbol, isin, quantity FROM equity_holding
                   WHERE entity_id=%s AND broker ILIKE %s AND isin IS NOT NULL""",
                (ent_id, f"%{broker}%"))
    holdings = [(r["symbol"], r["isin"], r["quantity"]) for r in cur.fetchall()]
    name_isin, unresolved = build_name_isin_map(net_by_name(broker, paths), holdings)
    if name_isin:
        print(f"  bridge: resolved {len(name_isin)} name(s) to ISIN"
              + (f"; {len(unresolved)} held name(s) unresolved: {unresolved}" if unresolved else ""))
    return name_isin


def import_file(cur, broker, ent_id, kind, path, commit, recon, isin_map=None):
    if kind != "tradebook":
        print(f"  (skipping {kind} — only tradebooks supported here)")
        return
    parser = PARSERS[broker]
    inserted = skipped = dupes = 0
    net = {}  # symbol -> net qty, for reconciliation
    for r in parser(path):
        if r.get("skipped_reason"):
            skipped += 1
            continue
        if not r.get("isin") and isin_map:
            r["isin"] = isin_map.get(r["symbol"])
        if not (r["symbol"] and r["date"] and r["side"] and r["qty"] and r["price"]):
            skipped += 1
            continue
        net[r["symbol"]] = net.get(r["symbol"], 0) + (r["qty"] if r["side"] == "BUY" else -r["qty"])
        amount = r["qty"] * r["price"]
        charges = sum(x for x in (r["brokerage"], r["stt"], r["other"]) if x) or None
        total_cost = amount + (charges or 0)
        sref = f"{broker}:" + (r["trade_id"] or hashlib.md5(
            f"{ent_id}|{r['symbol']}|{r['date']}|{r['side']}|{r['qty']}|{r['price']}".encode()).hexdigest()[:16])
        cur.execute("SELECT 1 FROM stock_transaction WHERE source_ref = %s", (sref,))
        if cur.fetchone():
            dupes += 1
            continue
        sec_id = get_or_create_security(cur, r["isin"], r["symbol"], r["exchange"], r["currency"], commit)
        if commit:
            cur.execute("""
                INSERT INTO stock_transaction
                  (entity_id, security_id, transaction_date, transaction_type, quantity, price,
                   amount, brokerage, stt, other_charges, total_cost, currency, exchange,
                   source, source_ref, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            """, (ent_id, sec_id, r["date"], r["side"], r["qty"], r["price"], amount,
                  r["brokerage"], r["stt"], r["other"], total_cost, r["currency"],
                  r["exchange"], broker, sref))
        inserted += 1
    verb = "inserted" if commit else "would insert"
    print(f"  {verb} {inserted}, skipped {skipped} (incomplete/F&O), {dupes} dupes")
    if recon:
        reconcile(cur, broker, ent_id, net)


def reconcile(cur, broker, ent_id, net):
    """Compare tradebook net positions to current equity_holding for this entity+broker."""
    cur.execute("""SELECT symbol, quantity FROM equity_holding
                   WHERE entity_id=%s AND broker ILIKE %s""", (ent_id, f"%{broker}%"))
    held = {row["symbol"]: float(row["quantity"]) for row in cur.fetchall()}
    syms = sorted(set(net) | set(held))
    print(f"  reconcile (tradebook net vs equity_holding):")
    print(f"    {'SYMBOL':14} {'tb_net':>10} {'held':>10}  flag")
    for s in syms:
        tb = net.get(s)
        hd = held.get(s)
        flag = "ok" if tb is not None and hd is not None and abs(tb - hd) < 0.5 else \
               ("HELD-NOT-IN-TB" if tb is None else "TB-ONLY/closed" if (hd is None and abs(tb) > 0.5)
                else "mismatch" if (tb is not None and hd is not None) else "")
        if flag in ("ok",) and abs((tb or 0)) < 0.5:
            continue
        print(f"    {s:14} {('' if tb is None else f'{tb:.0f}'):>10} {('' if hd is None else f'{hd:.0f}'):>10}  {flag}")


def main():
    ap = argparse.ArgumentParser(description="Multi-broker tradebook importer.")
    ap.add_argument("--broker", choices=list(PARSERS))
    ap.add_argument("--entity")
    ap.add_argument("--kind", default="tradebook", choices=["tradebook", "ledger"])
    ap.add_argument("--file")
    ap.add_argument("--batch", help="CSV manifest with columns: file,broker,entity,kind")
    ap.add_argument("--commit", action="store_true", help="write to DB (default: dry-run)")
    ap.add_argument("--no-recon", action="store_true", help="skip holdings reconciliation")
    args = ap.parse_args()

    jobs = []
    if args.batch:
        with open(args.batch, newline="") as fh:
            for row in csv.DictReader(fh):
                if not row.get("file") or row["file"].strip().startswith("#"):
                    continue
                jobs.append((row["broker"].strip(), row["entity"].strip(),
                             row.get("kind", "tradebook").strip(), row["file"].strip()))
    elif args.file and args.broker and args.entity:
        jobs.append((args.broker, args.entity, args.kind, args.file))
    else:
        ap.error("provide --batch, or all of --broker --entity --file")

    conn = connect()
    cur = conn.cursor()
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'} — {len(jobs)} file(s)\n")

    # Pre-build name->ISIN bridges for name-only brokers, per (entity, broker),
    # aggregating across all that group's tradebook files.
    bridges = {}
    for broker, ent_code, kind, path in jobs:
        if broker in ("angel_one", "dhan") and kind == "tradebook":
            bridges.setdefault((ent_code, broker), []).append(path)
    bridge_maps = {}
    for (ent_code, broker), paths in bridges.items():
        eid = entity_id(cur, ent_code)
        print(f"[bridge {broker} / {ent_code}]  ({len(paths)} file(s))")
        bridge_maps[(ent_code, broker)] = build_bridge(cur, broker, eid, paths)
        print()

    for broker, ent_code, kind, path in jobs:
        if not os.path.exists(path):
            print(f"!! missing: {path}"); continue
        print(f"[{broker} / {ent_code} / {kind}] {os.path.basename(path)}")
        eid = entity_id(cur, ent_code)
        try:
            import_file(cur, broker, eid, kind, path, args.commit, not args.no_recon,
                        isin_map=bridge_maps.get((ent_code, broker)))
        except Exception as e:
            print(f"  ERROR: {e}")
        print()
    if args.commit:
        conn.commit()
        print("committed.")
    else:
        conn.rollback()
        print("dry-run — nothing written.")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
