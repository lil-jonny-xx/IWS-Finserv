#!/usr/bin/env python3
"""
ISIN Resolver — IWS MIS Portal
Resolves ISIN → AMFI scheme code using mfapi.in search.
Called by CAS parser when seeding new securities.
Also runs standalone to fix missing amfi_codes.
"""
import requests
import logging
import time

logger = logging.getLogger(__name__)

MFAPI_SEARCH = "https://api.mfapi.in/mf/search"
MFAPI_BASE   = "https://api.mfapi.in/mf"


def search_by_name(scheme_name: str) -> str:
    """Search mfapi.in by scheme name, return amfi_code."""
    try:
        query = scheme_name[:50].strip()
        response = requests.get(
            MFAPI_SEARCH,
            params={"q": query},
            timeout=10
        )
        response.raise_for_status()
        results = response.json()
        if results:
            return str(results[0].get("schemeCode", ""))
    except Exception as e:
        logger.warning(f"Search failed for '{scheme_name[:40]}': {e}")
    return None


def verify_isin_matches(amfi_code: str, target_isin: str) -> bool:
    """Verify that amfi_code actually contains the target ISIN."""
    try:
        response = requests.get(f"{MFAPI_BASE}/{amfi_code}", timeout=10)
        response.raise_for_status()
        data = response.json()
        meta  = data.get("meta", {})
        isin1 = meta.get("isin_growth", "")
        isin2 = meta.get("isin_div_reinvestment", "")
        return target_isin in (isin1, isin2)
    except Exception:
        return False


def resolve_isin(isin: str, scheme_name: str) -> str:
    """
    Given ISIN + scheme name, return amfi_code.
    1. Search by scheme name
    2. Verify ISIN matches
    3. Return amfi_code if verified
    """
    logger.info(f"Resolving: {isin} | {scheme_name[:50]}")

    amfi_code = search_by_name(scheme_name)
    if not amfi_code:
        logger.warning(f"No amfi_code found for: {scheme_name[:50]}")
        return None

    if verify_isin_matches(amfi_code, isin):
        logger.info(f"✅ Resolved: {isin} → {amfi_code}")
        return amfi_code
    else:
        logger.warning(f"ISIN mismatch for amfi_code {amfi_code}. Skipping.")
        return None


def resolve_all_missing(conn):
    """
    Fix all securities in DB that have ISIN but no amfi_code.
    Called automatically by AMFI NAV worker at startup.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, isin, security_name
        FROM security_master
        WHERE isin IS NOT NULL
        AND amfi_code IS NULL
        AND security_type NOT IN (
            'EQUITY', 'CASH_FOREX', 'PPF', 'UNLISTED_EQUITY',
            'FOREIGN_EQUITY', 'BROKER_CASH', 'BANK_CASH',
            'TRANSIT', 'PMS'
        )
    """)
    missing = cursor.fetchall()
    cursor.close()

    if not missing:
        logger.info("No missing amfi_codes.")
        return

    logger.info(f"Resolving {len(missing)} missing amfi_codes...")
    resolved = 0

    for row in missing:
        amfi_code = resolve_isin(row["isin"], row["security_name"])
        if amfi_code:
            cur2 = conn.cursor()
            cur2.execute(
                "UPDATE security_master SET amfi_code = %s WHERE id = %s",
                (amfi_code, row["id"])
            )
            cur2.close()
            resolved += 1
        time.sleep(0.3)  # Be polite to the API

    conn.commit()
    logger.info(f"Resolved {resolved}/{len(missing)} amfi_codes.")
    