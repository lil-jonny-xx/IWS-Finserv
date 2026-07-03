#!/usr/bin/env python3
"""
Staleness monitor — email when a critical cron worker has NOT run on schedule.

Silent non-execution is the one failure mode cron_wrapper cannot catch: it alerts
only on a NON-ZERO exit, i.e. a worker that RAN and failed. A worker whose cron
line never fires (as happened to broker_txn_sync_worker for ~5 weeks — the real
trades that back the Equity/Realised-Gains data simply stopped, silently) produces
no exit code and no alert.

This watches each worker's cron log file. Every scheduled run touches it (the
`>> logfile 2>&1` redirect opens the file, and the worker prints progress), so a
log that is missing or older than the worker's expected cadence means the job did
not run. One summary email per stale worker via alert.send_alert.

Intended cron (weekdays 12:00 UTC — after the watched jobs' daily slots):
  0 12 * * 1-5 /var/www/.venv/bin/python /var/www/mis-portal/workers/cron_wrapper.py workers/staleness_monitor.py >> /var/log/mis-portal-staleness.log 2>&1

Findings are emailed, not signalled through the exit code, so running it under
cron_wrapper does not double-alert: it exits 0 whenever the check itself ran, and
non-zero only on an internal monitor error (which cron_wrapper will then report).
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

import alert  # noqa: E402

# Each watched worker: its cron log, and the max age (hours) of that log before the
# job is considered to have missed its run. Thresholds assume this monitor runs at
# ~12:00 UTC on weekdays, comfortably after each worker's own daily slot, so a
# same-day run reads as a few hours old while a genuine miss reads as ≥1 day (or a
# whole weekend — harmless, since the monitor only runs on weekdays).
WATCHED = [
    # broker_txn_sync: weekdays 11:00 UTC (the authoritative trade capture).
    {"name": "broker_txn_sync_worker", "log": "/var/log/mis-portal-broker-txn-sync.log", "max_age_h": 6},
    # equity_sync: daily 01:30 UTC + weekdays 04:30 UTC (holdings/quantities).
    {"name": "equity_sync_worker",     "log": "/var/log/mis-portal-equity-sync.log",     "max_age_h": 12},
]


def check_stale():
    """Return a list of (name, log_path, reason) for every watched worker that
    looks like it missed its run."""
    now = time.time()
    stale = []
    for w in WATCHED:
        p = Path(w["log"])
        if not p.exists():
            stale.append((w["name"], w["log"], "log file missing — the cron line has never run"))
            continue
        age_h = (now - p.stat().st_mtime) / 3600.0
        if age_h > w["max_age_h"]:
            stale.append((w["name"], w["log"],
                          f"last ran {age_h:.1f}h ago (expected within {w['max_age_h']}h)"))
    return stale


def main():
    stale = check_stale()
    if not stale:
        print("staleness_monitor: all watched workers fresh.")
        return

    lines = ["These scheduled workers appear to have NOT run on time:", ""]
    for name, log, why in stale:
        lines.append(f"  • {name}: {why}")
        lines.append(f"      log: {log}")
    lines += [
        "",
        "cron_wrapper alerts only on a worker that ran and errored; this catches one",
        "that never fired at all. Check the crontab line and the log above.",
    ]
    body = "\n".join(lines)
    print(body)
    alert.send_alert(f"Worker not running: {', '.join(n for n, _, _ in stale)}", body)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"staleness_monitor internal error: {e}", file=sys.stderr)
        sys.exit(1)
