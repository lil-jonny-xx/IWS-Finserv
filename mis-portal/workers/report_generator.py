#!/usr/bin/env python3
"""
Report generator — produces per-entity and combined portfolio Excel reports.
Call generate_reports(conn, generated_by_user_id) to create reports.
"""
import os
from datetime import date, datetime
from typing import Optional
from collections import defaultdict
import psycopg2
from psycopg2.extras import RealDictCursor
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

REPORTS_DIR = "/var/www/mis-portal/reports"

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


def _fetch_mf_holdings(conn, entity_id: Optional[int] = None):
    """Fetch MF holdings. DB security_type values: MF_DEBT, MF_EQUITY, MF_HYBRID."""
    cur = conn.cursor()
    q = """
        SELECT
            h.entity_id, e.entity_name,
            sm.security_name, sm.asset_class, sm.security_type,
            h.invested_amount   AS cost,
            h.current_value,
            h.prev_week_value,
            h.market_value_as_on,
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
    if entity_id:
        cur.execute(q.format(where="WHERE h.entity_id = %s"), (entity_id,))
    else:
        cur.execute(q.format(where=""))
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_equity_holdings(conn, entity_id: Optional[int] = None):
    cur = conn.cursor()
    q = """
        SELECT
            eh.entity_id, e.entity_name,
            eh.broker, COALESCE(eh.symbol_override, eh.symbol) AS symbol, eh.isin,
            eh.cost, eh.current_market_value AS current_value,
            eh.prev_week_value, eh.market_value_as_on,
            eh.pnl_ytd, eh.pnl_inception,
            eh.returns_ytd_pct, eh.returns_inception_pct, eh.cagr_inception_pct,
            eh.first_invested_date,
            eh.weekly_change, eh.exposure_pct, eh.remarks
        FROM equity_holding eh
        JOIN entity e ON e.id = eh.entity_id
        {where}
        ORDER BY eh.symbol
    """
    if entity_id:
        cur.execute(q.format(where="WHERE eh.entity_id = %s"), (entity_id,))
    else:
        cur.execute(q.format(where=""))
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


def _fetch_fx_rates(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (from_currency)
            from_currency, rate
        FROM fx_rate
        WHERE to_currency = 'INR'
        ORDER BY from_currency, rate_date DESC
    """)
    rows = cur.fetchall()
    cur.close()
    return {r["from_currency"]: float(r["rate"]) for r in rows}


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

    for cat, label in [("pms", "PMS"), ("aif", "AIF")]:
        items = man_by_cat.get(cat, [])
        if not items:
            continue
        row = _write_section(ws, row, label)
        data_start = row
        for i, m in enumerate(items):
            _write_data_row(ws, row, m, i % 2 == 1); row += 1
        _write_total_row(ws, row, f"Total {label}", (data_start, row - 1)); row += 1

    _write_total_row(ws, row, "B. TOTAL EQUITY", None); row += 2

    # ── Alternates ────────────────────────────────────────────────────────────
    row = _write_section(ws, row, "ALTERNATES")

    for cat, label in [
        ("overseas_fund",   "Overseas Funds"),
        ("overseas_equity", "Overseas Direct Equity"),
        ("forex",           "Forex / Foreign Cash"),
        ("gold_etf",        "Gold / Silver ETF"),
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
        row = _write_section(ws, row, label)
        data_start = row
        for i, m in enumerate(items):
            _write_data_row(ws, row, m, i % 2 == 1); row += 1
        if items:
            _write_total_row(ws, row, f"Total {label.split('. ', 1)[-1]}", (data_start, row - 1))
            row += 1
        else:
            row += 1

    _write_total_row(ws, row, "H. GRAND TOTAL (A+B+D+E+F+G)", None); row += 1

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


def build_combined_report(conn, as_of: date):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "All Assets Daily MIS"
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A3"

    # Main title
    row = 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC)
    hdr = ws.cell(row=row, column=1,
                  value=f"ALL ENTITIES — COMBINED PORTFOLIO MIS   |   As on {as_of.strftime('%d %b %Y')}")
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

        mf_rows  = _fetch_mf_holdings(conn, eid)
        eq_rows  = _merge_equity_by_symbol(_fetch_equity_holdings(conn, eid))
        man_rows = _fetch_manual_inputs(conn, eid)
        man_by_cat: dict = {}
        for m in man_rows:
            man_by_cat.setdefault(m["category"], []).append(m)

        if not mf_rows and not eq_rows and not man_rows:
            continue

        # Entity header row
        _comb_entity_header(ws, row, ename, as_of); row += 1

        # ── Fixed Income ──────────────────────────────────────────────────────
        debt_holds = [h for h in mf_rows if h["security_type"] == "MF_DEBT"]
        ppf_man    = man_by_cat.get("ppf", []) + man_by_cat.get("fixed_income", [])

        if debt_holds or ppf_man:
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

            if ppf_man:
                row = _comb_section(ws, row, "PPF / Other Fixed Income")
                sub_start = row
                alt = False
                for m in ppf_man:
                    _comb_data_row(ws, row, m, "Manual", alt); row += 1; alt = not alt
                _comb_total(ws, row, "Total PPF / Fixed Income", sub_start, row - 1)
                fi_sub_total_rows.append(row); row += 1

            _comb_total(ws, row, "Total Fixed Income"); row += 1

        # ── Equity ────────────────────────────────────────────────────────────
        eq_mf  = [h for h in mf_rows if h["security_type"] == "MF_EQUITY"]
        hyb_mf = [h for h in mf_rows if h["security_type"] == "MF_HYBRID"]
        pms    = man_by_cat.get("pms", [])
        aif    = man_by_cat.get("aif", [])

        if eq_mf or hyb_mf or eq_rows or pms or aif:
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

            if eq_rows:
                row = _comb_section(ws, row, "Direct Equities")
                sub_start = row
                alt = False
                for h in eq_rows:
                    _comb_data_row(ws, row, h, h.get("broker", "Broker"), alt); row += 1; alt = not alt
                _comb_total(ws, row, "Total Direct Equities", sub_start, row - 1); row += 1

            if pms:
                row = _comb_section(ws, row, "PMS")
                sub_start = row
                alt = False
                for m in pms:
                    _comb_data_row(ws, row, m, "PMS", alt);  row += 1; alt = not alt
                _comb_total(ws, row, "Total PMS", sub_start, row - 1); row += 1

            if aif:
                row = _comb_section(ws, row, "AIF")
                sub_start = row
                alt = False
                for m in aif:
                    _comb_data_row(ws, row, m, "AIF", alt);  row += 1; alt = not alt
                _comb_total(ws, row, "Total AIF", sub_start, row - 1); row += 1

            _comb_total(ws, row, "Total Equity"); row += 1

        # ── Alternates ────────────────────────────────────────────────────────
        alt_cats = [
            ("overseas_fund",   "Overseas Funds"),
            ("overseas_equity", "Overseas Direct Equity"),
            ("forex",           "Forex / Foreign Cash"),
            ("gold_etf",        "Gold / Silver ETF"),
            ("unlisted",        "Unlisted Equity"),
            ("startup",         "Startups"),
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

        # ── Below-the-line ────────────────────────────────────────────────────
        btl = []
        for cat in ("funds_transit", "broker_balance", "bank"):
            btl.extend(man_by_cat.get(cat, []))
        if btl:
            row = _comb_section(ws, row, "Funds in Transit / Bank / Broker")
            data_start = row
            for i, m in enumerate(btl):
                _comb_data_row(ws, row, m, "Bank/Broker", i % 2 == 1); row += 1
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
    return wb


# ── equity daily print report ─────────────────────────────────────────────────

def build_equity_daily_print(conn, entity_codes: list, as_of: date):
    """
    Build a single-sheet Equity Daily Print workbook for the given entity codes
    (e.g. ['DHR', 'HHR', 'SDR']).  Sections: Grand Total (combined), then per entity.
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
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Equity Daily Print"
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A3"

    row = 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NC_EDP)
    hdr = ws.cell(row=row, column=1,
                  value=f"EQUITY DAILY PRINT — RAJANI GROUP   |   As on {as_of.strftime('%d %b %Y')}")
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
    return wb


# ── public entry point ────────────────────────────────────────────────────────

def generate_reports(conn, generated_by_user_id: Optional[int] = None) -> list[dict]:
    """
    Generate all reports (one per entity + one combined).
    Returns list of dicts describing each generated file.
    """
    as_of  = date.today()
    folder = os.path.join(REPORTS_DIR, as_of.strftime("%Y-%m-%d"))
    os.makedirs(folder, exist_ok=True)

    entities = _fetch_entities(conn)
    results  = []
    cur      = conn.cursor()

    for entity in entities:
        eid   = entity["id"]
        ename = entity["entity_name"]
        fname = f"{ename.replace(' ', '_')}_{as_of.strftime('%Y%m%d')}.xlsx"
        fpath = os.path.join(folder, fname)

        wb = build_individual_report(conn, eid, ename, as_of)
        wb.save(fpath)

        cur.execute("""
            INSERT INTO generated_report
                (report_type, entity_id, entity_name, filename, filepath, as_of_date, generated_by, generated_at)
            VALUES ('individual', %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        """, (eid, ename, fname, fpath, as_of, generated_by_user_id))
        report_id = cur.fetchone()["id"]
        results.append({"id": report_id, "type": "individual", "entity": ename,
                         "filename": fname, "path": fpath})

    # Combined report
    fname = f"All_Entities_Combined_{as_of.strftime('%Y%m%d')}.xlsx"
    fpath = os.path.join(folder, fname)
    wb    = build_combined_report(conn, as_of)
    wb.save(fpath)

    cur.execute("""
        INSERT INTO generated_report
            (report_type, entity_id, entity_name, filename, filepath, as_of_date, generated_by, generated_at)
        VALUES ('combined', NULL, 'All Entities', %s, %s, %s, %s, NOW())
        RETURNING id
    """, (fname, fpath, as_of, generated_by_user_id))
    report_id = cur.fetchone()["id"]
    results.append({"id": report_id, "type": "combined", "entity": "All Entities",
                     "filename": fname, "path": fpath})

    # Equity Daily Print (DHR, HHR, SDR)
    EQUITY_DAILY_ENTITIES = ["DHR", "HHR", "SDR"]
    fname = f"Equity_Daily_Print_{as_of.strftime('%Y%m%d')}.xlsx"
    fpath = os.path.join(folder, fname)
    wb    = build_equity_daily_print(conn, EQUITY_DAILY_ENTITIES, as_of)
    wb.save(fpath)

    cur.execute("""
        INSERT INTO generated_report
            (report_type, entity_id, entity_name, filename, filepath, as_of_date, generated_by, generated_at)
        VALUES ('equity_daily', NULL, 'DHR / HHR / SDR', %s, %s, %s, %s, NOW())
        RETURNING id
    """, (fname, fpath, as_of, generated_by_user_id))
    report_id = cur.fetchone()["id"]
    results.append({"id": report_id, "type": "equity_daily", "entity": "DHR / HHR / SDR",
                     "filename": fname, "path": fpath})

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
        print(f"✅  {r['type']:12s} {r['entity']:30s} → {r['filename']}")
    conn.close()
