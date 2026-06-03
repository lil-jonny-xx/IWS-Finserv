"""
Zerodha Trade Book CSV Importer

Downloads the Trade Book from Zerodha Console (Reports → Trade History → Download CSV).
Parses the earliest BUY date per symbol and backfills `first_invested_date` in
`equity_holding` — which unlocks CAGR calculation.

Usage:
    python equity/zerodha_tradebook_import.py --entity DHR --file tradebook.csv
    python equity/zerodha_tradebook_import.py --entity DHR --file tradebook.csv --dry-run

The script handles Zerodha's two common CSV formats:
  Format A (older Console):  symbol, trade_date, trade_type, quantity, price, ...
  Format B (newer Console):  tradingsymbol, order_execution_time, transaction_type, ...
"""
import argparse
import csv
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Column name candidates (Zerodha uses different names across report versions)
_SYMBOL_COLS   = ["tradingsymbol", "symbol", "stock_symbol", "instrument"]
_DATE_COLS     = ["trade_date", "order_execution_time", "date", "trade_execution_time"]
_TYPE_COLS     = ["trade_type", "transaction_type", "order_type", "buy_sell"]
_BUY_KEYWORDS  = {"buy", "b", "purchase"}


def _detect_col(headers: list[str], candidates: list[str]) -> str | None:
    lc = [h.strip().lower() for h in headers]
    for c in candidates:
        if c in lc:
            return headers[lc.index(c)]
    return None


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d",
                "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d %H:%M:%S",
                "%d-%m-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def parse_tradebook(csv_path: str) -> dict[str, date]:
    """Return {tradingsymbol: earliest_buy_date} from the CSV."""
    earliest: dict[str, date] = {}

    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        # Skip comment / metadata lines that some Zerodha exports prepend
        lines = []
        for line in fh:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(line)

    reader = csv.DictReader(lines)
    headers = reader.fieldnames or []
    if not headers:
        raise ValueError("CSV has no headers")

    sym_col  = _detect_col(headers, _SYMBOL_COLS)
    date_col = _detect_col(headers, _DATE_COLS)
    type_col = _detect_col(headers, _TYPE_COLS)

    if not sym_col or not date_col or not type_col:
        raise ValueError(
            f"Could not detect required columns.\n"
            f"  Found headers: {headers}\n"
            f"  Detected — symbol: {sym_col}, date: {date_col}, type: {type_col}\n"
            f"  Expected symbol in {_SYMBOL_COLS}\n"
            f"  Expected date   in {_DATE_COLS}\n"
            f"  Expected type   in {_TYPE_COLS}"
        )

    logger.info(f"Columns — symbol: '{sym_col}', date: '{date_col}', type: '{type_col}'")

    for row in reader:
        trade_type = row.get(type_col, "").strip().lower()
        if trade_type not in _BUY_KEYWORDS:
            continue

        symbol = row.get(sym_col, "").strip().upper()
        if not symbol:
            continue

        raw_date = row.get(date_col, "").strip()
        trade_date = _parse_date(raw_date)
        if not trade_date:
            logger.warning(f"Could not parse date '{raw_date}' for {symbol}, skipping row")
            continue

        if symbol not in earliest or trade_date < earliest[symbol]:
            earliest[symbol] = trade_date

    logger.info(f"Parsed {len(earliest)} unique symbols with buy trades")
    return earliest


def get_db():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        dbname=os.environ["DB_NAME"],
        user=os.environ.get("DB_USER", os.environ["DB_NAME"]),
        password=os.environ["DB_PASSWORD"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def backfill(entity_code: str, earliest: dict[str, date], dry_run: bool) -> None:
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("SELECT id FROM entity WHERE entity_name = %s", (entity_code,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Entity '{entity_code}' not found in DB")
    entity_id = row["id"]

    cur.execute(
        "SELECT symbol, first_invested_date FROM equity_holding WHERE entity_id = %s",
        (entity_id,),
    )
    holdings = {r["symbol"]: r["first_invested_date"] for r in cur.fetchall()}

    if not holdings:
        logger.warning(f"No equity holdings found for entity '{entity_code}'")
        conn.close()
        return

    updated = 0
    skipped = 0
    not_held = 0

    for symbol, new_date in sorted(earliest.items()):
        if symbol not in holdings:
            not_held += 1
            continue

        current = holdings[symbol]
        if current is not None and current <= new_date:
            logger.debug(f"  {symbol}: DB has {current} ≤ CSV {new_date}, no change")
            skipped += 1
            continue

        action = "NULL → " if current is None else f"{current} → "
        logger.info(f"  {symbol}: {action}{new_date}")

        if not dry_run:
            cur.execute(
                """UPDATE equity_holding
                   SET first_invested_date = %s, updated_at = NOW()
                   WHERE entity_id = %s AND symbol = %s""",
                (new_date, entity_id, symbol),
            )
        updated += 1

    if not dry_run:
        conn.commit()
        logger.info(f"Committed. Updated: {updated}, Skipped (already older): {skipped}, "
                    f"Not in holdings: {not_held}")
    else:
        logger.info(f"[DRY RUN] Would update: {updated}, Skip: {skipped}, "
                    f"Not in holdings: {not_held}")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill first_invested_date from Zerodha trade book CSV")
    parser.add_argument("--entity", required=True, help="Entity code, e.g. DHR")
    parser.add_argument("--file",   required=True, help="Path to Zerodha trade book CSV")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing to DB")
    args = parser.parse_args()

    csv_path = Path(args.file)
    if not csv_path.exists():
        logger.error(f"File not found: {csv_path}")
        sys.exit(1)

    logger.info(f"Parsing trade book: {csv_path}")
    earliest = parse_tradebook(str(csv_path))

    if not earliest:
        logger.warning("No BUY trades found in CSV")
        sys.exit(0)

    logger.info(f"Backfilling entity '{args.entity}'" +
                (" [DRY RUN]" if args.dry_run else ""))
    backfill(args.entity, earliest, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
