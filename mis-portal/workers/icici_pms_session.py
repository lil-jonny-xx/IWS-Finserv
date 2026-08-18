#!/usr/bin/env python3
"""
ICICI Prudential PMS — persistent introspection session (DEV TOOL, not cron).

The portal login costs a PAN + password + OTP round-trip (the OTP is mailed to
the central collector inbox and is single-use), so probing selectors by running
icici_pms_worker.py over and over burns an OTP per attempt and rate-limits the
account. This script logs in ONCE, then stays alive and serves commands from a
spool directory, so any number of probes share one session. It also keeps
several browser tabs in the same context, so different strategy tabs
(Allocation / Performance / Holdings / Transactions) can be held open and read
side by side instead of being re-navigated one at a time.

  start:  .venv/bin/python workers/icici_pms_session.py --dir <spool> [--entity RAJANICORP]
  drive:  write <spool>/cmd/<seq>.json, read <spool>/res/<seq>.json

Command envelope: {"op": ..., "tab": <int, default 0>, ...op args}
  goto {url}            navigate the tab
  url                   current url + title
  text [max]            page inner_text
  html [name]           dump full HTML to <spool>/art/<name>.html
  tables                every <table> on the page as {headers, rows}
  grid                  role=row/gridcell scrape (for non-<table> grids)
  shot {name}           screenshot to <spool>/art/<name>.png
  click {sel}           click first match of a CSS selector
  click_text {text}     click first element containing text
  sels {sel}            count + first few inner_texts for a selector (probe)
  js {code}             page.evaluate(code)
  newtab                open another tab in the same context -> returns index
  tabs                  list open tabs
  quit                  shut the session down

Nothing here writes to the database; it is read-only against the portal.
"""
import argparse
import json
import os
import random
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from pyvirtualdisplay import Display

import icici_pms_worker as W


def _log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Page readers
# ---------------------------------------------------------------------------
def read_tables(page):
    """Every <table> on the page as {headers, rows}. Read in one evaluate() so a
    re-rendering SPA grid cannot shift under us between per-cell round-trips."""
    return page.evaluate("""() => {
        const out = [];
        for (const t of document.querySelectorAll('table')) {
            const rows = [];
            for (const tr of t.querySelectorAll('tr')) {
                const cells = [...tr.querySelectorAll('th,td')]
                    .map(c => c.innerText.replace(/\\s+/g, ' ').trim());
                if (cells.length) rows.push(cells);
            }
            if (!rows.length) continue;
            out.push({
                caption: (t.innerText || '').slice(0, 80),
                headers: rows[0],
                rows: rows.slice(1),
                n: rows.length - 1,
            });
        }
        return out;
    }""")


def read_grid(page):
    """ARIA/CDK grids that are not <table> elements."""
    return page.evaluate("""() => {
        const rows = [];
        for (const r of document.querySelectorAll('[role="row"]')) {
            const cells = [...r.querySelectorAll('[role="cell"],[role="gridcell"],[role="columnheader"]')]
                .map(c => c.innerText.replace(/\\s+/g, ' ').trim());
            if (cells.length) rows.push(cells);
        }
        return rows;
    }""")


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------
def handle(cmd, ctx, tabs, art: Path):
    op = cmd.get("op")
    idx = int(cmd.get("tab", 0))

    if op == "newtab":
        p = ctx.new_page()
        Stealth().apply_stealth_sync(p)
        tabs.append(p)
        return {"tab": len(tabs) - 1}
    if op == "tabs":
        return {"tabs": [{"i": i, "url": p.url, "title": p.title()}
                         for i, p in enumerate(tabs)]}

    while idx >= len(tabs):
        p = ctx.new_page()
        Stealth().apply_stealth_sync(p)
        tabs.append(p)
    page = tabs[idx]

    if op == "goto":
        page.goto(cmd["url"], timeout=W.NAV_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(int(cmd.get("wait", 2000)))
        return {"url": page.url, "title": page.title()}
    if op == "url":
        return {"url": page.url, "title": page.title()}
    if op == "text":
        return {"text": page.inner_text("body")[:int(cmd.get("max", 6000))]}
    if op == "html":
        f = art / f"{cmd.get('name', 'page')}.html"
        f.write_text(page.content(), encoding="utf-8")
        return {"file": str(f), "bytes": f.stat().st_size}
    if op == "tables":
        return {"tables": read_tables(page)}
    if op == "grid":
        return {"rows": read_grid(page)}
    if op == "shot":
        f = art / f"{cmd.get('name', 'shot')}.png"
        page.screenshot(path=str(f), full_page=bool(cmd.get("full", False)))
        return {"file": str(f)}
    if op == "click":
        page.locator(cmd["sel"]).nth(int(cmd.get("nth", 0))).click(
            timeout=W.ACTION_TIMEOUT, force=bool(cmd.get("force", True)))
        page.wait_for_timeout(int(cmd.get("wait", 2000)))
        return {"url": page.url}
    if op == "click_text":
        page.get_by_text(cmd["text"], exact=bool(cmd.get("exact", False))).nth(
            int(cmd.get("nth", 0))).click(timeout=W.ACTION_TIMEOUT, force=True)
        page.wait_for_timeout(int(cmd.get("wait", 2000)))
        return {"url": page.url}
    if op == "sels":
        loc = page.locator(cmd["sel"])
        n = loc.count()
        sample = []
        for i in range(min(n, int(cmd.get("max", 12)))):
            try:
                sample.append(loc.nth(i).inner_text().replace("\n", " ")[:120])
            except Exception:
                sample.append("<unreadable>")
        return {"count": n, "sample": sample}
    if op == "js":
        return {"value": page.evaluate(cmd["code"])}
    raise ValueError(f"unknown op {op!r}")


# ---------------------------------------------------------------------------
def serve(ctx, page, spool: Path, idle_timeout: int):
    cmd_dir, res_dir, art = spool / "cmd", spool / "res", spool / "art"
    for d in (cmd_dir, res_dir, art):
        d.mkdir(parents=True, exist_ok=True)
    tabs = [page]
    done, last = set(), time.time()
    (spool / "status").write_text("ready\n")
    _log(f"session ready — spooling on {spool}")

    while True:
        pend = sorted(p for p in cmd_dir.glob("*.json") if p.stem not in done)
        if not pend:
            if time.time() - last > idle_timeout:
                _log("idle timeout — shutting down")
                return
            time.sleep(0.4)
            continue
        for f in pend:
            done.add(f.stem)
            last = time.time()
            try:
                cmd = json.loads(f.read_text())
            except Exception as e:
                (res_dir / f.name).write_text(json.dumps({"ok": False, "error": f"bad json: {e}"}))
                continue
            if cmd.get("op") == "quit":
                (res_dir / f.name).write_text(json.dumps({"ok": True, "bye": True}))
                _log("quit received")
                return
            _log(f"-> {f.stem}: {cmd.get('op')} {str(cmd)[:120]}")
            try:
                out = {"ok": True, **(handle(cmd, ctx, tabs, art) or {})}
            except Exception as e:
                out = {"ok": False, "error": f"{type(e).__name__}: {e}",
                       "trace": traceback.format_exc()[-1200:]}
            (res_dir / f.name).write_text(json.dumps(out, default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="spool directory")
    ap.add_argument("--entity", default="RAJANICORP", help="env prefix")
    ap.add_argument("--idle-timeout", type=int, default=1800)
    ap.add_argument("--reuse", action="store_true",
                    help="try the saved browser profile's session before logging in")
    a = ap.parse_args()

    spool = Path(a.dir)
    spool.mkdir(parents=True, exist_ok=True)
    (spool / "status").write_text("starting\n")

    cfg = next((c for c in W.load_configs() if c.prefix == a.entity), None)
    if cfg is None:
        _log(f"no config for entity prefix {a.entity}")
        sys.exit(1)

    display = Display(visible=False, size=(1440, 900))
    display.start()
    W.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = W.BROWSER_PROFILE_DIR / stale
        if p.exists() or p.is_symlink():
            p.unlink()
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(W.BROWSER_PROFILE_DIR),
                headless=False,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"],
                user_agent=random.choice(W._USER_AGENTS),
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)
            try:
                logged_in = False
                if a.reuse:
                    try:
                        page.goto(W.LOGIN_URL, timeout=W.NAV_TIMEOUT,
                                  wait_until="domcontentloaded")
                        page.wait_for_timeout(3000)
                        logged_in = W._has_text(page, "list of strategies", "dashboard") \
                            and not W._has_text(page, "generate otp")
                        _log(f"profile session reusable: {logged_in}")
                    except Exception as e:
                        _log(f"reuse probe failed: {e}")
                if not logged_in:
                    W._login(page, cfg)
                serve(ctx, page, spool, a.idle_timeout)
            except Exception as e:
                (spool / "status").write_text(f"error: {e}\n")
                _log(f"FATAL: {e}")
                traceback.print_exc()
                raise
            finally:
                (spool / "status").write_text("closed\n")
                ctx.close()
    finally:
        display.stop()


if __name__ == "__main__":
    main()
