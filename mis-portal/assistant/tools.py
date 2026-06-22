"""
Jarvis tool layer — read-only data tools + quant tool dispatch.

Two responsibilities:
  1. TOOL_SCHEMAS: the JSON-schema tool definitions handed to the Claude Messages API.
  2. dispatch(name, tool_input, conn, eid): execute a tool against a shared DB
     connection, already scoped to entity `eid` (None = all-entities admin view).

SECURITY: the model passes *intent* only. Every SQL statement here is read-only and
constrained by `eid`, which the caller derives from `_resolve_entity` BEFORE invoking
any tool. The model never supplies an entity id; it cannot reach another entity's rows.
The SELECTs mirror the hardened read handlers in main.py — we re-query rather than
refactor those handlers.
"""

from __future__ import annotations

import json
import math
from typing import Optional

from . import analytics

# Brokerage / MF metric columns we treat as "performance" laggard candidates.
_PERF_METRICS = {
    "returns_inception_pct", "returns_ytd_pct", "weekly_change",
    "pnl_inception", "pnl_ytd", "cagr_inception_pct",
}


# ---------------------------------------------------------------------------
# Tool schemas (client-side tools). web_search is added separately in engine.py.
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "get_portfolio_overview",
        "description": (
            "High-level portfolio snapshot for the in-scope entity (or the whole "
            "family if scope is all-entities): total invested, current value, P&L, "
            "weekly change, weighted CAGR, and the asset-class allocation breakdown. "
            "Call this first when asked to assess or summarise the portfolio."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_mf_holdings",
        "description": (
            "List mutual-fund holdings in scope with per-fund invested amount, current "
            "value, returns, XIRR, CAGR, weekly change and asset class. Use for fund-level "
            "questions or to feed concentration/laggard analysis."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_equity_holdings",
        "description": (
            "List direct-equity holdings in scope with quantity, cost, current value, "
            "sector, returns and weekly change. Use for stock-level questions or to feed "
            "sector-exposure, concentration or laggard analysis."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_sector_exposure",
        "description": (
            "Combined exposure by sector/asset-class across the whole book in scope: "
            "direct-equity positions grouped by sector, mutual-fund and manual positions "
            "grouped by asset class. Returns each group's value and percent of total. "
            "Use to find over/under-weight areas."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_performance",
        "description": (
            "Rank holdings by a performance metric to surface laggards (or leaders). "
            "Combines MF + equity. Returns the worst N by the chosen metric."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": sorted(_PERF_METRICS),
                    "description": "Metric to rank by (ascending = worst first).",
                },
                "n": {"type": "integer", "description": "How many laggards to return (default 5)."},
            },
            "required": ["metric"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_transactions",
        "description": (
            "Recent mutual-fund transactions in scope (purchases, redemptions, SIPs, "
            "switches), most recent first. Use for activity/cash-flow questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max rows (default 50, capped 200)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "compute_concentration",
        "description": (
            "Deterministic concentration analysis (Herfindahl index, effective number of "
            "positions, largest weight) over the in-scope book. Universe = equity, mf, or "
            "all. Computed from real position values, never estimated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "universe": {"type": "string", "enum": ["all", "equity", "mf"],
                             "description": "Which positions to measure (default all)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "compute_drawdown",
        "description": (
            "Maximum peak-to-trough drawdown of the in-scope direct-equity book, computed "
            "from the daily equity value history (equity_holding_history). Returns the worst "
            "decline as a negative percent plus peak/trough values."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lookback_days": {"type": "integer",
                                  "description": "Days of history to consider (default 365)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "compute_volatility",
        "description": (
            "Annualised volatility of the in-scope direct-equity book, computed from daily "
            "value history. Returns annualised and per-day volatility as percents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lookback_days": {"type": "integer",
                                  "description": "Days of history to consider (default 365)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "compute_correlation",
        "description": (
            "Pearson correlation matrix of daily price returns across the largest in-scope "
            "equity holdings, from equity_holding_history close prices. Use to assess "
            "diversification / clustered risk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer",
                          "description": "Number of largest holdings to include (default 8, capped 15)."},
                "lookback_days": {"type": "integer",
                                  "description": "Days of history to consider (default 365)."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "compute_scenario",
        "description": (
            "Apply hypothetical percentage price shocks to one or more sectors / asset "
            "classes and roll up the impact on the in-scope portfolio. Example: a 10% drop "
            "in Gold -> {\"Gold\": -10}. Use group \"*\" for an across-the-board shock. "
            "Returns pre/post value, absolute and percent impact, per-group contribution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "shocks": {
                    "type": "object",
                    "description": "Map of sector/asset-class -> percent change (e.g. -10 for a 10% fall).",
                    "additionalProperties": {"type": "number"},
                },
            },
            "required": ["shocks"],
            "additionalProperties": False,
        },
    },
    {
        "name": "render_chart",
        "description": (
            "Render a chart in the chat to visualise numbers you ALREADY obtained from the "
            "data/compute tools — never invent values. Use when a picture beats prose: asset "
            "allocation or sector mix (donut or bar), a value/drawdown path over time (line), "
            "or a correlation matrix (heatmap). Keep to 1-2 charts per answer and still state "
            "the takeaway in text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": ["donut", "bar", "line", "heatmap"]},
                "title": {"type": "string", "description": "Short chart title."},
                "unit": {"type": "string", "description": "Optional value unit, e.g. '₹' or '%'."},
                "series": {
                    "type": "array",
                    "description": "For donut/bar: categories and their values.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "number"},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                },
                "points": {
                    "type": "array",
                    "description": "For line: ordered points (x label, y value).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "string"},
                            "y": {"type": "number"},
                        },
                        "required": ["x", "y"],
                        "additionalProperties": False,
                    },
                },
                "labels": {
                    "type": "array",
                    "description": "For heatmap: axis labels (square matrix).",
                    "items": {"type": "string"},
                },
                "matrix": {
                    "type": "array",
                    "description": "For heatmap: rows of values aligned to labels.",
                    "items": {"type": "array", "items": {"type": "number"}},
                },
            },
            "required": ["chart_type"],
            "additionalProperties": False,
        },
    },
    # ── Fundamentals seam (Phase 3 uses web_search for fundamentals; this is the
    # documented hook for a future structured provider). To enable a paid API later,
    # add a schema like the following and a `get_fundamentals` branch in dispatch():
    #   {
    #     "name": "get_fundamentals",
    #     "description": "Structured fundamentals for a listed symbol (P/E, ROE, growth, D/E ...).",
    #     "input_schema": {"type": "object",
    #         "properties": {"symbol": {"type": "string"}},
    #         "required": ["symbol"], "additionalProperties": False},
    #   },
    # No engine change is needed — it is a client tool like the get_* tools above.
]


# ---------------------------------------------------------------------------
# Small SQL helpers — every query is read-only and scoped by `eid`.
# ---------------------------------------------------------------------------

def _entity_clause(eid: Optional[int], col: str) -> tuple[str, list]:
    """Return ('WHERE col = %s', [eid]) or ('', []) for the all-entities view."""
    if eid is None:
        return "", []
    return f"WHERE {col} = %s", [eid]


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


# ---------------------------------------------------------------------------
# Internal data tools
# ---------------------------------------------------------------------------

def _mf_holdings(conn, eid: Optional[int]) -> list[dict]:
    where, params = _entity_clause(eid, "h.entity_id")
    cur = conn.cursor()
    cur.execute(f"""
        SELECT h.entity_id, h.folio_number, h.invested_amount, h.current_value,
               h.market_value_as_on, h.weekly_change, h.exposure_pct,
               h.pnl_inception, h.pnl_ytd, h.returns_inception_pct, h.returns_ytd_pct,
               h.cagr_inception_pct, h.xirr_inception_pct,
               sm.security_name, sm.isin, sm.asset_class, sm.security_type
        FROM holding h
        JOIN security_master sm ON sm.id = h.security_id
        {where}
        ORDER BY sm.asset_class, sm.security_name
    """, params)
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        out.append({
            "security_name": r["security_name"],
            "isin": r["isin"],
            "asset_class": r["asset_class"],
            "security_type": r["security_type"],
            "folio_number": r["folio_number"],
            "invested_amount": _f(r["invested_amount"]),
            "current_value": _f(r["market_value_as_on"]) or _f(r["current_value"]),
            "weekly_change": _f(r["weekly_change"]),
            "exposure_pct": _f(r["exposure_pct"]),
            "pnl_inception": _f(r["pnl_inception"]),
            "pnl_ytd": _f(r["pnl_ytd"]),
            "returns_inception_pct": _f(r["returns_inception_pct"]),
            "returns_ytd_pct": _f(r["returns_ytd_pct"]),
            "cagr_inception_pct": _f(r["cagr_inception_pct"]),
            "xirr_inception_pct": _f(r["xirr_inception_pct"]),
        })
    return out


def _equity_holdings(conn, eid: Optional[int]) -> list[dict]:
    where, params = _entity_clause(eid, "eh.entity_id")
    # Indian (equity_holding) + foreign (foreign_equity_holding), both in INR-
    # converted columns, so the assistant sees the whole equity book.
    select = """
        SELECT eh.entity_id, eh.broker, eh.symbol, eh.symbol_override, eh.isin,
               eh.sector, eh.quantity, eh.cost, eh.current_market_value,
               eh.market_value_as_on, eh.weekly_change, eh.exposure_pct,
               eh.pnl_inception, eh.pnl_ytd, eh.returns_inception_pct,
               eh.returns_ytd_pct, eh.cagr_inception_pct
        FROM {table} eh
        {where}
    """
    cur = conn.cursor()
    cur.execute(
        select.format(table="equity_holding", where=where)
        + " UNION ALL "
        + select.format(table="foreign_equity_holding", where=where)
        + " ORDER BY current_market_value DESC NULLS LAST",
        list(params) + list(params),
    )
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        out.append({
            "symbol": r["symbol_override"] or r["symbol"],
            "isin": r["isin"],
            "broker": r["broker"],
            "sector": r["sector"],
            "quantity": _f(r["quantity"]),
            "cost": _f(r["cost"]),
            "current_value": _f(r["market_value_as_on"]) or _f(r["current_market_value"]),
            "weekly_change": _f(r["weekly_change"]),
            "exposure_pct": _f(r["exposure_pct"]),
            "pnl_inception": _f(r["pnl_inception"]),
            "pnl_ytd": _f(r["pnl_ytd"]),
            "returns_inception_pct": _f(r["returns_inception_pct"]),
            "returns_ytd_pct": _f(r["returns_ytd_pct"]),
            "cagr_inception_pct": _f(r["cagr_inception_pct"]),
        })
    return out


def _manual_positions(conn, eid: Optional[int]) -> list[dict]:
    """Latest manual position per (entity, category, label) — mirrors get_manual_inputs."""
    where, params = _entity_clause(eid, "m.entity_id")
    cur = conn.cursor()
    cur.execute(f"""
        SELECT DISTINCT ON (m.entity_id, m.category, m.label)
            m.entity_id, m.category, m.label, m.current_value, m.cost
        FROM manual_input m
        {where}
        ORDER BY m.entity_id, m.category, m.label, m.updated_at DESC
    """, params)
    rows = cur.fetchall()
    cur.close()
    return [{
        "category": r["category"],
        "label": r["label"],
        "current_value": _f(r["current_value"]),
        "cost": _f(r["cost"]),
    } for r in rows]


def _portfolio_overview(conn, eid: Optional[int]) -> dict:
    mf = _mf_holdings(conn, eid)
    eq = _equity_holdings(conn, eid)
    manual = _manual_positions(conn, eid)

    def vsum(rows, key):
        return round(sum((r.get(key) or 0.0) for r in rows), 2)

    mf_value = vsum(mf, "current_value")
    eq_value = vsum(eq, "current_value")
    manual_value = vsum(manual, "current_value")
    mf_invested = vsum(mf, "invested_amount")
    eq_invested = vsum(eq, "cost")
    manual_invested = vsum(manual, "cost")
    total_value = round(mf_value + eq_value + manual_value, 2)
    total_invested = round(mf_invested + eq_invested + manual_invested, 2)

    # weighted CAGR over MF + equity positions that carry a cagr figure
    w_sum = w_cagr = 0.0
    for r in mf + eq:
        c = r.get("cagr_inception_pct")
        v = r.get("current_value") or 0.0
        if c is not None and v:
            w_cagr += c * v
            w_sum += v
    weighted_cagr = round(w_cagr / w_sum, 4) if w_sum else None

    return {
        "scope": "all_entities" if eid is None else f"entity_{eid}",
        "total_invested": total_invested,
        "total_value": total_value,
        "total_pnl": round(total_value - total_invested, 2),
        "weekly_change": round(vsum(mf, "weekly_change") + vsum(eq, "weekly_change"), 2),
        "weighted_cagr_pct": weighted_cagr,
        "asset_class_breakdown": analytics.sector_exposure(
            [{"group": "DIRECT_EQUITY", "value": eq_value}]
            + [{"group": (r.get("asset_class") or "MF"), "value": (r.get("current_value") or 0.0)} for r in mf]
            + [{"group": (r.get("category") or "MANUAL").upper(), "value": (r.get("current_value") or 0.0)} for r in manual]
        )["breakdown"],
        "counts": {"mf": len(mf), "equity": len(eq), "manual": len(manual)},
    }


def _sector_exposure(conn, eid: Optional[int]) -> dict:
    eq = _equity_holdings(conn, eid)
    mf = _mf_holdings(conn, eid)
    manual = _manual_positions(conn, eid)
    positions = (
        [{"group": (r.get("sector") or "Unclassified Equity"), "value": (r.get("current_value") or 0.0)} for r in eq]
        + [{"group": (r.get("asset_class") or "Mutual Fund"), "value": (r.get("current_value") or 0.0)} for r in mf]
        + [{"group": (r.get("category") or "Manual").replace("_", " ").title(), "value": (r.get("current_value") or 0.0)} for r in manual]
    )
    return analytics.sector_exposure(positions)


def _performance(conn, eid: Optional[int], metric: str, n: int) -> list[dict]:
    if metric not in _PERF_METRICS:
        raise ValueError(f"unknown metric: {metric}")
    mf = _mf_holdings(conn, eid)
    eq = _equity_holdings(conn, eid)
    combined = (
        [{"name": r["security_name"], metric: r.get(metric)} for r in mf if metric in r]
        + [{"name": r["symbol"], metric: r.get(metric)} for r in eq if metric in r]
    )
    return analytics.laggard_ranking(combined, metric, n=n)


def _transactions(conn, eid: Optional[int], limit: int) -> list[dict]:
    limit = max(1, min(limit, 200))
    where, params = _entity_clause(eid, "t.entity_id")
    cur = conn.cursor()
    cur.execute(f"""
        SELECT t.transaction_date, t.transaction_type, t.amount, t.units, t.nav,
               t.folio_number, sm.security_name, sm.isin
        FROM mf_transaction t
        JOIN security_master sm ON sm.id = t.security_id
        {where}
        ORDER BY t.transaction_date DESC, t.id DESC
        LIMIT %s
    """, params + [limit])
    rows = cur.fetchall()
    cur.close()
    return [{
        "date": str(r["transaction_date"]),
        "type": r["transaction_type"],
        "amount": _f(r["amount"]),
        "units": _f(r["units"]),
        "nav": _f(r["nav"]),
        "folio_number": r["folio_number"],
        "security_name": r["security_name"],
        "isin": r["isin"],
    } for r in rows]


# ---------------------------------------------------------------------------
# Quant tools — pull real history, compute via analytics.py
# ---------------------------------------------------------------------------

def _concentration(conn, eid: Optional[int], universe: str) -> dict:
    values: list[float] = []
    if universe in ("all", "equity"):
        values += [r["current_value"] or 0.0 for r in _equity_holdings(conn, eid)]
    if universe in ("all", "mf"):
        values += [r["current_value"] or 0.0 for r in _mf_holdings(conn, eid)]
    result = analytics.hhi_concentration(values)
    result["universe"] = universe
    return result


def _equity_value_series(conn, eid: Optional[int], lookback_days: int) -> list[float]:
    """Daily total direct-equity market value for the scope, oldest -> newest."""
    where, params = _entity_clause(eid, "entity_id")
    extra = ("AND " if where else "WHERE ") + "snapshot_date >= CURRENT_DATE - %s::int"
    cur = conn.cursor()
    cur.execute(f"""
        SELECT snapshot_date, SUM(market_value) AS total
        FROM equity_holding_history
        {where} {extra}
        GROUP BY snapshot_date
        ORDER BY snapshot_date ASC
    """, params + [lookback_days])
    rows = cur.fetchall()
    cur.close()
    return [float(r["total"]) for r in rows if r["total"] is not None]


def _drawdown(conn, eid: Optional[int], lookback_days: int) -> dict:
    series = _equity_value_series(conn, eid, lookback_days)
    result = analytics.max_drawdown(series)
    result["observations"] = len(series)
    return result


def _volatility(conn, eid: Optional[int], lookback_days: int) -> dict:
    series = _equity_value_series(conn, eid, lookback_days)
    result = analytics.annualized_volatility(series)
    result["observations"] = len(series)
    return result


def _correlation(conn, eid: Optional[int], top_n: int, lookback_days: int) -> dict:
    top_n = max(2, min(top_n, 15))
    # largest current holdings by value, scoped
    holdings = _equity_holdings(conn, eid)[:top_n]
    symbols = [h["symbol"] for h in holdings]
    if len(symbols) < 2:
        return {"labels": symbols, "matrix": [], "note": "need at least two holdings"}

    where, params = _entity_clause(eid, "entity_id")
    extra = ("AND " if where else "WHERE ") + "snapshot_date >= CURRENT_DATE - %s::int"
    cur = conn.cursor()
    # pull close-price series per symbol (a symbol may span brokers -> average close)
    cur.execute(f"""
        SELECT symbol, snapshot_date, AVG(close_price) AS px
        FROM equity_holding_history
        {where} {extra}
        GROUP BY symbol, snapshot_date
        ORDER BY symbol, snapshot_date ASC
    """, params + [lookback_days])
    rows = cur.fetchall()
    cur.close()

    series_map: dict[str, list[float]] = {}
    for r in rows:
        if r["symbol"] in symbols and r["px"] is not None:
            series_map.setdefault(r["symbol"], []).append(float(r["px"]))
    return analytics.correlation_matrix(series_map)


def _scenario(conn, eid: Optional[int], shocks: dict) -> dict:
    exposure = _sector_exposure(conn, eid)
    positions = [{"group": b["group"], "value": b["value"]} for b in exposure["breakdown"]]
    # normalise shock keys case-insensitively against known groups
    group_lookup = {p["group"].lower(): p["group"] for p in positions}
    norm_shocks: dict[str, float] = {}
    for k, v in shocks.items():
        if k == "*":
            norm_shocks["*"] = float(v)
        else:
            norm_shocks[group_lookup.get(str(k).lower(), k)] = float(v)
    return analytics.scenario_shock(positions, norm_shocks)


# ---------------------------------------------------------------------------
# Chart spec validation (render_chart is intent-only — no DB, no model data trust)
# ---------------------------------------------------------------------------

_CHART_TYPES = {"donut", "bar", "line", "heatmap"}


def validate_chart_spec(tool_input: dict) -> dict:
    """
    Validate + normalise a render_chart spec. The model supplies the numbers (sourced from
    prior tool results); we only sanity-check shape and coerce types — no DB access. Returns
    a clean spec dict; raises ValueError on malformed input so the engine can hand the model
    a recoverable error rather than rendering garbage.
    """
    ti = tool_input or {}
    ctype = ti.get("chart_type")
    if ctype not in _CHART_TYPES:
        raise ValueError(f"chart_type must be one of {sorted(_CHART_TYPES)}")

    spec: dict = {"chart_type": ctype, "title": str(ti.get("title") or "").strip()}
    unit = ti.get("unit")
    if unit:
        spec["unit"] = str(unit)[:8]

    if ctype in ("donut", "bar"):
        series = []
        for item in (ti.get("series") or []):
            if not isinstance(item, dict):
                continue
            label, val = item.get("label"), item.get("value")
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            if label is None or not math.isfinite(fval):
                continue
            series.append({"label": str(label), "value": round(fval, 4)})
        if not series:
            raise ValueError(f"{ctype} chart needs a non-empty 'series' of {{label, value}}")
        spec["series"] = series[:30]

    elif ctype == "line":
        points = []
        for item in (ti.get("points") or []):
            if not isinstance(item, dict):
                continue
            x, y = item.get("x"), item.get("y")
            try:
                fy = float(y)
            except (TypeError, ValueError):
                continue
            if x is None or not math.isfinite(fy):
                continue
            points.append({"x": str(x), "y": round(fy, 4)})
        if len(points) < 2:
            raise ValueError("line chart needs at least two 'points' of {x, y}")
        spec["points"] = points[:500]

    elif ctype == "heatmap":
        labels = [str(l) for l in (ti.get("labels") or [])]
        matrix_in = ti.get("matrix") or []
        n = len(labels)
        if n < 2:
            raise ValueError("heatmap needs at least two 'labels'")
        if len(matrix_in) != n:
            raise ValueError("heatmap 'matrix' rows must match the number of labels")
        matrix = []
        for row in matrix_in:
            if not isinstance(row, (list, tuple)) or len(row) != n:
                raise ValueError("heatmap 'matrix' must be square (labels x labels)")
            out_row = []
            for v in row:
                try:
                    fv = float(v)
                    out_row.append(round(fv, 4) if math.isfinite(fv) else None)
                except (TypeError, ValueError):
                    out_row.append(None)
            matrix.append(out_row)
        spec["labels"] = labels
        spec["matrix"] = matrix

    return spec


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch(name: str, tool_input: dict, conn, eid: Optional[int]) -> str:
    """
    Execute a client-side tool and return its result as a JSON string (the form the
    Messages API expects in a tool_result block). Raises ValueError on unknown tool.
    `eid` is the resolved entity scope (None = all-entities) — enforced in every query.
    """
    tool_input = tool_input or {}
    if name == "get_portfolio_overview":
        result = _portfolio_overview(conn, eid)
    elif name == "get_mf_holdings":
        result = {"holdings": _mf_holdings(conn, eid)}
    elif name == "get_equity_holdings":
        result = {"holdings": _equity_holdings(conn, eid)}
    elif name == "get_sector_exposure":
        result = _sector_exposure(conn, eid)
    elif name == "get_performance":
        result = {"laggards": _performance(conn, eid, tool_input["metric"],
                                           int(tool_input.get("n", 5)))}
    elif name == "get_transactions":
        result = {"transactions": _transactions(conn, eid, int(tool_input.get("limit", 50)))}
    elif name == "compute_concentration":
        result = _concentration(conn, eid, tool_input.get("universe", "all"))
    elif name == "compute_drawdown":
        result = _drawdown(conn, eid, int(tool_input.get("lookback_days", 365)))
    elif name == "compute_volatility":
        result = _volatility(conn, eid, int(tool_input.get("lookback_days", 365)))
    elif name == "compute_correlation":
        result = _correlation(conn, eid, int(tool_input.get("top_n", 8)),
                              int(tool_input.get("lookback_days", 365)))
    elif name == "compute_scenario":
        result = _scenario(conn, eid, tool_input.get("shocks", {}))
    else:
        raise ValueError(f"unknown tool: {name}")
    return json.dumps(result, default=str)
