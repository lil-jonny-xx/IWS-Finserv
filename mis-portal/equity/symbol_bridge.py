"""
Symbol bridge — resolve broker-tradebook security identifiers to a canonical ISIN.

Zerodha tradebooks already carry ISIN. Angel One and Dhan tradebooks identify a
stock only by a descriptive company NAME ("BEACON TRUSTEESHIP LTD", "Indian Energy
Exchange"), which does not match the suffixed tickers in equity_holding ("BEACON-ST").

This maps each tradebook name to an ISIN using two signals against the entity's own
equity_holding rows (which carry both symbol and isin):

  1. Quantity reconstruction (primary, high-confidence): a name whose net traded
     quantity equals a holding's current quantity is that holding. Proven reliable
     where the tradebook spans the full holding history.
  2. Token/prefix match (fallback, for holdings whose buys predate the tradebook
     window): the holding symbol (suffix stripped) is a prefix of the name's
     concatenated alphanumerics, or vice-versa.

Names that resolve to neither are returned in `unresolved` so the caller can report
them or supply a manual override. Only currently-held names need resolving for the
holdings-metrics backfill; fully sold-out names (net 0) are skipped.
"""
import re

SUFFIX = re.compile(r"-(EQ|ST|SM|BE|BZ|GB|N\d*)$", re.I)


def _norm_sym(s):
    return SUFFIX.sub("", str(s).strip().upper())


def _alnum(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def _token_match(na, sym):
    """True if normalised name `na` and stripped symbol `sym` plausibly identify
    the same stock by prefix overlap (handles broker name truncation)."""
    if not sym or len(sym) < 3:
        return False
    return na.startswith(sym) or sym.startswith(na[:max(5, len(sym))])


def build_name_isin_map(net_by_name, holdings, manual=None):
    """net_by_name: {tradebook_name: deduped_net_qty}.  holdings: [(symbol, isin, qty)].
    Returns (name_isin: {name: isin}, unresolved: [name]).

    Token/prefix match is primary (and many names may map to one ISIN — broker name
    truncation produces several spellings of one stock). Quantity reconstruction
    disambiguates ties and resolves acronym tickers the name can't prefix-match.
    """
    name_isin = dict(manual or {})
    holds = [(_norm_sym(sym), isin, float(qty)) for sym, isin, qty in holdings]
    held_names = {n: q for n, q in net_by_name.items() if abs(q) >= 0.5 and n not in name_isin}

    for name, q in held_names.items():
        na = _alnum(name)
        tok = [(s, i) for s, i, _ in holds if _token_match(na, s)]
        if len(tok) == 1:
            name_isin[name] = tok[0][1]; continue
        if len(tok) > 1:                                   # disambiguate by quantity
            qm = [(s, i) for s, i, hq in holds if _token_match(na, s) and abs(hq - q) < 0.5]
            if len(qm) == 1:
                name_isin[name] = qm[0][1]
            continue
        # no token match — fall back to a unique quantity reconstruction (acronyms)
        qm = [(s, i) for s, i, hq in holds if abs(hq - q) < 0.5]
        if len(qm) == 1:
            name_isin[name] = qm[0][1]

    unresolved = sorted(n for n in held_names if n not in name_isin)
    return name_isin, unresolved
