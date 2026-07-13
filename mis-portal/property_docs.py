"""
Property register — document checklist + PDF conversion.

DOC_TYPES is the authoritative checklist the Properties page renders as its
"add document" dropdown and completeness indicators. Every land document also
applies to buildings; scope='building' rows are the construction-era extras.

  slug      — stable id stored in property_document.doc_type
  label     — what the user sees
  scope     — 'land' (applies to both types) | 'building' (buildings only)
  optional  — "(if any)" documents; missing ones don't count against complete
  parent    — grouping only: the five department NOCs sit under Construction
              Licence; Floor Plans / Elevation sit under Approved Plans.

Multiple uploads per slug are allowed (receipts, e-challans, floor plans…).
"""
import os
import shutil
import subprocess
import tempfile

DOC_TYPES = [
    # ---- land (and therefore every property) -----------------------------
    {"slug": "sale_deed",              "label": "Sale Deed",                             "scope": "land", "optional": False, "parent": None},
    {"slug": "survey_plan",            "label": "Survey Plan",                           "scope": "land", "optional": False, "parent": None},
    {"slug": "form_1_14",              "label": "Form I & XIV",                          "scope": "land", "optional": False, "parent": None},
    {"slug": "title_report",           "label": "Title Report from Lawyer",              "scope": "land", "optional": False, "parent": None},
    {"slug": "contour_plans",          "label": "Contour Plans",                         "scope": "land", "optional": False, "parent": None},
    {"slug": "sanad",                  "label": "Sanad",                                 "scope": "land", "optional": False, "parent": None},
    {"slug": "zoning_certificate",     "label": "Zoning Certificate",                    "scope": "land", "optional": False, "parent": None},
    {"slug": "land_use",               "label": "Land Use",                              "scope": "land", "optional": False, "parent": None},
    {"slug": "approved_plans",         "label": "Approved Plans",                        "scope": "land", "optional": True,  "parent": None},
    {"slug": "tree_cutting_license",   "label": "Tree Cutting License",                  "scope": "land", "optional": True,  "parent": None},
    {"slug": "gazette_conversion",     "label": "Gazette Notification for Conversion",   "scope": "land", "optional": True,  "parent": None},
    {"slug": "property_access",        "label": "Property Access",                       "scope": "land", "optional": True,  "parent": None},
    {"slug": "zone_change_application","label": "Application Paper for Zone Change",     "scope": "land", "optional": True,  "parent": None},
    {"slug": "receipts_echallans",     "label": "Receipts & e-Challans Paid",            "scope": "land", "optional": True,  "parent": None},
    {"slug": "valuation_report",       "label": "Valuation Report",                      "scope": "land", "optional": True,  "parent": None},
    # ---- building extras --------------------------------------------------
    {"slug": "construction_approval",  "label": "Approval for Construction of Project",  "scope": "building", "optional": False, "parent": None},
    {"slug": "construction_licence",   "label": "Construction Licence",                  "scope": "building", "optional": False, "parent": None},
    {"slug": "noc_healthcare",         "label": "NOC — Healthcare",                      "scope": "building", "optional": False, "parent": "construction_licence"},
    {"slug": "noc_pwd",                "label": "NOC — PWD",                             "scope": "building", "optional": False, "parent": "construction_licence"},
    {"slug": "noc_sewage",             "label": "NOC — Sewage",                          "scope": "building", "optional": False, "parent": "construction_licence"},
    {"slug": "noc_electricity",        "label": "NOC — Electricity Dept",                "scope": "building", "optional": False, "parent": "construction_licence"},
    {"slug": "noc_fire",               "label": "NOC — Fire & Emergency Services",       "scope": "building", "optional": False, "parent": "construction_licence"},
    {"slug": "final_construction_licence", "label": "Final Construction Licence",        "scope": "building", "optional": False, "parent": None},
    {"slug": "noc_municipal",          "label": "NOC from Municipal Corporation",        "scope": "building", "optional": False, "parent": None},
    {"slug": "completion_certificate", "label": "Completion Certificate",                "scope": "building", "optional": False, "parent": None},
    {"slug": "final_noc_fire",         "label": "Final NOC — Fire & Emergency Service",  "scope": "building", "optional": False, "parent": None},
    {"slug": "occupancy_certificate",  "label": "Occupancy Certificate",                 "scope": "building", "optional": False, "parent": None},
    {"slug": "property_card",          "label": "Property Card",                         "scope": "building", "optional": False, "parent": None},
    {"slug": "floor_plan",             "label": "Floor Plans (PDF / AutoCAD)",           "scope": "building", "optional": False, "parent": "approved_plans"},
    {"slug": "elevation",              "label": "Building Elevation",                    "scope": "building", "optional": False, "parent": "approved_plans"},
    {"slug": "house_tax_receipts",     "label": "House Tax Receipts",                    "scope": "building", "optional": True,  "parent": None},
    {"slug": "water_bill",             "label": "Water Bill",                            "scope": "building", "optional": True,  "parent": None},
    {"slug": "electricity_bill",       "label": "Electricity Bill",                      "scope": "building", "optional": True,  "parent": None},
    # ---- catch-all ---------------------------------------------------------
    {"slug": "other",                  "label": "Other Document",                        "scope": "land", "optional": True,  "parent": None},
]

DOC_SLUGS = {d["slug"] for d in DOC_TYPES}

# Fair value = area x RRR x this (the 1.75x midpoint the portfolio has always
# used for real estate — RRR is the circle rate).
FAIR_VALUE_MULTIPLIER = 1.75

# Formats we never try to convert — CAD sources stay downloadable originals.
_NEVER_CONVERT_EXT = {".dwg", ".dxf"}
_IMAGE_MIME_PREFIX = "image/"
_DOCX_EXTS = {".doc", ".docx", ".odt", ".rtf", ".txt", ".xls", ".xlsx", ".ods"}


def doc_types_for(property_type: str) -> list:
    """Checklist rows applicable to a 'land' or 'building' property."""
    if property_type == "building":
        return DOC_TYPES
    return [d for d in DOC_TYPES if d["scope"] == "land"]


def _soffice_bin():
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert_to_pdf(data: bytes, filename: str, mime: str):
    """Return PDF bytes for the upload, or None when no conversion applies
    (already a PDF, a CAD file, or an office doc with LibreOffice absent —
    callers then store/serve the original as-is)."""
    ext = os.path.splitext(filename or "")[1].lower()
    if mime == "application/pdf" or ext == ".pdf" or ext in _NEVER_CONVERT_EXT:
        return None

    if (mime or "").startswith(_IMAGE_MIME_PREFIX):
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "PDF", resolution=150.0)
                return buf.getvalue()
        except Exception:
            return None

    if ext in _DOCX_EXTS:
        soffice = _soffice_bin()
        if not soffice:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in" + ext)
            with open(src, "wb") as fh:
                fh.write(data)
            try:
                subprocess.run(
                    [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, src],
                    capture_output=True, timeout=120, check=True,
                )
                out = os.path.join(tmp, "in.pdf")
                if os.path.exists(out):
                    with open(out, "rb") as fh:
                        return fh.read()
            except Exception:
                return None
    return None
