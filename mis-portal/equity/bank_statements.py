"""
Bank-statement parser — best-effort closing-balance extraction.

Banks have no API and every bank's statement layout differs, so this never
commits a balance on its own: it returns a *best guess* that the admin confirms
(or overrides) in the UI before it is written to bank_account.balance. When the
guess is shaky the status is ``needs_review`` so the UI nudges the human to check.

Supported uploads (dispatched by extension):
  - PDF  (.pdf)        → PyMuPDF text + closing/available/ending-balance regex
  - CSV  (.csv)        → pandas; last value of a balance-like column
  - Excel(.xlsx/.xls)  → pandas; same as CSV

Returns a dict:
  {"balance": Decimal|None, "as_of": date|None, "status": "parsed"|"needs_review"|"failed",
   "note": str, "kind": "pdf"|"csv"|"xlsx"}

All figures are in the statement's NATIVE currency — conversion to INR happens
later at read time via the fx_rate table.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)

# Phrases that tend to label the figure we want, most-specific first. The closing
# balance is preferred over a generic "available balance"; we keep the LAST match
# in the document for each tier, since statements often repeat the label and the
# final occurrence is usually the period-end figure.
_BALANCE_LABELS = [
    r"closing balance",
    r"ending balance",
    r"balance carried forward",
    r"available balance",
    r"current balance",
    r"book balance",
    r"closing bal",
]

# A money figure: optional currency symbol/code, grouped digits, optional decimals.
# Captures the numeric part only.
_MONEY = r"([+-]?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?)"

_DATE_LABELS = [
    r"statement date",
    r"closing date",
    r"as on",
    r"as of",
    r"period ending",
    r"statement period",
]


def detect_kind(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext == ".csv":
        return "csv"
    if ext in (".xlsx", ".xls"):
        return "xlsx"
    return None


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def _parse_date(raw: str) -> date | None:
    raw = raw.strip().rstrip(".,")
    for fmt in (
        "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y",
        "%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%b %d, %Y", "%B %d, %Y",
        "%d/%m/%y", "%d-%b-%y",
    ):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse(filepath: str, filename: str | None = None) -> dict:
    """Extract a best-guess balance + as-of date from an uploaded statement."""
    kind = detect_kind(filename or filepath)
    result = {"balance": None, "as_of": None, "status": "failed", "note": "", "kind": kind}
    if kind is None:
        result["note"] = "Unsupported file type — upload a PDF, CSV, or Excel statement."
        return result

    try:
        if kind == "pdf":
            result.update(_parse_pdf(filepath))
        else:  # csv / xlsx
            result.update(_parse_tabular(filepath, kind))
    except Exception as e:  # never let a bad file 500 the request
        logger.warning(f"bank statement parse failed for {filename or filepath}: {e}")
        result["status"] = "failed"
        result["note"] = f"Could not read the file ({type(e).__name__}). Enter the balance manually."
    result["kind"] = kind
    return result


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _parse_pdf(filepath: str) -> dict:
    import fitz  # PyMuPDF

    doc = fitz.open(filepath)
    if doc.needs_pass:
        doc.close()
        return {"status": "needs_review",
                "note": "PDF is password-protected — couldn't read it. Enter the balance manually."}
    text = "\n".join(page.get_text() for page in doc)
    doc.close()

    if not text.strip():
        return {"status": "needs_review",
                "note": "No selectable text (scanned image?). Enter the balance manually."}

    flat = re.sub(r"[ \t]+", " ", text)
    balance = note = None
    for label in _BALANCE_LABELS:
        # label ... money, allowing a currency token and a colon in between
        matches = re.findall(rf"{label}[^\d\-+]{{0,20}}{_MONEY}", flat, flags=re.IGNORECASE)
        if matches:
            balance = _to_decimal(matches[-1])  # last occurrence ≈ period end
            note = f"Matched '{label}'."
            break

    as_of = _extract_date(flat)

    if balance is None:
        return {"status": "needs_review", "as_of": as_of,
                "note": "Couldn't find a closing-balance line. Enter the balance manually."}
    return {"balance": balance, "as_of": as_of, "status": "parsed",
            "note": note + (" Verify before saving." if as_of is None else "")}


def _extract_date(flat: str) -> date | None:
    date_pat = r"(\d{1,2}[/\-. ][A-Za-z0-9]{2,9}[/\-. ]\d{2,4}|[A-Za-z]{3,9} \d{1,2},? \d{4})"
    for label in _DATE_LABELS:
        m = re.search(rf"{label}[^\dA-Za-z]{{0,5}}{date_pat}", flat, flags=re.IGNORECASE)
        if m:
            d = _parse_date(m.group(1))
            if d:
                return d
    return None


# ---------------------------------------------------------------------------
# CSV / Excel
# ---------------------------------------------------------------------------

def _parse_tabular(filepath: str, kind: str) -> dict:
    import pandas as pd

    df = pd.read_csv(filepath) if kind == "csv" else pd.read_excel(filepath)
    if df.empty:
        return {"status": "needs_review", "note": "Statement has no rows. Enter the balance manually."}

    cols = {str(c).strip().lower(): c for c in df.columns}

    # Balance column — prefer an explicit running/closing balance over "amount".
    bal_col = None
    for key in ("closing balance", "running balance", "balance", "available balance",
                "book balance", "ledger balance"):
        for lc, orig in cols.items():
            if key in lc:
                bal_col = orig
                break
        if bal_col:
            break

    if bal_col is None:
        return {"status": "needs_review",
                "note": "No balance column found in the sheet. Enter the balance manually."}

    series = df[bal_col].dropna()
    if series.empty:
        return {"status": "needs_review",
                "note": f"Balance column '{bal_col}' is empty. Enter the balance manually."}

    balance = _coerce_cell(series.iloc[-1])  # last row = latest balance
    if balance is None:
        return {"status": "needs_review",
                "note": f"Couldn't read the balance value in '{bal_col}'. Enter it manually."}

    as_of = _tabular_date(df, cols)
    return {"balance": balance, "as_of": as_of, "status": "parsed",
            "note": f"Read last value of column '{bal_col}'." +
                    ("" if as_of else " Date not found — verify.")}


def _coerce_cell(val) -> Decimal | None:
    if isinstance(val, (int, float)):
        try:
            return Decimal(str(val))
        except InvalidOperation:
            return None
    return _to_decimal(re.sub(r"[^\d.,+-]", "", str(val)))


def _tabular_date(df, cols) -> date | None:
    for key in ("date", "txn date", "transaction date", "value date", "posting date"):
        for lc, orig in cols.items():
            if key in lc:
                raw = df[orig].dropna()
                if raw.empty:
                    continue
                d = _parse_date(str(raw.iloc[-1]))
                if d:
                    return d
    return None
