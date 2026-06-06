#!/usr/bin/env python3
"""
Report generator — produces per-entity and combined portfolio Excel reports.
Call generate_reports(conn, generated_by_user_id) to create reports.
"""
import os
from datetime import date, datetime
from typing import Optional
import psycopg2
from psycopg2.extras import RealDictCursor
import openpyxl
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side, numbers
)
from openpyxl.utils import get_column_letter

REPORTS_DIR = "/var/www/mis-portal/reports"

# ── colour palette (matching bas.xlsx tone) ───────────────────────────────────
HDR_FILL   = PatternFill("solid", fgColor="1F3864")   # dark navy
SUB_FILL   = PatternFill("solid", fgColor="2F5496")   # mid navy
SEC_FILL   = PatternFill("solid", fgColor="D6E4F0")   # light blue section header
TOT_FILL   = PatternFill("solid", fgColor="BDD7EE")   # total row
ALT_FILL   = PatternFill("solid", fgColor="F2F7FC")   # alternating row
WHITE_FILL = PatternFill("solid", fgColor="FFFFFF")

HDR_FONT   = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
SUB_FONT   = Font(name="Calibri", bold=True, color="FFFFFF", size=9)
SEC_FONT   = Font(name="Calibri", bold=True, color="1F3864", size=9)
TOT_FONT   = Font(name="Calibri", bold=True, color="1F3864", size=9)
BODY_FONT  = Font(name="Calibri", size=9)
LABEL_FONT = Font(name="Calibri", italic=True, size=8, color="595959")

THIN  = Side(style="thin", color="BDD7EE")
MED   = Side(style="medium", color="2F5496")
THIN_BORDER  = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
MED_BORDER   = Border(left=MED, right=MED, top=MED, bottom=MED)

INR_FMT   = '₹#,##0.00'
PCT_FMT   = '0.00%'
DATE_FMT  = 'DD-MMM-YYYY'


def _fmt_inr(ws, row, col, value):
    cell = ws.cell(row=row, column=col, value=value)
    cell.number_format = INR_FMT
    cell.font = BODY_FONT
    cell.alignment = Alignment(horizontal="right")
    return cell


def _fmt_pct(ws, row, col, value):
    cell = ws.cell(row=row, column=col, value=value / 100 if value is not None else None)
    cell.number_format = PCT_FMT
    cell.font = BODY_FONT
    cell.alignment = Alignment(horizontal="right")
    return cell


def _label(ws, row, col, text, bold=False, fill=None, font=None):
    cell = ws.cell(row=row, column=col, value=text)
    cell.font = font or (TOT_FONT if bold else BODY_FONT)
    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    if fill:
        cell.fill = fill
    return cell


def _apply_border(ws, row, col_start, col_end, border=THIN_BORDER):
    for c in range(col_start, col_end + 1):
        ws.cell(row=row, column=c).border = border


# ── DB helpers ────────────────────────────────────────────────────────────────

def _fetch_entities(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, entity_name FROM entity ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    return rows


def _fetch_mf_holdings(conn, entity_id: Optional[int] = None):
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
        JOIN entity e ON e.id = h.entity_id
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
            eh.broker, eh.symbol,
            eh.cost, eh.current_market_value AS current_value,
            eh.prev_week_value, eh.market_value_as_on,
            eh.pnl_ytd, eh.pnl_inception,
            eh.returns_ytd_pct, eh.returns_inception_pct, eh.cagr_inception_pct,
            eh.first_invested_date,
            eh.weekly_change, eh.exposure_pct, eh.remarks
        FROM equity_holding eh
        JOIN entity e ON e.id = eh.entity_id
        {where}
        ORDER BY eh.broker, eh.symbol
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


# ── individual entity report ──────────────────────────────────────────────────

COLS = [
    ("Asset Class / Fund", 38),
    ("Inception Date", 14),
    ("Cost (₹)", 14),
    ("Mkt Value\n31-Mar", 14),
    ("Current\nMkt Value (₹)", 14),
    ("Prev Week\nValue (₹)", 14),
    ("Weekly\nChange (₹)", 12),
    ("P&L YTD (₹)", 12),
    ("P&L Inception (₹)", 14),
    ("Returns\nYTD %", 10),
    ("Returns\nInception %", 10),
    ("CAGR\nInception %", 10),
    ("Remarks", 20),
]


def _write_header_row(ws, row, as_of: date, entity_name: str):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(COLS))
    cell = ws.cell(row=row, column=1,
                   value=f"Performance Summary — {entity_name}   |   As on {as_of.strftime('%d %b %Y')}")
    cell.font = Font(name="Calibri", bold=True, color="FFFFFF", size=12)
    cell.fill = HDR_FILL
    cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 22


def _write_col_headers(ws, row):
    for c, (name, _) in enumerate(COLS, 1):
        cell = ws.cell(row=row, column=c, value=name)
        cell.font = SUB_FONT
        cell.fill = SUB_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER
    ws.row_dimensions[row].height = 30


def _write_section(ws, row, label):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(COLS))
    cell = ws.cell(row=row, column=1, value=label)
    cell.font = SEC_FONT
    cell.fill = SEC_FILL
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    cell.border = Border(bottom=Side(style="medium", color="2F5496"))
    ws.row_dimensions[row].height = 16
    return row + 1


def _write_data_row(ws, row, holding, alt=False):
    fill = ALT_FILL if alt else WHITE_FILL
    f = float

    def v(key):
        val = holding.get(key)
        return float(val) if val is not None else None

    vals = [
        holding.get("security_name") or holding.get("label") or holding.get("symbol", ""),
        holding.get("first_invested_date") or holding.get("inception_date"),
        v("cost"),
        v("market_value_as_on"),
        v("current_value"),
        v("prev_week_value"),
        v("weekly_change"),
        v("pnl_ytd"),
        v("pnl_inception"),
        v("returns_ytd_pct"),
        v("returns_inception_pct"),
        v("cagr_inception_pct"),
        holding.get("remarks") or holding.get("notes") or "",
    ]

    for c, val in enumerate(vals, 1):
        cell = ws.cell(row=row, column=c, value=val)
        cell.fill = fill
        cell.border = THIN_BORDER
        cell.font = BODY_FONT
        if c == 1:
            cell.alignment = Alignment(horizontal="left", indent=2, wrap_text=True)
        elif c == 2:
            cell.number_format = DATE_FMT
            cell.alignment = Alignment(horizontal="center")
        elif c in (3, 4, 5, 6, 7, 8, 9):
            cell.number_format = INR_FMT
            cell.alignment = Alignment(horizontal="right")
        elif c in (10, 11, 12):
            if val is not None:
                cell.value = val / 100
            cell.number_format = PCT_FMT
            cell.alignment = Alignment(horizontal="right")
        else:
            cell.alignment = Alignment(horizontal="left", wrap_text=True)


def _write_total_row(ws, row, label, rows_range, col_count=13):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
    cell = ws.cell(row=row, column=1, value=label)
    cell.font = TOT_FONT
    cell.fill = TOT_FILL
    cell.alignment = Alignment(horizontal="left", indent=1)
    cell.border = THIN_BORDER

    sum_cols = {3, 4, 5, 6, 7, 8, 9}  # INR sum cols
    for c in range(3, col_count + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = TOT_FILL
        cell.font = TOT_FONT
        cell.border = THIN_BORDER
        if c in sum_cols and rows_range:
            r_start, r_end = rows_range
            col_letter = get_column_letter(c)
            cell.value = f"=SUM({col_letter}{r_start}:{col_letter}{r_end})"
            cell.number_format = INR_FMT
            cell.alignment = Alignment(horizontal="right")


def _set_col_widths(ws):
    for c, (_, width) in enumerate(COLS, 1):
        ws.column_dimensions[get_column_letter(c)].width = width


def build_individual_report(conn, entity_id: int, entity_name: str, as_of: date):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Performance Summary"
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A3"

    mf_rows  = _fetch_mf_holdings(conn, entity_id)
    eq_rows  = _fetch_equity_holdings(conn, entity_id)
    man_rows = _fetch_manual_inputs(conn, entity_id)

    man_by_cat = {}
    for m in man_rows:
        man_by_cat.setdefault(m["category"], []).append(m)

    row = 1
    _write_header_row(ws, row, as_of, entity_name); row += 1
    _write_col_headers(ws, row); row += 1

    # ── Fixed Income ──────────────────────────────────────────────────────────
    row = _write_section(ws, row, "FIXED INCOME")

    fi_types = ["LIQUID_FUND", "DEBT_FUND", "ARBITRAGE_FUND", "PPF"]
    fi_labels = {
        "LIQUID_FUND":    "MF — Liquid Fund",
        "DEBT_FUND":      "MF — Debt Fund",
        "ARBITRAGE_FUND": "MF — Arbitrage Fund",
        "PPF":            "Public Provident Fund",
    }

    for ftype in fi_types:
        holds = [h for h in mf_rows if h["security_type"] == ftype]
        manual = man_by_cat.get(ftype.lower(), [])
        if not holds and not manual:
            continue
        row = _write_section(ws, row, fi_labels.get(ftype, ftype))
        data_start = row
        alt = False
        for h in holds:
            _write_data_row(ws, row, h, alt); row += 1; alt = not alt
        for m in manual:
            _write_data_row(ws, row, m, alt); row += 1; alt = not alt
        if holds or manual:
            _write_total_row(ws, row, f"Total {fi_labels.get(ftype, ftype)}", (data_start, row - 1))
            row += 1

    fi_man = [m for cat in ["ppf", "fixed_income"] for m in man_by_cat.get(cat, [])]
    if fi_man:
        row = _write_section(ws, row, "Other Fixed Income")
        data_start = row
        for i, m in enumerate(fi_man):
            _write_data_row(ws, row, m, i % 2 == 1); row += 1
        _write_total_row(ws, row, "Total Other Fixed Income", (data_start, row - 1)); row += 1

    _write_total_row(ws, row, "A. TOTAL FIXED INCOME", None); row += 2

    # ── Equity ────────────────────────────────────────────────────────────────
    row = _write_section(ws, row, "EQUITY")

    eq_mf_types = ["EQUITY_FUND", "INDEX_FUND", "SECTOR_FUND", "HYBRID_FUND"]
    eq_mf_labels = {
        "EQUITY_FUND":  "MF — Market Equity Fund",
        "INDEX_FUND":   "MF — Index Fund",
        "SECTOR_FUND":  "MF — Sector / Thematic Fund",
        "HYBRID_FUND":  "MF — Hybrid (Debt & Equity) Fund",
    }
    for ftype in eq_mf_types:
        holds = [h for h in mf_rows if h["security_type"] == ftype]
        if not holds:
            continue
        row = _write_section(ws, row, eq_mf_labels.get(ftype, ftype))
        data_start = row
        for i, h in enumerate(holds):
            _write_data_row(ws, row, h, i % 2 == 1); row += 1
        _write_total_row(ws, row, f"Total {eq_mf_labels.get(ftype, ftype)}", (data_start, row - 1)); row += 1

    for cat, label in [("pms", "PMS"), ("direct_equity", "Direct Equities"), ("aif", "AIF")]:
        items = man_by_cat.get(cat, [])
        db_eq = [h for h in eq_rows if cat == "direct_equity"]
        if not items and not db_eq:
            continue
        row = _write_section(ws, row, label)
        data_start = row
        alt = False
        for h in db_eq:
            _write_data_row(ws, row, h, alt); row += 1; alt = not alt
        for m in items:
            _write_data_row(ws, row, m, alt); row += 1; alt = not alt
        _write_total_row(ws, row, f"Total {label}", (data_start, row - 1)); row += 1

    _write_total_row(ws, row, "B. TOTAL EQUITY", None); row += 2

    # ── Alternates ────────────────────────────────────────────────────────────
    row = _write_section(ws, row, "ALTERNATES")

    for cat, label in [
        ("overseas_fund",    "Overseas Funds"),
        ("overseas_equity",  "Overseas Direct Equity"),
        ("forex",            "Forex / Foreign Cash"),
        ("gold_etf",         "Gold / Silver ETF"),
        ("unlisted",         "Unlisted Equity"),
        ("startup",          "Startups"),
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
        ("funds_transit",    "E. Funds in Transit"),
        ("broker_balance",   "F. Broker Balance"),
        ("bank",             "G. Funds in Bank"),
    ]:
        items = man_by_cat.get(cat, [])
        row = _write_section(ws, row, label)
        data_start = row
        for i, m in enumerate(items):
            _write_data_row(ws, row, m, i % 2 == 1); row += 1
        if items:
            _write_total_row(ws, row, f"Total {label.split('. ', 1)[-1]}", (data_start, row - 1)); row += 1
        else:
            row += 1

    _write_total_row(ws, row, "H. GRAND TOTAL (A+B+D+E+F+G)", None); row += 1

    # ── footer note ───────────────────────────────────────────────────────────
    row += 1
    note = ws.cell(row=row, column=1,
                   value=f"Report generated on {datetime.utcnow().strftime('%d %b %Y %H:%M')} UTC  |  MF data auto-populated from CAS  |  Manual inputs as last updated in portal")
    note.font = LABEL_FONT
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(COLS))

    _set_col_widths(ws)
    ws.print_title_rows = "1:2"
    ws.page_setup.fitToPage = True

    return wb


# ── combined all-entities report ──────────────────────────────────────────────

COMB_COLS = [
    ("Asset / Fund", 38),
    ("Entity", 12),
    ("Source", 12),
    ("Cost (₹)", 14),
    ("Mkt Val\n31-Mar (₹)", 14),
    ("Current Mkt\nValue (₹)", 14),
    ("Prev Week\nValue (₹)", 14),
    ("Weekly\nChange (₹)", 12),
    ("YTD\nReturns %", 10),
    ("Inception\nReturns %", 10),
    ("CAGR\nInception %", 10),
]


def build_combined_report(conn, as_of: date):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "All Assets Daily MIS"
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A3"

    mf_rows  = _fetch_mf_holdings(conn)
    eq_rows  = _fetch_equity_holdings(conn)
    man_rows = _fetch_manual_inputs(conn)

    # Group manual by (entity_id, category)
    man_by_ent_cat = {}
    for m in man_rows:
        man_by_ent_cat.setdefault((m["entity_id"], m["category"]), []).append(m)

    # Header
    row = 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(COMB_COLS))
    hdr = ws.cell(row=row, column=1,
                  value=f"ALL ASSETS DAILY MIS   |   As on {as_of.strftime('%d %b %Y')}")
    hdr.font = Font(name="Calibri", bold=True, color="FFFFFF", size=12)
    hdr.fill = HDR_FILL
    hdr.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 22
    row += 1

    # Column headers
    for c, (name, _) in enumerate(COMB_COLS, 1):
        cell = ws.cell(row=row, column=c, value=name)
        cell.font = SUB_FONT
        cell.fill = SUB_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER
    ws.row_dimensions[row].height = 30
    row += 1

    def write_combined_row(r, label, entity_name, source, cost, mkt_mar, cur_val,
                           prev_wk, wkly_chg, ytd_pct, inc_pct, cagr_pct, alt=False):
        fill = ALT_FILL if alt else WHITE_FILL
        vals = [label, entity_name, source, cost, mkt_mar, cur_val,
                prev_wk, wkly_chg, ytd_pct, inc_pct, cagr_pct]
        for c, val in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.fill = fill
            cell.border = THIN_BORDER
            cell.font = BODY_FONT
            if c == 1:
                cell.alignment = Alignment(horizontal="left", indent=2, wrap_text=True)
            elif c in (2, 3):
                cell.alignment = Alignment(horizontal="center")
            elif c in (4, 5, 6, 7, 8):
                cell.number_format = INR_FMT
                cell.alignment = Alignment(horizontal="right")
            elif c in (9, 10, 11):
                if val is not None:
                    cell.value = val / 100
                cell.number_format = PCT_FMT
                cell.alignment = Alignment(horizontal="right")

    def section(r, label):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(COMB_COLS))
        cell = ws.cell(row=r, column=1, value=label)
        cell.font = SEC_FONT; cell.fill = SEC_FILL
        cell.alignment = Alignment(horizontal="left", indent=1)
        cell.border = Border(bottom=Side(style="medium", color="2F5496"))
        ws.row_dimensions[r].height = 16
        return r + 1

    def total_row(r, label, data_start, data_end):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        cell = ws.cell(row=r, column=1, value=label)
        cell.font = TOT_FONT; cell.fill = TOT_FILL
        cell.alignment = Alignment(horizontal="left", indent=1)
        cell.border = THIN_BORDER
        for c in range(4, len(COMB_COLS) + 1):
            cell = ws.cell(row=r, column=c)
            cell.fill = TOT_FILL; cell.font = TOT_FONT; cell.border = THIN_BORDER
            if c in (4, 5, 6, 7, 8) and data_start and data_end:
                col_letter = get_column_letter(c)
                cell.value = f"=SUM({col_letter}{data_start}:{col_letter}{data_end})"
                cell.number_format = INR_FMT
                cell.alignment = Alignment(horizontal="right")

    def v(h, key):
        val = h.get(key)
        return float(val) if val is not None else None

    # ── Fixed Income ──────────────────────────────────────────────────────────
    row = section(row, "FIXED INCOME")
    fi_types = {"LIQUID_FUND": "MF — Liquid", "DEBT_FUND": "MF — Debt",
                "ARBITRAGE_FUND": "MF — Arbitrage"}
    for ftype, flabel in fi_types.items():
        holds = [h for h in mf_rows if h["security_type"] == ftype]
        if not holds:
            continue
        row = section(row, flabel)
        data_start = row
        for i, h in enumerate(holds):
            write_combined_row(row, h["security_name"], h["entity_name"], "CAMS/KARVY",
                               v(h, "cost"), v(h, "market_value_as_on"), v(h, "current_value"),
                               v(h, "prev_week_value"), v(h, "weekly_change"),
                               v(h, "returns_ytd_pct"), v(h, "returns_inception_pct"),
                               v(h, "cagr_inception_pct"), i % 2 == 1)
            row += 1
        total_row(row, f"Total {flabel}", data_start, row - 1); row += 1

    # PPF manual
    ppf_items = [(eid, cat, items) for (eid, cat), items in man_by_ent_cat.items() if cat == "ppf"]
    if ppf_items:
        row = section(row, "PPF")
        data_start = row
        alt = False
        for eid, _, items in ppf_items:
            for m in items:
                write_combined_row(row, m["label"], m.get("entity_name", ""), "BANK",
                                   v(m, "cost"), None, v(m, "current_value"),
                                   v(m, "prev_week_value"), v(m, "weekly_change"),
                                   None, None, None, alt)
                row += 1; alt = not alt
        total_row(row, "Total PPF", data_start, row - 1); row += 1

    total_row(row, "TOTAL FIXED INCOME", None, None); row += 2

    # ── Equity ────────────────────────────────────────────────────────────────
    row = section(row, "EQUITY")
    eq_mf_types = {"EQUITY_FUND": "MF Equity", "INDEX_FUND": "MF Index",
                   "SECTOR_FUND": "MF Sector", "HYBRID_FUND": "MF Hybrid"}
    for ftype, flabel in eq_mf_types.items():
        holds = [h for h in mf_rows if h["security_type"] == ftype]
        if not holds:
            continue
        row = section(row, flabel)
        data_start = row
        for i, h in enumerate(holds):
            write_combined_row(row, h["security_name"], h["entity_name"], "CAMS/KARVY",
                               v(h, "cost"), v(h, "market_value_as_on"), v(h, "current_value"),
                               v(h, "prev_week_value"), v(h, "weekly_change"),
                               v(h, "returns_ytd_pct"), v(h, "returns_inception_pct"),
                               v(h, "cagr_inception_pct"), i % 2 == 1)
            row += 1
        total_row(row, f"Total {flabel}", data_start, row - 1); row += 1

    # Direct equity from DB
    if eq_rows:
        row = section(row, "Direct Equities (Brokers)")
        data_start = row
        for i, h in enumerate(eq_rows):
            write_combined_row(row, h["symbol"], h["entity_name"], h["broker"].title(),
                               v(h, "cost"), v(h, "market_value_as_on"), v(h, "current_value"),
                               v(h, "prev_week_value"), v(h, "weekly_change"),
                               v(h, "returns_ytd_pct"), v(h, "returns_inception_pct"),
                               v(h, "cagr_inception_pct"), i % 2 == 1)
            row += 1
        total_row(row, "Total Direct Equities", data_start, row - 1); row += 1

    # PMS manual
    for cat, label, source in [("pms", "PMS", "Login"), ("aif", "AIF", "Login")]:
        items = [(eid, m) for (eid, c), ms in man_by_ent_cat.items() if c == cat for m in ms]
        if not items:
            continue
        row = section(row, label)
        data_start = row
        for i, (eid, m) in enumerate(items):
            write_combined_row(row, m["label"], m.get("entity_name", ""), source,
                               v(m, "cost"), None, v(m, "current_value"),
                               v(m, "prev_week_value"), v(m, "weekly_change"),
                               None, None, None, i % 2 == 1)
            row += 1
        total_row(row, f"Total {label}", data_start, row - 1); row += 1

    total_row(row, "TOTAL EQUITY", None, None); row += 2

    # ── Alternates ────────────────────────────────────────────────────────────
    row = section(row, "ALTERNATES")
    alt_cats = [
        ("overseas_fund",   "Overseas Funds",       "Login"),
        ("overseas_equity", "Overseas Direct Equity","Login"),
        ("forex",           "Forex / Foreign Cash",  "Bank"),
        ("gold_etf",        "Gold / Silver ETF",     "Demat"),
        ("unlisted",        "Unlisted Equity",       "Login"),
        ("startup",         "Startups",              ""),
    ]
    for cat, label, source in alt_cats:
        items = [(eid, m) for (eid, c), ms in man_by_ent_cat.items() if c == cat for m in ms]
        if not items:
            continue
        row = section(row, label)
        data_start = row
        for i, (eid, m) in enumerate(items):
            write_combined_row(row, m["label"], m.get("entity_name", ""), source,
                               v(m, "cost"), None, v(m, "current_value"),
                               v(m, "prev_week_value"), v(m, "weekly_change"),
                               None, None, None, i % 2 == 1)
            row += 1
        total_row(row, f"Total {label}", data_start, row - 1); row += 1

    total_row(row, "TOTAL ALTERNATES", None, None); row += 2

    # ── Below-the-line ────────────────────────────────────────────────────────
    for cat, label in [("funds_transit", "E. Funds in Transit"),
                       ("broker_balance", "F. Broker Balance"),
                       ("bank", "G. Funds in Bank")]:
        items = [(eid, m) for (eid, c), ms in man_by_ent_cat.items() if c == cat for m in ms]
        row = section(row, label)
        data_start = row
        for i, (eid, m) in enumerate(items):
            write_combined_row(row, m["label"], m.get("entity_name", ""), "Bank/Broker",
                               None, None, v(m, "current_value"),
                               None, None, None, None, None, i % 2 == 1)
            row += 1
        if items:
            total_row(row, f"Total {label.split('. ', 1)[-1]}", data_start, row - 1); row += 1
        else:
            row += 1

    total_row(row, "H. GRAND TOTAL", None, None); row += 1

    row += 1
    note = ws.cell(row=row, column=1,
                   value=f"Generated {datetime.utcnow().strftime('%d %b %Y %H:%M')} UTC  |  MF & equity from DB  |  Manual items from portal")
    note.font = LABEL_FONT
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(COMB_COLS))

    for c, (_, width) in enumerate(COMB_COLS, 1):
        ws.column_dimensions[get_column_letter(c)].width = width

    ws.print_title_rows = "1:2"
    return wb


# ── public entry point ────────────────────────────────────────────────────────

def generate_reports(conn, generated_by_user_id: Optional[int] = None) -> list[dict]:
    """
    Generate all reports (one per entity + combined).
    Returns list of dicts describing each generated file.
    """
    as_of = date.today()
    folder = os.path.join(REPORTS_DIR, as_of.strftime("%Y-%m-%d"))
    os.makedirs(folder, exist_ok=True)

    entities = _fetch_entities(conn)
    results  = []
    cur = conn.cursor()

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
        results.append({"id": report_id, "type": "individual", "entity": ename, "filename": fname, "path": fpath})

    # Combined
    fname = f"All_Entities_Combined_{as_of.strftime('%Y%m%d')}.xlsx"
    fpath = os.path.join(folder, fname)
    wb = build_combined_report(conn, as_of)
    wb.save(fpath)

    cur.execute("""
        INSERT INTO generated_report
            (report_type, entity_id, entity_name, filename, filepath, as_of_date, generated_by, generated_at)
        VALUES ('combined', NULL, 'All Entities', %s, %s, %s, %s, NOW())
        RETURNING id
    """, (fname, fpath, as_of, generated_by_user_id))
    report_id = cur.fetchone()["id"]
    results.append({"id": report_id, "type": "combined", "entity": "All Entities", "filename": fname, "path": fpath})

    conn.commit()
    cur.close()
    return results


if __name__ == "__main__":
    import os
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
