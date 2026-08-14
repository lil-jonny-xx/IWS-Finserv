#!/usr/bin/env python3
"""
CAS Automation Worker — IWS MIS Portal
Orchestrates daily CAS fetch for all 6 entities via one central Gmail inbox.
All entity emails (or their aliases) forward CAMS CAS emails to the central collector inbox.

Flow:
  1. Start a Gmail collector thread that polls for new CAS PDFs until 8 AM IST
  2. Shuffle entities so no two consecutive entries share the same PAN
  3. Fire CAMS requests one by one; between each trigger wait a random delay
     where the ceiling is itself randomised between 45–75 min (floor always 30 min)
  4. Each parser thread claims the first PDF in the shared queue that opens with
     its entity's password (fitz.authenticate), then parses + upserts to DB
  5. All parser threads run concurrently — wall time ≈ slowest email delivery
  6. After all threads complete (or 8 AM IST deadline), refresh NAVs once

Schedule: Daily at 11 PM IST (17:30 UTC)
Cron:     30 17 * * * /var/www/.venv/bin/python /var/www/mis-portal/workers/cas_automation_worker.py
"""
import os
import sys
import time
import random
import secrets
import logging
import tempfile
import threading
import datetime
import zoneinfo
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import fitz  # PyMuPDF — used for password matching only
import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

import cams_trigger_worker
import gmail_worker
import cas_parser_worker
import amfi_nav_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # File persistence handled by cron_wrapper stdout -> crontab log redirect
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

WORKERS_DIR = Path(__file__).parent
IST         = zoneinfo.ZoneInfo("Asia/Kolkata")


def _deadline_8am_ist() -> float:
    """Unix timestamp for 8:00 AM IST — same day if before 8 AM, next day otherwise."""
    now    = datetime.datetime.now(IST)
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now >= target:
        target += datetime.timedelta(days=1)
    return target.timestamp()


@dataclass
class EntityConfig:
    code: str
    email: str
    pan: str


def _entity_configs() -> list[EntityConfig]:
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"], dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
    )
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT e.entity_name AS code, e.cas_email AS email, pg.pan_number AS pan
        FROM   entity e
        JOIN   pan_group pg ON pg.id = e.pan_group_id
        WHERE  e.cas_email IS NOT NULL AND pg.pan_number IS NOT NULL
        ORDER  BY e.id
    """)
    rows = cur.fetchall()
    conn.close()
    return [EntityConfig(code=r["code"], email=r["email"], pan=r["pan"]) for r in rows]


def _pan_spread_score(order: list[EntityConfig]) -> tuple[float, float]:
    """
    (smallest gap, mean gap) between triggers sharing a PAN. Higher is better;
    a PAN appearing once imposes no constraint. Used to rank candidate orderings.
    """
    last: dict[str, int] = {}
    gaps: list[int] = []
    for i, cfg in enumerate(order):
        if cfg.pan in last:
            gaps.append(i - last[cfg.pan])
        last[cfg.pan] = i
    if not gaps:
        return (float("inf"), float("inf"))
    return (min(gaps), sum(gaps) / len(gaps))


def _shuffled_pan_spread(configs: list[EntityConfig]) -> list[EntityConfig]:
    """
    Returns configs in a random order that spreads same-PAN entities evenly across
    the run, rather than merely keeping them non-adjacent.

    This used to only guarantee no two CONSECUTIVE triggers shared a PAN, which is
    a much weaker property than it sounds: on 2026-08-13 HHR submitted at 21:09 and
    a second entity on that same PAN came up again 2.5h later as the last of the
    8 triggers, and was refused with "unable to process your request" on all three
    retry attempts. Adjacency was satisfied; the PAN was still hit twice in one
    evening, with the second hit at the point where the reCAPTCHA v3 score for a
    long-running session is worst. Maximising the minimum gap pushes the second
    trigger of a shared PAN toward the opposite end of the run instead.

    Deliberately targets EVEN spacing rather than the maximum possible gap. Simply
    keeping the best-scoring candidate pins a shared PAN to the first and last slots
    every single night — which would park one of them permanently in the tail slot
    where the reCAPTCHA score is worst, i.e. exactly the position that failed. So:
    sample candidates, keep every one that reaches the even-spacing target, and pick
    among them at random. Spread and positional variety, neither traded for the other.
    """
    if len(configs) < 2:
        return list(configs)

    counts = Counter(c.pan for c in configs)
    # Even-spacing gap. Floored at 2 so a small roster never loses the original
    # non-adjacency guarantee (3 entities with one shared PAN target 3//2 == 1,
    # which would permit back-to-back same-PAN triggers again). Unreachable targets
    # are handled by the fallback below. All-unique PANs score inf and always pass.
    target = max(2, len(configs) // max(counts.values()))
    candidate = list(configs)
    good: list[list[EntityConfig]] = []
    best: list[EntityConfig] = list(candidate)
    best_score = _pan_spread_score(best)

    for _ in range(500):
        random.shuffle(candidate)
        score = _pan_spread_score(candidate)
        if score[0] >= target:
            good.append(list(candidate))
        if score > best_score:
            best, best_score = list(candidate), score

    # Fall back to the best seen when the target is unreachable (e.g. one PAN
    # dominating the roster), so this always returns a usable order.
    return random.choice(good) if good else best


def _random_pdf_password() -> str:
    """
    Generate a CAMS-compliant PDF password.
    CAMS requires: starts with a letter, ≥1 uppercase, ≥2 digits, ≥1 special char,
    and allowed specials are only @ # $ * _
    token_urlsafe produces '-' which CAMS rejects — build manually instead.

    The special char is MANDATORY as of 2026-08-08: CAMS tightened the rule to
    "Must contain at least one special character (@, #, $, *,_)." and every
    alphanumeric-only password silently failed form validation from that date —
    the request was never submitted, so all 8 entities timed out waiting for a
    PDF that was never sent. Do not "simplify" this back to alnum-only.
    """
    import string as _string
    letters  = _string.ascii_letters
    digits   = _string.digits
    specials = "@#$*_"
    # Guarantee 2 digits + 1 special + 1 lowercase; fill the remaining 7 from the full
    # allowed set, then shuffle ONLY those 11 and prepend an UPPERCASE letter.
    #
    # The leading letter is mandatory: CAMS also enforces "Must start with an alphabet
    # letter." Shuffling all 12 left the first character to chance, so roughly 40% of
    # runs failed field validation and never submitted — which is precisely how IWS and
    # HDR silently produced no PDF on 2026-08-10 while the other six succeeded. Do not
    # fold the first character back into the shuffle.
    #
    # That leading letter is now pinned to UPPERCASE, which also satisfies a third rule
    # found on 2026-08-13: "Must contain at least one uppercase letter (A-Z)." Case was
    # previously left entirely to chance — a lowercase lead plus 8 free chars drawn from
    # a 67-char set missed uppercase ~1.1% of the time, i.e. roughly one entity every
    # 12 days. SDR drew such a password that night, was rejected at field validation,
    # and timed out at 08:00 IST waiting for a PDF that was never requested. The
    # explicit lowercase pick guards the symmetric rule in case CAMS adds it next.
    parts  = [secrets.choice(digits), secrets.choice(digits), secrets.choice(specials),
              secrets.choice(_string.ascii_lowercase)]
    parts += [secrets.choice(letters + digits + specials) for _ in range(7)]
    random.shuffle(parts)
    return secrets.choice(_string.ascii_uppercase) + "".join(parts)


def _pdf_matches_password(pdf_path: str, password: str) -> bool:
    """
    Returns True if `password` correctly unlocks the PDF.
    fitz.Document.authenticate() returns 0 on wrong password, non-zero on correct.
    """
    try:
        doc    = fitz.open(pdf_path)
        result = doc.authenticate(password)
        doc.close()
        return result != 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Gmail collector thread
# ---------------------------------------------------------------------------

def _gmail_collector(
    token_file: str,
    tmp_dir: str,
    run_start_ts: int,
    pending: list,
    pending_lock: threading.Lock,
    seen_ids: set,
    stop_event: threading.Event,
    deadline: float,
):
    """
    Polls Gmail every 30s for new CAS PDFs (subject-based filter, no per-entity
    to: filter). Appends (msg_id, pdf_path) to `pending` as emails arrive.
    """
    logger.info("Gmail collector started")
    while not stop_event.is_set() and time.time() < deadline:
        try:
            with pending_lock:
                current_seen = set(seen_ids)

            new_pdfs = gmail_worker.collect_new_cas_pdfs(
                token_file=token_file,
                save_dir=tmp_dir,
                after_ts=run_start_ts,
                exclude_ids=current_seen,
            )

            for msg_id, pdf_path in new_pdfs:
                with pending_lock:
                    if msg_id not in seen_ids:
                        pending.append((msg_id, pdf_path))
                        seen_ids.add(msg_id)
                        logger.info(f"Queued CAS PDF: {Path(pdf_path).name} (msg={msg_id})")

        except Exception as e:
            logger.warning(f"Gmail collector error: {e}")

        stop_event.wait(timeout=30)

    logger.info("Gmail collector stopped")


# ---------------------------------------------------------------------------
# Per-entity parser thread
# ---------------------------------------------------------------------------

def _entity_worker(
    cfg: EntityConfig,
    pdf_password: str,
    pending: list,
    pending_lock: threading.Lock,
    deadline: float,
    results: dict,
):
    """
    Waits for a PDF in `pending` that matches this entity's password, then parses it.
    Password matching via fitz.authenticate() — each entity's PDF has a unique random
    password, so only the right PDF will authenticate successfully.
    """
    logger.info(f"[{cfg.code}] Waiting for matching PDF...")
    claimed_path = None

    while time.time() < deadline and claimed_path is None:
        with pending_lock:
            snapshot = list(pending)

        for item in snapshot:
            msg_id, pdf_path = item
            if _pdf_matches_password(pdf_path, pdf_password):
                with pending_lock:
                    if item in pending:
                        pending.remove(item)
                        claimed_path = pdf_path
                        logger.info(f"[{cfg.code}] Claimed PDF: {Path(pdf_path).name}")
                        break
        else:
            time.sleep(15)

    if not claimed_path:
        deadline_str = datetime.datetime.fromtimestamp(deadline, IST).strftime("%H:%M IST")
        logger.error(f"[{cfg.code}] Timed out — no matching PDF arrived before {deadline_str}")
        results[cfg.code] = False
        return

    try:
        cas_parser_worker.run(claimed_path, pdf_password)
        logger.info(f"[{cfg.code}] Parsed and upserted successfully")
        results[cfg.code] = True
    except SystemExit:
        logger.error(f"[{cfg.code}] Parser exited with error")
        results[cfg.code] = False
    finally:
        try:
            os.remove(claimed_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

LOCK_FILE = Path("/tmp/cas_automation.lock")

def _acquire_lock() -> bool:
    """Return True if we got the lock, False if another instance is running."""
    try:
        if LOCK_FILE.exists():
            pid = int(LOCK_FILE.read_text().strip())
            try:
                os.kill(pid, 0)  # 0 = check existence only
                logger.error(f"Another CAS Automation is already running (PID {pid}). Exiting.")
                return False
            except (ProcessLookupError, PermissionError):
                logger.warning(f"Stale lock file (PID {pid} gone) — removing and continuing.")
        LOCK_FILE.write_text(str(os.getpid()))
        return True
    except Exception as e:
        logger.warning(f"Could not acquire lock: {e} — proceeding anyway")
        return True


def main(only: set[str] | None = None):
    if not _acquire_lock():
        sys.exit(1)

    try:
        _main(only)
    finally:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def _main(only: set[str] | None = None):
    logger.info(f"╔══ CAS Automation starting — {date.today()} ══╗")

    try:
        configs = _entity_configs()
    except KeyError as e:
        logger.error(f"Missing env var: {e}. Check .env file.")
        sys.exit(1)

    # --entity re-runs a subset without re-triggering the whole book. A CAS request
    # is a real submission against CAMS, which rate-limits and bot-scores, so after a
    # partial run the only safe retry is the entities that actually failed.
    if only:
        wanted  = {c.casefold() for c in only}
        configs = [c for c in configs if c.code.casefold() in wanted]
        missing = wanted - {c.code.casefold() for c in configs}
        if missing:
            logger.error(f"Unknown entity name(s): {sorted(missing)}")
            sys.exit(1)
        logger.info(f"Scoped run — {len(configs)} entity(ies): {[c.code for c in configs]}")

    central_token = str(WORKERS_DIR / os.environ.get(
        "GMAIL_TOKEN_CENTRAL", "gmail_token_central.json"
    ))
    if not Path(central_token).exists():
        logger.error(
            f"Central Gmail token not found: {central_token}\n"
            f"  Run: python workers/oauth_setup.py --token {central_token}"
        )
        sys.exit(1)

    run_start_ts   = int(time.time())
    deadline       = _deadline_8am_ist()
    deadline_str   = datetime.datetime.fromtimestamp(deadline, IST).strftime("%H:%M IST")
    results        = {}
    pending        = []
    pending_lock   = threading.Lock()
    seen_ids       = set()
    stop_collector = threading.Event()

    # Shuffle so triggers sharing a PAN land as far apart in the run as possible
    ordered = _shuffled_pan_spread(configs)
    logger.info(f"Gmail collector active until {deadline_str}")
    logger.info(f"Trigger order: {' → '.join(c.code for c in ordered)}")

    with tempfile.TemporaryDirectory(prefix="cas_auto_") as tmp_dir:

        collector_thread = threading.Thread(
            target=_gmail_collector,
            args=(central_token, tmp_dir, run_start_ts,
                  pending, pending_lock, seen_ids, stop_collector, deadline),
            name="gmail-collector",
            daemon=True,
        )
        collector_thread.start()

        entity_threads = []
        for idx, cfg in enumerate(ordered):
            if idx > 0:
                # Randomise the ceiling between 45–75 min, then pick a delay
                # uniformly between 30 min and that ceiling.
                ceil_s  = random.uniform(45 * 60, 75 * 60)
                delay_s = random.uniform(30 * 60, ceil_s)
                fire_at = datetime.datetime.fromtimestamp(
                    time.time() + delay_s, IST
                ).strftime("%H:%M IST")
                logger.info(
                    f"Next trigger [{cfg.code}] in {delay_s/60:.0f}m "
                    f"(ceil {ceil_s/60:.0f}m, fires ~{fire_at})"
                )
                time.sleep(delay_s)

            logger.info(f"━━━ [{cfg.code}] ━━━")
            pdf_password = _random_pdf_password()

            ok = cams_trigger_worker.trigger_cas_request_with_retry(cfg.pan, cfg.email, pdf_password)
            if not ok:
                logger.error(f"[{cfg.code}] CAMS trigger reported failure — watching for PDF anyway")

            t = threading.Thread(
                target=_entity_worker,
                args=(cfg, pdf_password, pending, pending_lock, deadline, results),
                name=f"parser-{cfg.code}",
                daemon=True,
            )
            t.start()
            entity_threads.append(t)

        logger.info(f"All triggers fired. Waiting until {deadline_str} for PDFs...")
        for t in entity_threads:
            remaining = max(0.0, deadline - time.time())
            t.join(timeout=remaining)

        stop_collector.set()
        collector_thread.join(timeout=5)

    logger.info("━━━ Refreshing NAVs ━━━")
    try:
        amfi_nav_worker.run()
    except SystemExit:
        logger.error("NAV worker failed")

    passed = [k for k, v in results.items() if v]
    failed = [k for k, v in results.items() if not v]
    logger.info(f"╚══ Done. Passed: {passed} | Failed: {failed} ══╝")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="IWS CAS automation (trigger + collect + parse)")
    ap.add_argument("--entity", action="append", metavar="NAME",
                    help="entity_name to run (repeatable). Default: every configured entity.")
    _args = ap.parse_args()
    main(set(_args.entity) if _args.entity else None)
