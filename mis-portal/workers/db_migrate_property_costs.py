#!/usr/bin/env python3
"""
DB migration — property purchase/sale cost fields + a second valuation.

Adds the transaction costs the Properties page now captures on either side of
the deal, plus a second independent valuation (amount + its own report upload):

  purchase_brokerage   — brokerage paid when the property was bought
  sale_lawyer_fees     — lawyer/conveyancing fees paid on sale
  sale_brokerage       — brokerage paid on sale
  valuation_1_amount   — first valuer's figure (report = doc slug 'valuation_report')
  valuation_2_amount   — second valuer's figure (report = doc slug 'valuation_report_2')

Capital gain is DERIVED, never stored: sale_price − purchase_price − sale costs
(lawyer + brokerage). See _serialize_property in main.py.

Idempotent: every column is ADD COLUMN IF NOT EXISTS, so re-running is a no-op.

Run once:  python -m workers.db_migrate_property_costs
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
ALTER TABLE property ADD COLUMN IF NOT EXISTS purchase_brokerage NUMERIC(16, 2);
ALTER TABLE property ADD COLUMN IF NOT EXISTS sale_lawyer_fees   NUMERIC(16, 2);
ALTER TABLE property ADD COLUMN IF NOT EXISTS sale_brokerage     NUMERIC(16, 2);
ALTER TABLE property ADD COLUMN IF NOT EXISTS valuation_1_amount NUMERIC(16, 2);
ALTER TABLE property ADD COLUMN IF NOT EXISTS valuation_2_amount NUMERIC(16, 2);
"""


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        print("property purchase/sale cost + valuation columns ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
