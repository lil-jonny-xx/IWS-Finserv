#!/usr/bin/env python3
"""
AMFI NAV Worker — IWS MIS Portal
Fetches daily NAV for tracked MFs using amfi_code.
Auto-resolves missing amfi_codes via isin_resolver.

Two modes:
  (default)   full sweep of every tracked fund — cron 02:30 UTC (08:00 IST)
  --catchup   only funds behind the newest NAV date — cron hourly

Slow AMCs publish to mfapi.in well after the 08:00 IST sweep, which left those
holdings showing the previous business day's NAV until the next morning. Catch-up
mode chases just that gap and no-ops once every fund is level.
"""
import os
import sys
import logging
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timezone
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from isin_resolver import resolve_all_missing

load_dotenv('/var/www/mis-portal/.env', override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        # File persistence handled by cron_wrapper stdout -> crontab log redirect
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

MFAPI_BASE = "https://api.mfapi.in/mf"

# Session-level advisory-lock key guarding the whole NAV refresh. Arbitrary but
# fixed — 'AMFINAV' as ASCII hex. Any other worker taking a pg advisory lock must
# not reuse this value.
_ADVISORY_LOCK_KEY = 0x414D46494E4156


def now_utc():
    return datetime.now(timezone.utc)


def get_db():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def fetch_nav(amfi_code: str) -> dict:
    """Fetch latest NAV from mfapi.in with 3 retries and exponential backoff."""
    import time
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(f"{MFAPI_BASE}/{amfi_code}", timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "SUCCESS":
                return None
            nav_data = data.get("data", [])
            if not nav_data:
                return None
            latest = nav_data[0]
            return {
                "nav":  float(latest["nav"]),
                "date": datetime.strptime(latest["date"], "%d-%m-%Y").date(),
                "name": data.get("meta", {}).get("scheme_name", ""),
            }
        except Exception as e:
            last_exc = e
            if attempt < 2:
                logger.warning(f"mfapi.in attempt {attempt + 1} failed for {amfi_code}: {e} — retrying")
                time.sleep(5 * (attempt + 1))
    raise last_exc


def check_unresolved_held_mfs(conn) -> list:
    """Guard: flag held MF schemes that still lack an amfi_code after auto-resolution.

    A row in `holding` with no amfi_code on its security can never get a NAV, so it
    silently shows stale/zero value — exactly the gap this guard surfaces. Emails an
    alert listing the offenders. Never raises; returns the offending rows (possibly []).

    Only live (quantity > 0) holdings count. Fully-exited schemes carry a zero-quantity
    holding row from the full-history CAS import and contribute nothing to value, so a
    missing amfi_code on one is correct-by-design, not a gap — see resolve_all_missing().
    Without this the guard emailed the same 7 dead HDR schemes on every daily run,
    which is exactly how a real alert gets ignored.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT sm.id, sm.security_name, sm.isin
        FROM   holding h
        JOIN   security_master sm ON sm.id = h.security_id
        WHERE  sm.amfi_code IS NULL
          AND  COALESCE(sm.security_type, '') <> 'EQUITY'
          AND  h.quantity > 0
        ORDER  BY sm.security_name
    """)
    rows = cursor.fetchall()
    cursor.close()

    if not rows:
        logger.info("Guard OK: every held MF scheme has an amfi_code.")
        return rows

    listing = "\n".join(
        f"  - {r['security_name']} (isin={r['isin'] or 'NULL'}, security_id={r['id']})"
        for r in rows
    )
    body = (
        f"{len(rows)} held mutual-fund scheme(s) have NO amfi_code after auto-resolution.\n"
        f"They will receive NO NAV and show stale/zero value until fixed:\n\n"
        f"{listing}\n\n"
        f"Fix: assign amfi_code in security_master (see workers/isin_resolver.py), "
        f"then re-run workers/nav_history_backfill.py."
    )
    logger.warning("GUARD TRIPPED — held MF(s) without NAV source:\n" + body)
    try:
        from alert import send_alert
        send_alert("Held MF without NAV source", body)
    except Exception as e:
        logger.error(f"Guard alert failed to send: {e}")
    return rows


def get_tracked(conn) -> list:
    """Get securities with amfi_code."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, isin, amfi_code, security_name
        FROM security_master
        WHERE amfi_code IS NOT NULL
        AND isin IS NOT NULL
    """)
    rows = cursor.fetchall()
    cursor.close()
    return rows


def get_laggards(conn) -> list:
    """Securities whose newest NAV is behind the newest NAV any tracked fund has.

    Catch-up mode only. The daily 08:00 IST run sweeps everything; by the time it
    finishes, the funds that published on time define the "leader" date and the slow
    AMCs sit behind it. Chasing only that gap keeps the hourly poll proportional to
    the problem — 6 requests today, 0 once they catch up — instead of re-fetching all
    35 every hour. It also self-disables on market holidays: nobody publishes, so
    every fund is level with the leader and there is nothing to chase.

    Deliberately NOT anchored to "yesterday" — on a holiday that would put every fund
    behind and turn the hourly job into a 35-request-per-hour spin.
    """
    cursor = conn.cursor()
    cursor.execute("""
        WITH latest AS (
            SELECT sm.id, sm.isin, sm.amfi_code, sm.security_name,
                   MAX(nh.nav_date) AS newest
            FROM   security_master sm
            JOIN   nav_history nh ON nh.security_id = sm.id
            WHERE  sm.amfi_code IS NOT NULL
              AND  sm.isin IS NOT NULL
            GROUP  BY sm.id, sm.isin, sm.amfi_code, sm.security_name
        )
        SELECT id, isin, amfi_code, security_name, newest
        FROM   latest
        WHERE  newest < (SELECT MAX(newest) FROM latest)
        ORDER  BY security_name
    """)
    rows = cursor.fetchall()
    cursor.close()
    return rows


def save_nav(conn, security_id, nav_date, nav):
    """Upsert NAV into nav_history."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO nav_history (security_id, nav_date, nav, created_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (security_id, nav_date)
        DO UPDATE SET nav = EXCLUDED.nav, created_at = EXCLUDED.created_at
    """, (security_id, nav_date, nav, now_utc()))
    cursor.close()


def update_holding(conn, security_id, nav):
    """Update current NAV and value on holding table."""
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE holding
        SET last_updated_nav = %s,
            current_value    = quantity * %s,
            last_updated     = %s
        WHERE security_id = %s
    """, (nav, nav, now_utc(), security_id))
    cursor.close()


def log_run(conn, status, processed, failed, started, error=None):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ingestion_run
            (run_type, run_date, status, records_processed,
             records_failed, error_message, started_at, completed_at)
        VALUES ('amfi_nav', %s, %s, %s, %s, %s, %s, %s)
    """, (date.today(), status, processed, failed,
          error, started, now_utc()))
    cursor.close()


def run(catchup=False):
    started   = now_utc()
    today     = date.today()

    # Catch-up runs hourly and is idle most of the time. Defer the banner until we
    # know there is work, so 20-odd no-op passes a day don't bury the daily run's
    # output in the shared log. A failure still surfaces: cron_wrapper alerts on a
    # non-zero exit regardless of what was logged.
    if not catchup:
        logger.info(f"=== AMFI NAV Worker starting for {today} (daily) ===")

    conn      = None
    processed = 0
    failed    = 0
    skipped   = 0

    try:
        conn = get_db()

        # Serialise NAV refreshes across processes.
        #
        # The daily cron (30 2 * * *) and cas_automation_worker's in-process chain
        # both fire at 02:30 UTC by design — the CAS run reaches its 08:00 IST
        # deadline at exactly that minute and then calls run() itself. On 2026-07-19
        # and 2026-08-14 the two collided and deadlocked while updating the same
        # `holding` rows; the victim's transaction was aborted, so every remaining
        # security failed with "current transaction is aborted" and the run exited 1.
        #
        # The "already ran today" check below cannot prevent this: both processes
        # evaluate it before either has written a row. The lock closes that window
        # by covering the check as well as the work.
        #
        # try_ rather than a blocking acquire: the loser would only redo work the
        # winner is already doing, so it exits cleanly (0) instead of queueing.
        # Session-level, so it is released when the connection closes in `finally`,
        # including on every early return below. It also survives a transaction
        # rollback, which a transaction-scoped lock would not.
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s) AS got", (_ADVISORY_LOCK_KEY,))
        got_lock = cursor.fetchone()["got"]
        cursor.close()

        if not got_lock:
            logger.info(
                "Another AMFI NAV refresh is already running (advisory lock held) — "
                "skipping this pass."
            )
            return

        # Skip if already ran today.
        #
        # Catch-up mode must bypass this. The check counts rows *dated* today, not rows
        # *written* today — normally a no-op because NAVs carry the previous business
        # day's date, but once the late-evening publishes land (NAV dated today) it
        # trips and would kill every remaining hourly run of the day, stranding exactly
        # the slow AMCs this mode exists to chase.
        if not catchup:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) AS c FROM nav_history WHERE nav_date = %s",
                (today,)
            )
            existing = cursor.fetchone()["c"]
            cursor.close()

            if existing > 0:
                logger.info(f"Already ran today ({existing} records). Skipping.")
                return

        if catchup:
            # Skip amfi_code resolution and the unresolved-MF guard: both are
            # daily-sweep concerns, and the guard emails on every trip — hourly that
            # would be 24 identical alerts a day.
            tracked = get_laggards(conn)
            if not tracked:
                return  # silent: all funds level with newest NAV
            logger.info(f"=== AMFI NAV Worker starting for {today} (catch-up) ===")
            logger.info(f"Catch-up: {len(tracked)} fund(s) behind newest NAV date")
        else:
            # Auto-resolve any missing amfi_codes first
            logger.info("Checking for missing amfi_codes...")
            resolve_all_missing(conn)

            # Guard: alert on any held MF that resolution couldn't map (no NAV source)
            check_unresolved_held_mfs(conn)

            # Get tracked securities
            tracked = get_tracked(conn)
            logger.info(f"Tracking {len(tracked)} securities")

        if not tracked:
            logger.info("No securities with amfi_code yet. Run CAS parser first.")
            log_run(conn, "success", 0, 0, started)
            conn.commit()
            return

        # Fetch NAV for each
        for sec in tracked:
            name      = sec["security_name"]
            amfi_code = sec["amfi_code"]
            sec_id    = sec["id"]

            try:
                entry = fetch_nav(amfi_code)
                if not entry:
                    logger.warning(f"No NAV for amfi_code {amfi_code}")
                    skipped += 1
                    continue

                # Catch-up polls the same laggards every hour until they publish.
                # Writing an unchanged NAV would rewrite holding rows and trigger a
                # full metrics recompute on every one of those empty passes.
                if catchup and entry["date"] <= sec["newest"]:
                    logger.info(f"⏳ {name[:45]} | still {entry['date']} — not published yet")
                    skipped += 1
                    continue

                save_nav(conn, sec_id, entry["date"], entry["nav"])
                update_holding(conn, sec_id, entry["nav"])
                # Commit per security rather than once after the loop. The whole
                # sweep used to be a single transaction, so any mid-loop failure
                # discarded every NAV saved up to that point. It also held row
                # locks on `holding` for the full ~10min run, which is what let
                # the 02:30 UTC double-schedule deadlock in the first place.
                conn.commit()
                logger.info(f"✅ {name[:45]} | {entry['nav']} | {entry['date']}")
                processed += 1

            except Exception as e:
                failed += 1
                logger.error(f"Failed {name[:40]}: {e}")
                # Without this the connection stays in a failed transaction and
                # EVERY later security dies with "current transaction is aborted"
                # — one transient error (a deadlock victim, a network blip) took
                # out all 249 remaining funds and exited 1. See 2026-07-19 and
                # 2026-08-14. Roll back and carry on with the next fund.
                try:
                    conn.rollback()
                except Exception as rb_err:
                    # Rollback itself failing means the connection is gone; there
                    # is nothing left to continue with, so stop rather than log
                    # one identical failure per remaining security.
                    logger.error(f"Rollback failed — connection unusable: {rb_err}")
                    raise

        log_run(conn, "success", processed, failed, started)
        conn.commit()

        logger.info(f"=== Done: {processed} saved | {skipped} skipped | {failed} failed ===")

        # Nothing moved — skip the metrics chain rather than recompute identical numbers.
        if catchup and processed == 0:
            return

    except Exception as e:
        logger.error(f"AMFI Worker FAILED: {e}")
        if conn:
            try:
                log_run(conn, "failed", processed, failed, started, str(e))
                conn.commit()
            except:
                pass
        sys.exit(1)

    finally:
        if conn:
            conn.close()

    # Chain the MF metrics worker so market_value_as_on stays in lockstep with
    # the NAVs we just wrote. Running it here means EVERY NAV refresh — scheduled
    # OR manual — is immediately followed by a metrics recompute, instead of
    # relying on a separately-timed cron that only fires for the two daily runs
    # (which left market_value_as_on stale whenever NAV ran at any other time).
    # Runs on its own DB connection; isolated so a metrics failure never undoes
    # the NAV save.
    try:
        from mf_metrics_worker import run as run_mf_metrics
        logger.info("Chaining MF metrics worker after NAV update…")
        run_mf_metrics()
        logger.info("MF metrics worker finished.")
    except SystemExit as e:
        # mf_metrics_worker calls sys.exit(1) on its own failure — don't let that
        # propagate as an AMFI failure; the NAVs were already saved & committed.
        if e.code:
            logger.error(f"MF metrics chain exited with code {e.code} (NAV still saved).")
    except Exception as e:
        logger.error(f"MF metrics chain failed (NAV still saved): {e}")


if __name__ == "__main__":
    run(catchup="--catchup" in sys.argv[1:])