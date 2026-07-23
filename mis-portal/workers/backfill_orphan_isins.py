#!/usr/bin/env python3
"""
Backfill ISINs onto ISIN-less Angel/Dhan "orphan" securities using the Dhan scrip
master (the one broker feed that carries ISIN — see equity/isin_lookup.py).

WHY, precisely
--------------
Zerodha tradebooks carry an ISIN per line; Angel One and Dhan identify a stock only
by a symbol/name, so their importer created security_master rows with isin NULL. The
paranoid name-merge tool (db_migrate_merge_orphan_securities) already fused the ones
that had a canonical ISIN twin it could match by name. What is left is 150-odd rows
whose NAME is itself a real NSE/BSE ticker (RBZJEWEL, MARICO, IGL) but which simply
never carry an ISIN. Reversing the Dhan map (symbol -> ISIN) resolves most of them.

Two outcomes, and they are NOT the same operation:
  * ASSIGN — the resolved ISIN has no existing canonical row. The orphan just lacks
    a label: stamp the ISIN on it and it becomes a proper security. Realised gains and
    dividends already worked for these (report_generator keys FIFO on security_id, and
    the dividend worker falls back to security_name as the ticker), so the real win is
    that broker HOLDINGS syncs — which match on ISIN — stop forking a fresh duplicate
    every time the position reappears.
  * MERGE — the resolved ISIN matches an existing canonical row. That means the name
    matcher MISSED a genuine duplicate (e.g. a renamed ticker like ETERNAL=Zomato).
    Repoint its trades into the canonical, exactly like the merge tool.

SAFETY — same price gate as the merge tool
------------------------------------------
A ticker can be REASSIGNED to a different company over time (the Dhan map only knows
today's mapping), so a sold-out orphan's name could resolve to the wrong ISIN. So every
proposed change — assign OR merge — is put through equity price agreement first
(db_migrate_merge_orphan_securities.price_agrees): the orphan's own fills must track the
resolved ticker's market close on those days. Pass -> apply; contradict -> REJECT;
no evidence (delisted / no priced fills) -> REVIEW, never applied on trust. A resolved
ISIN shared by two orphans (name collision) is also parked in REVIEW rather than guessed.

    python -m workers.backfill_orphan_isins            # dry-run report
    python -m workers.backfill_orphan_isins --commit
    # then, as the merge tool also advises:
    #   python -m workers.equity_txn_metrics_worker --commit
    #   python workers/dividend_worker.py --commit
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from equity import isin_lookup  # noqa: E402
from workers.db_migrate_merge_orphan_securities import (  # noqa: E402
    get_conn, price_agrees,
)

# ── Human-verified overrides (--manual pass) ───────────────────────────────────
# orphan security_name -> ISIN, hand-researched for the rows the automatic Dhan pass
# could not resolve or price-gated away (renames, demergers, delistings, stock splits
# that moved the price, sovereign gold bonds). ISINs are ERA-APPROPRIATE: a rename
# keeps the ISIN, but a face-value split MINTS A NEW ONE, so a row that traded before
# the split gets the OLD ISIN (Shriram Transport -> INE721A01013, not the post-split
# INE721A01047 — merging across that would corrupt cost basis, the HDFCMFGETF lesson).
# Entries that resolve to an existing canonical are MERGED; the rest are ASSIGNED, and
# truncated spellings sharing one ISIN collapse together. Deliberately ABSENT (left
# untouched): the trap rows (JUBILANT, BILNCD2021, HDFCMFGETF, "Tata Motors"=TMPV,
# "HDFC Gold ETF" ambiguous, "CANARA BANK" two live ISINs) and rows whose ISIN could
# not be pinned down (Advent Hotels, Majesco, Inox Leisure, IBULPP, four ICICI ETFs).
MANUAL_ISIN = {
    # --- split-blocked: exact ticker, ISIN authoritative, only the price moved (a split) ---
    "LALPATHLAB": "INE600L01024", "APTECHT": "INE266F01018", "ALLCARGO": "INE418H01029",
    "VSTIND": "INE710A01016", "IGL": "INE203G01027", "IRCTC": "INE335Y01020",
    "TATAINVEST": "INE672A01026", "TORNTPHARM": "INE685A01028", "IPCALAB": "INE571A01038",
    "APLAPOLLO": "INE702C01027", "SARDAEN": "INE385C01021", "BHARATRAS": "INE838B01021",
    "RATNAMANI": "INE703B01027", "TTKPRESTIG": "INE690A01028", "VSSL": "INE050M01012",
    # --- sovereign gold bonds (no equity price feed; assign the SGB ISIN) ---
    "SGBJUL28IV": "IN0020200146", "SGBJUN28": "IN0020200104",
    # --- renamed / delisted equities (ISIN preserved across the rename) ---
    "RBL": "INE976G01028", "MCDOWELL-N": "INE854D01024", "MOTHERSUMI": "INE775A01035",
    "GDL": "INE079J01017", "INFIBEAM": "INE483S01020", "GUJGASLTD": "INE844O01030",
    "TINPLATE": "INE422C01014", "IIFLWAM": "INE466L01038", "MAHINDCIE": "INE536H01010",
    "MINDAIND": "INE405E01023", "ADANITRANS": "INE931S01010", "PEL": "INE140A01024",
    "KALPATPOWR": "INE220B01022", "DEEPAKNI": "INE288B01029", "TATAMETALI": "INE056C01010",
    "CADILAHC": "INE010B01027", "SRTRANSFIN": "INE721A01013", "GMM": "INE541A01023",
    "DFMFOODS": "INE456C01020", "NALCO": "INE139A01034", "TV18BRDCST": "INE886H01027",
    "RNAM": "INE298J01013", "IBREALEST": "INE069I01010",
    # --- Kotak Gold ETF (two spellings) ---
    "KOTAKGOLD": "INF174KA1HJ8", "Kotak Gold ETF": "INF174KA1HJ8",
    # --- ICICI Prudential ETFs (old tickers) that resolved cleanly ---
    "ICICINIFTY": "INF109K012R6", "ICICINXT50": "INF109KC1NS5", "ICICIBANKN": "INF109KC15I8",
    # --- company-name rows with a canonical or a known ISIN ---
    "INDIA SHELTER FIN CORP L": "INE922K01024", "INDIA SHELTER FIN CO": "INE922K01024",
    "KALYANI INVEST CO LTD": "INE029L01018", "KALYANI INVEST CO LT": "INE029L01018",
    "TIRUPATI FORGE LIMITED": "INE238Y01018", "KALYAN JEWELLERS IND LTD": "INE303R01014",
    "BALU FORGE INDUSTRIES LTD": "INE011E01029", "NMDC STEEL LIMITED": "INE0NNS01018",
    "MOIL LIMITED": "INE490G01020", "RACL Geartech": "INE704B01017",
    "V MARC INDIA LIMITED": "INE0GXK01018", "ASIAN HOTELS (NORTH)": "INE363A01022",
    "Patel Engineering": "INE244B01030", "Mirza International": "INE0LXT01019",
    "National Securities": "INE301O01023", "INTERNATIO GEMM INS": "INE0Q9301021",
    "STRING METAVERSE LIMITED": "INE958L01026",
}

# Same broker exchange-series suffixes the symbol bridge strips (Angel's -EQ, BSE -BE…).
_SUFFIX = re.compile(r"-(EQ|BE|BZ|ST|SM|GB|N\d*)$", re.I)


def strip_suffix(s: str) -> str:
    return _SUFFIX.sub("", (s or "").strip().upper())


def build_sym2isin() -> dict:
    """Reverse the Dhan ISIN->'EXCH:SYMBOL' map into SYMBOL->ISIN (NSE preferred, since
    get_map already lets NSE win, and dict insertion keeps the first seen per symbol)."""
    out: dict = {}
    for isin, es in isin_lookup.get_map().items():
        sym = es.split(":", 1)[1]
        out.setdefault(sym, isin)
    return out


def load_orphans(cur, ticker_only=True):
    """ISIN-less INR securities. The automatic pass restricts to ticker-shaped names
    (`ticker_only`), since only those can match the Dhan symbol map; the manual pass
    passes ticker_only=False so its hand-entered company-name overrides are found too."""
    name_filter = "AND sm.security_name !~ '[a-z ]'" if ticker_only else ""
    cur.execute(f"""
        SELECT sm.id, sm.security_name, COUNT(st.id) AS txns,
               COUNT(*) FILTER (WHERE st.transaction_type = 'SELL') AS sells
          FROM security_master sm
          JOIN stock_transaction st ON st.security_id = sm.id
         WHERE sm.isin IS NULL AND COALESCE(st.currency, 'INR') = 'INR'
           {name_filter}
         GROUP BY sm.id, sm.security_name
         ORDER BY COUNT(st.id) DESC
    """)
    return cur.fetchall()


def canon_by_isin(cur):
    cur.execute("""
        SELECT id, isin, security_name FROM security_master
         WHERE isin IS NOT NULL AND COALESCE(currency, 'INR') = 'INR'
    """)
    return {r["isin"]: r for r in cur.fetchall()}


def repoint_corporate_actions(cur, orphan_id, canon_id):
    """Corporate actions (splits/bonuses) belong to the stock, so move the orphan's
    to the canonical — otherwise the FK from corporate_action blocks dropping the
    shell. Guard the (security_id, ex_date, action_type) unique key: repoint only the
    rows the canonical doesn't already record, then drop any leftover duplicate."""
    cur.execute("""
        UPDATE corporate_action ca SET security_id=%s
         WHERE ca.security_id=%s
           AND NOT EXISTS (SELECT 1 FROM corporate_action c2
                            WHERE c2.security_id=%s AND c2.ex_date=ca.ex_date
                              AND c2.action_type=ca.action_type)
    """, (canon_id, orphan_id, canon_id))
    cur.execute("DELETE FROM corporate_action WHERE security_id=%s", (orphan_id,))


def drop_shell_if_unreferenced(cur, sid):
    """Remove a now-empty orphan row only when NOTHING else still points at it.
    Every table with a FK to security_master is counted (an uncounted one aborts the
    whole transaction on the DELETE — corporate_action did exactly that for BEL)."""
    cur.execute("""
        SELECT (SELECT COUNT(*) FROM stock_transaction WHERE security_id=%(i)s)
             + (SELECT COUNT(*) FROM holding            WHERE security_id=%(i)s)
             + (SELECT COUNT(*) FROM daily_snapshot     WHERE security_id=%(i)s)
             + (SELECT COUNT(*) FROM mf_transaction     WHERE security_id=%(i)s)
             + (SELECT COUNT(*) FROM nav_history        WHERE security_id=%(i)s)
             + (SELECT COUNT(*) FROM manual_valuation   WHERE security_id=%(i)s)
             + (SELECT COUNT(*) FROM ppf_transaction    WHERE security_id=%(i)s)
             + (SELECT COUNT(*) FROM reconciliation_ticket WHERE security_id=%(i)s)
             + (SELECT COUNT(*) FROM corporate_action   WHERE security_id=%(i)s)
             AS refs
    """, {"i": sid})
    if (cur.fetchone()["refs"] or 0) == 0:
        cur.execute("DELETE FROM security_master WHERE id=%s", (sid,))
        return cur.rowcount
    return 0


def _merge_into(cur, orphan_id, target_id):
    """Repoint an orphan's trades into target, moving derived/CA rows, then drop shell."""
    cur.execute("UPDATE stock_transaction SET security_id=%s WHERE security_id=%s",
                (target_id, orphan_id))
    moved = cur.rowcount
    cur.execute("DELETE FROM dividend WHERE security_id=%s", (orphan_id,))
    cur.execute("DELETE FROM dividend_coverage WHERE security_id=%s", (orphan_id,))
    repoint_corporate_actions(cur, orphan_id, target_id)
    dropped = drop_shell_if_unreferenced(cur, orphan_id)
    return moved, dropped


def apply_manual(conn, cur, commit):
    """Apply the hand-verified MANUAL_ISIN map. Grouped by target ISIN so truncated
    spellings collapse: if a canonical row already has the ISIN, everything merges into
    it; otherwise the ISIN is assigned to the first spelling and the siblings merge into
    that. No price gate — these are human-verified identities (that's the whole point of
    the manual list), and the ISINs are era-appropriate so no merge crosses a split."""
    orphans = {o["security_name"]: o for o in load_orphans(cur, ticker_only=False)}
    canon = canon_by_isin(cur)
    groups = defaultdict(list)
    for name, isin in MANUAL_ISIN.items():
        if name in orphans:
            groups[isin].append(orphans[name])
    missing = [n for n in MANUAL_ISIN if n not in orphans]

    plan = []          # (op, orphan_name, target_desc)
    skipped = []       # (orphan_name, target, why) — merges the price gate held back
    assigned = merged = moved = dropped = 0
    for isin, os_ in groups.items():
        c = canon.get(isin)
        if c:                                   # merge every spelling into the canonical
            # Merging fuses cost bases, so — unlike an assign — re-run the price gate
            # against the current ticker. A face-value split mints a new ISIN, so if the
            # orphan's ISIN still equals a live canonical's yet the prices disagree, the
            # two are on opposite sides of a split and MUST NOT be fused (skip instead).
            ticker = (isin_lookup.symbol_for_isin(isin) or "").split(":", 1)[-1] or c["security_name"]
            for o in os_:
                ok, why = price_agrees(cur, o["id"], ticker)
                if ok is False:
                    skipped.append((o["security_name"], f"{c['security_name']} {isin}", why))
                    continue
                plan.append(("MERGE", o["security_name"], f"{c['security_name']} {isin}"))
                if commit:
                    m, d = _merge_into(cur, o["id"], c["id"]); moved += m; dropped += d; merged += 1
        else:                                   # assign to the first, merge siblings into it
            primary = os_[0]
            plan.append(("ASSIGN", primary["security_name"], f"{isin} (new)"))
            if commit:
                cur.execute("SELECT 1 FROM security_master WHERE isin=%s", (isin,))
                if cur.fetchone():
                    print(f"  SKIP assign {primary['security_name']}: {isin} already in use")
                else:
                    cur.execute("UPDATE security_master SET isin=%s WHERE id=%s", (isin, primary["id"]))
                    assigned += 1
            for o in os_[1:]:
                plan.append(("MERGE", o["security_name"], f"{primary['security_name']} (sibling {isin})"))
                if commit:
                    m, d = _merge_into(cur, o["id"], primary["id"]); moved += m; dropped += d; merged += 1

    for op, name, desc in plan:
        print(f"  {op:<7} {name[:28]:<28} -> {desc}")
    if skipped:
        print(f"\nSKIPPED — {len(skipped)} merge(s) the price gate held back "
              f"(likely a split-crossing ISIN; left as orphans):")
        for name, target, why in skipped:
            print(f"  {name[:28]:<28} -> {target}  {why}")
    n_assign = sum(1 for p in plan if p[0] == "ASSIGN")
    print(f"\n{len(groups)} ISIN group(s): {n_assign} assign, {len(plan) - n_assign} merge, "
          f"{len(skipped)} skipped; {len(missing)} override name(s) not present")
    if not commit:
        print("\nDRY RUN — re-run with --manual --commit to apply")
        return
    conn.commit()
    print(f"\nCOMMITTED (manual): assigned {assigned}, merged {merged} "
          f"({moved} txns repointed, {dropped} shell(s) removed)")


def main():
    ap = argparse.ArgumentParser(description="Backfill ISINs on ticker-named orphan securities.")
    ap.add_argument("--commit", action="store_true", help="apply (default dry-run)")
    ap.add_argument("--manual", action="store_true",
                    help="apply the hand-verified MANUAL_ISIN overrides instead of the "
                         "automatic Dhan+price-gate pass")
    args = ap.parse_args()

    if args.manual:
        conn = get_conn(); cur = conn.cursor()
        apply_manual(conn, cur, args.commit)
        cur.close(); conn.close()
        return

    conn = get_conn()
    cur = conn.cursor()

    sym2isin = build_sym2isin()
    orphans = load_orphans(cur)
    canon = canon_by_isin(cur)
    print(f"{len(orphans)} ticker-named ISIN-less orphans; Dhan master has "
          f"{len(sym2isin)} symbols; {len(canon)} canonical ISINs to match against\n")

    # Resolve, then group by target ISIN so a symbol two orphans share is spotted.
    resolved = []          # (orphan, isin)
    noresolve = []
    for o in orphans:
        isin = sym2isin.get(strip_suffix(o["security_name"]))
        (resolved if isin else noresolve).append((o, isin) if isin else o)
    by_isin = defaultdict(list)
    for o, isin in resolved:
        by_isin[isin].append(o)

    assign, merge, review, reject, collision = [], [], [], [], []
    for isin, os_ in by_isin.items():
        canon_row = canon.get(isin)
        symname = (isin_lookup.symbol_for_isin(isin) or "").split(":", 1)[-1] or None
        if len(os_) > 1 and not canon_row:
            # Two ISIN-less rows resolving to one NEW ISIN — a genuine duplicate pair,
            # but which becomes the keeper is a judgement call; park for a human.
            collision.append((os_, isin, symname))
            continue
        for o in os_:
            # Always price-check against the Dhan-resolved CURRENT symbol — it is a real
            # NSE/BSE ticker, whereas a canonical row's stored security_name is sometimes
            # a descriptive name ("BHARAT ELECTRONICS L") that Yahoo can't resolve, which
            # made obvious ISIN matches (BEL, ETERNAL) needlessly abstain.
            ok, why = price_agrees(cur, o["id"], symname or (canon_row and canon_row["security_name"]))
            if ok is True:
                (merge if canon_row else assign).append((o, canon_row or {"isin": isin, "security_name": symname}, why))
            elif ok is False:
                reject.append((o, canon_row or {"isin": isin, "security_name": symname}, why))
            else:
                review.append((o, canon_row or {"isin": isin, "security_name": symname}, why))

    if assign:
        print(f"ASSIGN — {len(assign)} orphan(s): resolved to a NEW ISIN, prices agree:")
        for o, c, why in assign:
            print(f"  {o['security_name'][:16]:<16} = {c['isin']}  ({o['txns']}t/{o['sells']}s)  [{why}]")
    if merge:
        print(f"\nMERGE — {len(merge)} orphan(s): ISIN matches a canonical (missed duplicate), prices agree:")
        for o, c, why in merge:
            print(f"  {o['security_name'][:16]:<16} -> {c['security_name'][:14]:<14} {c['isin']}  ({o['txns']}t/{o['sells']}s)  [{why}]")
    if reject:
        print(f"\nREJECTED — {len(reject)}: resolved ISIN's prices do NOT match the orphan's fills:")
        for o, c, why in reject:
            print(f"  {o['security_name'][:16]:<16} -> {c.get('security_name'):<14} {c['isin']}  {why}")
    if review:
        print(f"\nNEEDS REVIEW — {len(review)}: no price evidence either way, not applied:")
        for o, c, why in review:
            print(f"  {o['security_name'][:16]:<16} -> {c.get('security_name'):<14} {c['isin']}  ({o['txns']}t/{o['sells']}s)  [{why}]")
    if collision:
        print(f"\nCOLLISION — {len(collision)}: >1 orphan resolves to one new ISIN, skipped:")
        for os_, isin, symname in collision:
            print(f"  {isin} ({symname}): {', '.join(o['security_name'] for o in os_)}")
    print(f"\nNO RESOLVE — {len(noresolve)} orphan(s) not in the Dhan master "
          f"(renamed/delisted tickers, SME/old ETF names)")

    if not args.commit:
        print("\nDRY RUN — re-run with --commit to apply ASSIGN + MERGE")
        cur.close(); conn.close()
        return

    assigned = moved = dropped = 0
    for o, c, _ in assign:
        # Guard: never create a second row for an ISIN already in use.
        cur.execute("SELECT 1 FROM security_master WHERE isin=%s", (c["isin"],))
        if cur.fetchone():
            print(f"  SKIP assign {o['security_name']}: {c['isin']} now in use")
            continue
        cur.execute("UPDATE security_master SET isin=%s WHERE id=%s", (c["isin"], o["id"]))
        assigned += cur.rowcount
    for o, c, _ in merge:
        cur.execute("UPDATE stock_transaction SET security_id=%s WHERE security_id=%s",
                    (c["id"], o["id"]))
        moved += cur.rowcount
        # Dividends are rebuilt from scratch by dividend_worker; drop to avoid an
        # ex-date collision with the canonical row's own computed entry.
        cur.execute("DELETE FROM dividend WHERE security_id=%s", (o["id"],))
        cur.execute("DELETE FROM dividend_coverage WHERE security_id=%s", (o["id"],))
        repoint_corporate_actions(cur, o["id"], c["id"])
        dropped += drop_shell_if_unreferenced(cur, o["id"])
    conn.commit()
    print(f"\nCOMMITTED: assigned {assigned} ISIN(s); merged {len(merge)} "
          f"({moved} txns repointed, {dropped} shell(s) removed)")
    print("Now re-run:  equity_txn_metrics_worker --commit   and   dividend_worker --commit")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
