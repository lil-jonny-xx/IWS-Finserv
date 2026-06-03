#!/usr/bin/env python3
"""
CAS Parser Worker — IWS MIS Portal
Reads CAMS CAS PDF and:
1. Extracts folio→holder-name mapping directly from the PDF text
2. Maps each holder to the correct entity via DB lookup
3. Seeds security_master with ISINs + amfi_code
4. Seeds folio_mapping
5. Updates holding (current units + cost basis) per entity
6. Inserts all transactions into mf_transaction

Usage:
  python workers/cas_parser_worker.py --pdf workers/cas_statement.pdf --password 123456

  # Optional: restrict to a single entity (overrides auto-detection)
  python workers/cas_parser_worker.py --pdf workers/cas_statement.pdf --password 123456 --entity-id 7

Holder-name → entity mapping is driven by the 'entity' table.
The NAME_PATTERNS dict (below) maps lower-case substrings to entity_name codes.
"""
import os
import sys
import re
import logging
import casparser
import fitz  # PyMuPDF
from decimal import Decimal
from datetime import datetime, date, timezone
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv('/var/www/mis-portal/.env', override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/var/log/mis-portal-cas-worker.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Map lower-case substrings of the folio holder name → entity_name code in DB.
# Add more entries here if new entities are onboarded.
NAME_PATTERNS: list[tuple[str, str]] = [
    ("dhruv harish rajani",    "DHR"),
    ("atharv dhruv rajani",    "ADR"),
    ("iws finserv",            "IWS"),
    ("imperial wealth",        "IWS"),
    ("harsh harish rajani",    "HHR"),
    ("iws fincorp",            "IWS Fincorp"),
    ("stuti dhruv rajani",     "SDR"),
]


def now_utc():
    return datetime.now(timezone.utc)


def get_db():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


# ---------------------------------------------------------------------------
# PDF text extraction – folio → holder name
# ---------------------------------------------------------------------------

def extract_folio_holders(pdf_path: str, password: str) -> dict[str, str]:
    """
    Parse raw PDF text to build {folio_number: holder_name}.

    CAMS CAS layout (each scheme section):
        Folio No: 12276491 / 69
        ***REMOVED***
          Nominee 1: …
    The line immediately after "Folio No:" is the account holder name.
    """
    mapping: dict[str, str] = {}
    try:
        doc = fitz.open(pdf_path)
        doc.authenticate(password)
        full_text = "\n".join(page.get_text() for page in doc)
    except Exception as e:
        logger.error(f"PyMuPDF extraction failed: {e}")
        return mapping

    lines = full_text.split("\n")
    folio_re = re.compile(r"Folio\s+No\s*:\s*(.+)", re.IGNORECASE)

    for i, line in enumerate(lines):
        m = folio_re.search(line)
        if not m:
            continue
        folio_raw = m.group(1).strip()

        # Holder name is on the very next non-empty line
        for j in range(i + 1, min(i + 5, len(lines))):
            candidate = lines[j].strip()
            if candidate and not candidate.lower().startswith("nominee"):
                mapping[folio_raw] = candidate
                break

    logger.info(f"Extracted {len(mapping)} folio→holder entries from PDF")
    for f, h in sorted(mapping.items()):
        logger.info(f"  {f:<22} → {h}")
    return mapping


def holder_to_entity_id(
    holder_name: str,
    entity_map: dict[str, int],   # entity_name_code → entity_id
) -> int | None:
    """Match a holder name string to an entity_id using NAME_PATTERNS."""
    lower = holder_name.lower()
    for pattern, code in NAME_PATTERNS:
        if pattern in lower:
            eid = entity_map.get(code)
            if eid:
                return eid
    logger.warning(f"No entity match for holder: '{holder_name}'")
    return None


def load_entity_map(conn) -> dict[str, int]:
    """Load {entity_name: entity_id} from DB."""
    cur = conn.cursor()
    cur.execute("SELECT id, entity_name FROM entity ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    return {r["entity_name"]: r["id"] for r in rows}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def map_type(scheme_type):
    m = {
        "EQUITY":    ("MF_EQUITY",    "EQUITY"),
        "DEBT":      ("MF_DEBT",      "FIXED_INCOME"),
        "HYBRID":    ("MF_HYBRID",    "EQUITY"),
        "FOF":       ("MF_FOF",       "EQUITY"),
        "LIQUID":    ("MF_LIQUID",    "FIXED_INCOME"),
        "ELSS":      ("MF_ELSS",      "EQUITY"),
        "GOLD":      ("GOLD_ETF",     "ALTERNATES"),
        "ETF":       ("MF_ETF",       "EQUITY"),
        "ARBITRAGE": ("MF_ARBITRAGE", "FIXED_INCOME"),
    }
    return m.get(
        str(scheme_type).upper() if scheme_type else "OTHER",
        ("MF_OTHER", "EQUITY")
    )


def upsert_security(conn, isin, name, sec_type, asset_class, amfi_code=None):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO security_master
            (isin, security_name, security_type, asset_class, currency, amfi_code)
        VALUES (%s, %s, %s, %s, 'INR', %s)
        ON CONFLICT (isin) DO UPDATE SET
            security_name = EXCLUDED.security_name,
            security_type = EXCLUDED.security_type,
            asset_class   = EXCLUDED.asset_class,
            amfi_code     = COALESCE(EXCLUDED.amfi_code, security_master.amfi_code)
        RETURNING id, amfi_code
    """, (isin, name, sec_type, asset_class, amfi_code))
    row = cur.fetchone()
    cur.close()
    return row["id"], row["amfi_code"]


def upsert_folio(cur, folio_number, entity_id, scheme_name):
    cur.execute("""
        INSERT INTO folio_mapping (folio_number, entity_id, mf_scheme)
        VALUES (%s, %s, %s)
        ON CONFLICT (folio_number) DO UPDATE SET
            entity_id = EXCLUDED.entity_id,
            mf_scheme = EXCLUDED.mf_scheme
    """, (folio_number.strip(), entity_id, scheme_name))


def upsert_holding(cur, entity_id, security_id, folio_number,
                   units, cost_basis, avg_cost, first_date, nav):
    cur.execute("""
        UPDATE holding SET
            quantity             = %s,
            cost_basis           = %s,
            avg_cost             = %s,
            invested_amount      = %s,
            last_updated_nav     = %s,
            source               = 'CAS',
            last_updated         = %s,
            first_invested_date  = COALESCE(first_invested_date, %s)
        WHERE entity_id    = %s
        AND   security_id  = %s
        AND   folio_number = %s
        AND   account_id IS NULL
    """, (
        float(units),
        float(cost_basis) if cost_basis else None,
        float(avg_cost)   if avg_cost   else None,
        float(cost_basis) if cost_basis else None,
        float(nav)        if nav        else None,
        now_utc(),
        first_date,
        entity_id, security_id, folio_number.strip()
    ))

    if cur.rowcount == 0:
        cur.execute("""
            INSERT INTO holding (
                entity_id, security_id, folio_number, quantity,
                cost_basis, avg_cost, invested_amount,
                first_invested_date, last_updated_nav,
                currency, source, last_updated
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'INR', 'CAS', %s)
        """, (
            entity_id, security_id, folio_number.strip(),
            float(units),
            float(cost_basis) if cost_basis else None,
            float(avg_cost)   if avg_cost   else None,
            float(cost_basis) if cost_basis else None,
            first_date,
            float(nav)        if nav        else None,
            now_utc()
        ))


def insert_transaction(cur, entity_id, security_id, folio, txn):
    try:
        units    = float(txn.units)   if txn.units   else None
        amount   = float(txn.amount)  if txn.amount  else None
        nav      = float(txn.nav)     if txn.nav     else None
        bal      = float(txn.balance) if txn.balance else None
        txn_type = str(txn.type.value) if txn.type   else "UNKNOWN"

        cur.execute("""
            INSERT INTO mf_transaction (
                entity_id, security_id, folio_number,
                transaction_date, description,
                amount, units, nav, balance_units,
                transaction_type, source
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'CAS')
            ON CONFLICT DO NOTHING
        """, (
            entity_id, security_id, folio.strip(),
            txn.date, txn.description,
            amount, units, nav, bal, txn_type
        ))
        return True
    except Exception as e:
        logger.warning(f"Txn skipped: {e}")
        return False


def log_run(conn, status, processed, failed, started, error=None):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ingestion_run
            (run_type, run_date, status, records_processed,
             records_failed, error_message, started_at, completed_at)
        VALUES ('cas_parser', %s, %s, %s, %s, %s, %s, %s)
    """, (date.today(), status, processed, failed,
          error, started, now_utc()))
    cur.close()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(pdf_path: str, password: str, entity_id_override: int | None = None):
    started   = now_utc()
    processed = 0
    failed    = 0
    txn_count = 0
    conn      = None

    logger.info("=== CAS Parser starting ===")
    logger.info(f"PDF: {pdf_path}")
    if entity_id_override:
        logger.info(f"Entity override: {entity_id_override} (auto-detection disabled)")

    try:
        # Step 1: build folio→holder map from raw PDF text
        folio_holders = extract_folio_holders(pdf_path, password)

        # Step 2: parse PDF via casparser
        logger.info("Parsing CAS PDF via casparser...")
        data = casparser.read_cas_pdf(pdf_path, password=password)
        logger.info(f"Investor: {data.investor_info.name}")
        logger.info(f"Email:    {data.investor_info.email}")
        logger.info(f"Folios:   {len(data.folios)}")

        conn       = get_db()
        cur        = conn.cursor()
        entity_map = load_entity_map(conn)
        logger.info(f"Entity map: {entity_map}")

        for folio in data.folios:
            folio_num = folio.folio.strip()

            # Resolve entity_id
            if entity_id_override:
                eid = entity_id_override
                holder_label = "override"
            else:
                holder_name = folio_holders.get(folio_num, "")
                if not holder_name:
                    logger.warning(f"Holder not found for folio {folio_num}, skipping")
                    failed += 1
                    continue
                eid = holder_to_entity_id(holder_name, entity_map)
                if not eid:
                    logger.warning(
                        f"Cannot map '{holder_name}' to an entity — skipping folio {folio_num}"
                    )
                    failed += 1
                    continue
                holder_label = holder_name

            for scheme in folio.schemes:
                try:
                    isin = scheme.isin
                    if not isin:
                        logger.warning(f"No ISIN: {scheme.scheme[:50]}")
                        continue

                    sec_type, asset_class = map_type(scheme.type)
                    amfi_code = str(scheme.amfi) if scheme.amfi else None

                    security_id, saved_amfi = upsert_security(
                        conn, isin, scheme.scheme,
                        sec_type, asset_class, amfi_code
                    )
                    conn.commit()

                    logger.info(
                        f"[entity={eid}/{holder_label[:20]}] "
                        f"{scheme.scheme[:40]} | "
                        f"ISIN: {isin} | AMFI: {saved_amfi or 'N/A'}"
                    )

                    upsert_folio(cur, folio_num, eid, scheme.scheme)

                    units    = scheme.close or Decimal("0")
                    cost     = scheme.valuation.cost if scheme.valuation else Decimal("0")
                    nav      = scheme.valuation.nav  if scheme.valuation else None
                    avg_cost = (cost / units) if units > 0 else None

                    purchases = [
                        t for t in scheme.transactions
                        if t.type and "PURCHASE" in str(t.type.value).upper() and t.date
                    ]
                    first_date = min(t.date for t in purchases) if purchases else None

                    upsert_holding(
                        cur, eid, security_id, folio_num,
                        units, cost, avg_cost, first_date, nav
                    )

                    for txn in scheme.transactions:
                        if insert_transaction(cur, eid, security_id, folio_num, txn):
                            txn_count += 1

                    logger.info(
                        f"  ✅ Units: {float(units):.3f} | "
                        f"Cost: ₹{float(cost):,.0f} | "
                        f"Txns: {len(scheme.transactions)}"
                    )
                    processed += 1

                except Exception as e:
                    logger.error(f"Error on {scheme.scheme[:40]}: {e}")
                    failed += 1

        log_run(conn, "success", processed, failed, started)
        conn.commit()
        cur.close()

        logger.info("=== CAS Parser complete ===")
        logger.info(f"Securities: {processed} | Txns: {txn_count} | Failed: {failed}")

    except Exception as e:
        logger.error(f"CAS Parser FAILED: {e}")
        if conn:
            try:
                log_run(conn, "failed", processed, failed, started, str(e))
                conn.commit()
            except Exception:
                pass
        sys.exit(1)

    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="IWS CAS Parser")
    p.add_argument("--pdf",       required=True, help="Path to CAS PDF")
    p.add_argument("--password",  required=True, help="PDF password")
    p.add_argument("--entity-id", type=int,      help="Force all folios to this entity_id (overrides auto-detection)")
    args = p.parse_args()
    run(args.pdf, args.password, args.entity_id)
