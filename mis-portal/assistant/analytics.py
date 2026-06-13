"""
Deterministic portfolio analytics — pure functions, no DB or LLM.

Every number Jarvis quotes about the *portfolio* is computed here from real data,
never produced by the model. Inputs are plain Python lists / dicts so each function
is unit-testable against known expected numbers (see tests/test_analytics.py).

Conventions:
- "values" time series are ordered oldest -> newest.
- Monetary inputs are floats in the position's reporting currency (already FX-adjusted
  by the caller where relevant).
- Percentages are returned as numbers like 12.34 meaning 12.34%, unless noted.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _clean_values(values: list[float]) -> np.ndarray:
    """Drop None / non-finite, coerce to float array, preserving order."""
    out = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if np.isfinite(f):
            out.append(f)
    return np.asarray(out, dtype=float)


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------

def hhi_concentration(values: list[float]) -> dict:
    """
    Herfindahl-Hirschman concentration over position market values.

    Returns:
      hhi              sum of squared fractional shares, 0..1 (1 = single position)
      hhi_index        the same on the 0..10000 regulatory scale
      effective_n      1/hhi, the "effective number of equally-weighted positions"
      top_weight_pct   largest single position's share, as a percent
      n                number of positions counted
    """
    v = _clean_values(values)
    v = v[v > 0]
    total = v.sum()
    if total <= 0 or v.size == 0:
        return {"hhi": 0.0, "hhi_index": 0.0, "effective_n": 0.0,
                "top_weight_pct": 0.0, "n": 0}
    shares = v / total
    hhi = float(np.sum(shares ** 2))
    return {
        "hhi": round(hhi, 6),
        "hhi_index": round(hhi * 10000, 2),
        "effective_n": round(1.0 / hhi, 2) if hhi > 0 else 0.0,
        "top_weight_pct": round(float(shares.max()) * 100, 2),
        "n": int(v.size),
    }


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def max_drawdown(values: list[float]) -> dict:
    """
    Maximum peak-to-trough decline of a value series (oldest -> newest).

    Returns max_drawdown_pct as a negative percent (0.0 if the series only rises),
    plus the peak/trough values and their indices.
    """
    v = _clean_values(values)
    if v.size < 2:
        return {"max_drawdown_pct": 0.0, "peak_value": None, "trough_value": None,
                "peak_index": None, "trough_index": None}
    running_peak = np.maximum.accumulate(v)
    drawdowns = (v - running_peak) / running_peak
    trough_idx = int(np.argmin(drawdowns))
    mdd = float(drawdowns[trough_idx])
    # the peak that precedes this trough
    peak_idx = int(np.argmax(v[: trough_idx + 1]))
    return {
        "max_drawdown_pct": round(mdd * 100, 4),
        "peak_value": round(float(v[peak_idx]), 4),
        "trough_value": round(float(v[trough_idx]), 4),
        "peak_index": peak_idx,
        "trough_index": trough_idx,
    }


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------

def annualized_volatility(values: list[float],
                          periods_per_year: int = TRADING_DAYS) -> dict:
    """
    Annualized volatility of simple period-over-period returns of a value series.

    Returns annualized_vol_pct and the underlying per-period std (both percent).
    """
    v = _clean_values(values)
    if v.size < 3:
        return {"annualized_vol_pct": 0.0, "period_vol_pct": 0.0, "n_returns": 0}
    rets = np.diff(v) / v[:-1]
    rets = rets[np.isfinite(rets)]
    if rets.size < 2:
        return {"annualized_vol_pct": 0.0, "period_vol_pct": 0.0, "n_returns": int(rets.size)}
    period_std = float(np.std(rets, ddof=1))
    return {
        "annualized_vol_pct": round(period_std * np.sqrt(periods_per_year) * 100, 4),
        "period_vol_pct": round(period_std * 100, 4),
        "n_returns": int(rets.size),
    }


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def correlation_matrix(series_map: dict[str, list[float]]) -> dict:
    """
    Pearson correlation of period returns across named price series.

    series_map: {name: [price, price, ...]} — series are inner-joined on position,
    so callers should pass equal-length, date-aligned price vectors.

    Returns {"labels": [...], "matrix": [[...]]} where matrix[i][j] is corr(i, j),
    rounded; insufficient overlap yields an empty result.
    """
    cleaned = {k: _clean_values(v) for k, v in series_map.items()}
    cleaned = {k: v for k, v in cleaned.items() if v.size >= 3}
    if len(cleaned) < 2:
        return {"labels": list(cleaned.keys()), "matrix": []}

    min_len = min(v.size for v in cleaned.values())
    # align on the most-recent min_len observations
    rets = {}
    for k, v in cleaned.items():
        tail = v[-min_len:]
        rets[k] = np.diff(tail) / tail[:-1]
    df = pd.DataFrame(rets)
    corr = df.corr(method="pearson")
    labels = list(corr.columns)
    matrix = [[round(float(corr.iloc[i, j]), 4) for j in range(len(labels))]
              for i in range(len(labels))]
    return {"labels": labels, "matrix": matrix}


# ---------------------------------------------------------------------------
# Exposure (sector / asset-class)
# ---------------------------------------------------------------------------

def sector_exposure(positions: list[dict],
                    group_key: str = "group",
                    value_key: str = "value") -> dict:
    """
    Group position values by `group_key` and compute each group's percent of total.

    positions: [{group: "Financials", value: 100.0}, ...]
    Returns {"total": T, "breakdown": [{group, value, pct}, ...]} sorted desc by value.
    """
    totals: dict[str, float] = {}
    for p in positions:
        g = p.get(group_key) or "Unclassified"
        try:
            val = float(p.get(value_key) or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        totals[g] = totals.get(g, 0.0) + val

    total = sum(totals.values())
    breakdown = [
        {
            "group": g,
            "value": round(val, 2),
            "pct": round(val / total * 100, 2) if total else 0.0,
        }
        for g, val in sorted(totals.items(), key=lambda x: -x[1])
    ]
    return {"total": round(total, 2), "breakdown": breakdown}


# ---------------------------------------------------------------------------
# Scenario / shock propagation
# ---------------------------------------------------------------------------

def scenario_shock(positions: list[dict],
                   shocks: dict[str, float],
                   group_key: str = "group",
                   value_key: str = "value") -> dict:
    """
    Apply percentage price shocks to groups and roll up the portfolio impact.

    positions: [{group: "Gold", value: 100.0}, ...]
    shocks:    {"Gold": -10.0}  -> a 10% decline in everything tagged "Gold"
               group "*" applies to all positions not otherwise matched.

    Returns the pre/post portfolio value, absolute and percent impact, and a
    per-group contribution breakdown. Groups with no shock are held flat.
    """
    base_total = 0.0
    post_total = 0.0
    contrib: dict[str, dict] = {}
    default_shock = float(shocks.get("*", 0.0))

    for p in positions:
        g = p.get(group_key) or "Unclassified"
        try:
            val = float(p.get(value_key) or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        shock_pct = float(shocks[g]) if g in shocks else default_shock
        delta = val * shock_pct / 100.0
        base_total += val
        post_total += val + delta
        c = contrib.setdefault(g, {"group": g, "shock_pct": shock_pct,
                                   "base_value": 0.0, "impact": 0.0})
        c["base_value"] += val
        c["impact"] += delta

    impact = post_total - base_total
    breakdown = [
        {
            "group": c["group"],
            "shock_pct": round(c["shock_pct"], 4),
            "base_value": round(c["base_value"], 2),
            "impact": round(c["impact"], 2),
        }
        for c in sorted(contrib.values(), key=lambda x: x["impact"])
    ]
    return {
        "base_value": round(base_total, 2),
        "post_value": round(post_total, 2),
        "impact": round(impact, 2),
        "impact_pct": round(impact / base_total * 100, 4) if base_total else 0.0,
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# Laggards
# ---------------------------------------------------------------------------

def laggard_ranking(holdings: list[dict],
                    metric_key: str,
                    n: int = 5,
                    name_key: str = "name") -> list[dict]:
    """
    Return the `n` worst holdings by `metric_key` (ascending — most negative first).

    Holdings missing the metric are excluded. Each result carries name + metric value.
    """
    scored = []
    for h in holdings:
        val = h.get(metric_key)
        if val is None:
            continue
        try:
            scored.append((float(val), h.get(name_key)))
        except (TypeError, ValueError):
            continue
    scored.sort(key=lambda x: x[0])
    return [{"name": name, metric_key: round(val, 4)} for val, name in scored[: max(0, n)]]
