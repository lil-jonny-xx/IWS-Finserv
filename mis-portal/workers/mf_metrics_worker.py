#!/usr/bin/env python3
"""
MF Metrics Worker — IWS MIS Portal

Runs after amfi_nav_worker has refreshed NAVs. Reads the latest NAV already
stored on each holding row, fetches historical NAV anchors from nav_history,
and writes the full set of metric columns back to holding.

Metrics computed:
  market_value_as_on      = quantity × last_updated_nav  (= current_value)
  as_of_date              = nav_date of the latest NAV in nav_history
  prev_week_value         = quantity × closest NAV on or before the last completed
                            Friday (week-to-date change, same anchor as equity)
  weekly_change           = market_value_as_on - prev_week_value
  exposure_pct            = market_value_as_on / entity_total_mf_value × 100
  pnl_inception           = market_value_as_on - cost_basis
  pnl_ytd                 = per FIFO unit-lot: units bought during the FY are measured
                            from their purchase NAV, units held at FY start from the
                            Apr-1 NAV. (The old whole-position formula credited units
                            bought mid-year with gains from Apr 1 they never earned.)
                            Falls back to the whole-position formula only when the
                            transaction ledger doesn't reconcile.
  pnl_weekly_change       = pnl_inception - (prev_week_value - cost_basis)
  returns_inception_pct   = pnl_inception / cost_basis × 100
  returns_ytd_pct         = pnl_ytd / ytd_capital_base × 100
  cagr_inception_pct      = (market_value_as_on/cost_basis)^(1/years) − 1
  xirr_inception_pct      = money-weighted from mf_transaction flows (equity.finmath)

CAGR and XIRR are annualised figures: both are suppressed (NULL) until the holding
is ≥1 year old, via the same ann_guard used by the equity metrics worker.

Schedule: Daily at 10:15 PM IST (16:45 UTC) — runs after amfi_nav_worker
Cron:     45 16 * * * /var/www/.venv/bin/python /var/www/mis-portal/workers/mf_metrics_worker.py >> /var/log/mis-portal-mf-metrics.log 2>&1
"""
import os
import sys
import logging
import psycopg2
import psycopg2.extras
from collections import deque
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

sys.path.insert(0, "/var/www/mis-portal")
from equity.finmath import xirr as solve_xirr            # noqa: E402
from workers.equity_txn_metrics_worker import ann_guard  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # File persistence handled by cron_wrapper stdout -> crontab log redirect
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


def xirr(cash_flows: list[tuple], days_held: int | None = None) -> float | None:
    """Gated XIRR in percent. cash_flows: [(date, amount)], purchases negative,
    receipts positive.

    Solving is delegated to equity.finmath.xirr (Newton + residual check +
    bisection fallback — the old local Newton loop could return a non-converged
    garbage rate). The result is gated by the shared ann_guard: annualised
    figures are suppressed until the holding is ≥1 year old and must fall in a
    plausible band."""
    if len(cash_flows) < 2:
        return None
    if days_held is None:
        days_held = (max(d for d, _ in cash_flows) - min(d for d, _ in cash_flows)).days
    rate = solve_xirr(cash_flows)
    return ann_guard(rate * 100 if rate is not None else None, days_held)


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

def load_transactions(conn) -> tuple[dict, dict]:
    """
    Batch-load all MF transactions.
    Returns two dicts keyed by (entity_id, security_id, folio_number):
      flows — [(date, signed cash flow)] for XIRR. Purchases negative (money out),
              redemptions/payouts positive (money in).
      lots  — [(date, units, unit_price)] of the CURRENT position via FIFO
              (redemptions consume the oldest purchases), for lot-based pnl_ytd.

    Sign rules: rows with units use the units' sign (buy = outflow). Zero-unit
    rows are classified by transaction_type — taxes/charges are investor costs
    (outflow), dividend/IDCW payouts are receipts (inflow); unknown zero-unit
    types are excluded from XIRR (logged) rather than guessed. The old rule
    (`units >= 0` → outflow) silently treated any zero-unit payout as a purchase.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT entity_id, security_id, folio_number,
               transaction_date, transaction_type, amount, units
        FROM   mf_transaction
        WHERE  amount IS NOT NULL
        ORDER  BY entity_id, security_id, folio_number, transaction_date
    """)
    rows = cur.fetchall()
    cur.close()
    flows: dict = {}
    raw:   dict = {}
    unknown_types: set = set()
    for r in rows:
        key = (r["entity_id"], r["security_id"], r["folio_number"])
        amt   = float(r["amount"])
        units = float(r["units"]) if r["units"] is not None else 0.0
        ttype = (r["transaction_type"] or "").upper()
        if abs(units) > 1e-9:
            cf = -abs(amt) if units > 0 else abs(amt)
        elif "TAX" in ttype or "CHARGE" in ttype or "FEE" in ttype:
            cf = -abs(amt)                        # stamp duty / STT / TDS: investor cost
        elif "DIVIDEND" in ttype or "IDCW" in ttype or "PAYOUT" in ttype or "INTEREST" in ttype:
            cf = abs(amt)                         # payout received
        else:
            cf = None                             # unknown zero-unit row: keep out of XIRR
            unknown_types.add(ttype)
        if cf is not None:
            flows.setdefault(key, []).append((r["transaction_date"], cf))
        raw.setdefault(key, []).append((r["transaction_date"], units, amt))
    if unknown_types:
        logger.warning(f"Zero-unit transaction types excluded from XIRR flows: {sorted(unknown_types)}")

    lots = {key: _fifo_unit_lots(txns) for key, txns in raw.items()}
    return flows, lots


def _fifo_unit_lots(txns: list[tuple]) -> list[tuple]:
    """FIFO unit lots of the current position: [(buy_date, units, unit_price)].
    Redemptions consume the oldest purchases; what remains are the held units with
    their real purchase dates and NAVs (amount/units — CAS redemption amounts are
    negative, hence abs)."""
    dq = deque()
    for d, units, amt in txns:
        if units > 1e-9:
            dq.append([d, units, abs(amt) / units])
        elif units < -1e-9:
            s = -units
            while s > 1e-9 and dq:
                if dq[0][1] <= s + 1e-9:
                    s -= dq[0][1]
                    dq.popleft()
                else:
                    dq[0][1] -= s
                    s = 0
    return [(d, u, p) for d, u, p in dq if u > 1e-9]


def load_unit_balances(conn) -> dict:
    """
    Signed net units per (entity_id, security_id, folio_number) for the CURRENT LOT.
    Used to detect a holding whose transaction history does not reconcile with
    its stored quantity (e.g. a corrupted/incomplete CAS parse). Such a ledger
    yields garbage cost/P&L/XIRR, so the worker suppresses those metrics rather
    than publishing nonsense.

    "Current lot" = transactions after the folio's last full exit. The parser writes
    balance_units = NULL on a transaction that takes the position to zero, so that
    row marks the boundary. Summing across the *whole* history instead would drag in
    pre-exit lots, and any gap in that older history — common, since a CAS only
    covers the period it was generated for — makes the sum diverge from a quantity
    that is actually correct.

    A liquid-fund folio with several full exits and an incomplete early history was
    misreported as corrupt for months on exactly this: the all-time sum went sharply
    negative while the current lot reconciled to the stored quantity on the nose, and
    its CAGR/XIRR were suppressed the whole time. Same current-lot rule already
    governs first_invested_date.
    """
    cur = conn.cursor()
    # Ordered by (transaction_date, id) rather than date alone: an exit and a
    # re-entry can share a date (switch-out/switch-in), and a date-only comparison
    # would discard the re-entry. Every folio emits a row — a folio sitting fully
    # exited nets 0, which correctly reconciles against a zero quantity instead of
    # dropping out of the map and reading as unreconcilable.
    cur.execute("""
        WITH seq AS (
            SELECT entity_id, security_id, folio_number, units, balance_units,
                   ROW_NUMBER() OVER (
                       PARTITION BY entity_id, security_id, folio_number
                       ORDER BY transaction_date, id
                   ) AS rn
            FROM   mf_transaction
        ),
        exits AS (
            -- Last row that zeroed the folio: units present, running balance NULL.
            SELECT entity_id, security_id, folio_number, MAX(rn) AS exit_rn
            FROM   seq
            WHERE  units IS NOT NULL AND balance_units IS NULL
            GROUP  BY entity_id, security_id, folio_number
        )
        SELECT s.entity_id, s.security_id, s.folio_number,
               COALESCE(SUM(s.units) FILTER (WHERE s.rn > COALESCE(x.exit_rn, 0)), 0)
                   AS net_units
        FROM   seq s
        LEFT   JOIN exits x
               ON  x.entity_id   = s.entity_id
               AND x.security_id = s.security_id
               AND x.folio_number IS NOT DISTINCT FROM s.folio_number
        GROUP  BY s.entity_id, s.security_id, s.folio_number
    """)
    rows = cur.fetchall()
    cur.close()
    return {(r["entity_id"], r["security_id"], r["folio_number"]): Decimal(str(r["net_units"]))
            for r in rows}


def ledger_reconciles(quantity: Decimal, net_units: Decimal | None) -> bool:
    """True if the ledger's signed unit sum matches the holding quantity.

    Tolerance: 1 unit or 0.5% of quantity, whichever is larger, to absorb
    rounding across many transactions without masking real corruption.
    """
    if net_units is None:
        return False
    tol = max(Decimal("1"), abs(quantity) * Decimal("0.005"))
    return abs(quantity - net_units) <= tol


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
    ledger_ok: bool = True,
    unit_lots: list | None = None,
    fy_anchor: date | None = None,
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

    # P&L & returns come from cost_basis — the registrar/CAMS per-folio figure,
    # which is reliable even when the transaction ledger is incomplete. So these
    # are always computed when a cost basis is present.
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

        # Time-weighted metrics (CAGR, XIRR) need the full dated cash-flow
        # history. Suppress them when the ledger doesn't reconcile, since a
        # partial flow series yields nonsense (e.g. a liquid fund showing 49%
        # XIRR). Absolute P&L/return above stays, as it only needs cost + value.
        if ledger_ok:
            # CAGR inception — annualised, so only once held ≥1 year (same rule as
            # equity; the old 0.08-year floor annualised 1-month returns into
            # wildly misleading figures).
            fid = h.get("first_invested_date")
            if fid:
                years = (today - fid).days / 365.25
                if years >= 1.0 and float(cost) > 0:
                    ratio = float(cur_val / cost)
                    if ratio > 0:
                        out["cagr_inception_pct"] = ann_guard(
                            (ratio ** (1.0 / years) - 1.0) * 100, (today - fid).days
                        )

            # XIRR inception — actual per-transaction cash flows; gated ≥1 year
            # inside xirr() via ann_guard. Clip to the current lot (flows on/after
            # first_invested_date): a folio fully redeemed and re-bought otherwise
            # dragged in the closed lot's buy/redeem pair and dated XIRR from the
            # old entry, matching the corrupted first_invested_date it came from.
            if cash_flows:
                lot_flows = [(d, c) for d, c in cash_flows if fid is None or d >= fid]
                if lot_flows:
                    days_held = (today - min(d for d, _ in lot_flows)).days
                    flows = list(lot_flows) + [(today, float(cur_val))]
                    out["xirr_inception_pct"] = xirr(flows, days_held)

    # P&L YTD — per FIFO unit-lot when the ledger reconciles: units bought during
    # the FY are measured from their purchase NAV, units held at FY start from the
    # FY-start NAV. The whole-position formula (current qty × FY-start NAV) credited
    # mid-year purchases with gains from Apr 1 they never earned.
    nav_f = float(nav)
    ytd_done = False
    if ledger_ok and unit_lots and fy_anchor:
        pnl = base = 0.0
        computable = True
        for d, u, p in unit_lots:
            if d >= fy_anchor:
                ref = p
            elif fy_start_nav is not None:
                ref = fy_start_nav
            else:
                computable = False          # pre-FY units but no FY-start NAV
                break
            pnl  += u * (nav_f - ref)
            base += u * ref
        if computable and base > 0:
            out["pnl_ytd"]         = round(pnl, 2)
            out["returns_ytd_pct"] = round(pnl / base * 100, 4)
            ytd_done = True
    if not ytd_done and fy_start_nav is not None:
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
        all_txn_flows, all_unit_lots = load_transactions(conn)
        unit_balances = load_unit_balances(conn)

        if not holdings:
            logger.info("No holdings to process.")
            log_run(conn, "success", 0, 0, started)
            conn.commit()
            return

        logger.info(f"Loaded {len(holdings)} holding rows")

        # Unique security IDs for batch NAV lookups
        sec_ids = list({h["security_id"] for h in holdings})

        # NAVs are DAILY (AMFI), so the weekly anchor is the last completed Friday —
        # week-to-date change, the same anchor the equity side uses. (The old code
        # assumed weekly NAVs and anchored to the Friday before that, making
        # "weekly change" span ~10-12 days.)
        anchor_friday   = last_completed_friday(today)
        ytd_anchor      = fy_start(today)

        logger.info(f"Anchors — weekly: {anchor_friday} | FY start: {ytd_anchor}")

        prev_week_navs = batch_nav_on_or_before(conn, sec_ids, anchor_friday)
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
                lots    = all_unit_lots.get(txn_key)

                ledger_ok = ledger_reconciles(
                    Decimal(str(h["quantity"])), unit_balances.get(txn_key)
                )
                if not ledger_ok:
                    logger.warning(
                        f"Ledger mismatch for holding id={h['id']} ({h['security_name']}): "
                        f"qty={h['quantity']} vs net_units={unit_balances.get(txn_key)} "
                        f"— suppressing time-weighted metrics (CAGR/XIRR); P&L kept from cost basis"
                    )

                m = compute(h, e_total, pw_nav, fy_start_nav, aod, today, flows,
                            ledger_ok, lots, ytd_anchor)
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
