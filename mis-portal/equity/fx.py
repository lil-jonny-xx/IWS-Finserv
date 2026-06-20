"""
FX rate lookup for multi-currency equity holdings.

Reads the `fx_rate` table that workers/fx_rates_worker.py populates daily
(rows are `from_currency` → INR, e.g. USD→INR, SGD→INR). Returns INR per 1 unit
of the requested currency, using the most recent rate at or before `on_date`
so a weekend / holiday sync still finds Friday's rate.
"""
import logging
from datetime import date
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


def get_rate(conn, currency: str, on_date: date) -> Optional[Decimal]:
    """
    INR per 1 unit of `currency` at or before `on_date`.

    Returns Decimal('1') for INR. Returns None if no rate is on record (caller
    decides how to handle — typically skip the holding rather than store a wrong
    INR value).
    """
    cur = currency.upper()
    if cur == "INR":
        return Decimal("1")

    c = conn.cursor()
    c.execute(
        """
        SELECT rate
        FROM   fx_rate
        WHERE  from_currency = %s
          AND  to_currency   = 'INR'
          AND  rate_date     <= %s
        ORDER BY rate_date DESC
        LIMIT 1
        """,
        (cur, on_date),
    )
    row = c.fetchone()
    c.close()
    if not row:
        logger.warning(f"No fx_rate for {cur}→INR on/before {on_date}")
        return None
    # RealDictCursor → dict, default cursor → tuple. Support both.
    rate = row["rate"] if isinstance(row, dict) else row[0]
    return Decimal(str(rate))
