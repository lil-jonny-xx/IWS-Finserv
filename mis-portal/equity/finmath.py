"""
Finance math helpers — money-weighted return (XIRR).

Used by the foreign-broker scrapers (Vested / DBS) to turn a holding's dated
cash-flow ledger (buys = outflow, sells = inflow, plus the current market value
as the final inflow) into an annualised money-weighted return. Computed in the
security's NATIVE currency (the flows and the current value are native), so the
result is the true investment return independent of INR/FX movement.
"""
from datetime import date
from typing import Optional


def xirr(flows: list[tuple[date, float]], guess: float = 0.1) -> Optional[float]:
    """Annualised money-weighted rate of return for dated cash flows.

    `flows` is a list of (date, amount): investments negative, proceeds/current
    value positive. Returns the rate as a fraction (0.1842 == 18.42%), or None
    if it cannot be solved (e.g. all flows the same sign, < 2 flows).

    Newton-Raphson first, bisection fallback for the pathological cases Newton
    diverges on. Rate domain is (-0.9999, large) since (1+r) must stay positive.
    """
    if len(flows) < 2:
        return None
    flows = sorted(flows, key=lambda f: f[0])
    t0 = flows[0][0]
    amounts = [float(a) for _, a in flows]
    if not (any(a > 0 for a in amounts) and any(a < 0 for a in amounts)):
        return None
    years = [(d - t0).days / 365.0 for d, _ in flows]
    scale = sum(abs(a) for a in amounts) or 1.0

    def npv(r: float) -> float:
        return sum(a / (1.0 + r) ** y for a, y in zip(amounts, years))

    def dnpv(r: float) -> float:
        return sum(-y * a / (1.0 + r) ** (y + 1.0) for a, y in zip(amounts, years))

    # --- Newton-Raphson ---
    r = guess
    solved = None
    for _ in range(100):
        try:
            f = npv(r)
            d = dnpv(r)
            if d == 0:
                break
            r_next = r - f / d
        except (OverflowError, ZeroDivisionError, ValueError):
            break
        if r_next <= -0.9999:                 # bounce back into domain
            r_next = (r - 0.9999) / 2.0
        if abs(r_next - r) < 1e-8:
            r = r_next
            solved = r
            break
        r = r_next
    if solved is not None and abs(npv(solved)) < 1e-4 * scale:
        return solved

    # --- Bisection fallback over a wide bracket ---
    lo, hi = -0.9999, 1.0e6
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        return None                            # no sign change → no root in range
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6 * scale or (hi - lo) < 1e-9:
            return mid
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    return (lo + hi) / 2.0
