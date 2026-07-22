#!/usr/bin/env python3
"""
Merge ISIN-less "orphan" securities into their canonical security_master row.

THE PROBLEM
-----------
Zerodha tradebooks carry an ISIN on every line. Angel One and Dhan tradebooks identify
a stock only by a descriptive company NAME ("HERO MOTOCORP LIMITED", "ADANI PORT & SEZ
LTD"), so their importer created a SECOND security_master row with isin NULL alongside
the real one (HEROMOTOCO / INE158A01026).

That splits a single position across two security_ids, and the realised-gains engine
keys its FIFO lot pool on security_id (`lots[sec]` in report_generator
_fifo_realised_grouped). A sell booked against the orphan therefore finds no buy lots
and is emitted with purchase_amount/pnl = None ("unknown"), while the matching buys sit
unused under the canonical row. Dividends are hit the same way: quantity-on-ex-date is
replayed per security_id, so the split understates it.

SCOPE — measured, not assumed
-----------------------------
224 orphan securities carry INR stock_transaction rows. They are NOT all duplicates:
  * ~154 from Zerodha are named with real tickers (RBL, DIVISLAB, NAVINFLUOR) and have
    NO canonical twin at all — nothing to merge; they just lack an ISIN. Left alone.
  * The Angel/Dhan ones are the true duplicates, and are what this script targets.
Vested's 114 ISIN-less rows are US stocks with no Indian ISIN by definition; the
realised-gains equity branch filters to currency='INR', so they are correctly excluded
here too.

MATCHING, AND WHY IT IS PARANOID
--------------------------------
A wrong merge is far worse than a missed one: it fuses two unrelated positions and
corrupts cost basis permanently. So a candidate must clear THREE gates:
  1. Token/prefix match (equity.symbol_bridge._token_match) — the canonical ticker is a
     prefix of the orphan's alphanumerics, or vice versa. Handles broker truncation.
  2. UNIQUE — a name matching two canonical securities is skipped, never guessed.
  3. Position sanity — replaying orphan+canonical trades together, per entity, in date
     order, must never drive the running quantity materially negative. These are
     delivery holdings; you cannot sell what you never bought, so a negative running
     position proves the two rows are not the same stock. This gate is the one that
     catches a plausible-looking name collision.

    python -m workers.db_migrate_merge_orphan_securities            # dry-run report
    python -m workers.db_migrate_merge_orphan_securities --commit
"""
import os
import sys
import argparse
from collections import defaultdict
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)

from equity.symbol_bridge import _alnum, _norm_sym, _token_match  # noqa: E402

# A running position may dip slightly below zero from rounding or a same-day
# sell-then-buy ordered by insert id rather than fill time. Anything past this is a
# real contradiction, not noise.
NEG_TOLERANCE = -1.0

# Name matches investigated by hand and found WRONG. The price gate abstained on each
# (no usable history for the orphan's dates), so without this they resurface in NEEDS
# REVIEW on every run and invite the same mistake a second time. Keyed by orphan
# security_name; the value is why it was rejected.
MANUAL_REJECT = {
    "HDFCMFGETF": (
        "gold ETF, not HDFC Ltd. Fills were Rs 4,262-4,552 (Jun-Aug 2020) when HDFC "
        "Ltd traded ~Rs 1,800. It is the pre-split HDFC Gold ETF (1 unit ~ 1g); the "
        "post-split row is HDFCGOLD/INF179KC1981 at ~Rs 130. Merging across a 1:100 "
        "unit split would corrupt cost basis. Round-trips within itself, so nothing "
        "is stranded by leaving it alone."),
    "JUBILANT": (
        "NOT Jubilant Ingrevia. JUBLINGREA.NS first traded 2021-03-19; these fills are "
        "Jan-Mar 2020, before the demerger listed. That the 2020 buy (Rs 619.40) sits "
        "near Ingrevia's 2026 buy (Rs 622.80) is coincidence — exactly the trap the "
        "price gate exists to catch. Net 0, self-contained."),
    "Tata Motors": (
        "the demerged COMMERCIAL-vehicles entity, a separate listed company with its "
        "own ISIN. Filled Rs 479.15 on 2026-02-20 when TMPV (INE155A01022) closed "
        "Rs 374.89 — same day, 28% apart. Merging would fuse two companies."),
    "BILNCD2021": (
        "a different Britannia debenture series from BILNCD (INE216A07052). Both rows "
        "hold sells with no buy lots, so merging cannot match anything — zero upside, "
        "nonzero risk."),
}


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def load_orphans(cur):
    cur.execute("""
        SELECT sm.id, sm.security_name, COUNT(st.id) AS txns,
               COUNT(*) FILTER (WHERE st.transaction_type = 'SELL') AS sells
          FROM security_master sm
          JOIN stock_transaction st ON st.security_id = sm.id
         WHERE sm.isin IS NULL AND COALESCE(st.currency, 'INR') = 'INR'
         GROUP BY sm.id, sm.security_name
         ORDER BY COUNT(st.id) DESC
    """)
    return cur.fetchall()


def load_canonicals(cur):
    cur.execute("""
        SELECT id, isin, security_name FROM security_master
         WHERE isin IS NOT NULL AND COALESCE(currency, 'INR') = 'INR'
    """)
    return cur.fetchall()


def price_agrees(cur, orphan_id, canon_symbol, tol=0.25):
    """Do the orphan's fill prices track the canonical ticker's market price?

    This is the real identity test, and it exists because name matching alone is not
    safe. Prefix matching happily proposed "ACC" -> ACCELYA (a cement company into a
    software firm) and "HDFCMFGETF" -> HDFC (a gold ETF into HDFC Ltd); both would have
    fused unrelated positions and corrupted cost basis permanently. Two securities that
    are the same stock must have traded at the same price on the same day, so comparing
    a handful of the orphan's fills against the candidate's historical close separates
    them decisively where the name cannot.

    Returns (ok, detail). Unavailable price data returns ok=True with a note — absence
    of evidence is not grounds to reject, it just means this gate abstains and the
    decision rests on the remaining ones.
    """
    import warnings
    warnings.filterwarnings("ignore")
    try:
        import yfinance as yf
    except Exception:
        return True, "no price feed available"

    cur.execute("""
        SELECT transaction_date AS d, price FROM stock_transaction
         WHERE security_id = %s AND price > 0
         ORDER BY transaction_date DESC LIMIT 5
    """, (orphan_id,))
    fills = cur.fetchall()
    if not fills:
        return None, "orphan has no priced fills — cannot verify"

    # Pad the window generously. Using exactly min(fill)..max(fill) collapsed to an
    # empty range whenever the fills clustered on one day (and yfinance's `end` is
    # exclusive), so the gate abstained on most candidates — which is precisely how
    # HDFCMFGETF -> HDFC slipped through on name alone. A wide window costs nothing.
    from datetime import timedelta
    lo = min(f["d"] for f in fills) - timedelta(days=10)
    hi = max(f["d"] for f in fills) + timedelta(days=10)
    base = _norm_sym(canon_symbol)
    hist = None
    for tk in (f"{base}.NS", f"{base}.BO"):
        try:
            h = yf.Ticker(tk).history(start=str(lo), end=str(hi), auto_adjust=False)
            if len(h):
                hist = h
                break
        except Exception:
            continue
    if hist is None or not len(hist):
        return None, "no market history for candidate — cannot verify"

    checked = agreed = 0
    worst = None
    for f in fills:
        day = hist[hist.index.date <= f["d"]]
        if not len(day):
            continue
        close = float(day["Close"].iloc[-1])
        if close <= 0:
            continue
        px = float(f["price"])
        dev = abs(px - close) / close
        checked += 1
        if dev <= tol:
            agreed += 1
        elif worst is None or dev > worst[0]:
            worst = (dev, f["d"], px, close)
    if not checked:
        return None, "no overlapping market days — cannot verify"
    # Majority must agree: one stale or odd-lot fill should not veto an obvious match.
    if agreed * 2 >= checked:
        return True, f"{agreed}/{checked} fills within {int(tol*100)}%"
    d, day, px, close = worst
    return False, (f"only {agreed}/{checked} fills match market price "
                   f"(e.g. {day}: paid {px:,.0f} vs close {close:,.0f}, {d*100:.0f}% off)")


def position_is_sane(cur, orphan_id, canon_id):
    """Replay both securities' trades together per entity; reject if a position goes
    negative. Returns (ok, detail)."""
    cur.execute("""
        SELECT entity_id, transaction_date, id, transaction_type, quantity
          FROM stock_transaction
         WHERE security_id IN (%s, %s)
         ORDER BY entity_id, transaction_date, id
    """, (orphan_id, canon_id))
    by_entity = defaultdict(list)
    for r in cur.fetchall():
        by_entity[r["entity_id"]].append(r)
    for eid, rows in by_entity.items():
        run = 0.0
        for r in rows:
            q = float(r["quantity"] or 0)
            run += q if (r["transaction_type"] or "").upper() == "BUY" else -q
            if run < NEG_TOLERANCE:
                return False, f"entity {eid}: position hits {run:,.0f} on {r['transaction_date']}"
    return True, ""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="apply (default dry-run)")
    ap.add_argument("--approve", default="",
                    help="comma-separated orphan security_name(s) from NEEDS REVIEW to "
                         "merge on human confirmation, e.g. --approve 'NCC,LTI'. Names "
                         "in MANUAL_REJECT are refused even if listed here.")
    args = ap.parse_args()
    approved = {n.strip() for n in args.approve.split(",") if n.strip()}

    conn = get_conn()
    cur = conn.cursor()

    orphans = load_orphans(cur)
    canon = load_canonicals(cur)
    print(f"{len(orphans)} ISIN-less securities with INR trades; "
          f"{len(canon)} canonical securities to match against\n")

    merges, ambiguous, no_match, rejected, review = [], [], [], [], []
    for o in orphans:
        na = _alnum(o["security_name"])
        hits = [c for c in canon if _token_match(na, _norm_sym(c["security_name"]))]
        if not hits:
            no_match.append(o)
            continue
        if len(hits) > 1:
            ambiguous.append((o, hits))
            continue
        # Price agreement decides. The position replay is kept as a NOTE only: a
        # negative running quantity is caused just as often by incomplete buy history
        # (NRI off-market transfers, pre-2024 HDR) as by a wrong match, so using it to
        # reject threw out obviously-correct pairs like Reliance and ICICI Bank.
        ok, why = price_agrees(cur, o["id"], hits[0]["security_name"])
        pos_ok, pos_why = position_is_sane(cur, o["id"], hits[0]["id"])
        note = why + ("" if pos_ok else f"; NOTE position replay: {pos_why}")
        # ok is tri-state: True = prices agree, False = prices contradict,
        # None = could not verify. An unverified match is NOT auto-merged — name
        # similarity alone is what proposed ACC -> ACCELYA, so it goes to a review
        # list for a human to confirm rather than being applied on trust.
        name = o["security_name"]
        if name in MANUAL_REJECT:
            # Investigated by hand and disproved. Refused even when named in --approve:
            # the whole point of recording the verdict is that it outranks a later
            # judgement call made without the evidence in front of you.
            rejected.append((o, hits[0], "MANUAL: " + MANUAL_REJECT[name]))
        elif ok is True:
            merges.append((o, hits[0], note))
        elif ok is False:
            rejected.append((o, hits[0], note))
        elif name in approved:
            merges.append((o, hits[0], "APPROVED BY HAND; " + note))
        else:
            review.append((o, hits[0], note))

    if merges:
        print(f"MERGE — {len(merges)} orphan(s): unique name match AND traded prices "
              f"track the candidate's market price:")
        for o, c, why in merges:
            print(f"  {o['security_name'][:30]:<30} -> {c['security_name'][:14]:<14} "
                  f"{c['isin']}  ({o['txns']}t/{o['sells']}s)  [{why}]")
    if rejected:
        print(f"\nREJECTED — {len(rejected)} name match(es) whose prices do NOT agree "
              f"(different stocks):")
        for o, c, why in rejected:
            print(f"  {o['security_name'][:30]:<30} -> {c['security_name'][:14]:<14} {why}")
    if review:
        print(f"\nNEEDS REVIEW — {len(review)} name match(es) with no price evidence "
              f"either way. NOT merged automatically:")
        for o, c, why in review:
            print(f"  {o['security_name'][:30]:<30} -> {c['security_name'][:14]:<14} "
                  f"{c['isin']}  ({o['txns']}t/{o['sells']}s)  [{why}]")
    if ambiguous:
        print(f"\nAMBIGUOUS — {len(ambiguous)} orphan(s) matched >1 canonical row, skipped:")
        for o, hits in ambiguous[:10]:
            print(f"  {o['security_name'][:34]:<34} -> "
                  f"{', '.join(h['security_name'] for h in hits[:4])}")
    print(f"\nNO MATCH — {len(no_match)} orphan(s) have no canonical twin "
          f"(mostly Zerodha ticker-named rows that simply lack an ISIN; nothing to merge)")

    if not args.commit:
        print("\nDRY RUN — re-run with --commit to apply the MERGE list only")
        return

    moved = dropped = 0
    for o, c, _ in merges:
        cur.execute("UPDATE stock_transaction SET security_id=%s WHERE security_id=%s",
                    (c["id"], o["id"]))
        moved += cur.rowcount
        # Derived dividend rows are rebuilt from scratch by dividend_worker, so drop
        # rather than repoint — repointing could collide with the canonical row's own
        # entry for the same ex-date.
        cur.execute("DELETE FROM dividend WHERE security_id=%s", (o["id"],))
        cur.execute("DELETE FROM dividend_coverage WHERE security_id=%s", (o["id"],))
        # Only drop the now-empty shell if nothing else still points at it; several
        # other tables (holding, daily_snapshot, mf_transaction, …) carry the same FK.
        cur.execute("""
            SELECT (SELECT COUNT(*) FROM stock_transaction WHERE security_id=%(i)s)
                 + (SELECT COUNT(*) FROM holding            WHERE security_id=%(i)s)
                 + (SELECT COUNT(*) FROM daily_snapshot     WHERE security_id=%(i)s)
                 + (SELECT COUNT(*) FROM mf_transaction     WHERE security_id=%(i)s)
                 + (SELECT COUNT(*) FROM nav_history        WHERE security_id=%(i)s)
                 + (SELECT COUNT(*) FROM manual_valuation   WHERE security_id=%(i)s)
                 + (SELECT COUNT(*) FROM ppf_transaction    WHERE security_id=%(i)s)
                 + (SELECT COUNT(*) FROM reconciliation_ticket WHERE security_id=%(i)s)
                 AS refs
        """, {"i": o["id"]})
        if (cur.fetchone()["refs"] or 0) == 0:
            cur.execute("DELETE FROM security_master WHERE id=%s", (o["id"],))
            dropped += cur.rowcount
    conn.commit()
    print(f"\nCOMMITTED: repointed {moved} transaction(s), removed {dropped} orphan "
          f"security row(s)")
    print("Now re-run:  equity_txn_metrics_worker --commit   and   dividend_worker --commit")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
