"""DBS Wealth "Holdings" statement parser.

DBS Treasures / Wealth exports a point-in-time *holdings* CSV (not a transaction
log): a short metadata preamble (Account, Date) followed by a wide table whose
header row starts with "Asset Type". Each data row is one position — cash lines
("Cash and Cash Investment") or securities ("Equity").

We turn that into a normalised snapshot the foreign-equity ingest can snapshot-
replace into foreign_equity_holding (broker='dbs'). Values are native-currency
(SGD/USD/…); INR conversion + live-price refresh happen downstream.

Deliberately tolerant: DBS pads every row to ~26 columns with quoted, comma-
grouped numbers and "-" / "" for blanks, so we map by header *name*, not index.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation


# Bloomberg-style exchange suffix (Security Code "AMZN UW") → readable exchange.
# US suffixes all price fine on the equity feed; SP/HK/etc. won't and fall back
# to the statement's own market value.
_EXCH = {
    "UW": "NASDAQ", "UN": "NYSE", "UR": "NASDAQ", "UQ": "NASDAQ",
    "UA": "NYSE Amer", "UF": "US", "UP": "US",
    "SP": "SGX", "HK": "HKEX", "LN": "LSE", "JP": "TSE", "GR": "XETRA",
}
_US_SUFFIXES = {"UW", "UN", "UR", "UQ", "UA", "UF", "UP"}


def detect(filename: str, head: str = "") -> bool:
    """Best-effort: is this a DBS holdings CSV? Filename hint OR the telltale
    'Asset Type' + 'Value in SGD' header combination."""
    fn = (filename or "").lower()
    if "dbs" in fn and fn.endswith(".csv"):
        return True
    return "asset type" in head.lower() and "value in sgd" in head.lower()


def _num(v):
    """'1,200' / '13,068.00' / '"0.00"' → Decimal; '' / '-' / None → None."""
    if v is None:
        return None
    s = str(v).replace(",", "").replace('"', "").strip()
    if s in ("", "-", "nan", "None", "N/A"):
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _date(v):
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _issuer_name(holding: str) -> str:
    """'Ordinary Share, Issuer: Keppel Ltd; Custodian: DBS Nominees' → 'Keppel Ltd'.
    Strips a trailing ', (MNSO UN)' ticker parenthetical that DBS sometimes appends."""
    s = (holding or "").strip()
    m = re.search(r"Issuer:\s*(.+?)\s*;", s)
    name = m.group(1).strip() if m else s
    name = re.sub(r",?\s*\([A-Z0-9./ ]+\)\s*$", "", name).strip()
    return name or s


def _symbol_from_code(code: str):
    """DBS Security Code 'AMZN UW' / 'BRK/B UN' → (feed_ticker, exchange, resolvable).
    Base is the token before the space; '/' → '.' (BRK/B → BRK.B). US suffix ⇒
    resolvable on the price feed; anything else keeps the statement value."""
    code = (code or "").strip()
    if not code:
        return None, None, False
    parts = code.split()
    ticker = parts[0].replace("/", ".").upper()
    suffix = parts[1].upper() if len(parts) > 1 else ""
    return ticker, _EXCH.get(suffix, suffix or None), suffix in _US_SUFFIXES


def parse(path: str) -> dict:
    """Parse a DBS holdings CSV.

    Returns {account, as_of, holdings[], cash[], note}. Each holding carries
    native-currency figures only (qty, avg_cost_native, price_native,
    cost_native, market_value_native) plus identity (symbol, isin, exchange,
    sector, resolvable). Cash lines are returned separately (not written to the
    equity table in v1)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))

    account = as_of = None
    header = None
    header_idx = None
    for i, r in enumerate(rows):
        if not r:
            continue
        key = (r[0] or "").strip().lower()
        if key == "account" and len(r) > 1:
            account = (r[1] or "").strip()
        elif key == "date" and len(r) > 1:
            as_of = _date(r[1])
        elif key == "asset type":
            header = [c.strip() for c in r]
            header_idx = i
            break

    if header is None:
        raise ValueError("Not a DBS holdings statement (no 'Asset Type' header row found).")

    col = {name.lower(): idx for idx, name in enumerate(header)}

    def cell(r, name):
        idx = col.get(name.lower())
        return r[idx] if idx is not None and idx < len(r) else None

    holdings, cash = [], []
    for r in rows[header_idx + 1:]:
        if not r or not (r[0] or "").strip():
            continue
        asset_type = (cell(r, "Asset Type") or "").strip()
        currency = (cell(r, "Currency") or "").strip().upper()
        mv = _num(cell(r, "Market Value"))

        if asset_type.lower().startswith("cash"):
            # Only surface cash currencies that actually hold a balance.
            if mv and mv != 0:
                cash.append({"currency": currency, "market_value_native": mv})
            continue

        qty = _num(cell(r, "Quantity"))
        if not qty:                       # no position → skip (fully exited line)
            continue

        code = (cell(r, "Security Code") or "").strip()
        ticker, exchange, resolvable = _symbol_from_code(code)
        name = _issuer_name(cell(r, "Holding") or "")
        holdings.append({
            "name":                 name,
            "symbol":               ticker or re.sub(r"[^A-Za-z0-9.]", "", name.split()[0])[:20].upper(),
            "isin":                 (cell(r, "ISIN") or "").strip() or None,
            "exchange":             exchange,
            "currency":             currency,
            "quantity":             qty,
            "avg_cost_native":      _num(cell(r, "Avg. Cost Price")),
            "price_native":         _num(cell(r, "Market Price")),
            "cost_native":          _num(cell(r, "Cost Value")),
            "market_value_native":  mv,
            "sector":               (cell(r, "Sector") or "").strip() or None,
            "resolvable":           resolvable,
            "security_code":        code or None,
        })

    note = (f"{len(holdings)} holding(s), {len(cash)} cash balance(s); "
            f"{sum(1 for h in holdings if not h['resolvable'])} won't price-refresh (statement value kept)")
    return {"account": account, "as_of": as_of, "holdings": holdings, "cash": cash, "note": note}


def parse_bytes(data: bytes) -> dict:
    """parse() variant for an in-memory upload."""
    import tempfile, os
    with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    try:
        return parse(tmp)
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    import sys, json
    res = parse(sys.argv[1])
    print(f"account={res['account']}  as_of={res['as_of']}")
    print(res["note"])
    for h in res["holdings"]:
        flag = "" if h["resolvable"] else "  [stmt-value]"
        print(f"  {h['symbol']:8} {h['currency']} qty={h['quantity']} "
              f"avg={h['avg_cost_native']} px={h['price_native']} mv={h['market_value_native']}{flag}")
    for c in res["cash"]:
        print(f"  CASH {c['currency']} {c['market_value_native']}")
