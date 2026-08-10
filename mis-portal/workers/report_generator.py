#!/usr/bin/env python3
"""
Report generator — produces per-entity and combined portfolio Excel reports.
Call generate_reports(conn, generated_by_user_id) to create reports.
"""
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from collections import defaultdict, deque

# Project root on sys.path so `workers.` / `equity.` resolve when this file is run
# DIRECTLY (cron_wrapper spawns `python workers/report_generator.py` as a fresh
# subprocess, which has neither the wrapper's sys.path nor a package context).
# Without this the scheduled run dies on ModuleNotFoundError before it starts.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2.extras import RealDictCursor
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# Split/bonus lot adjustment. Imported lazily-safe: this module is also used in
# contexts where the corporate_action table may not exist yet, and load_actions()
# returns {} in that case rather than raising.
from workers.corporate_actions import (  # noqa: E402
    load_actions as _ca_load, apply_actions as _ca_apply,
)
# Shared gold/silver/commodity classifier — the same one that tags equity_holding
# and drives the Gold/Silver page, so realised gains bucket the way holdings do.
from equity.asset_class import (  # noqa: E402
    classify_asset_class as _ac_classify, load_overrides as _ac_overrides,
    GOLD_SILVER_COMMODITY as _AC_COMMODITY,
)

REPORTS_DIR = "/var/www/mis-portal/reports"

# Canonical master template — every generated workbook is cloned from this so the output
# is formatting-identical to the client's MIS-REPORT.xlsx.
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "report_template.xlsx")

# Names of the per-group template sheets inside TEMPLATE_PATH that we clone from.
WEEKLY_TPL_SHEET   = "Dhruv Weekly Report"
REALISED_TPL_SHEET = "FY2627 Realised Profit & Loss"
SHARED_SHEETS      = ["Equity Daily Print", "All Assets Daily MIS", "All Entities Weekly Report"]

# PAN groups that bundle more than one entity. label -> member entity codes.
# (Membership is informational for now; data population is a follow-up — the cloned sheet
#  already carries the group's hardcoded fund rows.)  Standalone entities (SDR, Rajani Corp)
#  get only their own per-entity sheet.
PAN_GROUPS = {
    "Dhruv Group": ["DHR", "ADR", "IWS"],
    "Harsh Group": ["HHR", "IWS Fincorp"],
}

# ── colour palette ────────────────────────────────────────────────────────────
HDR_FILL   = PatternFill("solid", fgColor="1F3864")   # dark navy
ENT_FILL   = PatternFill("solid", fgColor="2E4B8A")   # entity header (lighter navy)
SUB_FILL   = PatternFill("solid", fgColor="2F5496")   # col headers
SEC_FILL   = PatternFill("solid", fgColor="D6E4F0")   # section header
TOT_FILL   = PatternFill("solid", fgColor="BDD7EE")   # total row
ALT_FILL   = PatternFill("solid", fgColor="F2F7FC")   # alternating row
WHITE_FILL = PatternFill("solid", fgColor="FFFFFF")

HDR_FONT   = Font(name="Calibri", bold=True, color="FFFFFF", size=12)
ENT_FONT   = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
SUB_FONT   = Font(name="Calibri", bold=True, color="FFFFFF", size=9)
SEC_FONT   = Font(name="Calibri", bold=True, color="1F3864", size=9)
TOT_FONT   = Font(name="Calibri", bold=True, color="1F3864", size=9)
BODY_FONT  = Font(name="Calibri", size=9)
LABEL_FONT = Font(name="Calibri", italic=True, size=8, color="595959")

THIN         = Side(style="thin",   color="BDD7EE")
MED          = Side(style="medium", color="2F5496")
THIN_BORDER  = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

INR_FMT  = '₹#,##0.00'
PCT_FMT  = '0.00%'
DATE_FMT = 'DD-MMM-YYYY'


# ── DB helpers ────────────────────────────────────────────────────────────────

def _fetch_entities(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, entity_name FROM entity ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_equity_daily_data(conn, entity_ids: list):
    """Fetch equity holdings with quantity, avg_cost, current_price for Equity Daily Print."""
    cur = conn.cursor()
    placeholders = ",".join(["%s"] * len(entity_ids))
    cur.execute(f"""
        SELECT
            eh.entity_id, e.entity_name,
            COALESCE(eh.symbol_override, eh.symbol) AS symbol,
            eh.isin, eh.broker,
            eh.quantity, eh.avg_cost,
            eh.cost, eh.current_price, eh.current_market_value,
            eh.pnl_inception, eh.returns_inception_pct
        FROM equity_holding eh
        JOIN entity e ON e.id = eh.entity_id
        WHERE eh.entity_id IN ({placeholders})
        ORDER BY eh.entity_id, eh.current_market_value DESC NULLS LAST
    """, entity_ids)
    rows = cur.fetchall()
    cur.close()
    return rows


def _merge_edp_rows(rows: list, cross_entity: bool = False) -> list:
    """
    Merge same-ISIN holdings. If cross_entity=False, merges within each entity.
    If cross_entity=True, merges across all entities (for grand total).
    Returns list sorted by current_market_value desc.
    """
    groups: dict = defaultdict(list)
    for r in rows:
        isin = (r.get("isin") or "").strip()
        sym  = r.get("symbol") or ""
        if cross_entity:
            key = isin if isin else sym
        else:
            key = (r["entity_id"], isin if isin else sym)
        groups[key].append(r)

    merged = []
    for key, rrows in groups.items():
        if len(rrows) == 1:
            merged.append(dict(rrows[0]))
            continue

        def _fsum(k):
            return sum(float(r[k]) for r in rrows if r.get(k) is not None)

        qty     = _fsum("quantity")
        cost    = _fsum("cost")
        cmv     = _fsum("current_market_value")
        pnl     = _fsum("pnl_inception")
        avg_c   = cost / qty if qty else None
        ret_inc = pnl / cost * 100 if cost else None
        cur_p   = next((float(r["current_price"]) for r in rrows if r.get("current_price")), None)

        m = dict(rrows[0])
        m.update({
            "quantity":             qty,
            "avg_cost":             avg_c,
            "cost":                 cost,
            "current_price":        cur_p,
            "current_market_value": cmv,
            "pnl_inception":        pnl,
            "returns_inception_pct": ret_inc,
        })
        merged.append(m)

    return sorted(merged, key=lambda r: -(float(r.get("current_market_value") or 0)))


# ---------------------------------------------------------------------------
# The "Mkt Value 31-Mar" column
#
# `holding.market_value_as_on` / `equity_holding.market_value_as_on` is NOT a 31-March
# anchor despite the column header: every writer sets it to the CURRENT market value
# (equity/models.py — "same as current_market_value; date stored separately"), so it
# was identical to the Current Mkt Value column on 100% of rows in both tables. Two
# columns, one number, one of them lying about which date it belongs to.
#
# So the reports derive the real thing here instead, from the same 31-March anchors the
# FY-growth columns use (security_price_history / nav_history, backfilled by
# fy_price_backfill). Where no anchor exists — an SME board Yahoo doesn't carry, a
# holding bought after 31-March, a manual entry with no price series — the cell is left
# BLANK rather than back-filled with today's value, which is the whole point.
# ---------------------------------------------------------------------------

def _feed_staleness(conn, as_of: date, entity_id: Optional[int] = None) -> list[str]:
    """Per-feed 'as of' where it is BEHIND the report date, as ['IBKR 05-Aug (4d)', …].

    Every sheet is stamped "As on <today>", but the feeds behind it do not all reach
    today: a throttled IBKR Flex pull serves a days-old statement, DBS is a weekly CSV
    someone has to upload, and a dead token freezes an entity indefinitely. Stamping the
    report date over all of that is how a 26-day-old position came to read as current.
    So the sheets now carry the real vintage of anything lagging, and say nothing when
    every feed is current.

    Reads as_of_date, which is the one column that means "the data is from this date"
    (updated_at only means "a worker touched the row").
    """
    # Measured against the last TRADING day, not the calendar date: a report run on a
    # Sunday finds every market feed showing Friday, which is current, not stale. Only
    # a feed that missed the last session is worth a warning.
    last_session = as_of
    while last_session.weekday() >= 5:            # 5=Sat, 6=Sun
        last_session -= timedelta(days=1)
    scope = " AND entity_id = %(eid)s" if entity_id else ""
    params = {"as_of": last_session, "eid": entity_id}
    cur = conn.cursor()
    out: list[str] = []
    try:
        # MIN, not MAX, at every level: this is a warning, so a feed is only as fresh as
        # its stalest entity. Taking the max let DHR's current IBKR pull hide SDR's,
        # which a dead Flex token had frozen 26 days earlier — exactly the kind of
        # masking this note exists to prevent.
        cur.execute(f"""
            SELECT label, MIN(d) AS d FROM (
                SELECT UPPER(broker) AS label, MIN(as_of_date) AS d
                  FROM foreign_equity_holding WHERE as_of_date IS NOT NULL {scope}
                 GROUP BY broker
                UNION ALL
                SELECT UPPER(broker), MIN(as_of_date)
                  FROM equity_holding WHERE as_of_date IS NOT NULL {scope}
                 GROUP BY broker
                UNION ALL
                SELECT 'MUTUAL FUNDS', MIN(as_of_date)
                  FROM holding WHERE as_of_date IS NOT NULL {scope}
            ) f
            WHERE d < %(as_of)s
            GROUP BY label ORDER BY MIN(d), label
        """, params)
        for r in cur.fetchall():
            lag = (as_of - r["d"]).days       # reported against the real report date
            out.append(f"{r['label']} {r['d'].strftime('%d-%b')} ({lag}d)")
    except Exception:
        conn.rollback()
    finally:
        cur.close()
    return out


def _staleness_note(conn, as_of: date, entity_id: Optional[int] = None) -> str:
    """One-line suffix for a sheet title; empty string when every feed is current."""
    stale = _feed_staleness(conn, as_of, entity_id)
    return ("   |   DATA BEHIND THIS DATE: " + ", ".join(stale)) if stale else ""


def _fy_mar31_for(as_of: Optional[date] = None) -> date:
    """31-Mar opening the current FY. Defined here (not via _fy_mar31, declared later)
    so the fetch helpers can default it without a forward reference."""
    d = as_of or date.today()
    return date(d.year if d.month >= 4 else d.year - 1, 3, 31)


def _fetch_mf_holdings(conn, entity_id: Optional[int] = None):
    """Fetch MF holdings. DB security_type values: MF_DEBT, MF_EQUITY, MF_HYBRID."""
    cur = conn.cursor()
    mar31 = _fy_mar31_for()
    q = """
        SELECT
            h.entity_id, e.entity_name,
            sm.security_name, sm.asset_class, sm.security_type,
            h.invested_amount   AS cost,
            h.current_value,
            h.prev_week_value,
            -- Units held now, valued at the FY-opening NAV. 31-Mar is routinely a
            -- holiday, so take the last NAV in a short window before it.
            (SELECT nh.nav * h.quantity FROM nav_history nh
              WHERE nh.security_id = h.security_id
                AND nh.nav_date <= %(mar31)s AND nh.nav_date >= %(mar31)s - 10
              ORDER BY nh.nav_date DESC LIMIT 1)   AS market_value_as_on,
            h.pnl_ytd, h.pnl_inception,
            h.returns_ytd_pct, h.returns_inception_pct, h.cagr_inception_pct,
            h.xirr_inception_pct,
            h.first_invested_date,
            h.weekly_change, h.exposure_pct, h.remarks
        FROM holding h
        JOIN entity e  ON e.id  = h.entity_id
        JOIN security_master sm ON sm.id = h.security_id
        {where}
        ORDER BY sm.asset_class, sm.security_name
    """
    # quantity > 0 — a with-zero-balance CAS carries every fully-exited scheme as a
    # zero-quantity holding (332 of them across the book) purely so the closed-folio
    # history reaches mf_transaction for realised gains. They are not positions and
    # must not be listed as holdings in the report.
    if entity_id:
        cur.execute(q.format(where="WHERE h.quantity > 0 AND h.entity_id = %(eid)s"),
                    {"mar31": mar31, "eid": entity_id})
    else:
        cur.execute(q.format(where="WHERE h.quantity > 0"), {"mar31": mar31})
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_equity_holdings(conn, entity_id: Optional[int] = None):
    """Domestic direct-equity holdings (equity_holding). Foreign brokers live in
    foreign_equity_holding — see _fetch_foreign_equity_holdings — and are reported
    in their own section so totals stay whole without mixing the two."""
    cur = conn.cursor()
    mar31 = _fy_mar31_for()
    q = """
        SELECT
            eh.entity_id, e.entity_name,
            eh.broker, COALESCE(eh.symbol_override, eh.symbol) AS symbol, eh.isin,
            eh.cost, eh.current_market_value AS current_value,
            eh.prev_week_value,
            -- Shares held now, valued at the FY-opening close (see the note above
            -- _fetch_mf_holdings). security_symbol_map resolves the broker ticker to
            -- the Yahoo one the anchors are stored under.
            (SELECT sph.close * eh.quantity
               FROM security_symbol_map m
               JOIN security_price_history sph ON sph.yahoo_symbol = m.resolved_symbol
              WHERE m.symbol = eh.symbol AND sph.price_date = %(mar31)s) AS market_value_as_on,
            eh.pnl_ytd, eh.pnl_inception,
            eh.returns_ytd_pct, eh.returns_inception_pct, eh.cagr_inception_pct,
            eh.first_invested_date,
            eh.weekly_change, eh.exposure_pct, eh.remarks
        FROM equity_holding eh
        JOIN entity e ON e.id = eh.entity_id
        WHERE COALESCE(eh.asset_class, 'equity') NOT IN ('gold', 'silver', 'commodity')
        {ent}
        ORDER BY eh.symbol
    """
    if entity_id:
        cur.execute(q.format(ent="AND eh.entity_id = %(eid)s"),
                    {"mar31": mar31, "eid": entity_id})
    else:
        cur.execute(q.format(ent=""), {"mar31": mar31})
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_foreign_equity_holdings(conn, entity_id: Optional[int] = None):
    """Foreign (multi-currency) direct-equity holdings (foreign_equity_holding).
    Returns the INR-converted columns used by the by-broker rollup AND the native
    currency columns used by the Foreign Equity Print detail sheet."""
    cur = conn.cursor()
    mar31 = _fy_mar31_for()
    q = """
        SELECT
            eh.entity_id, e.entity_name,
            eh.broker, COALESCE(eh.symbol_override, eh.symbol) AS symbol, eh.isin, eh.exchange,
            eh.cost, eh.current_market_value AS current_value,
            eh.prev_week_value,
            -- Shares held now at the FY-opening close, converted at the FY-opening FX
            -- rate — the column is in ₹, so a year's currency move belongs in it.
            -- symbol_override IS the Yahoo ticker for foreign rows (a bare LSE/SIX
            -- ticker collides with a US fund), so no symbol map is involved.
            (SELECT sph.close * eh.quantity * COALESCE(fx.rate, 1)
               FROM security_price_history sph
               LEFT JOIN LATERAL (
                    SELECT r.rate FROM fx_rate r
                     WHERE r.from_currency = COALESCE(eh.currency, 'USD')
                       AND r.to_currency = 'INR'
                       AND r.rate_date <= %(mar31)s AND r.rate_date >= %(mar31)s - 10
                     ORDER BY r.rate_date DESC LIMIT 1) fx ON TRUE
              WHERE sph.yahoo_symbol = COALESCE(NULLIF(eh.symbol_override,''), eh.symbol)
                AND sph.price_date = %(mar31)s)      AS market_value_as_on,
            eh.pnl_ytd, eh.pnl_inception,
            eh.returns_ytd_pct, eh.returns_inception_pct, eh.cagr_inception_pct,
            eh.first_invested_date,
            eh.weekly_change, eh.exposure_pct, eh.remarks,
            eh.currency, eh.fx_rate, eh.quantity,
            eh.avg_cost_native, eh.cost_native,
            eh.current_price_native, eh.current_market_value_native,
            eh.xirr_inception_pct
        FROM foreign_equity_holding eh
        JOIN entity e ON e.id = eh.entity_id
        WHERE COALESCE(eh.asset_class, 'equity') NOT IN ('gold', 'silver', 'commodity')
        {ent}
        ORDER BY eh.broker, eh.symbol
    """
    if entity_id:
        cur.execute(q.format(ent="AND eh.entity_id = %(eid)s"),
                    {"mar31": mar31, "eid": entity_id})
    else:
        cur.execute(q.format(ent=""), {"mar31": mar31})
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_commodity_holdings(conn, entity_id: Optional[int] = None):
    """Gold / silver / commodity holdings (asset_class in gold/silver/commodity),
    unioned across domestic (equity_holding) and foreign (foreign_equity_holding).
    These are split out of the Equity / Foreign Equity sections — mirroring the
    dedicated Commodities page — and reported on their own. Foreign rows use their
    INR-converted value columns so the grand totals stay whole."""
    cur = conn.cursor()
    mar31 = _fy_mar31_for()
    # FY-opening value (see the note above _fetch_mf_holdings). Domestic rows resolve
    # their Yahoo ticker through security_symbol_map; foreign rows carry it directly in
    # symbol_override — hence the two spellings of the same subquery.
    mv_dom = """
            (SELECT sph.close * eh.quantity
               FROM security_symbol_map m
               JOIN security_price_history sph ON sph.yahoo_symbol = m.resolved_symbol
              WHERE m.symbol = eh.symbol AND sph.price_date = %(mar31)s)
    """
    mv_fgn = """
            (SELECT sph.close * eh.quantity * COALESCE(fx.rate, 1)
               FROM security_price_history sph
               LEFT JOIN LATERAL (
                    SELECT r.rate FROM fx_rate r
                     WHERE r.from_currency = COALESCE(eh.currency, 'USD')
                       AND r.to_currency = 'INR'
                       AND r.rate_date <= %(mar31)s AND r.rate_date >= %(mar31)s - 10
                     ORDER BY r.rate_date DESC LIMIT 1) fx ON TRUE
              WHERE sph.yahoo_symbol = COALESCE(NULLIF(eh.symbol_override,''), eh.symbol)
                AND sph.price_date = %(mar31)s)
    """

    def cols(mv):
        return f"""
            eh.entity_id, e.entity_name, eh.broker,
            COALESCE(eh.symbol_override, eh.symbol) AS symbol, eh.isin,
            COALESCE(eh.asset_class, 'commodity') AS asset_class,
            eh.cost, eh.current_market_value AS current_value,
            eh.prev_week_value, {mv} AS market_value_as_on,
            eh.pnl_ytd, eh.pnl_inception,
            eh.returns_ytd_pct, eh.returns_inception_pct, eh.cagr_inception_pct,
            eh.first_invested_date, eh.weekly_change, eh.exposure_pct, eh.remarks
        """

    ent = "AND eh.entity_id = %(eid)s" if entity_id else ""
    q = f"""
        SELECT {cols(mv_dom)} FROM equity_holding eh JOIN entity e ON e.id = eh.entity_id
        WHERE COALESCE(eh.asset_class, 'equity') IN ('gold', 'silver', 'commodity') {ent}
        UNION ALL
        SELECT {cols(mv_fgn)} FROM foreign_equity_holding eh JOIN entity e ON e.id = eh.entity_id
        WHERE COALESCE(eh.asset_class, 'equity') IN ('gold', 'silver', 'commodity') {ent}
        ORDER BY broker, symbol
    """
    params = {"mar31": mar31}
    if entity_id:
        params["eid"] = entity_id
    cur.execute(q, params)
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_manual_inputs(conn, entity_id: Optional[int] = None):
    """Latest manual input per (entity_id, category, label)."""
    cur = conn.cursor()
    q = """
        SELECT DISTINCT ON (entity_id, category, label)
            entity_id, category, label,
            cost, current_value, prev_week_value,
            currency, raw_amount, fx_rate,
            inception_date, notes, updated_at
        FROM manual_input
        {where}
        ORDER BY entity_id, category, label, updated_at DESC
    """
    if entity_id:
        cur.execute(q.format(where="WHERE entity_id = %s"), (entity_id,))
    else:
        cur.execute(q.format(where=""))
    rows = cur.fetchall()
    cur.close()
    return rows


# ── equity merge (mirrors frontend Combined view) ─────────────────────────────

def _merge_equity_by_symbol(eq_rows: list) -> list:
    """
    Collapse same symbol + same entity rows across brokers into one row.
    Qty/cost/CMV/P&L summed; returns%, CAGR recalculated; earliest inception date kept.
    broker field becomes a comma-joined list of all brokers involved.
    """
    groups: dict = defaultdict(list)
    for h in eq_rows:
        isin = (h.get("isin") or "").strip()
        key  = (h["entity_id"], isin if isin else h["symbol"])
        groups[key].append(h)

    merged = []
    for (entity_id, symbol), rows in groups.items():
        if len(rows) == 1:
            merged.append(dict(rows[0]))
            continue

        def _sum(key):
            vals = [float(r[key]) for r in rows if r.get(key) is not None]
            return sum(vals) if vals else None

        cost    = _sum("cost")
        cur_val = _sum("current_value")
        pnl_inc = _sum("pnl_inception")
        pnl_ytd = _sum("pnl_ytd")
        prev_wk = _sum("prev_week_value")
        wkly    = _sum("weekly_change")
        mkt_mar = _sum("market_value_as_on")

        returns_inc = (pnl_inc / cost * 100) if cost and pnl_inc is not None else None
        returns_ytd = (pnl_ytd / cost * 100) if cost and pnl_ytd is not None else None

        dates = [r["first_invested_date"] for r in rows if r.get("first_invested_date")]
        first_date = min(dates) if dates else None
        cagr = None
        if first_date and cost and cur_val and cost > 0 and cur_val > 0:
            years = (date.today() - first_date).days / 365.25
            if years >= 1.0:
                try:
                    cagr = ((cur_val / cost) ** (1 / years) - 1) * 100
                except Exception:
                    pass

        _BROKER_LABELS = {"zerodha": "Zerodha", "angel_one": "Angel One", "dhan": "Dhan"}
        brokers_str  = ", ".join(sorted({_BROKER_LABELS.get(r["broker"], r["broker"].title()) for r in rows}))
        display_sym  = next((r["symbol"] for r in rows if r.get("symbol")), rows[0].get("isin", ""))
        merged_row = dict(rows[0])
        merged_row.update({
            "symbol":               display_sym,
            "cost":                 cost,
            "current_value":        cur_val,
            "prev_week_value":      prev_wk,
            "weekly_change":        wkly,
            "pnl_inception":        pnl_inc,
            "pnl_ytd":              pnl_ytd,
            "returns_inception_pct": returns_inc,
            "returns_ytd_pct":       returns_ytd,
            "cagr_inception_pct":   cagr,
            "first_invested_date":  first_date,
            "market_value_as_on":   mkt_mar,
            "broker":               brokers_str,
        })
        merged.append(merged_row)

    return sorted(merged, key=lambda h: h["symbol"])


_BROKER_DISPLAY = {"zerodha": "Zerodha", "angel_one": "Angel One", "dhan": "Dhan",
                   "ibkr": "Interactive Brokers", "vested": "Vested", "dbs": "DBS Wealth"}
_PMS_SOURCE_DISPLAY = {"nuvama_pms": "Nuvama", "zerodha_pms": "Zerodha PMS"}


def _rollup_equity_by_broker(raw_rows: list) -> list:
    """
    Collapse direct-equity holdings into ONE row per broker (for the All Assets
    report). Value columns are summed; returns are BLENDED — recomputed from the
    summed cost/value, not averaged across stocks — and CAGR is value-weighted.
    `raw_rows` are the per-(broker, symbol) equity_holding rows (NOT merged).
    """
    groups: dict = defaultdict(list)
    for h in raw_rows:
        groups[h["broker"]].append(h)

    def _sum(rows, key):
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return sum(vals) if vals else None

    out = []
    for broker, rows in groups.items():
        cost    = _sum(rows, "cost")
        cur_val = _sum(rows, "current_value")
        pnl_inc = _sum(rows, "pnl_inception")
        pnl_ytd = _sum(rows, "pnl_ytd")

        returns_inc = (pnl_inc / cost * 100) if cost and pnl_inc is not None else None
        returns_ytd = (pnl_ytd / cost * 100) if cost and pnl_ytd is not None else None

        # value-weighted CAGR (weight by current value)
        wsum = wcagr = 0.0
        for r in rows:
            c, w = r.get("cagr_inception_pct"), r.get("current_value")
            if c is not None and w:
                wcagr += float(c) * float(w); wsum += float(w)
        cagr = (wcagr / wsum) if wsum else None

        out.append({
            "label":                 _BROKER_DISPLAY.get(broker, broker.title()),
            "broker":                broker,
            "cost":                  cost,
            "current_value":         cur_val,
            "market_value_as_on":    _sum(rows, "market_value_as_on"),
            "prev_week_value":       _sum(rows, "prev_week_value"),
            "weekly_change":         _sum(rows, "weekly_change"),
            "pnl_inception":         pnl_inc,
            "pnl_ytd":               pnl_ytd,
            "returns_inception_pct": returns_inc,
            "returns_ytd_pct":       returns_ytd,
            "cagr_inception_pct":    cagr,
        })
    return sorted(out, key=lambda h: h["label"])


def _fetch_pms_holdings(conn, entity_id: Optional[int] = None):
    """PMS positions (pms_holding) — Nuvama + Zerodha-PMS, equity and cash."""
    cur = conn.cursor()
    q = """
        SELECT entity_id, source, holding_type,
               COALESCE(cost, 0) AS cost, COALESCE(market_value, 0) AS market_value
        FROM pms_holding
        {where}
    """
    if entity_id:
        cur.execute(q.format(where="WHERE entity_id = %s"), (entity_id,))
    else:
        cur.execute(q.format(where=""))
    rows = cur.fetchall()
    cur.close()
    return rows


def _pms_report_rows(pms_rows: list) -> list:
    """
    Shape pms_holding into report rows: per source, one summed Equity row
    (blended return) and one Cash row. Not per-stock — matching the summary
    style of the All Assets equity rollup.
    """
    agg: dict = defaultdict(lambda: {"eq_cost": 0.0, "eq_mv": 0.0, "cash": 0.0})
    for r in pms_rows:
        a = agg[r["source"]]
        if r["holding_type"] == "cash":
            a["cash"] += float(r["market_value"])
        else:
            a["eq_cost"] += float(r["cost"]); a["eq_mv"] += float(r["market_value"])

    out = []
    for source in sorted(agg):
        disp, a = _PMS_SOURCE_DISPLAY.get(source, source), agg[source]
        if a["eq_mv"] or a["eq_cost"]:
            ret = ((a["eq_mv"] - a["eq_cost"]) / a["eq_cost"] * 100) if a["eq_cost"] else None
            out.append({
                "label": f"PMS — {disp} (Equity)",
                "cost": a["eq_cost"], "current_value": a["eq_mv"],
                "returns_inception_pct": ret,
            })
        if a["cash"]:
            out.append({
                "label": f"PMS — {disp} (Cash)",
                "cost": a["cash"], "current_value": a["cash"],
            })
    return out


def _fetch_broker_cash(conn, entity_id: Optional[int] = None):
    """Available cash per (entity, broker) from broker_cash."""
    cur = conn.cursor()
    q = """
        SELECT entity_id, broker, COALESCE(balance, 0) AS balance
        FROM broker_cash
        {where}
    """
    if entity_id:
        cur.execute(q.format(where="WHERE entity_id = %s"), (entity_id,))
    else:
        cur.execute(q.format(where=""))
    rows = cur.fetchall()
    cur.close()
    return rows


def _broker_cash_report_rows(cash_rows: list) -> list:
    """One report row per broker cash balance (cost == value, no P&L)."""
    out = []
    for r in sorted(cash_rows, key=lambda x: x["broker"]):
        bal = float(r["balance"])
        out.append({
            "label": f"{_BROKER_DISPLAY.get(r['broker'], r['broker'].title())} (Cash)",
            "cost": bal, "current_value": bal,
        })
    return out


def _fetch_bank_accounts(conn, entity_id: Optional[int] = None):
    """Bank-account cash per (entity, bank), in native currency, from bank_account."""
    cur = conn.cursor()
    q = """
        SELECT entity_id, bank_name, account_type, currency, COALESCE(balance, 0) AS balance
        FROM bank_account
        {where}
    """
    if entity_id:
        cur.execute(q.format(where="WHERE entity_id = %s"), (entity_id,))
    else:
        cur.execute(q.format(where=""))
    rows = cur.fetchall()
    cur.close()
    return rows


def _bank_account_report_rows(conn, bank_rows: list, as_of: date) -> list:
    """One report row per bank account, native balance converted to INR at the
    as-of fx rate (cost == value, no P&L). A balance whose currency has no rate
    is skipped — it can't be valued in INR (matches the /overview behaviour)."""
    out = []
    for r in sorted(bank_rows, key=lambda x: (x["bank_name"], x["account_type"])):
        fx = _fx_rate_on(conn, r["currency"], as_of)
        if fx is None:
            continue
        bal_inr = float(r["balance"]) * fx
        ccy = "" if r["currency"] == "INR" else f" ({r['currency']})"
        out.append({
            "label": f"{r['bank_name']}{ccy} (Bank)",
            "cost": bal_inr, "current_value": bal_inr,
        })
    return out


# ── shared cell helpers ───────────────────────────────────────────────────────

def _v(holding, key):
    val = holding.get(key)
    return float(val) if val is not None else None


def _holding_label(h):
    """First column label: fund name, manual label, or symbol [broker]. Falls back to ISIN."""
    label = h.get("security_name") or h.get("label") or ""
    if not label:
        sym    = h.get("symbol") or h.get("isin", "")
        broker = h.get("broker", "")
        label  = f"{sym}  [{broker}]" if broker else sym
    return label


# ── equity daily print helpers ────────────────────────────────────────────────

EDP_COLS = [
    ("Script Name",               24),
    ("Ticker\nName",              12),
    ("Last Purchase\nDate",       13),
    ("Quantity",                  12),
    ("Avg Purchase\nPrice (₹)",   13),
    ("Total Purchase\nCost (₹)",  15),
    ("Current\nMkt Price (₹)",    13),
    ("Total Current\nMkt Val (₹)", 16),
    ("Total P&L\nInception (₹)",  14),
    ("Inception\nReturns %",      11),
    ("Stock Exp.\n(CMP) %",       11),
    ("Stock Exp.\n(Cost) %",      11),
    ("Change in\nExposure",       11),
]
NC_EDP = len(EDP_COLS)  # 13


def _edp_col_headers(ws, row):
    for c, (name, _) in enumerate(EDP_COLS, 1):
        cell = ws.cell(row=row, column=c, value=name)
        cell.font      = SUB_FONT
        cell.fill      = SUB_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border    = THIN_BORDER
    ws.row_dimensions[row].height = 30
    return row + 1


def _edp_section_header(ws, row, title):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC_EDP)
    cell = ws.cell(row=row, column=1, value=title)
    cell.font      = ENT_FONT
    cell.fill      = ENT_FILL
    cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 18
    return row + 1


def _edp_data_row(ws, row, holding, total_cmv, total_cost, alt=False):
    fill = ALT_FILL if alt else WHITE_FILL
    qty         = _v(holding, "quantity")
    avg_price   = _v(holding, "avg_cost")
    cost        = _v(holding, "cost")
    cur_price   = _v(holding, "current_price")
    cmv         = _v(holding, "current_market_value")
    pnl         = _v(holding, "pnl_inception")
    returns_pct = _v(holding, "returns_inception_pct")
    exp_cmp     = (cmv / total_cmv * 100) if (cmv and total_cmv) else None
    exp_cost    = (cost / total_cost * 100) if (cost and total_cost) else None
    chg_exp     = ((exp_cmp - exp_cost) if (exp_cmp is not None and exp_cost is not None) else None)

    vals = [
        holding.get("symbol") or "",
        holding.get("symbol") or "",
        None,                            # Last Purchase Date — no txn history yet
        qty,
        avg_price,
        cost,
        cur_price,
        cmv,
        pnl,
        returns_pct,
        exp_cmp,
        exp_cost,
        chg_exp,
    ]
    for c, val in enumerate(vals, 1):
        cell = ws.cell(row=row, column=c, value=val)
        cell.fill   = fill
        cell.border = THIN_BORDER
        cell.font   = BODY_FONT
        if c in (1, 2):
            cell.alignment = Alignment(horizontal="left", indent=1)
        elif c == 3:
            cell.number_format = DATE_FMT
            cell.alignment     = Alignment(horizontal="center")
        elif c == 4:
            cell.number_format = '#,##0.00##'
            cell.alignment     = Alignment(horizontal="right")
        elif c in (5, 6, 7, 8, 9):
            cell.number_format = INR_FMT
            cell.alignment     = Alignment(horizontal="right")
        elif c in (10, 11, 12, 13):
            if val is not None:
                cell.value = val / 100
            cell.number_format = PCT_FMT
            cell.alignment     = Alignment(horizontal="right")


def _edp_total_row(ws, row, label, data_start, data_end, section_cmv, section_cost, grand_cmv, grand_cost):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
    cell = ws.cell(row=row, column=1, value=label)
    cell.font      = TOT_FONT
    cell.fill      = TOT_FILL
    cell.alignment = Alignment(horizontal="left", indent=1)
    cell.border    = THIN_BORDER

    sum_cols  = {6, 8, 9}    # cost, CMV, P&L
    exp_cmp   = (section_cmv  / grand_cmv  * 100) if (section_cmv  and grand_cmv)  else None
    exp_cost  = (section_cost / grand_cost * 100) if (section_cost and grand_cost) else None
    chg_exp   = ((exp_cmp - exp_cost) if (exp_cmp is not None and exp_cost is not None) else None)

    for c in range(2, NC_EDP + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill   = TOT_FILL
        cell.font   = TOT_FONT
        cell.border = THIN_BORDER
        if c in sum_cols and data_start:
            cl = get_column_letter(c)
            cell.value         = f"=SUM({cl}{data_start}:{cl}{data_end})"
            cell.number_format = INR_FMT
            cell.alignment     = Alignment(horizontal="right")
        elif c == 4 and data_start:       # qty sum
            cl = get_column_letter(c)
            cell.value         = f"=SUM({cl}{data_start}:{cl}{data_end})"
            cell.number_format = '#,##0.00##'
            cell.alignment     = Alignment(horizontal="right")
        elif c == 10 and data_start:      # returns = P&L / cost
            cell.value         = f"=I{row}/F{row}"
            cell.number_format = PCT_FMT
            cell.alignment     = Alignment(horizontal="right")
        elif c == 11:
            if exp_cmp is not None:
                cell.value = exp_cmp / 100
            cell.number_format = PCT_FMT
            cell.alignment     = Alignment(horizontal="right")
        elif c == 12:
            if exp_cost is not None:
                cell.value = exp_cost / 100
            cell.number_format = PCT_FMT
            cell.alignment     = Alignment(horizontal="right")
        elif c == 13:
            if chg_exp is not None:
                cell.value = chg_exp / 100
            cell.number_format = PCT_FMT
            cell.alignment     = Alignment(horizontal="right")


# ── individual report helpers ─────────────────────────────────────────────────

COLS = [
    ("Asset Class / Fund",   38),
    ("Inception Date",       14),
    ("Cost (₹)",             14),
    ("Mkt Value\n31-Mar",    14),
    ("Current\nMkt Value (₹)", 14),
    ("Prev Week\nValue (₹)", 14),
    ("Weekly\nChange (₹)",   12),
    ("P&L YTD (₹)",          12),
    ("P&L Inception (₹)",    14),
    ("Returns\nYTD %",       10),
    ("Returns\nInception %", 10),
    ("CAGR\nInception %",    10),
    ("Remarks",              20),
]


def _write_header_row(ws, row, as_of: date, entity_name: str):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(COLS))
    cell = ws.cell(row=row, column=1,
                   value=f"Performance Summary — {entity_name}   |   As on {as_of.strftime('%d %b %Y')}")
    cell.font  = HDR_FONT
    cell.fill  = HDR_FILL
    cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 22


def _write_col_headers(ws, row):
    for c, (name, _) in enumerate(COLS, 1):
        cell = ws.cell(row=row, column=c, value=name)
        cell.font      = SUB_FONT
        cell.fill      = SUB_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border    = THIN_BORDER
    ws.row_dimensions[row].height = 30


def _write_section(ws, row, label):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(COLS))
    cell = ws.cell(row=row, column=1, value=label)
    cell.font      = SEC_FONT
    cell.fill      = SEC_FILL
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    cell.border    = Border(bottom=Side(style="medium", color="2F5496"))
    ws.row_dimensions[row].height = 16
    return row + 1


def _write_data_row(ws, row, holding, alt=False):
    fill = ALT_FILL if alt else WHITE_FILL
    vals = [
        _holding_label(holding),
        holding.get("first_invested_date") or holding.get("inception_date"),
        _v(holding, "cost"),
        _v(holding, "market_value_as_on"),
        _v(holding, "current_value"),
        _v(holding, "prev_week_value"),
        _v(holding, "weekly_change"),
        _v(holding, "pnl_ytd"),
        _v(holding, "pnl_inception"),
        _v(holding, "returns_ytd_pct"),
        _v(holding, "returns_inception_pct"),
        _v(holding, "cagr_inception_pct"),
        holding.get("remarks") or holding.get("notes") or "",
    ]
    for c, val in enumerate(vals, 1):
        cell = ws.cell(row=row, column=c, value=val)
        cell.fill   = fill
        cell.border = THIN_BORDER
        cell.font   = BODY_FONT
        if c == 1:
            cell.alignment = Alignment(horizontal="left", indent=2, wrap_text=True)
        elif c == 2:
            cell.number_format = DATE_FMT
            cell.alignment     = Alignment(horizontal="center")
        elif c in (3, 4, 5, 6, 7, 8, 9):
            cell.number_format = INR_FMT
            cell.alignment     = Alignment(horizontal="right")
        elif c in (10, 11, 12):
            if val is not None:
                cell.value = val / 100
            cell.number_format = PCT_FMT
            cell.alignment     = Alignment(horizontal="right")
        else:
            cell.alignment = Alignment(horizontal="left", wrap_text=True)


def _write_total_row(ws, row, label, rows_range, col_count=13):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
    cell = ws.cell(row=row, column=1, value=label)
    cell.font      = TOT_FONT
    cell.fill      = TOT_FILL
    cell.alignment = Alignment(horizontal="left", indent=1)
    cell.border    = THIN_BORDER
    sum_cols = {3, 4, 5, 6, 7, 8, 9}
    for c in range(3, col_count + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill   = TOT_FILL
        cell.font   = TOT_FONT
        cell.border = THIN_BORDER
        if c in sum_cols and rows_range:
            r_start, r_end = rows_range
            col_letter  = get_column_letter(c)
            cell.value  = f"=SUM({col_letter}{r_start}:{col_letter}{r_end})"
            cell.number_format = INR_FMT
            cell.alignment     = Alignment(horizontal="right")


def _set_col_widths(ws):
    for c, (_, width) in enumerate(COLS, 1):
        ws.column_dimensions[get_column_letter(c)].width = width


# ── individual entity report ──────────────────────────────────────────────────

def build_individual_report(conn, entity_id: int, entity_name: str, as_of: date):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Performance Summary"
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A3"

    mf_rows  = _fetch_mf_holdings(conn, entity_id)
    # Merge same stock held across multiple brokers (Combined view logic)
    eq_rows  = _merge_equity_by_symbol(_fetch_equity_holdings(conn, entity_id))
    # Gold / silver / commodity holdings are excluded from _fetch_equity_holdings,
    # so they need their own fetch — otherwise broker-held gold ETFs (GOLDBEES &c)
    # never reach this report at all.
    comm_rows  = _merge_equity_by_symbol(_fetch_commodity_holdings(conn, entity_id))
    pms_items  = _pms_report_rows(_fetch_pms_holdings(conn, entity_id))
    cash_items = _broker_cash_report_rows(_fetch_broker_cash(conn, entity_id))
    bank_items = _bank_account_report_rows(conn, _fetch_bank_accounts(conn, entity_id), as_of)
    man_rows = _fetch_manual_inputs(conn, entity_id)

    man_by_cat: dict = {}
    for m in man_rows:
        man_by_cat.setdefault(m["category"], []).append(m)

    row = 1
    _write_header_row(ws, row, as_of, entity_name); row += 1
    _write_col_headers(ws, row);                    row += 1

    # ── Fixed Income ──────────────────────────────────────────────────────────
    row = _write_section(ws, row, "FIXED INCOME")

    debt_holds = [h for h in mf_rows if h["security_type"] == "MF_DEBT"]
    if debt_holds:
        row = _write_section(ws, row, "MF — Debt / Liquid Funds")
        data_start = row
        for i, h in enumerate(debt_holds):
            _write_data_row(ws, row, h, i % 2 == 1); row += 1
        _write_total_row(ws, row, "Total MF Debt / Liquid", (data_start, row - 1)); row += 1

    for cat in ["ppf", "fixed_income"]:
        items = man_by_cat.get(cat, [])
        labels = {"ppf": "Public Provident Fund", "fixed_income": "Other Fixed Income"}
        if not items:
            continue
        row = _write_section(ws, row, labels[cat])
        data_start = row
        for i, m in enumerate(items):
            _write_data_row(ws, row, m, i % 2 == 1); row += 1
        _write_total_row(ws, row, f"Total {labels[cat]}", (data_start, row - 1)); row += 1

    _write_total_row(ws, row, "A. TOTAL FIXED INCOME", None); row += 2

    # ── Equity ────────────────────────────────────────────────────────────────
    row = _write_section(ws, row, "EQUITY")

    for sec_type, label in [("MF_EQUITY", "MF — Equity Funds"), ("MF_HYBRID", "MF — Hybrid Funds")]:
        holds = [h for h in mf_rows if h["security_type"] == sec_type]
        if not holds:
            continue
        row = _write_section(ws, row, label)
        data_start = row
        for i, h in enumerate(holds):
            _write_data_row(ws, row, h, i % 2 == 1); row += 1
        _write_total_row(ws, row, f"Total {label}", (data_start, row - 1)); row += 1

    if eq_rows:
        row = _write_section(ws, row, "Direct Equities")
        data_start = row
        alt = False
        for h in eq_rows:
            _write_data_row(ws, row, h, alt); row += 1; alt = not alt
        _write_total_row(ws, row, "Total Direct Equities", (data_start, row - 1)); row += 1

    if pms_items:
        row = _write_section(ws, row, "PMS (Managed Accounts)")
        data_start = row
        for i, h in enumerate(pms_items):
            _write_data_row(ws, row, h, i % 2 == 1); row += 1
        _write_total_row(ws, row, "Total PMS (Managed)", (data_start, row - 1)); row += 1

    for cat, label in [("pms", "PMS (Manual Entry)"), ("aif", "AIF")]:
        items = man_by_cat.get(cat, [])
        if not items:
            continue
        row = _write_section(ws, row, label)
        data_start = row
        for i, m in enumerate(items):
            _write_data_row(ws, row, m, i % 2 == 1); row += 1
        _write_total_row(ws, row, f"Total {label}", (data_start, row - 1)); row += 1

    _write_total_row(ws, row, "B. TOTAL EQUITY", None); row += 2

    # Real estate and art/collectibles are standalone registers, not part of the
    # tradeable book — they live in the Registers workbook (build_register_workbook).
    # The section that used to sit here read manual_input category "properties",
    # which has been empty since the register moved to the `property` table.

    # ── Commodities & Precious Metals ─────────────────────────────────────────
    # Gold / silver ETFs are precious metals, not Alternates — same bucket the
    # dashboard donut (GOLD_SILVER) and the combined report use. Broker-held
    # ETFs and manually-entered ones sit under one heading.
    gold_etf_man = man_by_cat.get("gold_etf", [])
    if comm_rows or gold_etf_man:
        row = _write_section(ws, row, "COMMODITIES & PRECIOUS METALS")
        data_start = row
        alt = False
        for h in comm_rows:
            _write_data_row(ws, row, h, alt); row += 1; alt = not alt
        for m in gold_etf_man:
            _write_data_row(ws, row, m, alt); row += 1; alt = not alt
        _write_total_row(ws, row, "Total Commodities", (data_start, row - 1)); row += 1

    _write_total_row(ws, row, "C. TOTAL COMMODITIES & PRECIOUS METALS", None); row += 2

    # ── Alternates ────────────────────────────────────────────────────────────
    row = _write_section(ws, row, "ALTERNATES")

    for cat, label in [
        ("overseas_fund",   "Overseas Funds"),
        ("overseas_equity", "Overseas Direct Equity"),
        ("forex",           "Forex / Foreign Cash"),
        ("nre_bank",        "NRE Accounts"),
        ("unlisted",        "Unlisted Equity"),
        ("startup",         "Startups"),
    ]:
        items = man_by_cat.get(cat, [])
        if not items:
            continue
        row = _write_section(ws, row, label)
        data_start = row
        for i, m in enumerate(items):
            _write_data_row(ws, row, m, i % 2 == 1); row += 1
        _write_total_row(ws, row, f"Total {label}", (data_start, row - 1)); row += 1

    _write_total_row(ws, row, "D. TOTAL ALTERNATES", None); row += 2

    # ── Below-the-line ────────────────────────────────────────────────────────
    for cat, label in [
        ("funds_transit",  "E. Funds in Transit"),
        ("broker_balance", "F. Broker Balance"),
        ("bank",           "G. Funds in Bank"),
    ]:
        items = man_by_cat.get(cat, [])
        # Fold automated broker cash (broker_cash) into the Broker Balance line.
        if cat == "broker_balance":
            items = cash_items + items
        # Fold bank-account balances (bank_account) into the Funds in Bank line.
        if cat == "bank":
            items = bank_items + items
        row = _write_section(ws, row, label)
        data_start = row
        for i, m in enumerate(items):
            _write_data_row(ws, row, m, i % 2 == 1); row += 1
        if items:
            _write_total_row(ws, row, f"Total {label.split('. ', 1)[-1]}", (data_start, row - 1))
            row += 1
        else:
            row += 1

    _write_total_row(ws, row, "H. GRAND TOTAL (A+B+C+D+E+F+G)", None); row += 1

    row += 1
    note = ws.cell(row=row, column=1,
                   value=f"Report generated on {datetime.utcnow().strftime('%d %b %Y %H:%M')} UTC  "
                         f"|  MF data auto-populated from CAS  |  Manual inputs as last updated in portal")
    note.font = LABEL_FONT
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(COLS))

    _set_col_widths(ws)
    ws.print_title_rows = "1:2"
    ws.page_setup.fitToPage = True
    return wb


# ── combined all-entities report ──────────────────────────────────────────────

COMB_COLS = [
    ("Asset / Fund",            38),
    ("Source",                  12),
    ("Cost (₹)",                14),
    ("Mkt Val\n31-Mar (₹)",     14),
    ("Current\nMkt Val (₹)",    14),
    ("Prev Week\nVal (₹)",      14),
    ("Weekly\nChg (₹)",         12),
    ("YTD\nReturns %",          10),
    ("Inception\nReturns %",    10),
    ("CAGR\nInception %",       10),
]

NC = len(COMB_COLS)   # column count for combined sheet


def _comb_entity_header(ws, row, entity_name: str, as_of: date):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC)
    cell = ws.cell(row=row, column=1,
                   value=f"  {entity_name}   |   As on {as_of.strftime('%d %b %Y')}")
    cell.font      = ENT_FONT
    cell.fill      = ENT_FILL
    cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 18


def _comb_section(ws, row, label):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC)
    cell = ws.cell(row=row, column=1, value=label)
    cell.font      = SEC_FONT
    cell.fill      = SEC_FILL
    cell.alignment = Alignment(horizontal="left", indent=1)
    cell.border    = Border(bottom=Side(style="medium", color="2F5496"))
    ws.row_dimensions[row].height = 15
    return row + 1


def _comb_data_row(ws, r, holding, source: str, alt=False):
    fill = ALT_FILL if alt else WHITE_FILL
    vals = [
        _holding_label(holding),
        source,
        _v(holding, "cost"),
        _v(holding, "market_value_as_on"),
        _v(holding, "current_value"),
        _v(holding, "prev_week_value"),
        _v(holding, "weekly_change"),
        _v(holding, "returns_ytd_pct"),
        _v(holding, "returns_inception_pct"),
        _v(holding, "cagr_inception_pct"),
    ]
    for c, val in enumerate(vals, 1):
        cell = ws.cell(row=r, column=c, value=val)
        cell.fill   = fill
        cell.border = THIN_BORDER
        cell.font   = BODY_FONT
        if c == 1:
            cell.alignment = Alignment(horizontal="left", indent=2, wrap_text=True)
        elif c == 2:
            cell.alignment = Alignment(horizontal="center")
        elif c in (3, 4, 5, 6, 7):
            cell.number_format = INR_FMT
            cell.alignment     = Alignment(horizontal="right")
        elif c in (8, 9, 10):
            if val is not None:
                cell.value = val / 100
            cell.number_format = PCT_FMT
            cell.alignment     = Alignment(horizontal="right")


def _comb_total(ws, row, label, data_start=None, data_end=None):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
    cell = ws.cell(row=row, column=1, value=label)
    cell.font      = TOT_FONT
    cell.fill      = TOT_FILL
    cell.alignment = Alignment(horizontal="left", indent=1)
    cell.border    = THIN_BORDER
    for c in range(3, NC + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill   = TOT_FILL
        cell.font   = TOT_FONT
        cell.border = THIN_BORDER
        if c in (3, 4, 5, 6, 7) and data_start and data_end:
            col_letter  = get_column_letter(c)
            cell.value  = f"=SUM({col_letter}{data_start}:{col_letter}{data_end})"
            cell.number_format = INR_FMT
            cell.alignment     = Alignment(horizontal="right")


def build_combined_report(conn, as_of: date, ws=None):
    if ws is None:
        wb = openpyxl.Workbook()
        ws = wb.active
    ws.title = "All Assets Daily MIS"
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A3"

    # Main title
    row = 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC)
    hdr = ws.cell(row=row, column=1,
                  value=f"ALL ENTITIES — COMBINED PORTFOLIO MIS   |   As on {as_of.strftime('%d %b %Y')}"
                        f"{_staleness_note(conn, as_of)}")
    hdr.font      = HDR_FONT
    hdr.fill      = HDR_FILL
    hdr.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 22
    row += 1

    # Column headers
    for c, (name, _) in enumerate(COMB_COLS, 1):
        cell = ws.cell(row=row, column=c, value=name)
        cell.font      = SUB_FONT
        cell.fill      = SUB_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border    = THIN_BORDER
    ws.row_dimensions[row].height = 30
    row += 1

    entities = _fetch_entities(conn)

    for entity in entities:
        eid, ename = entity["id"], entity["entity_name"]

        mf_rows    = _fetch_mf_holdings(conn, eid)
        eq_broker  = _rollup_equity_by_broker(_fetch_equity_holdings(conn, eid))
        fe_broker  = _rollup_equity_by_broker(_fetch_foreign_equity_holdings(conn, eid))
        comm_broker = _rollup_equity_by_broker(_fetch_commodity_holdings(conn, eid))
        pms_items  = _pms_report_rows(_fetch_pms_holdings(conn, eid))
        cash_items = _broker_cash_report_rows(_fetch_broker_cash(conn, eid))
        bank_items = _bank_account_report_rows(conn, _fetch_bank_accounts(conn, eid), as_of)
        man_rows   = _fetch_manual_inputs(conn, eid)
        man_by_cat: dict = {}
        for m in man_rows:
            man_by_cat.setdefault(m["category"], []).append(m)

        if not (mf_rows or eq_broker or fe_broker or comm_broker
                or pms_items or cash_items or bank_items or man_rows):
            continue

        # Entity header row
        _comb_entity_header(ws, row, ename, as_of); row += 1

        # ── Fixed Income ──────────────────────────────────────────────────────
        debt_holds = [h for h in mf_rows if h["security_type"] == "MF_DEBT"]
        # Manual fixed income: PPF + manually-entered MF (liquid/debt/arbitrage) +
        # any legacy "fixed_income" rows. Previously only ppf/fixed_income showed.
        fi_man     = (man_by_cat.get("liquid_fund", []) + man_by_cat.get("debt_fund", [])
                      + man_by_cat.get("arbitrage_fund", []) + man_by_cat.get("ppf", [])
                      + man_by_cat.get("fixed_income", []))

        if debt_holds or fi_man:
            row = _comb_section(ws, row, "FIXED INCOME")
            fi_sub_total_rows: list = []

            if debt_holds:
                row = _comb_section(ws, row, "MF Debt / Liquid Funds")
                sub_start = row
                alt = False
                for h in debt_holds:
                    _comb_data_row(ws, row, h, "CAS", alt); row += 1; alt = not alt
                _comb_total(ws, row, "Total MF Debt / Liquid", sub_start, row - 1)
                fi_sub_total_rows.append(row); row += 1

            if fi_man:
                row = _comb_section(ws, row, "PPF / Manual Fixed Income")
                sub_start = row
                alt = False
                for m in fi_man:
                    _comb_data_row(ws, row, m, "Manual", alt); row += 1; alt = not alt
                _comb_total(ws, row, "Total PPF / Manual Fixed Income", sub_start, row - 1)
                fi_sub_total_rows.append(row); row += 1

            _comb_total(ws, row, "Total Fixed Income"); row += 1

        # ── Equity ────────────────────────────────────────────────────────────
        eq_mf  = [h for h in mf_rows if h["security_type"] == "MF_EQUITY"]
        hyb_mf = [h for h in mf_rows if h["security_type"] == "MF_HYBRID"]
        deq_man = man_by_cat.get("direct_equity", [])
        pms    = man_by_cat.get("pms", [])
        aif    = man_by_cat.get("aif", [])

        if eq_mf or hyb_mf or eq_broker or deq_man or fe_broker or pms_items or pms or aif:
            row = _comb_section(ws, row, "EQUITY")

            if eq_mf or hyb_mf:
                row = _comb_section(ws, row, "MF Equity / Hybrid Funds")
                sub_start = row
                alt = False
                for h in eq_mf:
                    _comb_data_row(ws, row, h, "CAS", alt);  row += 1; alt = not alt
                for h in hyb_mf:
                    _comb_data_row(ws, row, h, "CAS", alt);  row += 1; alt = not alt
                _comb_total(ws, row, "Total MF Equity / Hybrid", sub_start, row - 1); row += 1

            if eq_broker:
                row = _comb_section(ws, row, "Direct Equities (by broker)")
                sub_start = row
                alt = False
                for h in eq_broker:
                    _comb_data_row(ws, row, h, "Equity", alt); row += 1; alt = not alt
                _comb_total(ws, row, "Total Direct Equities", sub_start, row - 1); row += 1

            if deq_man:
                row = _comb_section(ws, row, "Direct Equity (Manual)")
                sub_start = row
                alt = False
                for m in deq_man:
                    _comb_data_row(ws, row, m, "Manual", alt); row += 1; alt = not alt
                _comb_total(ws, row, "Total Direct Equity (Manual)", sub_start, row - 1); row += 1

            # Foreign equity: one summary row per broker (detail in the
            # "Foreign Equity Print" sheet). INR-converted, so it folds into the
            # entity / grand totals like every other section.
            if fe_broker:
                row = _comb_section(ws, row, "Foreign Equity (by broker)")
                sub_start = row
                alt = False
                for h in fe_broker:
                    _comb_data_row(ws, row, h, "Foreign", alt); row += 1; alt = not alt
                _comb_total(ws, row, "Total Foreign Equity", sub_start, row - 1); row += 1

            if pms_items:
                row = _comb_section(ws, row, "PMS (Managed Accounts)")
                sub_start = row
                alt = False
                for h in pms_items:
                    _comb_data_row(ws, row, h, "PMS", alt); row += 1; alt = not alt
                _comb_total(ws, row, "Total PMS (Managed)", sub_start, row - 1); row += 1

            if pms:
                row = _comb_section(ws, row, "PMS (Manual Entry)")
                sub_start = row
                alt = False
                for m in pms:
                    _comb_data_row(ws, row, m, "Manual", alt);  row += 1; alt = not alt
                _comb_total(ws, row, "Total PMS (Manual)", sub_start, row - 1); row += 1

            if aif:
                row = _comb_section(ws, row, "AIF")
                sub_start = row
                alt = False
                for m in aif:
                    _comb_data_row(ws, row, m, "AIF", alt);  row += 1; alt = not alt
                _comb_total(ws, row, "Total AIF", sub_start, row - 1); row += 1

            _comb_total(ws, row, "Total Equity"); row += 1

        # ── Commodities & Precious Metals ──────────────────────────────────────
        # Gold / silver / commodity broker holdings split out of Equity, plus
        # manually-entered gold/silver ETFs — all under one Commodities heading.
        gold_etf_man = man_by_cat.get("gold_etf", [])
        if comm_broker or gold_etf_man:
            row = _comb_section(ws, row, "COMMODITIES & PRECIOUS METALS")
            sub_start = row
            alt = False
            for h in comm_broker:
                _comb_data_row(ws, row, h, "Commodity", alt); row += 1; alt = not alt
            for m in gold_etf_man:
                _comb_data_row(ws, row, m, "Manual", alt); row += 1; alt = not alt
            _comb_total(ws, row, "Total Commodities", sub_start, row - 1); row += 1

        # ── Alternates ────────────────────────────────────────────────────────
        alt_cats = [
            ("overseas_fund",   "Overseas Funds"),
            ("overseas_equity", "Overseas Direct Equity"),
            ("forex",           "Forex / Foreign Cash"),
            ("nre_bank",        "NRE Accounts"),
            ("unlisted",        "Unlisted Equity"),
            ("startup",         "Startups"),
            ("art",             "Art / Collectibles"),
        ]
        alt_items = [(cat, label, man_by_cat.get(cat, [])) for cat, label in alt_cats if man_by_cat.get(cat)]
        if alt_items:
            row = _comb_section(ws, row, "ALTERNATES")
            for _cat, label, items in alt_items:
                row = _comb_section(ws, row, label)
                sub_start = row
                alt = False
                for m in items:
                    _comb_data_row(ws, row, m, "Manual", alt); row += 1; alt = not alt
                _comb_total(ws, row, f"Total {label}", sub_start, row - 1); row += 1
            _comb_total(ws, row, "Total Alternates"); row += 1

        # Real estate lives in the Registers workbook — see build_register_workbook.

        # ── Below-the-line ────────────────────────────────────────────────────
        btl = []
        for cat in ("funds_transit", "broker_balance", "bank"):
            btl.extend(man_by_cat.get(cat, []))
        if btl or cash_items or bank_items:
            row = _comb_section(ws, row, "Funds in Transit / Bank / Broker")
            data_start = row
            alt = False
            for h in cash_items:
                _comb_data_row(ws, row, h, "Broker Cash", alt); row += 1; alt = not alt
            for h in bank_items:
                _comb_data_row(ws, row, h, "Bank Cash", alt); row += 1; alt = not alt
            for m in btl:
                _comb_data_row(ws, row, m, "Bank/Broker", alt); row += 1; alt = not alt
            _comb_total(ws, row, "Total Liquidity", data_start, row - 1); row += 1

        # Entity grand total
        _comb_total(ws, row, f"TOTAL — {ename}"); row += 2

    # Overall footer
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC)
    cell = ws.cell(row=row, column=1, value="GRAND TOTAL — ALL ENTITIES")
    cell.font      = ENT_FONT
    cell.fill      = HDR_FILL
    cell.alignment = Alignment(horizontal="left", indent=1)
    ws.row_dimensions[row].height = 18
    row += 2

    note = ws.cell(row=row, column=1,
                   value=f"Generated {datetime.utcnow().strftime('%d %b %Y %H:%M')} UTC  "
                         f"|  MF & equity from DB  |  Manual items from portal")
    note.font = LABEL_FONT
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC)

    for c, (_, width) in enumerate(COMB_COLS, 1):
        ws.column_dimensions[get_column_letter(c)].width = width

    ws.print_title_rows = "1:2"
    return ws


# ── equity daily print report ─────────────────────────────────────────────────

def build_equity_daily_print(conn, entity_codes: list, as_of: date, ws=None):
    """
    Build a single-sheet Equity Daily Print for the given entity codes
    (e.g. ['DHR', 'HHR', 'SDR']).  Sections: Grand Total (combined), then per entity.
    Writes into `ws` if provided, else into a fresh workbook.
    """
    # Resolve entity codes → IDs + names
    cur = conn.cursor()
    placeholders = ",".join(["%s"] * len(entity_codes))
    cur.execute(
        f"SELECT id, entity_name FROM entity WHERE entity_name IN ({placeholders}) ORDER BY id",
        entity_codes,
    )
    entity_map = {r["entity_name"]: r["id"] for r in cur.fetchall()}
    cur.close()

    # Preserve order as requested
    entities = [(code, entity_map[code]) for code in entity_codes if code in entity_map]
    entity_ids = [eid for _, eid in entities]

    raw = _fetch_equity_daily_data(conn, entity_ids)

    # Per-entity merged rows (multi-broker same ISIN collapsed, sorted CMV desc)
    entity_rows: dict = {}
    for code, eid in entities:
        rows = [r for r in raw if r["entity_id"] == eid]
        entity_rows[code] = _merge_edp_rows(rows, cross_entity=False)

    # Grand total: merge across all entities by ISIN/symbol
    grand_rows = _merge_edp_rows(raw, cross_entity=True)

    # Totals for exposure % denominators
    def _total(rows, key):
        return sum(float(r[key]) for r in rows if r.get(key)) or None

    grand_cmv  = _total(grand_rows, "current_market_value")
    grand_cost = _total(grand_rows, "cost")

    # Build workbook
    if ws is None:
        wb = openpyxl.Workbook()
        ws = wb.active
    ws.title = "Equity Daily Print"
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A3"

    row = 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC_EDP)
    hdr = ws.cell(row=row, column=1,
                  value=f"EQUITY DAILY PRINT — RAJANI GROUP   |   As on {as_of.strftime('%d %b %Y')}"
                        f"{_staleness_note(conn, as_of)}")
    hdr.font      = HDR_FONT
    hdr.fill      = HDR_FILL
    hdr.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 22
    row += 1

    def _write_section(title, rows, sec_cmv, sec_cost, denom_cmv, denom_cost):
        nonlocal row
        row = _edp_section_header(ws, row, f"{title} as on {as_of.strftime('%d %b %Y')}")
        row = _edp_col_headers(ws, row)
        data_start = row
        for i, h in enumerate(rows):
            _edp_data_row(ws, row, h, sec_cmv, sec_cost, i % 2 == 1)
            row += 1
        data_end = row - 1
        _edp_total_row(ws, row, f"Total — {title}", data_start, data_end,
                       sec_cmv, sec_cost, denom_cmv, denom_cost)
        row += 2

    # Grand total section
    _write_section(
        "Total Rajani Group Direct Equity (DOMESTIC)",
        grand_rows,
        grand_cmv, grand_cost,
        grand_cmv, grand_cost,   # denominator = itself → 100 %
    )

    # Per-entity sections
    for code, _eid in entities:
        rows = entity_rows[code]
        sec_cmv  = _total(rows, "current_market_value")
        sec_cost = _total(rows, "cost")
        _write_section(
            f"{code} Direct Equity (DOMESTIC)",
            rows,
            sec_cmv, sec_cost,
            grand_cmv, grand_cost,   # denominator = grand total
        )

    note = ws.cell(row=row, column=1,
                   value=f"Generated {datetime.utcnow().strftime('%d %b %Y %H:%M')} UTC  "
                         f"|  Equity prices from broker API  |  Last Purchase Date not available (no transaction history)")
    note.font = LABEL_FONT
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC_EDP)

    for c, (_, width) in enumerate(EDP_COLS, 1):
        ws.column_dimensions[get_column_letter(c)].width = width

    ws.print_title_rows = "1:2"
    return ws


# ── foreign equity detail print ────────────────────────────────────────────────

# Native money has no currency symbol (the Ccy column carries it; one sheet may mix
# USD / SGD / …); INR columns reuse INR_FMT.
FE_NATIVE_FMT = '#,##0.00'
FE_COLS = [
    ("Script Name",            24),
    ("Broker",                 18),
    ("Ccy",                     6),
    ("Quantity",               11),
    ("Avg Cost\n(native)",     13),
    ("Total Cost\n(native)",   15),
    ("Cur Price\n(native)",    13),
    ("Cur Mkt Val\n(native)",  15),
    ("Cur Mkt Val\n(₹)",       15),
    ("P&L Inception\n(₹)",     15),
    ("Inception\nReturns %",   11),
    ("XIRR %\np.a.",           10),
]
NC_FE = len(FE_COLS)


def build_foreign_equity_print(conn, as_of: date, ws=None):
    """
    Per-holding detail of foreign (multi-currency) equity, in NATIVE currency plus
    the INR-converted value. One section per entity (subtotalled in ₹), so the
    All Assets sheet can carry just the by-broker summary. Returns the ws, or None
    if there are no foreign holdings.
    """
    rows = _fetch_foreign_equity_holdings(conn)
    if not rows:
        return None

    if ws is None:
        wb = openpyxl.Workbook()
        ws = wb.active
    ws.title = "Foreign Equity Print"
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A3"

    # Title
    row = 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC_FE)
    hdr = ws.cell(row=row, column=1,
                  value=f"FOREIGN EQUITY — HOLDINGS DETAIL   |   As on {as_of.strftime('%d %b %Y')}"
                        f"{_staleness_note(conn, as_of)}")
    hdr.font = HDR_FONT; hdr.fill = HDR_FILL
    hdr.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 22
    row += 1

    # Column headers
    for c, (name, _) in enumerate(FE_COLS, 1):
        cell = ws.cell(row=row, column=c, value=name)
        cell.font = SUB_FONT; cell.fill = SUB_FILL; cell.border = THIN_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30
    row += 1

    # Group by entity (preserve fetch order: entity_name, broker, symbol)
    by_entity: dict = defaultdict(list)
    for r in rows:
        by_entity[r["entity_name"]].append(r)

    def _data_row(r, alt):
        nonlocal row
        fill = ALT_FILL if alt else WHITE_FILL
        ret_inc = _v(r, "returns_inception_pct")
        xirr    = _v(r, "xirr_inception_pct")
        vals = [
            r.get("symbol") or r.get("isin") or "",
            _BROKER_DISPLAY.get(r["broker"], r["broker"].title()),
            r.get("currency") or "",
            _v(r, "quantity"),
            _v(r, "avg_cost_native"),
            _v(r, "cost_native"),
            _v(r, "current_price_native"),
            _v(r, "current_market_value_native"),
            _v(r, "current_value"),          # INR
            _v(r, "pnl_inception"),          # INR
            (ret_inc / 100) if ret_inc is not None else None,
            (xirr / 100) if xirr is not None else None,
        ]
        for c, val in enumerate(vals, 1):
            cell = ws.cell(row=row, column=c, value=val)
            cell.fill = fill; cell.border = THIN_BORDER; cell.font = BODY_FONT
            if c == 1:
                cell.alignment = Alignment(horizontal="left", indent=1, wrap_text=True)
            elif c in (2, 3):
                cell.alignment = Alignment(horizontal="center" if c == 3 else "left")
            elif c == 4:
                cell.number_format = FE_NATIVE_FMT; cell.alignment = Alignment(horizontal="right")
            elif c in (5, 6, 7, 8):
                cell.number_format = FE_NATIVE_FMT; cell.alignment = Alignment(horizontal="right")
            elif c in (9, 10):
                cell.number_format = INR_FMT; cell.alignment = Alignment(horizontal="right")
            else:
                cell.number_format = PCT_FMT; cell.alignment = Alignment(horizontal="right")
        row += 1

    def _subtotal(label, ent_rows, data_start, data_end):
        nonlocal row
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
        cell = ws.cell(row=row, column=1, value=label)
        cell.font = TOT_FONT; cell.fill = TOT_FILL
        cell.alignment = Alignment(horizontal="left", indent=1); cell.border = THIN_BORDER
        for c in range(2, 9):
            cc = ws.cell(row=row, column=c); cc.fill = TOT_FILL; cc.border = THIN_BORDER
        # Only the INR columns (9, 10) are summable across mixed currencies.
        for c in (9, 10):
            col = get_column_letter(c)
            tc = ws.cell(row=row, column=c, value=f"=SUM({col}{data_start}:{col}{data_end})")
            tc.font = TOT_FONT; tc.fill = TOT_FILL; tc.border = THIN_BORDER
            tc.number_format = INR_FMT; tc.alignment = Alignment(horizontal="right")
        for c in (11, 12):
            cc = ws.cell(row=row, column=c); cc.fill = TOT_FILL; cc.border = THIN_BORDER
        row += 1

    for ename, ent_rows in by_entity.items():
        # Entity banner
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC_FE)
        b = ws.cell(row=row, column=1, value=ename)
        b.font = ENT_FONT; b.fill = ENT_FILL
        b.alignment = Alignment(horizontal="left", indent=1)
        ws.row_dimensions[row].height = 16
        row += 1

        data_start = row
        for i, r in enumerate(ent_rows):
            _data_row(r, i % 2 == 1)
        _subtotal(f"Total Foreign Equity — {ename}", ent_rows, data_start, row - 1)
        row += 1

    note = ws.cell(row=row, column=1,
                   value=f"Generated {datetime.utcnow().strftime('%d %b %Y %H:%M')} UTC  "
                         f"|  Native amounts in each holding's own currency  "
                         f"|  ₹ values converted at the snapshot FX rate")
    note.font = LABEL_FONT
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC_FE)

    for c, (_, width) in enumerate(FE_COLS, 1):
        ws.column_dimensions[get_column_letter(c)].width = width

    ws.print_title_rows = "1:2"
    return ws


# ══════════════════════════════════════════════════════════════════════════════
#  Data-driven per-entity / per-group sheets (styled to match MIS-REPORT.xlsx)
# ══════════════════════════════════════════════════════════════════════════════
#
#  The Dhruv sheets in MIS-REPORT.xlsx are a hand-built model with hardcoded fund
#  rows; we DO NOT clone them anymore.  Instead we build each entity/group sheet
#  from its real DB holdings, reproducing the template's gold/yellow look, and emit
#  a section only when the entity actually holds something in it.

# Template palette (gold header / yellow section-total).
GOLD_FILL    = PatternFill("solid", fgColor="BF9000")
YELLOW_FILL  = PatternFill("solid", fgColor="FFD965")
BAND_FILL    = PatternFill("solid", fgColor="FFF2CC")   # light gold band (alt rows)
SUBSEC_FILL  = PatternFill("solid", fgColor="FFE699")   # mid gold — group sub-header
GOLD_HDR_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
GOLD_COL_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=8)
SUBSEC_FONT   = Font(name="Calibri", bold=True, color="3F2E00", size=9)
GTOT_FONT     = Font(name="Calibri", bold=True, color="000000", size=9)

MONEY_FMT = '#,##0'
GOLD_BORDER = Border(left=Side(style="thin", color="D6B656"), right=Side(style="thin", color="D6B656"),
                     top=Side(style="thin", color="D6B656"), bottom=Side(style="thin", color="D6B656"))

# Weekly-sheet columns (mirror the template's column set).
WK_COLS = [
    ("ASSET CLASS / HOLDING", 42),
    ("Inception",             12),
    ("Prev Week Value",       14),
    ("Mkt Value 31-Mar",      15),
    ("Cost",                  14),
    ("Exposure %",            11),
    ("Current Mkt Value",     16),
    ("Weekly Change",         13),
    ("P&L YTD",               13),
    ("P&L Inception",         14),
    ("Returns YTD %",         11),
    ("Returns Incept. %",     13),
    ("CAGR %",                10),
    ("Brokers / Remarks",     24),
]
NWK = len(WK_COLS)
# Column groups (1-based) by format.
_MONEY_COLS = (3, 4, 5, 7, 8, 9, 10)
_PCT_COLS   = (6, 11, 12, 13)


# Friendly group label keyed by the group's lead (first) entity code.
GROUP_DISPLAY = {"DHR": "Dhruv", "HHR": "Harsh"}


def _pan_groups(conn) -> list[dict]:
    """PAN groups that bundle >1 entity → [{label, entity_ids}] in member order."""
    cur = conn.cursor()
    cur.execute("""
        SELECT pg.id, pg.pan_name, e.id AS entity_id, e.entity_name
        FROM pan_group pg JOIN entity e ON e.pan_group_id = pg.id
        ORDER BY pg.id, e.id
    """)
    rows = cur.fetchall(); cur.close()
    by_group: dict = defaultdict(list)
    for r in rows:
        by_group[r["id"]].append(r)
    groups = []
    for members in by_group.values():
        if len(members) < 2:
            continue                      # standalone → covered by its own entity sheet
        lead = members[0]["entity_name"]  # group label from its first (lead) entity
        name = GROUP_DISPLAY.get(lead, lead)
        groups.append({"label": f"{name} Group",
                       "entity_ids": [m["entity_id"] for m in members]})
    return groups


def _bundle_for(conn, entity_ids: list, as_of: date) -> dict:
    """
    Gather + merge holdings for one entity or a set of entities (a group).
    Direct equity is merged by ISIN ACROSS the whole bundle so the same share held
    via several brokers (or several entities in a group) collapses to one line, with
    every broker named.  Returns {mf, eq, fe, comm, pms, cash, bank, manual_by_cat}.
    """
    mf_rows, eq_raw, fe_raw, comm_raw, man_rows = [], [], [], [], []
    pms_raw, cash_raw, bank_raw = [], [], []
    for eid in entity_ids:
        mf_rows.extend(_fetch_mf_holdings(conn, eid))
        eq_raw.extend(_fetch_equity_holdings(conn, eid))
        fe_raw.extend(_fetch_foreign_equity_holdings(conn, eid))
        comm_raw.extend(_fetch_commodity_holdings(conn, eid))
        pms_raw.extend(_fetch_pms_holdings(conn, eid))
        cash_raw.extend(_fetch_broker_cash(conn, eid))
        bank_raw.extend(_fetch_bank_accounts(conn, eid))
        man_rows.extend(_fetch_manual_inputs(conn, eid))

    # Collapse entity_id so _merge_equity_by_symbol merges across the whole bundle.
    for r in eq_raw + fe_raw + comm_raw:
        r["entity_id"] = 0
    eq   = _merge_equity_by_symbol(eq_raw)
    fe   = _merge_equity_by_symbol(fe_raw)    # foreign, merged by symbol across the bundle
    comm = _merge_equity_by_symbol(comm_raw)  # gold/silver/commodity, merged by symbol

    # Normalise broker labels (single-broker rows arrive raw e.g. "zerodha").
    for r in eq + fe + comm:
        b = r.get("broker")
        if b:
            r["broker"] = ", ".join(
                _BROKER_DISPLAY.get(p.strip(), p.strip().title()) for p in str(b).split(","))

    man_by_cat: dict = defaultdict(list)
    for m in man_rows:
        man_by_cat[m["category"]].append(m)
    return {"mf": mf_rows, "eq": eq, "fe": fe, "comm": comm,
            "pms":  _pms_report_rows(pms_raw),
            "cash": _broker_cash_report_rows(cash_raw),
            "bank": _bank_account_report_rows(conn, bank_raw, as_of),
            "manual_by_cat": man_by_cat}


def _wk_pct(h, key):
    v = h.get(key)
    return (float(v) / 100.0) if v is not None else None


# Display order of the benchmark series.
_BENCHMARK_ORDER = ["SENSEX", "NIFTY", "GS2032_YTM", "GS2032_PRICE", "GS2030_YTM", "GS2030_PRICE"]


def _fetch_benchmarks(conn, as_of: date) -> list[dict]:
    """
    Current / previous-week / 31-Mar values per benchmark (derived from the
    market_benchmark history) + week% and YTD% change.  Returns [] if the table
    is absent (migration not yet run) so callers still work.

    NOT used by the workbook any more — the Market Statistics block was dropped from
    the reports. This is now the data source for GET /api/v1/benchmarks (main.py),
    which feeds the dashboard market rail and the top ticker. Deleting it along with
    the report block took the overview page's market data down with it (2026-08-10),
    so it lives on here as an API helper. Move it to a shared module if this file's
    report-only role ever needs to be strict.
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT code, label, as_of_date, value, unit, prev_close
            FROM market_benchmark
            WHERE value IS NOT NULL AND as_of_date <= %s
            ORDER BY code, as_of_date
        """, (as_of,))
        rows = cur.fetchall()
    except Exception:
        conn.rollback(); cur.close()
        return []
    cur.close()

    series: dict = defaultdict(list)
    label_of, unit_of, prev_close_of = {}, {}, {}
    for r in rows:
        series[r["code"]].append((r["as_of_date"], float(r["value"])))
        label_of[r["code"]] = r["label"]
        unit_of[r["code"]] = r["unit"]
        # Rows are ordered by date, so the last one wins — the prior close belonging
        # to the most recent reading, which is the one day% is measured against.
        prev_close_of[r["code"]] = float(r["prev_close"]) if r["prev_close"] is not None else None

    prev_cut = as_of.fromordinal(as_of.toordinal() - 7)
    mar31    = _fy_mar31_for(as_of)

    def _at_or_before(pairs, cutoff):
        val = None
        for d, v in pairs:                       # pairs sorted ascending
            if d <= cutoff:
                val = v
        return val

    out = []
    codes = [c for c in _BENCHMARK_ORDER if c in series] + \
            [c for c in series if c not in _BENCHMARK_ORDER]
    for code in codes:
        pairs = series[code]
        current   = pairs[-1][1]
        prev_week = _at_or_before(pairs, prev_cut)
        mar       = _at_or_before(pairs, mar31)
        week_pct  = ((current - prev_week) / prev_week) if (prev_week) else None
        ytd_pct   = ((current - mar) / mar) if (mar) else None
        # Day move, against the previous session's official close as Yahoo reported
        # it — not against the previous row, which holds whatever the worker last
        # wrote that day (an intra-day print, not the close) and would silently
        # compare across a gap. None where no prior close is stored: the manual GS
        # bonds and the monthly IMF/FRED series have none, and a monthly reading has
        # no "day" move to show.
        prev_close = prev_close_of.get(code)
        day_pct    = ((current - prev_close) / prev_close) if prev_close else None
        out.append({"code": code, "label": label_of[code], "unit": unit_of[code],
                    "current": current, "prev_week": prev_week, "mar31": mar,
                    "prev_close": prev_close,
                    "week_pct": week_pct, "ytd_pct": ytd_pct, "day_pct": day_pct,
                    # Date of the CURRENT reading. Daily series are always ~today, but
                    # monthly ones (IMF inflation) can be a couple of months back, and
                    # the UI has to label them rather than imply they're live.
                    "as_of": pairs[-1][0].isoformat()})
    return out


def _fy_start(as_of: date) -> date:
    """1-Apr that opens the current financial year."""
    return date(as_of.year if as_of.month >= 4 else as_of.year - 1, 4, 1)


def _avg_cost_realised(seq: list, fy_start: date) -> list:
    """
    Average-cost realised P&L from a chronological buy/sell sequence for one security.
    Each item: {date, kind: 'buy'|'sell', units, amount, name, group}.
    Records a realised row for every sell ON/AFTER fy_start.  If a sell has no known
    cost basis (no prior buys in the data — common when CAS omits old purchases),
    purchase_amount/pnl are left None rather than overstating the gain.
    """
    held, cost, out = 0.0, 0.0, []
    for t in seq:
        u   = abs(float(t["units"] or 0))
        amt = abs(float(t["amount"] or 0))
        if t["kind"] == "buy":
            held += u; cost += amt
            continue
        # sell
        if held > 1e-9 and u > 0:
            avg       = cost / held
            sold      = min(u, held)
            cost_sold = avg * sold
            held -= sold; cost -= cost_sold
        else:
            cost_sold = None
        pnl = (amt - cost_sold) if cost_sold is not None else None
        if t["date"] >= fy_start:
            ret = (pnl / cost_sold) if (pnl is not None and cost_sold) else None
            out.append({"group": t["group"], "security_name": t["name"],
                        "purchase_amount": cost_sold, "sale_date": t["date"],
                        "sale_amount": amt, "pnl": pnl, "return_pct": ret})
    return out


def _avg_cost_realised_grouped(seq: list, fy_start: date) -> list:
    """
    Equity realised P&L for ONE entity's chronological trade stream, consolidating
    CONSECUTIVE sells of the same security into a single realised event.

    Continuity is strict: any interleaving trade — a buy, or a sell of a different
    security — closes the current run, so a later sell of the same security starts a
    fresh ('discontinuous') realised row. Average cost is tracked per security across
    the whole stream (buys before fy_start still establish basis); only sells on/after
    fy_start contribute to an emitted row. seq items carry an extra `sec` (security_id)
    key alongside the usual {date, kind, units, amount, name, group}.

    If any sell folded into a run has no known cost basis (CAS/tradebook omitted the
    purchase), that run's purchase_amount and pnl are left None rather than overstating
    the gain — same policy as _avg_cost_realised.
    """
    state: dict = defaultdict(lambda: {"held": 0.0, "cost": 0.0})  # per security
    out: list = []
    run = None  # open run of consecutive same-security sells

    def flush():
        nonlocal run
        if run is None:
            return
        if run["qualifying"]:
            if run["unknown_cost"]:
                purchase = pnl = ret = None
            else:
                purchase = run["purchase_amount"]
                pnl = run["sale_amount"] - purchase
                ret = (pnl / purchase) if purchase else None
            out.append({"group": "Equity", "security_name": run["name"],
                        "purchase_amount": purchase, "sale_date": run["sale_date"],
                        "sale_amount": run["sale_amount"], "pnl": pnl, "return_pct": ret})
        run = None

    for t in seq:
        sec = t["sec"]
        u   = abs(float(t["units"] or 0))
        amt = abs(float(t["amount"] or 0))
        st  = state[sec]

        if t["kind"] == "buy":
            flush()                       # a buy always breaks a sell run
            st["held"] += u; st["cost"] += amt
            continue

        # sell — consume average-cost basis whether or not the row is emitted
        if st["held"] > 1e-9 and u > 0:
            avg       = st["cost"] / st["held"]
            sold      = min(u, st["held"])
            cost_sold = avg * sold
            st["held"] -= sold; st["cost"] -= cost_sold
        else:
            cost_sold = None

        if run is not None and run["sec"] != sec:
            flush()                       # a different stock's sell breaks the run
        if run is None:
            run = {"sec": sec, "name": t["name"], "sale_amount": 0.0,
                   "purchase_amount": 0.0, "sale_date": None,
                   "qualifying": False, "unknown_cost": False}

        if t["date"] >= fy_start:
            run["qualifying"]   = True
            run["sale_amount"] += amt
            run["sale_date"]    = t["date"]
            if cost_sold is None:
                run["unknown_cost"] = True
            else:
                run["purchase_amount"] += cost_sold

    flush()
    return out


def _is_long_term_equity(buy_dt: date, sell_dt: date) -> bool:
    """Indian listed-equity long-term test: held for MORE than 12 months.

    The 12-month anniversary of the purchase is the boundary — a sale strictly
    after it is long-term (LTCG); on/before it is short-term (STCG). Computed on
    calendar months (not a flat 365 days) so leap years don't misclassify; a
    Feb-29 purchase rolls to Feb-28 of the anniversary year.
    """
    try:
        anniversary = buy_dt.replace(year=buy_dt.year + 1)
    except ValueError:  # Feb 29 → Feb 28 next year
        anniversary = buy_dt.replace(year=buy_dt.year + 1, day=28)
    return sell_dt > anniversary


def _fifo_realised_grouped(seq: list, fy_start: date, actions: dict | None = None) -> list:
    """
    FIFO realised P&L for ONE entity's chronological Indian-equity trade stream.

    Each sell is matched against the OLDEST open buy lots first (FIFO), mirroring
    broker-console realised P&L and Indian capital-gains reporting — so the number
    finally agrees with the client's own broker statement. This replaces the old
    average-cost method (_avg_cost_realised_grouped) for Indian equity only.

    Gross, price-to-price: gain on a matched slice = qty * (sell_price - buy_price),
    using fill prices only (brokerage/STT/other charges are NOT deducted). Each
    matched slice is split into short-term vs long-term via the lot's own buy date
    (see _is_long_term_equity), so every emitted sell carries st_pnl / lt_pnl.

    Buys before fy_start still establish lot basis; only sells on/after fy_start
    emit a row. One row per qualifying sell (aggregation into per-symbol subtotals,
    if wanted, is a display concern for the caller).

    If a sell cannot be fully covered by known lots (purchase history is
    incomplete), the whole row is flagged unknown — purchase_amount / pnl / st_pnl /
    lt_pnl are left None rather than overstating the gain, matching the
    non-overstatement policy of the average-cost engine.

    seq items: {date, kind:'buy'|'sell', units, price, name, sec}. Prices are the
    per-share INR fill price (already INR — the caller scopes to currency='INR').
    Emits: {group, security_name, purchase_amount, sale_date, sale_amount,
            pnl, st_pnl, lt_pnl, return_pct}.
    """
    lots: dict = defaultdict(deque)   # sec -> deque([[buy_date, qty, price], ...])
    out: list = []
    # Pending split/bonus events per security, consumed as the timeline reaches them.
    # Without this a 1:1 bonus makes the holder sell twice what the books show, and the
    # sell is dropped as "unknown" — the quantity is real, it just never arrived as a
    # BUY because the depository credited it. See workers/corporate_actions.py.
    pending = {s: list(v) for s, v in (actions or {}).items()}

    for t in seq:
        sec = t["sec"]
        qty = abs(float(t["units"] or 0))
        px  = float(t["price"] or 0)
        if qty <= 1e-9:
            continue

        # Must run BEFORE the trade is processed: every lot currently open was bought
        # before this date, and anything bought after the ex-date was already scaled.
        if pending.get(sec):
            _ca_apply(lots[sec], pending[sec], t["date"])

        if t["kind"] == "buy":
            lots[sec].append([t["date"], qty, px])
            continue

        # sell — consume FIFO lots for this security
        remaining       = qty
        sale_amount     = qty * px
        purchase_amount = 0.0
        st_pnl          = 0.0
        lt_pnl          = 0.0
        dq              = lots[sec]
        while remaining > 1e-9 and dq:
            lot   = dq[0]
            take  = min(remaining, lot[1])
            gain  = take * (px - lot[2])
            purchase_amount += take * lot[2]
            if _is_long_term_equity(lot[0], t["date"]):
                lt_pnl += gain
            else:
                st_pnl += gain
            lot[1]    -= take
            remaining -= take
            if lot[1] <= 1e-9:
                dq.popleft()

        if t["date"] < fy_start:
            continue

        if remaining > 1e-9:                       # not enough basis to cover the sell
            out.append({"group": t.get("group", "Equity"), "security_name": t["name"],
                        "purchase_amount": None, "sale_date": t["date"],
                        "sale_amount": sale_amount, "pnl": None,
                        "st_pnl": None, "lt_pnl": None, "return_pct": None})
        else:
            pnl = st_pnl + lt_pnl
            ret = (pnl / purchase_amount) if purchase_amount else None
            out.append({"group": t.get("group", "Equity"), "security_name": t["name"],
                        "purchase_amount": purchase_amount, "sale_date": t["date"],
                        "sale_amount": sale_amount, "pnl": pnl,
                        "st_pnl": st_pnl, "lt_pnl": lt_pnl, "return_pct": ret})
    return out


def _fx_rate_on(conn, currency: str, on_date: date) -> Optional[float]:
    """Currency→INR rate as of a date (nearest rate on/before; else earliest available).

    Trade-date FX: each leg of a foreign trade is valued at the rate on its own date,
    so realised P&L captures currency movement as well as price movement. Accuracy of
    historical legs depends on fx_rate being backfilled (see fx_backfill_worker.py);
    returns None when no rate exists at all (caller skips the leg).
    """
    if not currency or currency.upper() == "INR":
        return 1.0
    cur = conn.cursor()
    try:
        cur.execute("""SELECT rate FROM fx_rate
                       WHERE from_currency = %s AND to_currency = 'INR' AND rate_date <= %s
                       ORDER BY rate_date DESC LIMIT 1""", (currency, on_date))
        row = cur.fetchone()
        if not row:  # no rate on/before the date — fall back to the earliest we have
            cur.execute("""SELECT rate FROM fx_rate
                           WHERE from_currency = %s AND to_currency = 'INR'
                           ORDER BY rate_date ASC LIMIT 1""", (currency,))
            row = cur.fetchone()
    finally:
        cur.close()
    return float(row["rate"]) if row else None


# asset_class → realised-sheet subgroup. Driven off security_master.asset_class
# rather than security_type so the sheet buckets the same way the holdings
# sections do: gold/silver funds are precious metals, not Equity, and debt/liquid
# both land in Fixed Income (the old MF_DEBT-only test filed liquid under Equity).
# Arbitrage stays Equity — that is how it is taxed, which is what this sheet is for.
_MF_REALISED_GROUP = {
    "FIXED_INCOME": "Fixed Income",
    "GOLD_SILVER":  "Commodities",
    "ALTERNATES":   "Alternates",
}


def _mf_realised_group(asset_class: Optional[str], security_type: Optional[str]) -> str:
    ac = (asset_class or "").upper().strip()
    if ac in _MF_REALISED_GROUP:
        return _MF_REALISED_GROUP[ac]
    if ac:
        return "Equity"
    # asset_class missing (pre-classification rows) — fall back to the old test.
    return "Fixed Income" if security_type == "MF_DEBT" else "Equity"


def _fetch_realised_gains(conn, entity_ids: list, as_of: date, *,
                          since_inception: bool = False,
                          include_switches: bool = True,
                          by_broker: bool = False) -> list:
    """
    Realised gains for the given entity/entities.
    MF — auto from mf_transaction (REDEMPTION/SWITCH_OUT vs avg cost).
    Equity — from stock_transaction (SELL vs avg cost) once trades are imported.

    since_inception   — when True, count every sell on/after inception instead of
                        only the current FY (defaults to FY-to-date).
    include_switches  — when True (default), SWITCH_IN/SWITCH_OUT count as buys/sells;
                        when False they are dropped entirely (only real
                        PURCHASE/REDEMPTION flows feed cost basis and realisations).
    by_broker         — when True, FIFO/avg-cost lots are partitioned per demat
                        (broker), so a stock held at two brokers realises against
                        its own lots at each. Every emitted row carries `broker`
                        (None for MF / real estate, which have no demat). The
                        default per-entity view leaves broker None and is unchanged.
    """
    fy = date(1900, 1, 1) if since_inception else _fy_start(as_of)
    out: list = []
    cur = conn.cursor()
    ph = ",".join(["%s"] * len(entity_ids))

    # `category` is the asset bucket the Realised Gains page groups by (Equity vs
    # Mutual Funds vs …). It is intentionally separate from `group` (Fixed Income /
    # Equity / Alternates) which the XLSX realised sheet buckets by, so MF and stock
    # rows stay distinguishable on the page without disturbing the report.
    def _add(rows, category, broker=None):
        for r in rows:
            r["category"] = category
            r["broker"]   = broker
            out.append(r)

    # ---- MF ----
    # Mutual funds are held via CAS folios/AMCs, not a broker demat, so they carry
    # no broker even in the by_broker view.
    cur.execute(f"""
        SELECT t.security_id, sm.security_name, sm.security_type, sm.asset_class,
               t.transaction_date AS d, t.transaction_type AS tt, t.amount, t.units
        FROM mf_transaction t JOIN security_master sm ON sm.id = t.security_id
        WHERE t.entity_id IN ({ph})
        ORDER BY t.security_id, t.transaction_date
    """, entity_ids)
    BUY  = {"PURCHASE", "PURCHASE_SIP"} | ({"SWITCH_IN"}  if include_switches else set())
    SELL = {"REDEMPTION"}               | ({"SWITCH_OUT"} if include_switches else set())
    by_sec: dict = defaultdict(list)
    for r in cur.fetchall():
        by_sec[r["security_id"]].append(r)
    for txns in by_sec.values():
        seq = []
        for r in txns:
            kind = "buy" if r["tt"] in BUY else ("sell" if r["tt"] in SELL else None)
            if not kind:
                continue
            grp = _mf_realised_group(r["asset_class"], r["security_type"])
            seq.append({"date": r["d"], "kind": kind, "units": r["units"],
                        "amount": r["amount"], "name": r["security_name"], "group": grp})
        _add(_avg_cost_realised(seq, fy), "Mutual Funds")

    # ---- Indian equity (stock_transaction, currency='INR'; empty until imports) ----
    # FIFO lot-matching per entity as one chronological stream across all stocks:
    # each sell consumes the oldest open buy lots, so realised P&L mirrors the broker
    # console and Indian capital-gains reporting, and each sell splits into short- vs
    # long-term (see _fifo_realised_grouped). Gross, price-to-price (per-share `price`;
    # charges not deducted). Scoped to currency='INR' so Vested (USD) rows that also
    # live in stock_transaction are excluded here and handled by the foreign branch /
    # equity_trade_ledger. Ordering within a date falls back to insert order (id).
    try:
        cur.execute(f"""
            SELECT t.entity_id, t.security_id, sm.security_name, sm.isin,
                   t.transaction_date AS d, t.transaction_type AS tt,
                   t.price, t.quantity AS units,
                   -- Imported tradebook rows leave `broker` NULL and carry the executing
                   -- broker in `source` (zerodha/angel_one/dhan); only live/snapshot/manual
                   -- rows populate `broker` directly. Derive it the same way the rest of the
                   -- codebase does (fy_returns_worker, equity_txn_metrics_worker) so the demat
                   -- view isn't dominated by a "No demat" bucket of every imported trade.
                   COALESCE(t.broker,
                            CASE WHEN t.source IN ('zerodha','angel_one','dhan')
                                 THEN t.source END) AS broker
            FROM stock_transaction t JOIN security_master sm ON sm.id = t.security_id
            WHERE t.entity_id IN ({ph})
              AND COALESCE(t.currency, 'INR') = 'INR'
            ORDER BY t.entity_id, t.transaction_date, t.id
        """, entity_ids)
        srows = cur.fetchall()
    except Exception:
        conn.rollback(); srows = []
    try:
        ca_actions = _ca_load(cur)
    except Exception:
        conn.rollback(); ca_actions = {}
    # Gold/silver ETFs and SGBs live in stock_transaction like any other scrip —
    # security_master carries no asset_class for them (equity_holding does, and a
    # fully-sold position is pruned from there), so classify from symbol/ISIN with
    # the shared classifier, admin overrides included.
    try:
        _ac_ovr = _ac_overrides(cur)
    except Exception:
        conn.rollback(); _ac_ovr = {}
    # Per-entity by default; per (entity, broker) in the demat view so a stock held
    # at two brokers FIFO-matches against its own lots at each. The broker is not part
    # of the FIFO lot key itself — partitioning the input stream already keeps the
    # brokers' lots apart.
    eq_by_entity: dict = defaultdict(list)
    for r in srows:
        key = (r["entity_id"], r["broker"]) if by_broker else (r["entity_id"], None)
        eq_by_entity[key].append(r)
    for (_eid, bk), txns in eq_by_entity.items():
        seq = []
        for r in txns:
            tt = (r["tt"] or "").upper()
            kind = "buy" if tt in ("BUY", "B", "PURCHASE") else ("sell" if tt in ("SELL", "S", "SALE") else None)
            if not kind:
                continue
            cls = _ac_classify(r["security_name"], r["isin"], _ac_ovr)
            seq.append({"date": r["d"], "kind": kind, "units": r["units"],
                        "price": r["price"], "name": r["security_name"],
                        "group": "Commodities" if cls in _AC_COMMODITY else "Equity",
                        "sec": r["security_id"]})
        _add(_fifo_realised_grouped(seq, fy, ca_actions), "Equity", broker=bk)

    # ---- Foreign equity (equity_trade_ledger; native cash flows → INR at trade-date FX) ----
    # Switches do not exist for brokers, so include_switches is irrelevant here.
    # cash_flow_native is signed (BUY negative / SELL positive); we avg-cost on |amount|
    # converted to INR at each leg's own trade-date rate, so realised P&L embeds FX gain.
    try:
        cur.execute(f"""
            SELECT symbol, isin, trade_date AS d, side,
                   quantity AS units, currency, cash_flow_native, broker
            FROM equity_trade_ledger
            WHERE entity_id IN ({ph})
            ORDER BY symbol, trade_date
        """, entity_ids)
        frows = cur.fetchall()
    except Exception:
        conn.rollback(); frows = []
    fe_by_sec: dict = defaultdict(list)
    for r in frows:
        key = (r["symbol"], r["isin"], r["broker"]) if by_broker else (r["symbol"], r["isin"], None)
        fe_by_sec[key].append(r)
    for (_sym, _isin, bk), txns in fe_by_sec.items():
        seq = []
        for r in txns:
            side = (r["side"] or "").upper()
            kind = "buy" if side == "BUY" else ("sell" if side == "SELL" else None)
            if not kind:
                continue
            fx = _fx_rate_on(conn, r["currency"], r["d"])
            if fx is None:
                continue  # no rate available — cannot express this leg in INR
            amt_inr = abs(float(r["cash_flow_native"] or 0)) * fx
            seq.append({"date": r["d"], "kind": kind, "units": r["units"],
                        "amount": amt_inr, "name": r["symbol"], "group": "Foreign Equity"})
        _add(_avg_cost_realised(seq, fy), "Foreign Equity", broker=bk)

    # ---- PMS (pms_realised; broker-provided realised P&L, already computed) ----
    # No avg-cost: the statement gives cost/proceeds/P&L per lot directly. INR only.
    try:
        cur.execute(f"""
            SELECT security_name, sale_date AS d, purchase_amount, sale_amount, pnl
            FROM pms_realised
            WHERE entity_id IN ({ph}) AND sale_date >= %s
            ORDER BY sale_date
        """, entity_ids + [fy])
        prows = cur.fetchall()
    except Exception:
        conn.rollback(); prows = []
    for r in prows:
        pnl = float(r["pnl"]) if r["pnl"] is not None else None
        pa  = float(r["purchase_amount"]) if r["purchase_amount"] is not None else None
        ret = (pnl / pa) if (pnl is not None and pa) else None
        out.append({"group": "PMS", "category": "PMS", "security_name": r["security_name"],
                    "purchase_amount": pa, "sale_date": r["d"],
                    "sale_amount": float(r["sale_amount"]) if r["sale_amount"] is not None else None,
                    "pnl": pnl, "return_pct": ret})

    cur.close()
    return out


def build_weekly_sheet(wb, sheet_title: str, label: str, bundle: dict, as_of: date,
                       realised: list = None):
    """Create a data-driven Weekly Report sheet; sections appear only when non-empty.
    The FY-to-date Realised P&L detail (when any) is appended at the bottom of this
    same sheet — there is no separate Realised P&L sheet."""
    ws = wb.create_sheet(sheet_title)
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A3"

    mf   = bundle["mf"]
    eq   = bundle["eq"]
    fe   = bundle.get("fe", [])
    comm = bundle.get("comm", [])
    pms_managed = bundle.get("pms", [])
    cash = bundle.get("cash", [])
    bank = bundle.get("bank", [])
    man  = bundle["manual_by_cat"]

    def _cmv(rows):
        return sum(float(r["current_value"]) for r in rows if r.get("current_value")) or 0.0
    total_cmv = (_cmv(mf) + _cmv(eq) + _cmv(fe) + _cmv(comm)
                 + _cmv(pms_managed) + _cmv(cash) + _cmv(bank)
                 + sum(_cmv(v) for v in man.values()))

    # Title
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=NWK)
    t = ws.cell(row=1, column=1,
                value=f"PERFORMANCE SUMMARY — {label}    |    As on {as_of.strftime('%d %b %Y')}")
    t.font = GOLD_HDR_FONT; t.fill = GOLD_FILL
    t.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 22

    # Column headers
    for c, (name, _) in enumerate(WK_COLS, 1):
        cell = ws.cell(row=2, column=c, value=name)
        cell.font = GOLD_COL_FONT; cell.fill = GOLD_FILL; cell.border = GOLD_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[2].height = 28

    state = {"row": 3, "grand": True}

    def data_row(h, alt):
        r = state["row"]
        cmv  = _v(h, "current_value")
        cost = _v(h, "cost")
        prev = _v(h, "prev_week_value")
        m31  = _v(h, "market_value_as_on")
        wk   = _v(h, "weekly_change")
        if wk is None and cmv is not None and prev is not None:
            wk = cmv - prev
        exp = (cmv / total_cmv) if (cmv and total_cmv) else None
        label_txt = h.get("security_name") or h.get("symbol") or h.get("label") or ""
        remark    = h.get("broker") or h.get("remarks") or h.get("notes") or ""
        inc       = h.get("first_invested_date") or h.get("inception_date")
        vals = [label_txt, inc, prev, m31, cost, exp, cmv, wk,
                _v(h, "pnl_ytd"), _v(h, "pnl_inception"),
                _wk_pct(h, "returns_ytd_pct"), _wk_pct(h, "returns_inception_pct"),
                _wk_pct(h, "cagr_inception_pct"), remark]
        fill = BAND_FILL if alt else WHITE_FILL
        for c, val in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.fill = fill; cell.border = GOLD_BORDER; cell.font = BODY_FONT
            if c == 1:
                cell.alignment = Alignment(horizontal="left", indent=1, wrap_text=True)
            elif c == 2:
                cell.number_format = "dd-mmm-yy"; cell.alignment = Alignment(horizontal="center")
            elif c in _MONEY_COLS:
                cell.number_format = MONEY_FMT;   cell.alignment = Alignment(horizontal="right")
            elif c in _PCT_COLS:
                cell.number_format = PCT_FMT;     cell.alignment = Alignment(horizontal="right")
            else:
                cell.alignment = Alignment(horizontal="left", wrap_text=True)
        state["row"] += 1

    def total_row(text, rows, fill=YELLOW_FILL):
        r = state["row"]
        def s(k):
            return sum(float(x[k]) for x in rows if x.get(k) is not None) or None
        cmv = s("current_value"); cost = s("cost")
        prev = s("prev_week_value")
        wk = (cmv - prev) if (cmv is not None and prev is not None) else None
        pnl_inc = s("pnl_inception")
        ret_inc = (pnl_inc / cost) if (pnl_inc is not None and cost) else None
        exp = (cmv / total_cmv) if (cmv and total_cmv) else None
        vals = [text, None, prev, s("market_value_as_on"), cost, exp, cmv, wk,
                s("pnl_ytd"), pnl_inc, None, ret_inc, None, None]
        for c, val in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.fill = fill; cell.border = GOLD_BORDER; cell.font = GTOT_FONT
            if c == 1:
                cell.alignment = Alignment(horizontal="left", indent=1)
            elif c in _MONEY_COLS:
                cell.number_format = MONEY_FMT; cell.alignment = Alignment(horizontal="right")
            elif c in _PCT_COLS:
                cell.number_format = PCT_FMT;   cell.alignment = Alignment(horizontal="right")
        state["row"] += 1

    def subsection(name, rows):
        if not rows:
            return
        for i, h in enumerate(rows):
            data_row(h, i % 2 == 1)
        total_row(f"Total {name}", rows)

    def banner(text):
        r = state["row"]
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NWK)
        cell = ws.cell(row=r, column=1, value=text)
        cell.font = GOLD_HDR_FONT; cell.fill = GOLD_FILL
        cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[r].height = 16
        state["row"] += 1

    # ---- assemble groups → subsections (rendered only when non-empty) ----
    mf_by = lambda t: [h for h in mf if h.get("security_type") == t]
    groups = [
        ("A. FIXED INCOME", [
            ("MF Debt / Liquid Funds", mf_by("MF_DEBT")),
            ("MF Liquid (Manual)",     man.get("liquid_fund", [])),
            ("MF Debt (Manual)",       man.get("debt_fund", [])),
            ("Arbitrage Fund",         man.get("arbitrage_fund", [])),
            ("Public Provident Fund",  man.get("ppf", [])),
            ("Other Fixed Income",     man.get("fixed_income", [])),
        ]),
        ("B. EQUITY", [
            ("MF Equity Funds",        mf_by("MF_EQUITY")),
            ("MF Hybrid Funds",        mf_by("MF_HYBRID")),
            ("Direct Equities",        eq),
            ("Direct Equity (Manual)", man.get("direct_equity", [])),
            ("Foreign Equity",         fe),
            ("PMS (Managed)",          pms_managed),
            ("PMS (Manual)",           man.get("pms", [])),
            ("AIF",                    man.get("aif", [])),
            ("Futures & Options",      man.get("fno", [])),
            ("Foreign Funds (Manual)", man.get("overseas_fund", []) + man.get("overseas_equity", [])),
        ]),
        ("C. COMMODITIES & PRECIOUS METALS", [
            ("Gold / Silver / Commodities", comm),
            ("Gold / Silver ETF (Manual)",  man.get("gold_etf", [])),
        ]),
        ("D. ALTERNATES", [
            ("Unlisted Equity",        man.get("unlisted", [])),
            ("Startups",               man.get("startup", [])),
            ("Forex / Foreign Cash",   man.get("forex", [])),
            # NRE balances are in rupees but sit on the non-resident side of the
            # book, so they report under Alternates with forex, not under Liquidity.
            ("NRE Accounts",           man.get("nre_bank", [])),
        ]),
        ("E. LIQUIDITY", [
            ("Broker Cash",            cash),
            ("Bank Accounts",          bank),
            ("Funds in Transit",       man.get("funds_transit", [])),
            ("Broker Balance (Manual)", man.get("broker_balance", [])),
            ("Funds in Bank (Manual)", man.get("bank", [])),
        ]),
    ]

    all_rows = []
    for gtitle, subs in groups:
        if not any(rows for _, rows in subs):
            continue
        banner(gtitle)
        for name, rows in subs:
            if rows:
                banner_sub = ws.cell(row=state["row"], column=1, value=name)
                banner_sub.font = SUBSEC_FONT
                ws.merge_cells(start_row=state["row"], start_column=1, end_row=state["row"], end_column=NWK)
                state["row"] += 1
                subsection(name, rows)
                all_rows.extend(rows)

    if all_rows:
        total_row("GRAND TOTAL", all_rows, fill=GOLD_FILL)
        gr = state["row"] - 1
        for c in range(1, NWK + 1):
            ws.cell(row=gr, column=c).font = Font(name="Calibri", bold=True, color="FFFFFF", size=9)
    else:
        ws.cell(row=state["row"], column=1,
                value="No holdings on record for this entity.").font = LABEL_FONT

    # ---- Realised P&L detail — appended at the bottom of this same sheet ----
    if realised:
        state["row"] += 2
        state["row"] = _render_realised_block(ws, state["row"], label, realised, as_of, width=NWK)

    for c, (_, width) in enumerate(WK_COLS, 1):
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.print_title_rows = "1:2"
    return ws


REAL_COLS = [
    ("ASSET CLASS / SECURITY", 46),
    ("Purchase Amount",        16),
    ("Sale Date",              13),
    ("Sale Amount",            16),
    ("Profit / Loss",          16),
    ("Returns %",              11),
]


def _render_realised_block(ws, start_row: int, label: str, rows: list, as_of: date,
                           width: Optional[int] = None) -> int:
    """Render the FY-to-date Realised P&L table into `ws` starting at `start_row`,
    returning the next free row.

    One section per asset `category` (Equity / Mutual Funds / Foreign Equity / PMS,
    then any other present), mirroring the Realised Gains page's split and order,
    with Fixed Income / Equity sub-headers inside Mutual Funds. Used to append the
    realised detail to the bottom of a Weekly Report sheet (no separate sheet).
    `width` is how far the title/note banners span (defaults to the table's own
    6 columns; pass the host sheet's column count when embedding).
    """
    NRC = len(REAL_COLS)
    w = width or NRC
    state = {"row": start_row}

    def emit(fill, font, vals):
        r = state["row"]
        for c, val in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.fill = fill; cell.border = GOLD_BORDER; cell.font = font
            if c == 1:
                cell.alignment = Alignment(horizontal="left", indent=1, wrap_text=True)
            elif c == 3:
                cell.number_format = "dd-mmm-yy"; cell.alignment = Alignment(horizontal="center")
            elif c == 6:
                cell.number_format = PCT_FMT; cell.alignment = Alignment(horizontal="right")
            else:
                cell.number_format = MONEY_FMT; cell.alignment = Alignment(horizontal="right")
        state["row"] += 1

    # Title banner
    r = state["row"]
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=w)
    t = ws.cell(row=r, column=1,
                value=f"REALISED PROFIT & LOSS — {label}    |    FY {_fy_start(as_of).year}-"
                      f"{(_fy_start(as_of).year + 1) % 100:02d}   (as on {as_of.strftime('%d %b %Y')})")
    t.font = GOLD_HDR_FONT; t.fill = GOLD_FILL
    t.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[r].height = 22
    state["row"] += 1

    # Column headers
    for c, (name, _) in enumerate(REAL_COLS, 1):
        cell = ws.cell(row=state["row"], column=c, value=name)
        cell.font = GOLD_COL_FONT; cell.fill = GOLD_FILL; cell.border = GOLD_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[state["row"]].height = 26
    state["row"] += 1

    # Sectioning by category, falling back to `group` for legacy rows.
    def _cat(rr):
        return rr.get("category") or rr["group"]

    _KNOWN = ["Equity", "Mutual Funds", "Foreign Equity", "PMS"]
    _present = {_cat(r) for r in rows}
    categories = [c for c in _KNOWN if c in _present] + sorted(_present - set(_KNOWN))
    _GROUP_ORDER = ["Fixed Income", "Equity", "Foreign Equity", "PMS",
                    "Commodities", "Alternates"]

    def _sub_header(text):
        r = state["row"]
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NRC)
        cell = ws.cell(row=r, column=1, value=text)
        cell.font = SUBSEC_FONT; cell.fill = SUBSEC_FILL
        cell.alignment = Alignment(horizontal="left", indent=2)
        state["row"] += 1

    any_rows = False
    for category in categories:
        grp = [r for r in rows if _cat(r) == category]
        if not grp:
            continue
        any_rows = True
        r = state["row"]
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NRC)
        b = ws.cell(row=r, column=1, value=category.upper())
        b.font = GOLD_HDR_FONT; b.fill = GOLD_FILL
        b.alignment = Alignment(horizontal="left", indent=1)
        state["row"] += 1

        present_groups = {(rr.get("group") or category) for rr in grp}
        subgroups = ([g for g in _GROUP_ORDER if g in present_groups]
                     + sorted(present_groups - set(_GROUP_ORDER)))
        show_sub = len(subgroups) > 1
        band = 0
        for sg in subgroups:
            sg_rows = [rr for rr in grp if (rr.get("group") or category) == sg]
            if not sg_rows:
                continue
            if show_sub:
                _sub_header(sg)
            for rr in sg_rows:
                emit(BAND_FILL if band % 2 else WHITE_FILL, BODY_FONT,
                     [rr["security_name"], rr["purchase_amount"], rr["sale_date"],
                      rr["sale_amount"], rr["pnl"], rr["return_pct"]])
                band += 1

        pa = sum(r["purchase_amount"] for r in grp if r["purchase_amount"] is not None) or None
        sa = sum(r["sale_amount"] for r in grp if r["sale_amount"] is not None) or None
        pl = sum(r["pnl"] for r in grp if r["pnl"] is not None) or None
        ret = (pl / pa) if (pl is not None and pa) else None
        emit(YELLOW_FILL, GTOT_FONT, [f"Total {category}", pa, None, sa, pl, ret])

    if any_rows:
        pa = sum(r["purchase_amount"] for r in rows if r["purchase_amount"] is not None) or None
        sa = sum(r["sale_amount"] for r in rows if r["sale_amount"] is not None) or None
        pl = sum(r["pnl"] for r in rows if r["pnl"] is not None) or None
        ret = (pl / pa) if (pl is not None and pa) else None
        gt = state["row"]
        emit(GOLD_FILL, Font(name="Calibri", bold=True, color="FFFFFF", size=9),
             ["GRAND TOTAL", pa, None, sa, pl, ret])
        ws.cell(row=gt, column=1).font = Font(name="Calibri", bold=True, color="FFFFFF", size=9)
    else:
        ws.cell(row=state["row"], column=1,
                value="No realised gains/losses recorded for this financial year.").font = LABEL_FONT
        state["row"] += 1

    note_r = state["row"] + 1
    note = ws.cell(row=note_r, column=1,
                   value="MF realised auto-computed from CAS transactions (average cost). "
                         "Equity realised appears once broker trades are imported. "
                         "Blank cost basis = original purchase predates available transaction history.")
    note.font = LABEL_FONT
    ws.merge_cells(start_row=note_r, start_column=1, end_row=note_r, end_column=w)
    return note_r + 1


# ── register workbook (properties + art / collectibles) ──────────────────────
#
# These two registers are deliberately kept OUT of the portfolio reports and out
# of the dashboard totals (owner decision: they are standalone asset registers,
# not part of the tradeable book). They used to have sections in the entity and
# master workbooks that silently rendered empty — properties moved to their own
# `property` table in the 2026-07-13 rebuild and the report kept reading the old
# manual_input category, while the Art line read `art` after everything had been
# recategorised to `collectibles`. Both now live here, read from the right source,
# and the dead sections are gone from the portfolio reports.
#
# Every sheet gets an Excel AutoFilter on its header row, so each column can be
# filtered/sorted from its own dropdown.

# Valuation must match what the Properties page shows, or the register is worse
# than useless. Both constants are imported from the modules that own them rather
# than re-declared here — a second copy of 1.75 is exactly how the two drift apart.
try:                                          # worker runs with mis-portal/ on the path
    from property_docs import FAIR_VALUE_MULTIPLIER
except ImportError:                           # pragma: no cover — layout fallback
    from mis_portal.property_docs import FAIR_VALUE_MULTIPLIER  # type: ignore

OLD_LEASE_OWNER_SHARE = 0.5   # statutory sitting tenant holds the other half


def _fetch_property_register(conn):
    """Every property with its holder, valuation inputs and effective fair value.

    The value expression mirrors _fetch_property_overview_rows in main.py exactly:
    sale_price once sold; else land (hand-entered market_land_value, else
    area x RRR x the fair-value multiplier) plus summed floor costings; halved for
    an old statutory lease. Getting any of those wrong would put a number in the
    register that the Properties page contradicts.
    """
    val_expr = (
        f"CASE WHEN p.sold THEN p.sale_price ELSE "
        f"(COALESCE(p.market_land_value, p.area * p.rrr * {FAIR_VALUE_MULTIPLIER}) "
        f" + COALESCE(fv.bval, 0)) "
        f"* CASE WHEN p.is_old_lease THEN {OLD_LEASE_OWNER_SHARE} ELSE 1 END END"
    )
    cur = conn.cursor()
    cur.execute(f"""
        WITH fv AS (
            SELECT property_id,
                   SUM(COALESCE(built_up_area, area) * rate_per_unit) AS bval
            FROM property_floor WHERE rate_per_unit IS NOT NULL
            GROUP BY property_id
        )
        SELECT p.name, p.property_type, e.name AS holder_name, p.ownership,
               p.village, p.taluka, p.address, p.survey_no, p.property_no,
               p.area, p.area_unit, p.built_up_area, p.tenure, p.is_old_lease,
               p.acquisition_date, p.purchase_price, p.rrr, p.market_land_value,
               p.sold, p.sale_date, p.sale_price, p.notes,
               {val_expr} AS fair_value
        FROM   property p
        JOIN   property_entity e ON e.id = p.holder_id
        LEFT   JOIN fv ON fv.property_id = p.id
        ORDER  BY e.sort_order, e.name, p.sold, p.name
    """)
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_art_register(conn):
    """Latest art + collectibles entry per (entity, label), with painter/location
    detail. Both categories together: the split is a page-level distinction, and
    for a register you want the one list."""
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (m.entity_id, m.category, m.label)
               m.category, m.label, e.entity_name, m.cost, m.current_value,
               m.currency, m.inception_date, m.notes, m.updated_at,
               d.painter_name, d.location, d.seller_name, d.seller_address
        FROM   manual_input m
        JOIN   entity e ON e.id = m.entity_id
        LEFT   JOIN art_detail d
               ON d.entity_id = m.entity_id AND d.label = m.label
        WHERE  m.category IN ('art', 'collectibles')
        ORDER  BY m.entity_id, m.category, m.label, m.updated_at DESC
    """)
    rows = cur.fetchall()
    cur.close()
    return rows


def _register_sheet(ws, title: str, headers: list, rows: list, widths: list):
    """Title row, filterable header row, then the data. Shared by both registers
    so they read as one document."""
    ncols = len(headers)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    c = ws.cell(row=1, column=1, value=title)
    c.font, c.fill = HDR_FONT, HDR_FILL
    c.alignment = Alignment(horizontal="center", vertical="center")

    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=2, column=i, value=h)
        c.font, c.fill = SUB_FONT, SUB_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = widths[i - 1]

    for r, values in enumerate(rows, start=3):
        for i, v in enumerate(values, start=1):
            c = ws.cell(row=r, column=i, value=v)
            c.font = BODY_FONT
            if r % 2 == 1:
                c.fill = ALT_FILL
            if isinstance(v, (int, float)):
                c.number_format = '#,##0'
                c.alignment = Alignment(horizontal="right")

    # The filter dropdowns the registers are meant to be worked through.
    last = f"{get_column_letter(ncols)}{max(2, len(rows) + 2)}"
    ws.auto_filter.ref = f"A2:{last}"
    ws.freeze_panes = "A3"
    return ws


def build_register_workbook(conn, as_of: date):
    """Standalone register workbook: Properties + Art & Collectibles, all entities.
    Returns None when both registers are empty, so no pointless file is written."""
    props = _fetch_property_register(conn)
    art   = _fetch_art_register(conn)
    if not props and not art:
        return None

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    if props:
        headers = ["Property", "Type", "Holder", "Ownership", "Village", "Taluka",
                   "Address", "Survey No", "Property No", "Area", "Unit",
                   "Built-up Area", "Tenure", "Old Lease", "Acquired",
                   "Purchase Price (₹)", "RRR (₹)", "Fair Value (₹)", "Status",
                   "Sale Date", "Sale Price (₹)", "Notes"]
        widths  = [28, 12, 20, 14, 14, 14, 30, 14, 14, 10, 8, 12, 12, 10, 12,
                   16, 14, 16, 10, 12, 16, 30]
        rows = []
        for p in props:
            rows.append([
                p["name"], p["property_type"], p["holder_name"], p["ownership"],
                p["village"], p["taluka"], p["address"], p["survey_no"],
                p["property_no"],
                float(p["area"]) if p["area"] is not None else None,
                p["area_unit"],
                float(p["built_up_area"]) if p["built_up_area"] is not None else None,
                p["tenure"],
                "Yes" if p["is_old_lease"] else "",
                p["acquisition_date"].isoformat() if p["acquisition_date"] else None,
                float(p["purchase_price"]) if p["purchase_price"] is not None else None,
                float(p["rrr"]) if p["rrr"] is not None else None,
                float(p["fair_value"]) if p["fair_value"] is not None else None,
                "Sold" if p["sold"] else "Held",
                p["sale_date"].isoformat() if p["sale_date"] else None,
                float(p["sale_price"]) if p["sale_price"] is not None else None,
                p["notes"],
            ])
        _register_sheet(wb.create_sheet("Properties"),
                        f"PROPERTY REGISTER — as of {as_of:%d %b %Y}",
                        headers, rows, widths)

    if art:
        headers = ["Item", "Class", "Entity", "Painter", "Kept At", "Acquired",
                   "Purchase Price", "Current Valuation", "Currency",
                   "Seller", "Seller Address", "Last Updated", "Notes"]
        widths  = [30, 14, 20, 22, 20, 12, 16, 18, 10, 22, 30, 14, 30]
        rows = []
        for a in art:
            rows.append([
                a["label"],
                "Collectible" if a["category"] == "collectibles" else "Art",
                a["entity_name"], a["painter_name"], a["location"],
                a["inception_date"].isoformat() if a["inception_date"] else None,
                float(a["cost"]) if a["cost"] is not None else None,
                float(a["current_value"]) if a["current_value"] is not None else None,
                a["currency"], a["seller_name"], a["seller_address"],
                a["updated_at"].strftime("%Y-%m-%d") if a["updated_at"] else None,
                a["notes"],
            ])
        _register_sheet(wb.create_sheet("Art & Collectibles"),
                        f"ART & COLLECTIBLES REGISTER — as of {as_of:%d %b %Y}",
                        headers, rows, widths)

    return wb


def build_master_workbook(conn, as_of: date):
    """
    Consolidated MIS workbook, fully data-driven:
      • shared sheets: Equity Daily Print, All Assets Daily MIS
      • per-PAN-group Weekly sheet  (groups with >1 entity, via pan_group)
      • per-entity   Weekly sheet  (one per DB entity, named by code)
    Each Weekly sheet shows the entity/group's real holdings; broker-merged equity;
    a section appears only when it has data.  (Realised P&L sheets: Workstream B.)
    """
    wb = openpyxl.Workbook()

    # Shared sheet 1 — Equity Daily Print (uses default active sheet)
    entities = _fetch_entities(conn)
    eq_codes = [e["entity_name"] for e in entities]
    build_equity_daily_print(conn, eq_codes, as_of, ws=wb.active)

    # Shared sheet 2 — All Assets Daily MIS
    build_combined_report(conn, as_of, ws=wb.create_sheet("All Assets Daily MIS"))

    # Shared sheet 3 — Foreign Equity Print (per-holding native detail).
    # Created only when foreign holdings exist (else the helper returns None and
    # leaves no empty sheet behind).
    fe_ws = wb.create_sheet("Foreign Equity Print")
    if build_foreign_equity_print(conn, as_of, ws=fe_ws) is None:
        wb.remove(fe_ws)

    def _emit(label, ids):
        bundle   = _bundle_for(conn, ids, as_of)
        realised = _fetch_realised_gains(conn, ids, as_of)
        # Realised P&L is appended at the bottom of the Weekly sheet — no separate sheet.
        build_weekly_sheet(wb, f"{label} Weekly Report", label, bundle, as_of, realised)

    # Per-group sheets (Weekly + realised-at-bottom)
    for g in _pan_groups(conn):
        _emit(g["label"], g["entity_ids"])

    # Per-entity sheets (Weekly + realised-at-bottom)
    for e in entities:
        _emit(e["entity_name"], [e["id"]])

    return wb


def build_entity_workbook(conn, entity: dict, as_of: date):
    """One standalone workbook for a single entity: its Weekly Report (holdings
    across every asset class) with the FY-to-date Realised P&L appended at the
    bottom of that same sheet. Saved per entity as ENTITYNAME-DATE.xlsx."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # build_weekly_sheet creates its own sheet
    bundle   = _bundle_for(conn, [entity["id"]], as_of)
    realised = _fetch_realised_gains(conn, [entity["id"]], as_of)
    build_weekly_sheet(wb, "Weekly Report", entity["entity_name"],
                       bundle, as_of, realised)
    return wb


def _safe_filename(name: str) -> str:
    """Filesystem-safe entity name for report filenames (spaces/slashes → '_')."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "entity"


# ── public entry point ────────────────────────────────────────────────────────

def generate_reports(conn, generated_by_user_id: Optional[int] = None) -> list[dict]:
    """
    Generate (1) the consolidated master MIS workbook (all entities) and (2) one
    standalone workbook per entity, named ENTITYNAME-DATE.xlsx. Realised P&L is
    embedded at the bottom of each Weekly sheet — never a separate sheet/report.
    Registers every file in generated_report and returns a row per file.
    """
    as_of  = date.today()
    folder = os.path.join(REPORTS_DIR, as_of.strftime("%Y-%m-%d"))
    os.makedirs(folder, exist_ok=True)

    cur = conn.cursor()
    results: list[dict] = []

    def _register(report_type, entity_id, entity_name, fname, fpath):
        cur.execute("""
            INSERT INTO generated_report
                (report_type, entity_id, entity_name, filename, filepath, as_of_date, generated_by, generated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        """, (report_type, entity_id, entity_name, fname, fpath, as_of, generated_by_user_id))
        results.append({"id": cur.fetchone()["id"], "type": report_type,
                        "entity": entity_name, "filename": fname, "path": fpath})

    # 1) Consolidated master workbook (all entities)
    m_name = f"MIS-Report_{as_of.strftime('%Y%m%d')}.xlsx"
    m_path = os.path.join(folder, m_name)
    build_master_workbook(conn, as_of).save(m_path)
    _register("master", None, "MIS Report — All Entities", m_name, m_path)

    # 2) One standalone workbook per entity (ENTITYNAME-DATE.xlsx)
    for e in _fetch_entities(conn):
        ename = e["entity_name"]
        fname = f"{_safe_filename(ename)}-{as_of.strftime('%Y-%m-%d')}.xlsx"
        fpath = os.path.join(folder, fname)
        build_entity_workbook(conn, e, as_of).save(fpath)
        _register("individual", e["id"], ename, fname, fpath)

    # 3) Register workbook — properties + art/collectibles, all entities. These are
    # standalone registers kept out of the portfolio reports and totals, so they get
    # their own file rather than an empty section in everyone else's.
    reg_wb = build_register_workbook(conn, as_of)
    if reg_wb is not None:
        r_name = f"Registers_{as_of.strftime('%Y%m%d')}.xlsx"
        r_path = os.path.join(folder, r_name)
        reg_wb.save(r_path)
        _register("registers", None, "Registers — Properties & Art", r_name, r_path)

    conn.commit()
    cur.close()
    return results


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv("/var/www/mis-portal/.env")
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        cursor_factory=RealDictCursor,
    )
    results = generate_reports(conn)
    for r in results:
        print(f"✅  {r['type']:8s} {r['entity']:30s} → {r['filename']}")
    conn.close()
