#!/usr/bin/env python3
"""
Cron wrapper — runs a worker script and sends an email alert if it exits non-zero.

Usage (in crontab, replace the direct python call):
  /var/www/.venv/bin/python /var/www/mis-portal/workers/cron_wrapper.py workers/amfi_nav_worker.py

The script path is relative to the project root (/var/www/mis-portal).
Stdout/stderr are passed through so the crontab log redirect still captures everything.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

WORKERS_DIR  = Path(__file__).parent
PROJECT_ROOT = WORKERS_DIR.parent
sys.path.insert(0, str(WORKERS_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(str(PROJECT_ROOT / ".env"), override=True)

import alert  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] cron_wrapper: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

VENV_PYTHON = "/var/www/.venv/bin/python"
TAIL_LINES  = 60


def main():
    if len(sys.argv) < 2:
        print("Usage: cron_wrapper.py <script_path> [args...]", file=sys.stderr)
        sys.exit(1)

    script_rel  = sys.argv[1]
    script_path = PROJECT_ROOT / script_rel
    worker_name = Path(script_rel).name
    cmd = [VENV_PYTHON, str(script_path)] + sys.argv[2:]

    result = subprocess.run(cmd, capture_output=True, text=True)

    # Pass through all output so cron log captures it
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)

    if result.returncode != 0:
        combined = (result.stdout or "") + (result.stderr or "")
        tail = "\n".join(combined.splitlines()[-TAIL_LINES:])
        logger.error(f"[{worker_name}] exited {result.returncode}")
        alert.send_failure_alert(worker_name, result.returncode, tail)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
