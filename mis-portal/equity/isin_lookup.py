"""
ISIN → exchange:tradingsymbol reference.

Zerodha's instrument dump has no ISIN column, so to price a holding that we only
know by ISIN (e.g. a scraped Nuvama PMS position) we map ISIN → NSE/BSE trading
symbol using Dhan's public scrip master (which DOES carry ISIN), then quote that
symbol via any broker LTP API.

The master is fetched once and cached on disk for the day. NSE is preferred over
BSE (Zerodha LTP keys are EXCHANGE:TRADINGSYMBOL, e.g. NSE:RELIANCE).
"""
import csv
import io
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Detailed master carries the ISIN column (compact does not).
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

CACHE_PATH   = Path(os.getenv("ISIN_MAP_CACHE", "/var/www/mis-portal/.isin_symbol_map.json"))
CACHE_MAX_AGE = 24 * 3600   # rebuild once a day

# Equity series we accept (cash-segment listed equities). Avoids mapping an ISIN
# to a derivative/ETF-debt row that happens to share it.
_EQUITY_SERIES = {"EQ", "BE", "BZ", "A", "B", "T"}

_MAP: Optional[dict] = None   # in-process cache: isin -> "EXCHANGE:SYMBOL"


def _looks_like_isin(v: str) -> bool:
    return bool(v) and len(v) == 12 and v[:2].isalpha() and v[:2].upper() == "IN"


def _build_from_csv(text: str) -> dict:
    """Parse the Dhan master into {isin: 'EXCHANGE:SYMBOL'}, preferring NSE."""
    rdr    = csv.DictReader(io.StringIO(text))
    nse, bse = {}, {}
    for row in rdr:
        isin = (row.get("ISIN") or "").strip()
        if not _looks_like_isin(isin):
            continue
        exch   = (row.get("EXCH_ID") or "").strip().upper()
        series = (row.get("SERIES") or "").strip().upper()
        if exch not in ("NSE", "BSE"):
            continue
        # Listed equities only. Some equity rows leave SERIES blank, so also allow
        # the equity instrument type as a fallback.
        instr  = (row.get("INSTRUMENT") or row.get("INSTRUMENT_TYPE") or "").strip().upper()
        if series and series not in _EQUITY_SERIES and "EQUITY" not in instr and instr not in ("ES", "EQ"):
            continue
        sym = (row.get("UNDERLYING_SYMBOL") or row.get("SYMBOL_NAME") or "").strip().upper()
        if not sym:
            continue
        target = nse if exch == "NSE" else bse
        # First EQ-series wins; don't let a later B/T row clobber a clean EQ map.
        if isin not in target or series == "EQ":
            target[isin] = f"{exch}:{sym}"

    merged = dict(bse)
    merged.update(nse)   # NSE overrides BSE
    return merged


def _load_cache() -> Optional[dict]:
    try:
        if CACHE_PATH.exists() and (time.time() - CACHE_PATH.stat().st_mtime) < CACHE_MAX_AGE:
            with open(CACHE_PATH) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"ISIN map cache read failed: {e}")
    return None


def _save_cache(m: dict):
    try:
        tmp = CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(m, f)
        tmp.replace(CACHE_PATH)
    except Exception as e:
        logger.warning(f"ISIN map cache write failed: {e}")


def get_map(force: bool = False) -> dict:
    """Return {isin: 'EXCHANGE:SYMBOL'}, building/refreshing from Dhan as needed."""
    global _MAP
    if _MAP is not None and not force:
        return _MAP

    if not force:
        cached = _load_cache()
        if cached:
            _MAP = cached
            return _MAP

    logger.info("Building ISIN→symbol map from Dhan scrip master")
    text = urllib.request.urlopen(DHAN_MASTER_URL, timeout=60).read().decode("utf-8", "replace")
    _MAP = _build_from_csv(text)
    logger.info(f"ISIN→symbol map built: {len(_MAP)} ISINs")
    _save_cache(_MAP)
    return _MAP


def symbol_for_isin(isin: str) -> Optional[str]:
    """ISIN → 'EXCHANGE:SYMBOL' (e.g. 'NSE:RELIANCE'), or None if unmapped."""
    if not isin:
        return None
    return get_map().get(isin.strip().upper())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    m = get_map(force=True)
    for s in ("INE002A01018", "INE238A01034", "INE467B01029"):
        print(s, "->", m.get(s))
