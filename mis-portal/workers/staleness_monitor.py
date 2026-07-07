#!/usr/bin/env python3
"""
Staleness monitor — email when portal data goes stale in a way cron_wrapper can't see.

cron_wrapper alerts only on a worker that RAN and exited non-zero. Three failure
modes slip past it, each of which has bitten us:

  1. SILENT NON-EXECUTION — a cron line that never fires (broker_txn_sync sat dead
     ~5 weeks; Nuvama PMS froze 18 days because its /var/log log file couldn't be
     created so the redirect failed before Python started). Caught by watching each
     worker's log-file mtime (WATCHED).

  2. DEAD BROKER TOKEN — the worker runs fine every 60s but one entity's token is
     invalid, so its holdings/cash quietly freeze while prices (served from a shared
     token) still look current (SDR/zerodha, 2026-07-07). cron_wrapper sees exit 0.
     Caught by scanning the price worker's own per-cycle auth-failure log lines
     (check_token_health) — it already probes every entity/broker each cycle, so a
     STILL-dead token recurs in the last few minutes while a self-healed one (Dhan's
     transient TOTP-window rejection) leaves nothing recent. No broker API calls.

  3. STALE PMS FEED — pms_holding rows stop advancing (Nuvama again). Caught by a DB
     freshness check per source (check_pms_stale).

All findings are emailed via alert.send_alert; the exit code stays 0 whenever the
checks themselves ran, so running under cron_wrapper does not double-alert. It exits
non-zero only on an internal monitor error (which cron_wrapper then reports).

Intended cron (weekdays 12:00 UTC — after the watched daily jobs' slots):
  0 12 * * 1-5 /var/www/.venv/bin/python /var/www/mis-portal/workers/cron_wrapper.py workers/staleness_monitor.py >> /var/log/mis-portal-staleness.log 2>&1
"""
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

import alert  # noqa: E402

# ---------------------------------------------------------------------------
# 1. Silent non-execution — watch cron log mtimes
# ---------------------------------------------------------------------------
# Each watched worker: its cron log, and the max age (hours) of that log before the
# job is considered to have missed its run. Thresholds assume this monitor runs at
# ~12:00 UTC on weekdays, comfortably after each worker's own daily slot.
WATCHED = [
    # broker_txn_sync: weekdays 11:00 UTC (the authoritative trade capture).
    {"name": "broker_txn_sync_worker", "log": "/var/log/mis-portal-broker-txn-sync.log", "max_age_h": 6},
    # equity_sync: daily 01:30 UTC + weekdays 04:30 UTC (holdings/quantities).
    {"name": "equity_sync_worker",     "log": "/var/log/mis-portal-equity-sync.log",     "max_age_h": 12},
    # ibkr_flex: weekdays 06:00 ET (≤11:00 UTC) + 17:30 ET (provisional).
    {"name": "ibkr_flex_worker",       "log": "/var/log/mis-portal-ibkr.log",            "max_age_h": 8},
]

# ---------------------------------------------------------------------------
# 2. Dead broker token — scan the price worker's per-cycle auth failures
# ---------------------------------------------------------------------------
PRICE_LOG = "/var/log/mis-portal-equity-price.log"
# A dead token logs a failure every price cycle (~60s). Only flag an entity/broker
# whose failures are RECENT (still broken now) and RECURRING (not a one-off blip).
TOKEN_WINDOW_MIN = 15     # look only at the last N minutes of the log
TOKEN_MIN_FAILS  = 3      # need ≥ this many failures in the window to flag
TOKEN_TAIL_LINES = 6000   # bound how much of the log we read
# Reasons that mean "token/auth", not a transient network/API hiccup.
_AUTH_HINTS = ("api_key", "access_token", "invalid_authentication",
               "dh-901", "dh-808", "token is invalid", "token type", "incorrect")
_FAIL_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*?"
    r"\[([^\]]+/[a-z_]+)\] holdings light-refresh fetch failed\s+—\s+(.*)$"
)

# ---------------------------------------------------------------------------
# 3. Stale PMS feed — DB freshness per source
# ---------------------------------------------------------------------------
# source -> max age (hours) of the newest updated_at before it's considered stale.
# nuvama runs Mon/Wed/Fri (worst gap Fri→Mon ~3.5d), icici weekdays, zerodha_pms is
# repriced by the 60s price worker so it should never be more than an hour old.
PMS_EXPECTED = {
    "nuvama_pms": 100,   # ~4.2 days — tolerates the Fri→Mon weekend gap
    "icici_pms":  48,
    "zerodha_pms": 24,
}


def check_stale():
    """(name, detail) for every watched worker whose cron log looks like it missed."""
    now = time.time()
    out = []
    for w in WATCHED:
        p = Path(w["log"])
        if not p.exists():
            out.append((w["name"], "log file missing — the cron line has never run"))
            continue
        age_h = (now - p.stat().st_mtime) / 3600.0
        if age_h > w["max_age_h"]:
            out.append((w["name"],
                        f"last ran {age_h:.1f}h ago (expected within {w['max_age_h']}h)"))
    return out


def _tail_lines(path: str, n: int):
    with open(path, "r", errors="replace") as f:
        return list(deque(f, maxlen=n))


def check_token_health():
    """(entity/broker, detail) for every broker token still failing auth right now.

    Reads only recent price-log lines so a token that self-healed earlier in the day
    is not reported. Returns a soft note (not a per-token alert) if the price log is
    missing or itself stale, since then token health cannot be assessed.
    """
    p = Path(PRICE_LOG)
    if not p.exists():
        return [("equity_price_worker", "price log missing — cannot assess token health")]

    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, matches log timestamps
    cutoff = now - timedelta(minutes=TOKEN_WINDOW_MIN)
    try:
        lines = _tail_lines(PRICE_LOG, TOKEN_TAIL_LINES)
    except Exception as e:
        return [("equity_price_worker", f"could not read price log: {e}")]

    # If the newest line is old, the price worker itself has stalled — flag that
    # instead of silently seeing "no recent failures = healthy".
    newest = None
    for ln in reversed(lines):
        if len(ln) >= 19:
            try:
                newest = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
                break
            except ValueError:
                continue
    if newest is None:
        return [("equity_price_worker", "no timestamped lines in price log — cannot assess tokens")]
    if newest < now - timedelta(minutes=TOKEN_WINDOW_MIN):
        age = (now - newest).total_seconds() / 60.0
        return [("equity_price_worker",
                 f"price log last wrote {age:.0f} min ago — worker stalled; token health unknown")]

    # Tally recent auth failures per entity/broker.
    fails, last_reason = {}, {}
    for ln in lines:
        m = _FAIL_RE.match(ln)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < cutoff:
            continue
        tag, reason = m.group(2), m.group(3).strip()
        if not any(h in reason.lower() for h in _AUTH_HINTS):
            continue  # not an auth/token failure — leave to other alerting
        fails[tag] = fails.get(tag, 0) + 1
        last_reason[tag] = reason

    out = []
    for tag, cnt in sorted(fails.items()):
        if cnt >= TOKEN_MIN_FAILS:
            reason = last_reason[tag]
            if len(reason) > 120:
                reason = reason[:117] + "..."
            out.append((tag, f"token invalid — {cnt} auth failures in last "
                             f"{TOKEN_WINDOW_MIN} min; reason: {reason}"))
    return out


def check_pms_stale():
    """(source, detail) for every expected PMS feed that is missing or stale in the DB."""
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            dbname=os.getenv("DB_NAME", "mis_portal"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
        )
    except Exception as e:
        return [("pms", f"could not connect to DB to check PMS freshness: {e}")]

    out = []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT source, MAX(updated_at),
                   EXTRACT(EPOCH FROM (NOW() - MAX(updated_at))) / 3600.0
            FROM pms_holding GROUP BY source
        """)
        seen = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    except Exception as e:
        conn.close()
        return [("pms", f"could not query pms_holding: {e}")]
    finally:
        conn.close()

    for source, max_age_h in PMS_EXPECTED.items():
        if source not in seen:
            out.append((source, "no rows in pms_holding — feed never populated or wiped"))
            continue
        updated_at, age_h = seen[source]
        if age_h is not None and age_h > max_age_h:
            days = age_h / 24.0
            out.append((source,
                        f"last updated {age_h:.0f}h ({days:.1f}d) ago — "
                        f"expected within {max_age_h}h (last: {updated_at:%Y-%m-%d %H:%M})"))
    return out


def main():
    sections = [
        ("Scheduled workers that appear NOT to have run", check_stale()),
        ("Broker tokens failing authentication right now", check_token_health()),
        ("PMS feeds that have gone stale", check_pms_stale()),
    ]
    findings = [(title, items) for title, items in sections if items]

    if not findings:
        print("staleness_monitor: all checks pass — workers fresh, tokens healthy, PMS current.")
        return

    lines = []
    flat_names = []
    for title, items in findings:
        lines.append(f"{title}:")
        for name, why in items:
            lines.append(f"  • {name}: {why}")
            flat_names.append(name)
        lines.append("")
    lines += [
        "cron_wrapper alerts only on a worker that ran and errored; this monitor",
        "catches jobs that never fired, broker tokens that died mid-day, and PMS",
        "feeds that stopped advancing. Check the item above and its worker/log.",
    ]
    body = "\n".join(lines)
    print(body)
    alert.send_alert(f"Portal data stale: {', '.join(flat_names)}", body)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"staleness_monitor internal error: {e}", file=sys.stderr)
        sys.exit(1)
