#!/usr/bin/env python3
"""
MF Metrics Worker — IWS MIS Portal

Runs after amfi_nav_worker has refreshed NAVs. Reads the latest NAV already
stored on each holding row, fetches historical NAV anchors from nav_history,
and writes the full set of metric columns back to holding.

Metrics computed:
  market_value_as_on      = quantity × last_updated_nav  (= current_value)
  as_of_date              = nav_date of the latest NAV in nav_history
  prev_week_value         = quantity × closest NAV on or before last Friday
  weekly_change           = market_value_as_on - prev_week_value
  exposure_pct            = market_value_as_on / entity_total_mf_value × 100
  pnl_inception           = market_value_as_on - cost_basis
  pnl_ytd                 = market_value_as_on - (quantity × closest NAV ≤ Apr 1, fiscal year start)
  pnl_weekly_change       = pnl_inception - (prev_week_value - cost_basis)
  returns_inception_pct   = pnl_inception / cost_basis × 100
  returns_ytd_pct         = pnl_ytd / (quantity × fy_start_nav) × 100
  cagr_inception_pct      = (market_value_as_on/cost_basis)^(1/years) − 1

Schedule: Daily at 10:15 PM IST (16:45 UTC) — runs after amfi_nav_worker
Cron:     45 16 * * * /var/www/.venv/bin/python /var/www/mis-portal/workers/mf_metrics_worker.py >> /var/log/mis-portal-mf-metrics.log 2>&1
"""
import os
import sys
import logging
import psycopg2
import psycopg2.extras
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/var/log/mis-portal-mf-metrics.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

TWO  = Decimal("0.01")
FOUR = Decimal("0.0001")


def xirr(cash_flows: list[tuple]) -> float | None:
    """Newton-Raphson XIRR. cash_flows: [(date, amount)] where purchases negative, receipts positive."""
    if len(cash_flows) < 2:
        return None
    dates   = [cf[0] for cf in cash_flows]
    amounts = [cf[1] for cf in cash_flows]
    t0      = min(dates)
    days    = [(d - t0).days for d in dates]

    def npv(r):
        return sum(a / (1 + r) ** (d / 365.25) for a, d in zip(amounts, days))

    def dnpv(r):
        return sum(-a * (d / 365.25) / (1 + r) ** (d / 365.25 + 1) for a, d in zip(amounts, days))

    rate = 0.1
    for _ in range(200):
        f  = npv(rate)
        df = dnpv(rate)
        if abs(df) < 1e-12:
            break
        new_rate = rate - f / df
        if new_rate <= -1:
            new_rate = -0.9999
        if abs(new_rate - rate) < 1e-8:
            rate = new_rate
            break
        rate = new_rate

    return round(rate * 100, 4) if -99 < rate < 100 else None


def now_utc():
    return datetime.now(timezone.utc)


def get_db():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


# ---------------------------------------------------------------------------
# Date anchors
# ---------------------------------------------------------------------------

def last_completed_friday(today: date) -> date:
    """Most recent Friday strictly before today — the last completed trading week."""
    days_back = ((today.weekday() - 4) % 7) or 7
    return today - timedelta(days=days_back)


def fy_start(today: date) -> date:
    """April 1 of the current Indian fiscal year (Apr–Mar)."""
    if today.month >= 4:
        return date(today.year, 4, 1)
    return date(today.year - 1, 4, 1)


# ---------------------------------------------------------------------------
# Batch NAV lookups
# ---------------------------------------------------------------------------

def batch_nav_on_or_before(conn, security_ids: list[int], anchor: date) -> dict[int, tuple[float, date]]:
    """
    For each security_id, return (nav, nav_date) of the most recent NAV
    on or before `anchor`.  Returns {} for securities with no history that old.
    """
    if not security_ids:
        return {}
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT ON (security_id)
            security_id, nav, nav_date
        FROM   nav_history
        WHERE  security_id = ANY(%s)
          AND  nav_date   <= %s
        ORDER  BY security_id, nav_date DESC
        """,
        (security_ids, anchor),
    )
    rows = cur.fetchall()
    cur.close()
    return {r["security_id"]: (float(r["nav"]), r["nav_date"]) for r in rows}


def batch_latest_nav_date(conn, security_ids: list[int]) -> dict[int, date]:
    """Latest nav_date per security_id — used to populate as_of_date."""
    if not security_ids:
        return {}
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT ON (security_id) security_id, nav_date
        FROM   nav_history
        WHERE  security_id = ANY(%s)
        ORDER  BY security_id, nav_date DESC
        """,
        (security_ids,),
    )
    rows = cur.fetchall()
    cur.close()
    return {r["security_id"]: r["nav_date"] for r in rows}


# ---------------------------------------------------------------------------
# Load holdings
# ---------------------------------------------------------------------------

def load_transactions(conn) -> dict:
    """
    Batch-load all MF transactions.
    Returns dict keyed by (entity_id, security_id, folio_number) → list of (date, amount).
    Purchases: negative (money out). Redemptions/payouts: positive (money in).
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT entity_id, security_id, folio_number,
               transaction_date, amount, units
        FROM   mf_transaction
        WHERE  amount IS NOT NULL
        ORDER  BY entity_id, security_id, folio_number, transaction_date
    """)
    rows = cur.fetchall()
    cur.close()
    result: dict = {}
    for r in rows:
        key = (r["entity_id"], r["security_id"], r["folio_number"])
        if key not in result:
            result[key] = []
        amt   = float(r["amount"])
        units = float(r["units"]) if r["units"] is not None else 0.0
        # units > 0 → purchase (money leaves investor) → negative cash flow
        cf = -abs(amt) if units >= 0 else abs(amt)
        result[key].append((r["transaction_date"], cf))
    return result


def load_holdings(conn) -> list[dict]:
    """
    All holding rows that have a NAV and positive quantity.
    One row per (entity_id, security_id, folio_number).
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            h.id,
            h.entity_id,
            h.security_id,
            h.folio_number,
            h.quantity,
            h.cost_basis,
            h.avg_cost,
            h.first_invested_date,
            h.last_updated_nav,
            h.current_value,
            sm.security_name
        FROM   holding h
        JOIN   security_master sm ON sm.id = h.security_id
        WHERE  h.quantity         > 0
          AND  h.last_updated_nav IS NOT NULL
        ORDER  BY h.entity_id, h.security_id, h.folio_number
        """
    )
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def compute(
    h: dict,
    entity_total: Decimal,
    prev_week_nav: float | None,
    fy_start_nav: float | None,
    as_of_date: date | None,
    today: date,
    cash_flows: list | None = None,
) -> dict:
    qty       = Decimal(str(h["quantity"]))
    cost      = Decimal(str(h["cost_basis"])) if h["cost_basis"] else None
    nav       = Decimal(str(h["last_updated_nav"]))
    cur_val   = (qty * nav).quantize(TWO)

    out = {
        "id":              h["id"],
        "market_value_as_on": float(cur_val),
        "as_of_date":      as_of_date or today,
        "prev_week_value": None,
        "weekly_change":   None,
        "exposure_pct":    None,
        "pnl_inception":   None,
        "pnl_ytd":         None,
        "pnl_weekly_change": None,
        "returns_inception_pct": None,
        "returns_ytd_pct": None,
        "cagr_inception_pct":  None,
        "xirr_inception_pct":  None,
    }

    # Exposure
    if entity_total > 0:
        out["exposure_pct"] = float(
            (cur_val / entity_total * 100).quantize(FOUR, ROUND_HALF_UP)
        )

    # Previous week
    if prev_week_nav is not None:
        pw = (qty * Decimal(str(prev_week_nav))).quantize(TWO)
        out["prev_week_value"] = float(pw)
        out["weekly_change"]   = float((cur_val - pw).quantize(TWO))

    # P&L inception
    if cost and cost > 0:
        pnl_inc = (cur_val - cost).quantize(TWO)
        out["pnl_inception"] = float(pnl_inc)

        out["returns_inception_pct"] = float(
            (pnl_inc / cost * 100).quantize(FOUR, ROUND_HALF_UP)
        )

        # P&L weekly change
        if prev_week_nav is not None:
            pw = Decimal(str(out["prev_week_value"]))
            prev_pnl = (pw - cost).quantize(TWO)
            out["pnl_weekly_change"] = float((pnl_inc - prev_pnl).quantize(TWO))

        # CAGR inception
        fid = h.get("first_invested_date")
        if fid:
            years = (today - fid).days / 365.25
            if years >= 0.08 and float(cost) > 0:
                ratio = float(cur_val / cost)
                if ratio > 0:
                    out["cagr_inception_pct"] = round(
                        (ratio ** (1.0 / years) - 1.0) * 100, 4
                    )

        # XIRR inception — uses actual per-transaction cash flows
        if cash_flows:
            flows = list(cash_flows) + [(today, float(cur_val))]
            out["xirr_inception_pct"] = xirr(flows)

    # P&L YTD
    if fy_start_nav is not None:
        ytd_val = (qty * Decimal(str(fy_start_nav))).quantize(TWO)
        if ytd_val > 0:
            pnl_ytd = (cur_val - ytd_val).quantize(TWO)
            out["pnl_ytd"]        = float(pnl_ytd)
            out["returns_ytd_pct"] = float(
                (pnl_ytd / ytd_val * 100).quantize(FOUR, ROUND_HALF_UP)
            )

    return out


# ---------------------------------------------------------------------------
# Bulk update
# ---------------------------------------------------------------------------

def bulk_update(conn, metrics: list[dict]):
    cur = conn.cursor()
    psycopg2.extras.execute_batch(
        cur,
        """
        UPDATE holding SET
            market_value_as_on    = %(market_value_as_on)s,
            as_of_date            = %(as_of_date)s,
            prev_week_value       = %(prev_week_value)s,
            weekly_change         = %(weekly_change)s,
            exposure_pct          = %(exposure_pct)s,
            pnl_inception         = %(pnl_inception)s,
            pnl_ytd               = %(pnl_ytd)s,
            pnl_weekly_change     = %(pnl_weekly_change)s,
            returns_inception_pct = %(returns_inception_pct)s,
            returns_ytd_pct       = %(returns_ytd_pct)s,
            cagr_inception_pct    = %(cagr_inception_pct)s,
            xirr_inception_pct    = %(xirr_inception_pct)s,
            last_updated          = NOW()
        WHERE id = %(id)s
        """,
        metrics,
        page_size=200,
    )
    cur.close()


# ---------------------------------------------------------------------------
# Ingestion log
# ---------------------------------------------------------------------------

def log_run(conn, status, processed, failed, started, error=None):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ingestion_run
            (run_type, run_date, status, records_processed,
             records_failed, error_message, started_at, completed_at)
        VALUES ('mf_metrics', %s, %s, %s, %s, %s, %s, %s)
        """,
        (date.today(), status, processed, failed, error, started, now_utc()),
    )
    cur.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    started = now_utc()
    today   = date.today()
    logger.info(f"=== MF Metrics Worker starting for {today} ===")

    conn      = None
    processed = 0
    failed    = 0

    try:
        conn         = get_db()
        holdings     = load_holdings(conn)
        all_txn_flows = load_transactions(conn)

        if not holdings:
            logger.info("No holdings to process.")
            log_run(conn, "success", 0, 0, started)
            conn.commit()
            return

        logger.info(f"Loaded {len(holdings)} holding rows")

        # Unique security IDs for batch NAV lookups
        sec_ids = list({h["security_id"] for h in holdings})

        # The most recent NAV is typically from last Friday.
        # prev_week_value must use the Friday BEFORE that so the change is non-zero.
        current_friday  = last_completed_friday(today)
        prev_friday     = current_friday - timedelta(days=7)
        ytd_anchor      = fy_start(today)

        logger.info(f"Anchors — current Friday: {current_friday} | prev Friday: {prev_friday} | FY start: {ytd_anchor}")

        prev_week_navs = batch_nav_on_or_before(conn, sec_ids, prev_friday)
        fy_start_navs      = batch_nav_on_or_before(conn, sec_ids, ytd_anchor)
        as_of_dates    = batch_latest_nav_date(conn, sec_ids)

        # Per-entity total MF value for exposure_pct
        entity_totals: dict[int, Decimal] = {}
        for h in holdings:
            qty = Decimal(str(h["quantity"]))
            nav = Decimal(str(h["last_updated_nav"]))
            eid = h["entity_id"]
            entity_totals[eid] = entity_totals.get(eid, Decimal("0")) + (qty * nav)

        # Compute metrics for every row
        metrics_batch = []
        for h in holdings:
            try:
                sid = h["security_id"]
                pw_nav  = prev_week_navs.get(sid, (None, None))[0]
                fy_start_nav = fy_start_navs.get(sid, (None, None))[0]
                aod     = as_of_dates.get(sid)
                e_total = entity_totals.get(h["entity_id"], Decimal("0"))

                txn_key = (h["entity_id"], h["security_id"], h["folio_number"])
                flows   = all_txn_flows.get(txn_key)
                m = compute(h, e_total, pw_nav, fy_start_nav, aod, today, flows)
                metrics_batch.append(m)
                processed += 1
            except Exception as e:
                logger.error(f"Compute failed for holding id={h['id']} ({h['security_name']}): {e}")
                failed += 1

        # Write all metrics in one batch
        bulk_update(conn, metrics_batch)

        log_run(conn, "success", processed, failed, started)
        conn.commit()

        logger.info(f"=== Done: {processed} updated | {failed} failed ===")

    except Exception as e:
        logger.error(f"MF Metrics Worker FAILED: {e}")
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
    run()
