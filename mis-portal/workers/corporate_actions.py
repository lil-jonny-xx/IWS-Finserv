#!/usr/bin/env python3
"""
Detect, verify and record corporate actions (splits and bonus issues).

WHY THIS IS NOT OPTIONAL
------------------------
Bonus and split quantity is credited by the depository. It is never a BUY, so it
appears in NO tradebook, NO broker trade feed and NO order WebSocket. The first
evidence the system ever sees is a SELL it cannot cover from recorded buys, which FIFO
then drops as "unknown" — silently understating realised gains. Indian splits also mint
a NEW ISIN, so the same company arrives as a second security_master row and its lot
pool splits in two.

TWO SIGNALS, AND WHY BOTH ARE NEEDED
------------------------------------
  1. ISIN handover — two security_master rows share an issuer stem (first 9 chars of
     the ISIN) and trading moves from one to the other on a date.
  2. Oversold — a security whose lifetime net went negative.

NEITHER IS PROOF, and the stem heuristic is actively dangerous on its own: fund houses
issue every scheme under one stem, so INF204KB1 groups GOLDBEES with NIFTYBEES,
BANKBEES and PSUBNKBEES — four unrelated ETFs that a stem match would happily "merge".
So both signals are only CANDIDATE GENERATORS. Nothing is recorded until the ratio is
confirmed against the market's own split history from Yahoo Finance in a window around
the handover date. Same discipline as the orphan-securities merge: a name or a code
pattern proposes, evidence disposes.

    python -m workers.corporate_actions                 # report candidates + verdicts
    python -m workers.corporate_actions --commit        # write verified rows
"""
import os
import sys
import argparse
import warnings
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)
warnings.filterwarnings("ignore")

# How far either side of the ISIN handover / first uncovered sell to look for a split.
# Depository credit and the exchange's ex-date can sit a few weeks apart, and the
# tradebook window itself is coarse, so this is deliberately generous.
WINDOW_DAYS = 120

# Yahoo reports a 1:1 bonus as a 2.0 split, so the same field covers both. Anything
# below this is a dividend-like artefact, not a quantity event.
MIN_RATIO = 1.05
RATIO_TOL = 0.05


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        port=os.getenv("DB_PORT", "5432"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS corporate_action (
            id              SERIAL PRIMARY KEY,
            security_id     INTEGER NOT NULL REFERENCES security_master(id),
            action_type     TEXT NOT NULL,          -- split | bonus
            ex_date         DATE NOT NULL,
            ratio           NUMERIC NOT NULL,       -- 2.0 = 1:1 bonus or 1->2 split
            old_isin        TEXT,
            new_isin        TEXT,
            source          TEXT NOT NULL,          -- yfinance | broker | manual
            verified        BOOLEAN NOT NULL DEFAULT FALSE,
            evidence        TEXT,
            created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE (security_id, ex_date, action_type)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_corpaction_sec "
                "ON corporate_action(security_id, ex_date)")


def candidates_isin_handover(cur):
    """Pairs of security rows sharing an issuer stem. CANDIDATES ONLY — the stem is
    shared by every scheme of a fund house, so this over-generates badly."""
    cur.execute("""
        WITH s AS (
          SELECT sm.id, sm.isin, sm.security_name, substr(sm.isin,1,9) AS stem,
                 MIN(st.transaction_date) a, MAX(st.transaction_date) b,
                 COUNT(st.id) txns
            FROM security_master sm JOIN stock_transaction st ON st.security_id=sm.id
           WHERE sm.isin IS NOT NULL AND COALESCE(st.currency,'INR')='INR'
           GROUP BY 1,2,3,4
        )
        SELECT * FROM s
         WHERE stem IN (SELECT stem FROM s GROUP BY stem HAVING COUNT(*)>1)
         ORDER BY stem, a
    """)
    g = defaultdict(list)
    for r in cur.fetchall():
        g[r["stem"]].append(r)
    return g


def candidates_oversold(cur):
    """Securities sold beyond what was ever bought — quantity arrived from somewhere."""
    cur.execute("""
        SELECT sm.id, sm.isin, sm.security_name, st.entity_id,
               SUM(CASE WHEN upper(st.transaction_type)='BUY' THEN st.quantity
                        ELSE -st.quantity END) net,
               MIN(st.transaction_date) a, MAX(st.transaction_date) b
          FROM stock_transaction st JOIN security_master sm ON sm.id=st.security_id
         WHERE COALESCE(st.currency,'INR')='INR'
         GROUP BY 1,2,3,4
        HAVING SUM(CASE WHEN upper(st.transaction_type)='BUY' THEN st.quantity
                        ELSE -st.quantity END) < -0.5
         ORDER BY 5
    """)
    return cur.fetchall()


def candidates_unrecorded_split(cur):
    """Currently-held INR equities whose Yahoo split history (security_split, the table
    that back-adjusts the price series) contains an event NOT in corporate_action.

    This is the gap the other two signals structurally miss: a same-ISIN split that
    never went oversold (a reconstruct plug absorbed the credited quantity, e.g.
    HDFCAMC / NESTLEIND's 2nd split) and never handed over to a new ISIN. Held-only so
    we don't chase splits on positions long exited. Returns security rows; each is still
    put through live Yahoo verification below, which also drops foreign name-collisions
    (the Indian `.NS` ticker carries no such event)."""
    cur.execute("""
        SELECT DISTINCT sm.id, sm.security_name, sm.isin
        FROM security_split ss
        JOIN security_symbol_map m ON m.resolved_symbol = ss.yahoo_symbol
        JOIN security_master sm ON sm.security_name = m.symbol
        JOIN equity_holding eh ON eh.isin = sm.isin AND eh.quantity > 0
                              AND COALESCE(eh.currency, 'INR') = 'INR'
        WHERE ss.ratio >= %s
          AND sm.isin LIKE 'IN%%'                                   -- Indian listing only
          AND NOT EXISTS (SELECT 1 FROM corporate_action ca
                          WHERE ca.security_id = sm.id AND abs(ca.ex_date - ss.split_date) <= 15)
          -- ISIN-handover twin already carries the action (signal 1 owns those): the
          -- held row is the post-split ISIN, its trades need no scaling. Same issuer
          -- stem (first 9 ISIN chars) on another row that HAS an action ⇒ skip.
          AND NOT EXISTS (SELECT 1 FROM security_master s2 JOIN corporate_action ca2 ON ca2.security_id = s2.id
                          WHERE s2.id <> sm.id AND substr(s2.isin,1,9) = substr(sm.isin,1,9))
          -- foreign name-collision: a same-name sibling that is foreign (no ISIN / non-IN)
          -- means the Yahoo split line belongs to the foreign listing, not this one
          -- (Indian 'META' vs US Meta Platforms).
          AND NOT EXISTS (SELECT 1 FROM security_master s3
                          WHERE s3.security_name = sm.security_name AND s3.id <> sm.id
                            AND (s3.isin IS NULL OR s3.isin NOT LIKE 'IN%%'))
        ORDER BY sm.security_name
    """, (MIN_RATIO,))
    return cur.fetchall()


def yahoo_splits(symbol, lo, hi):
    """Split/bonus events for a symbol between lo and hi. Returns [(date, ratio)]."""
    try:
        import yfinance as yf
    except Exception:
        return None
    for suf in (".NS", ".BO"):
        try:
            s = yf.Ticker(str(symbol).strip().upper() + suf).splits
        except Exception:
            continue
        if s is None or len(s) == 0:
            continue
        out = []
        for ts, ratio in s.items():
            d = ts.date()
            if lo <= d <= hi and float(ratio) >= MIN_RATIO:
                out.append((d, float(ratio)))
        if out:
            return out
    return []


# ---------------------------------------------------------------------------
# Consumption side: adjusting FIFO lots. Both realised-gains
# (report_generator._fifo_realised_grouped) and per-holding metrics
# (equity_txn_metrics_worker.fifo_lots) call these, so the two engines cannot drift.
# ---------------------------------------------------------------------------

def load_actions(cur):
    """{security_id: [(ex_date, ratio), ...]} ascending. Verified rows only."""
    cur.execute("SELECT to_regclass('public.corporate_action') AS t")
    row = cur.fetchone()
    if not (row and (row["t"] if isinstance(row, dict) else row[0])):
        return {}
    cur.execute("""SELECT security_id, ex_date, ratio FROM corporate_action
                    WHERE verified ORDER BY security_id, ex_date""")
    out = defaultdict(list)
    for r in cur.fetchall():
        sid = r["security_id"] if isinstance(r, dict) else r[0]
        ex = r["ex_date"] if isinstance(r, dict) else r[1]
        ratio = float(r["ratio"] if isinstance(r, dict) else r[2])
        if ratio and ratio > 0:
            out[sid].append((ex, ratio))
    return dict(out)


def load_actions_by_isin(cur):
    """{isin: [(ex_date, ratio), ...]} for callers that key on ISIN rather than
    security_id (equity_txn_metrics_worker looks holdings up by ISIN).

    Note this keys on the security's CURRENT isin. Where a split minted a new ISIN the
    action row hangs off the new security row, which is the one a live holding matches.
    """
    by_sec = load_actions(cur)
    if not by_sec:
        return {}
    cur.execute("SELECT id, isin FROM security_master WHERE isin IS NOT NULL")
    out = {}
    for r in cur.fetchall():
        sid = r["id"] if isinstance(r, dict) else r[0]
        isin = r["isin"] if isinstance(r, dict) else r[1]
        if sid in by_sec:
            out.setdefault(isin, []).extend(by_sec[sid])
    for k in out:
        out[k].sort()
    return out


def apply_actions(lots, pending, upto):
    """Scale open FIFO lots for every action with ex_date <= upto. Mutates both.

    `lots`   : deque/list of mutable [buy_date, qty, price]
    `pending`: list of (ex_date, ratio) for THIS security, ascending; consumed in place
    `upto`   : the date of the trade about to be processed

    WHAT THE ADJUSTMENT IS. A 1:1 bonus (ratio 2.0) doubles the share count without
    anyone paying anything, so each open lot becomes `qty * ratio` shares at
    `price / ratio` each. Total cost basis is unchanged — it is the same money spread
    over more shares — which is exactly why this fixes the arithmetic without inventing
    profit. A 5:1 split (ratio 5.0) works identically.

    CALL THIS BEFORE PROCESSING A TRADE, NOT AFTER. Applied lazily at the first trade
    dated on/after the ex-date, every lot still in the deque was necessarily bought
    before that trade, and any lot bought after the ex-date was already scaled when its
    own buy was processed. Applying eagerly at load time, or after the trade, would
    double-scale post-ex-date purchases.

    The lot's BUY DATE is deliberately left alone: the holding-period clock keeps
    running from the original purchase, which is how a broker console reports it.

    TAX CAVEAT, stated because the two genuinely differ. Under Indian law bonus shares
    have NIL cost of acquisition (s.55(2)(aa)) and their holding period runs from
    ALLOTMENT, not from the original buy. This function implements the broker-console
    convention (cost spread, original date) because that is what the rest of this engine
    targets — it is already gross of STT/brokerage, so it was never the tax number.
    Do not cite its bonus figures as capital-gains computation.
    """
    n = 0
    while pending and pending[0][0] <= upto:
        _ex, ratio = pending.pop(0)
        for lot in lots:
            lot[1] *= ratio          # qty  up
            lot[2] /= ratio          # cost per share down; qty*price invariant
        n += 1
    return n


def cumulative_ratio_after(actions, d):
    """Product of ratios for actions whose ex_date is strictly AFTER date `d`.

    A share bought on `d` becomes this many shares today, so it is the factor that
    lifts a raw fill onto the FULLY split-adjusted basis — the basis a back-adjusted
    price series (Yahoo) and the current broker share count both live on. Unlike
    apply_actions (which scales lazily at each ex-date as a ledger is replayed), this
    is the whole-history factor: use it to reconcile a net quantity against the held
    quantity, or to restate an FY-end open lot for splits that fall AFTER the FY but
    which the price series has already priced in.
    """
    r = 1.0
    for ex, ratio in (actions or []):
        if ratio and ratio > 0 and ex > d:
            r *= float(ratio)
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="write verified rows")
    ap.add_argument("--limit", type=int, default=0, help="cap candidates (debugging)")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()
    ensure_table(cur)

    verified, unverified, rejected = [], [], []

    # ---- signal 1: ISIN handover ------------------------------------------------
    groups = candidates_isin_handover(cur)
    print(f"ISIN-stem groups: {len(groups)}  (candidates only — fund houses share stems)\n")
    for stem, rows in sorted(groups.items()):
        rows = sorted(rows, key=lambda r: r["a"])
        for old, new in zip(rows, rows[1:]):
            # A real split keeps the company. Different names under one stem are
            # different schemes of one issuer, which is the false positive this
            # check exists to survive.
            n1 = str(old["security_name"]).upper().replace(" ", "")
            n2 = str(new["security_name"]).upper().replace(" ", "")
            same_ish = n1 == n2 or n1.startswith(n2[:5]) or n2.startswith(n1[:5])
            lo = min(old["b"], new["a"]) - timedelta(days=WINDOW_DAYS)
            hi = max(old["b"], new["a"]) + timedelta(days=WINDOW_DAYS)
            ev = yahoo_splits(new["security_name"], lo, hi)
            label = f"{old['isin']} -> {new['isin']}  {old['security_name']}/{new['security_name']}"
            if not same_ish and not ev:
                rejected.append((label, "different schemes of one issuer; no split on Yahoo"))
            elif ev:
                for d, ratio in ev:
                    verified.append((new["id"], new["security_name"], d, ratio,
                                     old["isin"], new["isin"],
                                     f"Yahoo split {ratio:g}x on {d}; ISIN handover {label}"))
            else:
                unverified.append((label, f"names match but Yahoo shows no split in "
                                          f"{lo}..{hi}"))

    # ---- signal 2: oversold -----------------------------------------------------
    ov = candidates_oversold(cur)
    if args.limit:
        ov = ov[:args.limit]
    print(f"oversold (entity,security) pairs: {len(ov)}\n")
    seen = set()
    for r in ov:
        key = r["id"]
        if key in seen:
            continue
        seen.add(key)
        lo, hi = r["a"] - timedelta(days=WINDOW_DAYS), r["b"] + timedelta(days=WINDOW_DAYS)
        ev = yahoo_splits(r["security_name"], lo, hi)
        if ev:
            for d, ratio in ev:
                verified.append((r["id"], r["security_name"], d, ratio, None, r["isin"],
                                 f"Yahoo split {ratio:g}x on {d}; oversold by "
                                 f"{abs(float(r['net'])):,.0f}"))
        else:
            unverified.append((f"{r['security_name']} ({r['isin']})",
                               f"oversold {float(r['net']):,.0f}, no split on Yahoo "
                               f"{lo}..{hi} — transfer-in or missing history"))

    # ---- signal 3: held security with an unrecorded Yahoo split ------------------
    # Verified live against the INDIAN listing (name + .NS/.BO), which both re-confirms
    # the event and drops foreign name-collisions (e.g. Indian 'META' vs US Meta, whose
    # split history belongs to the US line). The window spans the security's whole trade
    # life so every unrecorded event in it is caught; the shared dedup below collapses
    # Yahoo's near-duplicate records (paired ex-dates a few days apart).
    us = candidates_unrecorded_split(cur)
    print(f"held securities with an unrecorded split: {len(us)}\n")
    for r in us:
        sid = r["id"] if isinstance(r, dict) else r[0]
        name = r["security_name"] if isinstance(r, dict) else r[1]
        isin = r["isin"] if isinstance(r, dict) else r[2]
        cur.execute("""SELECT MIN(transaction_date) a, MAX(transaction_date) b
                       FROM stock_transaction WHERE security_id=%s""", (sid,))
        span = cur.fetchone()
        a = span["a"] if isinstance(span, dict) else span[0]
        b = span["b"] if isinstance(span, dict) else span[1]
        if not a:
            continue
        lo, hi = a - timedelta(days=WINDOW_DAYS), b + timedelta(days=WINDOW_DAYS)
        ev = yahoo_splits(name, lo, hi)
        if ev:
            for d, ratio in ev:
                verified.append((sid, name, d, ratio, None, isin,
                                 f"Yahoo split {ratio:g}x on {d}; held, unrecorded"))
        else:
            unverified.append((f"{name} ({isin})",
                               f"security_split present but Indian listing shows no split "
                               f"in {lo}..{hi} — foreign collision or stale split row"))

    # Dedupe. Two separate collapses are needed:
    #   (a) the two signals frequently find the SAME event, keyed exactly;
    #   (b) Yahoo records one event twice a few days apart (HAL 2x on both 2023-09-28
    #       and 09-29; INOXWIND 4x on 05-17 and 05-24). Recording both would apply the
    #       adjustment twice and inflate the position by the square of the ratio, which
    #       is far worse than missing it. Same security + same ratio within a fortnight
    #       is one event; keep the earlier date (the ex-date leads the credit).
    uniq = {}
    for sid, name, d, ratio, oi, ni, why in verified:
        uniq[(sid, d)] = (sid, name, d, ratio, oi, ni, why)

    collapsed, dropped = {}, 0
    for key in sorted(uniq, key=lambda k: (k[0], uniq[k][3], k[1])):
        sid, d = key
        rec = uniq[key]
        prior = collapsed.get((sid, round(rec[3], 4)))
        if prior and abs((d - prior[2]).days) <= 15:
            dropped += 1
            continue
        collapsed[(sid, round(rec[3], 4))] = rec
    uniq = {(r[0], r[2]): r for r in collapsed.values()}
    if dropped:
        print(f"(collapsed {dropped} duplicate Yahoo record(s) of the same event)\n")

    print(f"VERIFIED against Yahoo — {len(uniq)} action(s):")
    for sid, name, d, ratio, oi, ni, why in sorted(uniq.values(), key=lambda x: x[2]):
        kind = "bonus" if abs(ratio - round(ratio)) < RATIO_TOL and ratio >= 2 else "split"
        print(f"  {d}  {str(name)[:22]:<22} {ratio:>7.4f}x  {kind:<6} {why[:70]}")

    print(f"\nUNVERIFIED — {len(unverified)} candidate(s) with no market evidence "
          f"(NOT recorded):")
    for label, why in unverified[:30]:
        print(f"  {str(label)[:44]:<44} {why}")
    if len(unverified) > 30:
        print(f"  ... and {len(unverified)-30} more")

    print(f"\nREJECTED — {len(rejected)} stem match(es) that are unrelated schemes:")
    for label, why in rejected[:15]:
        print(f"  {str(label)[:60]:<60} {why}")

    if not args.commit:
        print("\nDRY RUN — re-run with --commit to record the VERIFIED rows")
        conn.rollback(); return

    n = 0
    for sid, name, d, ratio, oi, ni, why in uniq.values():
        kind = "bonus" if abs(ratio - round(ratio)) < RATIO_TOL and ratio >= 2 else "split"
        cur.execute("""
            INSERT INTO corporate_action
                (security_id, action_type, ex_date, ratio, old_isin, new_isin,
                 source, verified, evidence)
            VALUES (%s,%s,%s,%s,%s,%s,'yfinance',TRUE,%s)
            ON CONFLICT (security_id, ex_date, action_type)
            DO UPDATE SET ratio=EXCLUDED.ratio, evidence=EXCLUDED.evidence,
                          verified=TRUE
        """, (sid, kind, d, ratio, oi, ni, why))
        n += 1
    conn.commit()
    print(f"\nCOMMITTED {n} verified corporate action(s) to corporate_action")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
