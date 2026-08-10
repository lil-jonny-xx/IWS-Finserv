#!/usr/bin/env python3
"""
Staleness monitor — email when portal data goes stale in a way cron_wrapper can't see.

cron_wrapper alerts only on a worker that RAN and exited non-zero. Six failure
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

  4. STALE HAND-ENTERED FIGURES — no worker owns manual_input, so a bank/forex/manual
     foreign-equity balance simply sits at whatever was last typed, and a stopped
     update routine looks identical to a quiet month. Caught by check_manual_stale,
     which watches whether a CATEGORY has gone untouched. It does not flag individual
     accounts: the owner updates what moved and leaves the rest, so an account older
     than its neighbours is normally just one whose balance did not change (confirmed
     2026-07-20 — an earlier per-account version flagged 10 accounts that held the
     most accurate figures available).

  5. LIVE TRADE CAPTURE NOT LISTENING — the order-update WebSocket daemons are the
     only path that sees a fill the moment it happens; the REST reconcile behind them
     can only ask Kite for the CURRENT day's trades. A daemon that never connected
     (dead daily token, auth reject) loses that session's fills silently, because
     systemd shows the unit "active" whether or not the socket ever opened. Caught by
     check_live_capture, which reads the daemons' own log for today's session.

  6. FILLS THAT NEVER REACHED THE LEDGER — the failure the other five only approximate.
     Between 2026-06-26 and 07-02 broker_txn_sync did not run; Kite retains only the
     current day's trades, so four fills (HHR PGINVIT 15,000 sold, HHR NETWEB 100 sold,
     HHR BRANDMAN 800 in, DHR META 111 in) were lost beyond recovery and one of them
     left a ghost holding visible for three weeks. Caught by check_unexplained_holdings,
     which reconciles what the broker feed says you HOLD against what the ledger says
     you TRADED. This is the check that would have caught June the next morning.

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

# ---------------------------------------------------------------------------
# 4. Stale hand-entered figures — DB freshness per manual_input CATEGORY
# ---------------------------------------------------------------------------
# Manual cash and manually-tracked foreign equity are only as current as the last
# time somebody typed them in, and no worker can refresh them.
#
# This deliberately measures the CATEGORY, not the account. The owner's workflow
# is to update the accounts that moved and leave the rest alone, so an account
# that was not re-saved in the last pass is normally just an account whose balance
# did not change — confirmed 2026-07-20, when 10 accounts flagged by an earlier
# per-account version turned out to hold the most accurate figures available.
# Alerting per account therefore means alerting on correct data, which is the
# fastest way to teach everyone to ignore this mail.
#
# What IS worth knowing is that a whole category has gone untouched: nobody has
# entered any bank balance at all in three weeks means the update itself stopped
# happening, which no other check would surface.
#
# category -> days without ANY entry in that category before it is called stale.
# Entered roughly fortnightly, so this allows one missed cycle of grace.
MANUAL_EXPECTED = {
    "bank":            21,
    "forex":           21,
    "nre_bank":        21,
    "overseas_equity": 21,
}
# Named in the mail so the reply-to-action is obvious, capped so one neglected
# category cannot produce a wall of text.
MANUAL_MAX_NAMED = 6

# ---------------------------------------------------------------------------
# 5. Live trade capture — did every order-update daemon listen this session?
# ---------------------------------------------------------------------------
# Keep in sync with deploy/systemd/mis-portal-live-trade-start.service (which units
# get started) and live_trade_daemon.ACCOUNTS (slug -> broker/entity). Imported as a
# literal rather than from the daemon module on purpose: importing that module pulls
# in kiteconnect / SmartApi / dhanhq, and this monitor must not fail because a broker
# SDK is unhappy.
LIVE_LOG = "/var/log/mis-portal-live-trade.log"
LIVE_ACCOUNTS = [
    ("zerodha-dhr",    "zerodha",   "DHR"),
    ("zerodha-hhr",    "zerodha",   "HHR"),
    ("zerodha-sdr",    "zerodha",   "SDR"),
    ("zerodha-rajani", "zerodha",   "Rajani Corp"),
    ("zerodha-hdr",    "zerodha",   "HDR"),
    ("angel-dhr",      "angel_one", "DHR"),
    ("angel-hhr",      "angel_one", "HHR"),
    ("dhan-hhr",       "dhan",      "HHR"),
    ("dhan-rajani",    "dhan",      "Rajani Corp"),
]
# The start/stop timers bound the daemons to 03:35–10:05 UTC (09:05–15:35 IST). This
# monitor runs at 12:00 UTC, i.e. AFTER the stop timer has already shut every daemon
# down — so "is the unit active right now" is guaranteed to be false and useless here.
# The session is therefore judged from the log the daemons wrote while they ran.
LIVE_SESSION_START_UTC = (3, 35)
LIVE_SESSION_END_UTC   = (10, 5)
LIVE_TAIL_LINES = 60000     # a full day is ~5k lines (Dhan heartbeats dominate)
# Dhan ticks every 5 min; allow two missed ticks before calling it dead. Zerodha and
# Angel have no recurring heartbeat (one-shot "connected:" only), so mid-session death
# is NOT detectable for them from the log — see the note in check_live_capture.
LIVE_DHAN_QUIET_MIN = 15
# Order statuses that mean the order is done and a fill should have been booked:
# Zerodha 'COMPLETE', Angel One 'complete', Dhan 'Traded'. Compared lower-cased.
LIVE_TERMINAL_STATUSES = ("complete", "traded")

# ---------------------------------------------------------------------------
# 6. Fills that never reached the ledger — holdings moved, no trade explains it
# ---------------------------------------------------------------------------
# Reconciles the broker feed's quantity against the trade ledger. Deliberately does
# NOT alert on "no trades recorded lately": a quiet fortnight is normal and that alarm
# would be ignored within a week. It alerts only when a position's quantity actually
# MOVED and no transaction accounts for the move — which is unambiguous.
#
# Window ends LEDGER_SETTLE_DAYS before today so ordinary T+1/T+2 settlement lag (the
# feed showing a buy a day or two after the trade date) cannot masquerade as a missing
# fill. Verified against the June incident: at a 3-day buffer the settlement noise
# (BHARTIARTL, CSLFINANCE, WAAREEENER, J&KBANK, WABAG) drops out and only the four
# genuinely-lost fills remain.
LEDGER_LOOKBACK_DAYS = 12
LEDGER_SETTLE_DAYS   = 3
# Ignore sub-share rounding and token dust: a move must be at least one whole unit AND
# a meaningful slice of the position before it is worth an email.
LEDGER_MIN_QTY = 1.0
LEDGER_MIN_PCT = 1.0
LEDGER_MAX_NAMED = 10
# Synthetic ledger rows: these are plugs derived FROM the holding, not evidence of a
# trade, so counting them here would let a position explain its own movement.
LEDGER_SYNTHETIC = ("reconstructed", "snapshot_open")


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


def _in_market_hours(now_utc) -> bool:
    """True Mon–Fri 09:30–15:30 IST (IST is a fixed +5:30, no DST)."""
    ist = now_utc + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return False
    mins = ist.hour * 60 + ist.minute
    return (9 * 60 + 30) <= mins <= (15 * 60 + 30)


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
        if _in_market_hours(now):
            age = (now - newest).total_seconds() / 60.0
            return [("equity_price_worker",
                     f"price log last wrote {age:.0f} min ago — worker stalled; token health unknown")]
        # Outside market hours the price worker is idle by design (last write is
        # ~15:30 IST close), so a quiet log is not a stall. Assess the final
        # window before it went quiet so a token that died late in the day is
        # still caught.
        cutoff = newest - timedelta(minutes=TOKEN_WINDOW_MIN)

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


def check_manual_stale():
    """(category, detail) for hand-entered categories nobody has touched lately.

    Fires only when NO account in a category has been entered inside its window —
    i.e. the update itself stopped happening. It deliberately says nothing about
    an individual account being older than its neighbours: the owner updates what
    moved and leaves the rest, so a stale-looking account is normally just one
    whose balance did not change, and alerting on it means alerting on correct
    data.

    Reports the value sitting behind the category and names its oldest accounts,
    so the mail says what to go and do.
    """
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            dbname=os.getenv("DB_NAME", "mis_portal"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
        )
    except Exception as e:
        return [("manual_input", f"could not connect to DB to check manual freshness: {e}")]

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (entity_id, category, label)
                       entity_id, category, label, updated_at, current_value
                FROM   manual_input
                WHERE  category = ANY(%s)
                ORDER  BY entity_id, category, label, updated_at DESC
            )
            SELECT l.entity_id, e.entity_name, l.category, l.label,
                   l.updated_at, l.current_value,
                   EXTRACT(EPOCH FROM (NOW() - l.updated_at)) / 86400.0 AS age_days,
                   -- Freshest entry anywhere in this category: the category is
                   -- only stale when even this one is past the threshold.
                   EXTRACT(EPOCH FROM (
                       NOW() - MAX(l.updated_at) OVER (PARTITION BY l.category)
                   )) / 86400.0 AS category_age_days
            FROM   latest l
            JOIN   entity e ON e.id = l.entity_id
        """, (list(MANUAL_EXPECTED),))
        rows = cur.fetchall()
    except Exception as e:
        return [("manual_input", f"could not query manual_input: {e}")]
    finally:
        conn.close()

    # Group by category, keeping only categories where even the freshest entry is
    # past the window. One account lagging its neighbours is not a finding.
    by_cat = {}
    for r in rows:
        cat = r["category"]
        if float(r["category_age_days"] or 0) <= MANUAL_EXPECTED.get(cat, 21):
            continue
        by_cat.setdefault(cat, []).append({
            "who":   f"{r['entity_name']} · {r['label']}",
            "age":   float(r["age_days"] or 0),
            "value": float(r["current_value"] or 0),
        })

    out = []
    for cat, items in sorted(by_cat.items()):
        items.sort(key=lambda x: x["age"], reverse=True)
        total   = sum(i["value"] for i in items)
        freshest = min(i["age"] for i in items)
        named   = items[:MANUAL_MAX_NAMED]
        detail = (f"nothing entered in {freshest:.0f} days (threshold "
                  f"{MANUAL_EXPECTED[cat]}d) — {len(items)} account(s) "
                  f"holding ₹{total:,.0f}. Oldest:")
        for i in named:
            detail += f"\n      – {i['who']}: {i['age']:.0f}d old, ₹{i['value']:,.0f}"
        if len(items) > len(named):
            detail += f"\n      – …and {len(items) - len(named)} more"
        out.append((cat, detail))
    return out


def _db_conn():
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def check_live_capture():
    """(account, detail) for every live-trade daemon that did not listen this session.

    Judged from the daemons' shared logfile, not systemd: the stop timer shuts every
    daemon at 10:05 UTC and this monitor runs at 12:00, so `systemctl is-active` is
    always false by then and would alert every single day.

    Per account the log gives, for today:
      "starting live_trade_daemon broker=<b> entity=<e>"   -> the process launched
      "connected: <broker>/<entity>"                        -> the socket actually opened
      "dhan/<entity> listening — alive Nm"                  -> Dhan's 5-min heartbeat
    Launched-but-never-connected is the important state: systemd reports the unit
    active either way, so without this the session's fills vanish with no signal.

    Known limit, stated rather than papered over: only Dhan emits a recurring
    heartbeat. For Zerodha and Angel a daemon that connects and then dies mid-session
    is invisible here (systemd's Restart=always would relaunch it and log a fresh
    "connected:", so the common case is covered; an exhausted StartLimitBurst is not).
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ist = now + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return []          # no session to judge at the weekend
    end_h, end_m = LIVE_SESSION_END_UTC
    if (now.hour, now.minute) < (end_h, end_m):
        return []          # session still open (or an ad-hoc early run) — nothing to conclude yet

    p = Path(LIVE_LOG)
    if not p.exists():
        return [("live_trade_daemon", "log file missing — daemons have never run")]
    try:
        lines = _tail_lines(LIVE_LOG, LIVE_TAIL_LINES)
    except Exception as e:
        return [("live_trade_daemon", f"could not read live-trade log: {e}")]

    today = now.strftime("%Y-%m-%d")
    todays = [ln for ln in lines if ln.startswith(today)]
    if not todays:
        return [("live_trade_daemon",
                 "nothing logged today — the start timer never fired, so NO live fill "
                 "capture happened this session (the REST reconcile is the only net left, "
                 "and Kite keeps only the current day's trades)")]

    def _ts(ln):
        try:
            return datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    out = []
    for slug, broker, entity in LIVE_ACCOUNTS:
        started = connected = last_beat = None
        auth_fatal = False
        for ln in todays:
            if f"broker={broker} entity={entity}" in ln:
                started = _ts(ln) or started
            if f"connected: {broker}/{entity}" in ln:
                connected = _ts(ln) or connected
            # Dhan has no on-open callback; "connecting:" plus a heartbeat is its proof.
            if broker == "dhan" and f"dhan/{entity} listening" in ln:
                last_beat = _ts(ln) or last_beat
                connected = connected or last_beat
            if f"AUTH-FATAL {entity}/{broker}" in ln:
                auth_fatal = True

        # Reported even when the daemon also connected earlier in the day: a token killed
        # mid-session (the daily re-login) means every fill after that point was lost.
        if auth_fatal:
            out.append((slug, "token was invalidated mid-session (AUTH-FATAL) — the daemon "
                              "exited for systemd to restart it. If this repeats daily, a "
                              "daemon is outliving the 01:00 UTC token refresh"))
            continue
        if started is None and connected is None:
            out.append((slug, "daemon never launched today — no fills captured for this account"))
            continue
        if connected is None:
            out.append((slug, "process launched but the order-update socket never opened "
                              "— usually a dead/rejected daily token; this session's fills "
                              "were NOT captured live"))
            continue
        if broker == "dhan" and last_beat is not None:
            session_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
            quiet_min = (session_end - last_beat).total_seconds() / 60.0
            if quiet_min > LIVE_DHAN_QUIET_MIN:
                out.append((slug, f"stopped sending heartbeats at {last_beat:%H:%M} UTC, "
                                  f"{quiet_min:.0f} min before the {end_h:02d}:{end_m:02d} "
                                  f"session end — capture died mid-session"))
    return out


def check_live_fill_mapping():
    """(account, detail) where order-update frames arrived but no fill was booked.

    The blind spot that let the Dhan camelCase bug run undetected: the socket
    connected, authenticated and heartbeated "events 4, fills 0" all session, so
    systemd, the token check and check_live_capture all stayed green while every real
    trade was dropped. It took placing a trade and noticing it missing to find it.

    Three tells, all read from the daemons' own log:

      * `status=None` on an order-event — the frame arrived but the mapper could not
        read it, i.e. the payload's shape or key casing changed under us. This is the
        one that catches a casing bug, and it has to be read from the raw event
        rather than inferred from a terminal-status test, because that same bug is
        what blanks the status in the first place.
      * a terminal status with no FILL line for that account — the status parsed but
        the fill did not book (a missing qty/price is the usual cause).
      * 'skipping incomplete fill' — record_fill rejected a fill outright.

    Deliberately NOT alerting on the bare "events > 0, fills 0" heartbeat: an order
    that is placed and then cancelled produces events and no fill, which is correct,
    and alerting on it would fire every time someone changes their mind. Accounts
    whose daemon never connected are skipped — check_live_capture owns that, and
    reporting both would double up on one fault.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ist = now + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return []
    end_h, end_m = LIVE_SESSION_END_UTC
    if (now.hour, now.minute) < (end_h, end_m):
        return []          # session still open — a fill may yet be booked

    if not Path(LIVE_LOG).exists():
        return []          # check_live_capture already reports a missing log
    try:
        lines = _tail_lines(LIVE_LOG, LIVE_TAIL_LINES)
    except Exception:
        return []          # ditto for an unreadable one

    today  = now.strftime("%Y-%m-%d")
    todays = [ln for ln in lines if ln.startswith(today)]
    if not todays:
        return []

    out = []
    for slug, broker, entity in LIVE_ACCOUNTS:
        ev_marker   = f"order-event {entity}/{broker} "
        fill_marker = f"FILL {entity}/{broker} "
        events = [ln for ln in todays if ev_marker in ln]
        if not events:
            continue       # no frames at all is check_live_capture's business
        fills = sum(1 for ln in todays if fill_marker in ln)
        # A replayed frame logs "dup fill <broker>:live:<id> — skip" instead of FILL;
        # the trade IS booked, so it must not read as a mapping failure.
        dups  = sum(1 for ln in todays if f"dup fill {broker}:live:" in ln)

        unparsed = sum(1 for ln in events if "status=None" in ln)
        if unparsed:
            out.append((slug, f"{unparsed} of {len(events)} order-update frames arrived "
                              f"with an unreadable status — the mapper cannot parse this "
                              f"broker's payload, so fills are being dropped silently. "
                              f"Compare a 'raw order msg' line against the field names in "
                              f"the handler; a key-casing change does exactly this"))
            continue

        terminal = sum(1 for ln in events
                       if any(f"status='{s}" in ln.lower() for s in LIVE_TERMINAL_STATUSES))
        if terminal and not fills and not dups:
            out.append((slug, f"{terminal} order(s) reached a terminal status but no fill "
                              f"was booked — the status parsed, the fill did not. Check the "
                              f"log for 'skipping incomplete fill' (missing qty/price)"))

    incomplete = sum(1 for ln in todays if "skipping incomplete fill" in ln)
    if incomplete:
        out.append(("live_trade_daemon",
                    f"{incomplete} fill(s) rejected as incomplete today — a required field "
                    f"(qty, price, side or order id) was missing from the broker payload"))
    return out


def check_unexplained_holdings():
    """(position, detail) where the broker feed's quantity moved but no trade explains it.

    The direct detector for a lost fill: the holdings feed is the broker's own book, so
    if a position's quantity changed and stock_transaction has nothing covering the
    change, a buy or sell happened that we never recorded. That is what June looked
    like, and it went unnoticed for three weeks.

    Compared per (entity, ISIN) rather than per broker leg, because a security held at
    two brokers nets across them in the ledger (there is no per-broker trade split for
    older rows) and a leg-level comparison invents differences that aren't there.
    """
    try:
        conn = _db_conn()
    except Exception as e:
        return [("ledger_reconcile", f"could not connect to DB: {e}")]

    try:
        cur = conn.cursor()
        cur.execute("""
            WITH b AS (
              SELECT (CURRENT_DATE - %s::int) AS d0, (CURRENT_DATE - %s::int) AS d1
            ),
            -- A newly-connected account arrives as a whole portfolio of 0 -> N moves on
            -- its first snapshot day, none of which has a ledger behind it. That is
            -- onboarding, not lost fills (HDR/zerodha went live 2026-07-13 and produced
            -- 40 such rows). Only reconcile a leg that already existed before the window,
            -- so an established position's movement is still caught from day one.
            established AS (
              SELECT h.entity_id, h.broker
                FROM equity_holding_history h, b
               WHERE h.broker NOT IN ('ibkr','vested','dbs')
               GROUP BY h.entity_id, h.broker, b.d0
              HAVING MIN(h.snapshot_date) < b.d0
            ),
            legs AS (
              SELECT DISTINCT h.entity_id, h.broker, h.isin
                FROM equity_holding_history h
                JOIN established x ON x.entity_id = h.entity_id AND x.broker = h.broker, b
               WHERE h.snapshot_date BETWEEN b.d0 AND b.d1
                 AND h.isin IS NOT NULL
                 -- foreign brokers have no stock_transaction rows to reconcile against
                 AND h.broker NOT IN ('ibkr','vested','dbs')
            ),
            q AS (
              SELECT g.entity_id, g.isin,
                     SUM(COALESCE((SELECT h.quantity FROM equity_holding_history h, b
                                    WHERE h.entity_id=g.entity_id AND h.broker=g.broker
                                      AND h.isin=g.isin AND h.snapshot_date <= b.d0
                                    ORDER BY h.snapshot_date DESC LIMIT 1), 0)) AS q0,
                     -- Closing quantity is the d1 snapshot, EXCEPT for a leg that no
                     -- longer exists in equity_holding at all, which closes at 0.
                     -- equity_holding_history is written by the price worker FROM
                     -- equity_holding, so a ghost (sold, but not yet pruned) keeps
                     -- snapshotting its old quantity every day. Reading the snapshot
                     -- alone would then see "quantity unchanged" against a ledger full
                     -- of real sells and report the exit as an unexplained PURCHASE —
                     -- precisely backwards. Zeroing a departed leg keeps the d1 settle
                     -- buffer while still telling the truth about positions that closed.
                     SUM(CASE WHEN EXISTS (SELECT 1 FROM equity_holding eh
                                            WHERE eh.entity_id = g.entity_id
                                              AND eh.broker = g.broker
                                              AND eh.isin = g.isin)
                              THEN COALESCE((SELECT h.quantity FROM equity_holding_history h, b
                                              WHERE h.entity_id=g.entity_id AND h.broker=g.broker
                                                AND h.isin=g.isin
                                                AND h.snapshot_date BETWEEN b.d1 - 2 AND b.d1
                                              ORDER BY h.snapshot_date DESC LIMIT 1), 0)
                              ELSE 0 END) AS q1
                FROM legs g GROUP BY 1, 2
            ),
            -- Two readings of the same ledger. STRICT counts only trades inside the
            -- holdings window; LOOSE also counts the settle-buffer days just before it,
            -- because a trade on d0-2 reaches the holdings feed on d0+1 (T+2) and would
            -- otherwise look like a purchase nobody recorded — that alone produced three
            -- false alarms (DHR GOLDBEES, SHILPAMED, both bought 07-08 and settled 07-10).
            -- A move is only reported when NEITHER reading explains it, so no legitimate
            -- settlement alignment can trigger the alert.
            led AS (
              SELECT st.entity_id, sm.isin,
                     SUM(CASE WHEN st.transaction_date > b.d0
                              THEN (CASE WHEN st.transaction_type='BUY'
                                         THEN st.quantity ELSE -st.quantity END)
                              ELSE 0 END) AS net_strict,
                     SUM(CASE WHEN st.transaction_type='BUY'
                              THEN st.quantity ELSE -st.quantity END) AS net_loose
                FROM stock_transaction st
                JOIN security_master sm ON sm.id = st.security_id, b
               WHERE st.transaction_date > (b.d0 - %s::int)
                 AND st.transaction_date <= b.d1
                 AND st.source <> ALL(%s::text[])
               GROUP BY 1, 2
            ),
            -- Lifetime books vs what the broker shows TODAY. Used only to judge
            -- decreases: pruning a ghost (or hand-deleting one) drops the quantity
            -- inside the window while the sells justifying it are months old, so a
            -- window-only view reports every legitimate cleanup as a lost fill.
            lifetime AS (
              SELECT st.entity_id, sm.isin,
                     SUM(CASE WHEN st.transaction_type='BUY'
                              THEN st.quantity ELSE -st.quantity END) AS net
                FROM stock_transaction st
                JOIN security_master sm ON sm.id = st.security_id
               WHERE st.source <> ALL(%s::text[])
               GROUP BY 1, 2
            ),
            now_held AS (
              SELECT entity_id, isin, SUM(quantity) AS qty
                FROM equity_holding
               WHERE broker NOT IN ('ibkr','vested','dbs') AND isin IS NOT NULL
               GROUP BY 1, 2
            ),
            scored AS (
              SELECT e.entity_name, sm.security_name, q.q0, q.q1,
                     (q.q1 - q.q0) - COALESCE(l.net_strict, 0) AS u_strict,
                     (q.q1 - q.q0) - COALESCE(l.net_loose,  0) AS u_loose,
                     COALESCE(lt.net, 0) AS books_lifetime,
                     COALESCE(nh.qty, 0) AS held_now
                FROM q
                JOIN entity e ON e.id = q.entity_id
                JOIN security_master sm ON sm.isin = q.isin
                LEFT JOIN led l ON l.entity_id = q.entity_id AND l.isin = q.isin
                LEFT JOIN lifetime lt ON lt.entity_id = q.entity_id AND lt.isin = q.isin
                LEFT JOIN now_held nh ON nh.entity_id = q.entity_id AND nh.isin = q.isin
            )
            SELECT entity_name, security_name, q0, q1,
                   CASE WHEN ABS(u_strict) <= ABS(u_loose) THEN u_strict ELSE u_loose END AS unexplained
              FROM scored
             -- both readings must agree something is missing, and they must agree on the
             -- direction (a sign flip means settlement timing, not a lost trade)
             WHERE SIGN(u_strict) = SIGN(u_loose)
               AND LEAST(ABS(u_strict), ABS(u_loose)) >= %s
               AND LEAST(ABS(u_strict), ABS(u_loose))
                   >= (%s / 100.0) * GREATEST(ABS(q0), ABS(q1), 1)
               -- An unexplained increase always matters. An unexplained decrease matters
               -- only while the books still claim shares the broker no longer shows —
               -- the signature of a sell that never arrived (HHR PGINVIT: feed 0, books
               -- still 15,025). If the books already agree with the broker, the position
               -- simply closed and there is nothing to chase.
               AND (CASE WHEN ABS(u_strict) <= ABS(u_loose) THEN u_strict ELSE u_loose END > 0
                    OR books_lifetime - held_now >= %s)
             ORDER BY LEAST(ABS(u_strict), ABS(u_loose)) DESC
        """, (LEDGER_LOOKBACK_DAYS, LEDGER_SETTLE_DAYS, LEDGER_SETTLE_DAYS,
              list(LEDGER_SYNTHETIC), list(LEDGER_SYNTHETIC),
              LEDGER_MIN_QTY, LEDGER_MIN_PCT, LEDGER_MIN_QTY))
        rows = cur.fetchall()
    except Exception as e:
        return [("ledger_reconcile", f"could not reconcile holdings against ledger: {e}")]
    finally:
        conn.close()

    if not rows:
        return []

    named = rows[:LEDGER_MAX_NAMED]
    detail = (f"{len(rows)} position(s) moved in the last "
              f"{LEDGER_LOOKBACK_DAYS - LEDGER_SETTLE_DAYS} days with no trade on record. "
              f"A fill was missed — check the broker tradebook for these and import it "
              f"(Kite only serves the CURRENT day, so act before it ages out):")
    for entity_name, sec, q0, q1, unexp in named:
        side = "acquired" if unexp > 0 else "disposed"
        detail += (f"\n      – {entity_name} · {sec}: {float(q0):,.0f} → {float(q1):,.0f}, "
                   f"{abs(float(unexp)):,.0f} {side} with no transaction")
    if len(rows) > len(named):
        detail += f"\n      – …and {len(rows) - len(named)} more"
    return [("holdings_vs_ledger", detail)]


def main():
    sections = [
        ("Scheduled workers that appear NOT to have run", check_stale()),
        ("Broker tokens failing authentication right now", check_token_health()),
        ("PMS feeds that have gone stale", check_pms_stale()),
        ("Hand-entered figures overdue for a refresh", check_manual_stale()),
        ("Live trade daemons that did not listen this session", check_live_capture()),
        ("Live order events that arrived but booked no fill", check_live_fill_mapping()),
        ("Holdings that moved with no trade to explain it", check_unexplained_holdings()),
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
        "catches jobs that never fired, broker tokens that died mid-day, PMS feeds",
        "that stopped advancing, hand-entered figures nobody has refreshed, live-trade",
        "daemons that never opened their socket, and holdings that moved with no trade",
        "on record. Check the item above and its worker/log — or, for manual entries,",
        "re-enter the balance from the statement in Manual Data.",
        "",
        "A 'holdings moved with no trade' item is time-critical: Kite serves only the",
        "CURRENT day's trades, so an un-imported fill becomes unrecoverable once the",
        "day rolls over (2026-06-26..07-02 lost four that way).",
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
