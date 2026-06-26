"""
Curated asset-class classification: gold / silver / commodity / equity.

Keeps gold ETFs, sovereign gold bonds, silver ETFs and tracked commodities
(e.g. the IBKR uranium ETF) OUT of the Equity page and ON the dedicated
Gold/Silver page. Two layers:

  1. A curated symbol/ISIN map (this file) handles the known instruments.
  2. An admin-editable override table (`asset_class_override`) handles anything
     new or mis-classified, without a code deploy.

Because classification runs on every broker-fetched holding in the daily sync,
a newly bought GOLDBEES / SGB / URNU is auto-classified on the next run and shows
up on the Gold/Silver page on its own — no manual re-tagging.

Resolution order (first match wins):
  override(symbol) → override(isin) → curated symbol map → SGB ISIN prefix →
  symbol keyword → 'equity'.
"""

GOLD = "gold"
SILVER = "silver"
COMMODITY = "commodity"
EQUITY = "equity"

# The non-equity classes that move off the Equity page onto Gold/Silver.
GOLD_SILVER_COMMODITY = (GOLD, SILVER, COMMODITY)
PRECIOUS_METALS = (GOLD, SILVER)

# Curated exact-symbol map (keys upper-cased). Not exhaustive — extend here for
# well-known tickers, or use the asset_class_override table for one-offs / new
# instruments. SGBs are caught by the IN0* ISIN rule below, not by symbol.
_SYMBOL_MAP = {
    # --- Gold ETFs (NSE/BSE) ---
    "GOLDBEES":  GOLD, "SETFGOLD":  GOLD, "AXISGOLD": GOLD, "GOLDIETF": GOLD,
    "HDFCGOLD":  GOLD, "IDBIGOLD":  GOLD, "KOTAKGOLD": GOLD, "ICICIGOLD": GOLD,
    "BSLGOLDETF": GOLD, "QGOLDHALF": GOLD, "GOLDSHARE": GOLD, "LICMFGOLD": GOLD,
    # --- Silver ETFs ---
    "SILVERBEES": SILVER, "SILVER": SILVER, "ICICISILVER": SILVER,
    "HDFCSILVER": SILVER, "AXISILVER": SILVER, "SILVERIETF": SILVER,
    # --- Commodities ---
    "URNU": COMMODITY,   # Sprott Uranium Miners / uranium (IBKR)
    "URNM": COMMODITY, "URA": COMMODITY,
}


def _keyword_class(sym: str):
    """Loose fallback on the symbol text for anything the exact map missed."""
    if "SILVER" in sym:
        return SILVER
    if "GOLD" in sym:
        return GOLD
    if "URANIUM" in sym:
        return COMMODITY
    return None


def classify_asset_class(symbol: str, isin: str, overrides: "dict | None" = None) -> str:
    """Return 'gold' | 'silver' | 'commodity' | 'equity' for a holding.

    `overrides` is the admin map loaded from asset_class_override (keys upper-cased
    by symbol AND/OR isin → class). Pass it to let the table win over the curated
    rules; omit it for pure curated classification.
    """
    sym    = (symbol or "").upper().strip()
    isin_u = (isin or "").upper().strip()

    if overrides:
        if sym and sym in overrides:
            return overrides[sym]
        if isin_u and isin_u in overrides:
            return overrides[isin_u]

    if sym in _SYMBOL_MAP:
        return _SYMBOL_MAP[sym]

    # Sovereign Gold Bonds carry IN0* ISINs (same rule classify_sector() uses).
    if isin_u.startswith("IN0"):
        return GOLD

    kw = _keyword_class(sym)
    if kw:
        return kw

    return EQUITY


def load_overrides(cur) -> dict:
    """Load the asset_class_override table into a lookup keyed by upper-cased
    symbol AND isin. Tolerates both RealDictCursor (dict rows) and plain cursors
    (tuple rows: symbol, isin, asset_class). Returns {} if the table is absent."""
    try:
        cur.execute("SELECT symbol, isin, asset_class FROM asset_class_override")
    except Exception:
        return {}
    out: dict = {}
    for row in cur.fetchall():
        if isinstance(row, dict):
            symbol, isin, cls = row.get("symbol"), row.get("isin"), row.get("asset_class")
        else:
            symbol, isin, cls = row[0], row[1], row[2]
        cls = (cls or "").lower().strip()
        if not cls:
            continue
        if symbol:
            out[symbol.upper().strip()] = cls
        if isin:
            out[isin.upper().strip()] = cls
    return out
