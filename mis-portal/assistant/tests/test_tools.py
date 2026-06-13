"""
Tests for assistant.tools dispatch — validates plumbing and entity scoping with a stub DB.

A StubCursor returns queued result-sets (dicts, like RealDictCursor) and records the SQL it
ran, so we can assert that the entity-scoping WHERE clause is applied. Real SQL execution is
covered by the live curl/scoping checks against Postgres.

Run:  python -m pytest assistant/tests/test_tools.py -q
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from assistant import tools  # noqa: E402


class StubCursor:
    def __init__(self, result_queue, sql_log):
        self._queue = result_queue
        self._sql_log = sql_log
        self._last = []

    def execute(self, sql, params=None):
        self._sql_log.append((sql, list(params) if params else []))
        self._last = self._queue.pop(0) if self._queue else []

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None

    def close(self):
        pass


class StubConn:
    def __init__(self, result_queue):
        self.result_queue = result_queue
        self.sql_log = []

    def cursor(self):
        return StubCursor(self.result_queue, self.sql_log)


def test_equity_holdings_scoped_query():
    rows = [{
        "entity_id": 5, "broker": "zerodha", "symbol": "RELIANCE", "symbol_override": None,
        "isin": "INE", "sector": "Energy", "quantity": 10, "cost": 1000,
        "current_market_value": 1500, "market_value_as_on": None, "weekly_change": 20,
        "exposure_pct": 5, "pnl_inception": 500, "pnl_ytd": 100,
        "returns_inception_pct": 50, "returns_ytd_pct": 10, "cagr_inception_pct": 12,
    }]
    conn = StubConn([rows])
    out = json.loads(tools.dispatch("get_equity_holdings", {}, conn, eid=5))
    assert out["holdings"][0]["symbol"] == "RELIANCE"
    assert out["holdings"][0]["current_value"] == 1500.0
    # entity scope must be in the SQL + params
    sql, params = conn.sql_log[0]
    assert "eh.entity_id = %s" in sql
    assert params == [5]


def test_equity_holdings_all_entities_no_filter():
    conn = StubConn([[]])
    tools.dispatch("get_equity_holdings", {}, conn, eid=None)
    sql, params = conn.sql_log[0]
    assert "WHERE" not in sql
    assert params == []


def test_concentration_equity_universe():
    rows = [
        {"entity_id": 1, "broker": "z", "symbol": "A", "symbol_override": None, "isin": "x",
         "sector": "Tech", "quantity": 1, "cost": 1, "current_market_value": 25,
         "market_value_as_on": None, "weekly_change": 0, "exposure_pct": 0,
         "pnl_inception": 0, "pnl_ytd": 0, "returns_inception_pct": 0,
         "returns_ytd_pct": 0, "cagr_inception_pct": 0}
        for _ in range(4)
    ]
    conn = StubConn([rows])
    out = json.loads(tools.dispatch("compute_concentration", {"universe": "equity"}, conn, eid=1))
    # four equal 25-value positions -> hhi 0.25, effective_n 4
    assert out["hhi"] == pytest.approx(0.25)
    assert out["effective_n"] == pytest.approx(4.0)
    assert out["universe"] == "equity"


def test_performance_laggards():
    mf_rows = [
        {"entity_id": 1, "folio_number": "F1", "invested_amount": 100, "current_value": 90,
         "market_value_as_on": 90, "weekly_change": -5, "exposure_pct": 1, "pnl_inception": -10,
         "pnl_ytd": -5, "returns_inception_pct": -10, "returns_ytd_pct": -5,
         "cagr_inception_pct": -3, "xirr_inception_pct": -4, "security_name": "Fund A",
         "isin": "i1", "asset_class": "EQUITY", "security_type": "EQUITY"},
    ]
    eq_rows = [
        {"entity_id": 1, "broker": "z", "symbol": "BAD", "symbol_override": None, "isin": "x",
         "sector": "Tech", "quantity": 1, "cost": 100, "current_market_value": 70,
         "market_value_as_on": 70, "weekly_change": -8, "exposure_pct": 1, "pnl_inception": -30,
         "pnl_ytd": -10, "returns_inception_pct": -30, "returns_ytd_pct": -10,
         "cagr_inception_pct": -9},
    ]
    conn = StubConn([mf_rows, eq_rows])
    out = json.loads(tools.dispatch(
        "get_performance", {"metric": "returns_inception_pct", "n": 2}, conn, eid=1))
    names = [h["name"] for h in out["laggards"]]
    # BAD (-30) is worse than Fund A (-10)
    assert names[0] == "BAD"


def test_scenario_via_exposure():
    # sector exposure pulls equity, then mf, then manual (3 queries)
    eq_rows = [
        {"entity_id": 1, "broker": "z", "symbol": "GOLDBEES", "symbol_override": None,
         "isin": "x", "sector": "Gold", "quantity": 1, "cost": 100, "current_market_value": 100,
         "market_value_as_on": 100, "weekly_change": 0, "exposure_pct": 0, "pnl_inception": 0,
         "pnl_ytd": 0, "returns_inception_pct": 0, "returns_ytd_pct": 0, "cagr_inception_pct": 0},
    ]
    mf_rows = [
        {"entity_id": 1, "folio_number": "F", "invested_amount": 400, "current_value": 400,
         "market_value_as_on": 400, "weekly_change": 0, "exposure_pct": 0, "pnl_inception": 0,
         "pnl_ytd": 0, "returns_inception_pct": 0, "returns_ytd_pct": 0, "cagr_inception_pct": 0,
         "xirr_inception_pct": 0, "security_name": "Eq Fund", "isin": "i",
         "asset_class": "Equity", "security_type": "EQUITY"},
    ]
    manual_rows = []
    conn = StubConn([eq_rows, mf_rows, manual_rows])
    out = json.loads(tools.dispatch("compute_scenario", {"shocks": {"Gold": -10}}, conn, eid=1))
    # gold is 100 of 500 -> -10% on gold = -10 absolute = -2% of book
    assert out["base_value"] == pytest.approx(500.0)
    assert out["impact"] == pytest.approx(-10.0)
    assert out["impact_pct"] == pytest.approx(-2.0)


# ---------------------------------------------------------------------------
# render_chart spec validation (intent-only — no DB touched)
# ---------------------------------------------------------------------------

def test_chart_donut_valid():
    spec = tools.validate_chart_spec({
        "chart_type": "donut", "title": " Mix ", "unit": "₹",
        "series": [{"label": "Equity", "value": 60}, {"label": "Debt", "value": 40}],
    })
    assert spec["chart_type"] == "donut"
    assert spec["title"] == "Mix"          # trimmed
    assert spec["unit"] == "₹"
    assert spec["series"] == [{"label": "Equity", "value": 60.0},
                              {"label": "Debt", "value": 40.0}]


def test_chart_bar_drops_bad_points_keeps_good():
    spec = tools.validate_chart_spec({
        "chart_type": "bar",
        "series": [{"label": "A", "value": "12.5"}, {"label": "B", "value": "x"},
                   {"value": 5}, {"label": "C", "value": 7}],
    })
    # "x" (unparseable) and the label-less entry are dropped; "12.5" coerces
    assert spec["series"] == [{"label": "A", "value": 12.5}, {"label": "C", "value": 7.0}]


def test_chart_line_needs_two_points():
    with pytest.raises(ValueError):
        tools.validate_chart_spec({"chart_type": "line", "points": [{"x": "Jan", "y": 1}]})
    spec = tools.validate_chart_spec({
        "chart_type": "line",
        "points": [{"x": "Jan", "y": 1}, {"x": "Feb", "y": 2.5}],
    })
    assert spec["points"] == [{"x": "Jan", "y": 1.0}, {"x": "Feb", "y": 2.5}]


def test_chart_heatmap_must_be_square():
    with pytest.raises(ValueError):
        tools.validate_chart_spec({"chart_type": "heatmap", "labels": ["A", "B"],
                                   "matrix": [[1, 0.5]]})  # only one row
    spec = tools.validate_chart_spec({"chart_type": "heatmap", "labels": ["A", "B"],
                                      "matrix": [[1, 0.5], [0.5, 1]]})
    assert spec["matrix"] == [[1.0, 0.5], [0.5, 1.0]]


def test_chart_rejects_unknown_type_and_empty_series():
    with pytest.raises(ValueError):
        tools.validate_chart_spec({"chart_type": "pie"})
    with pytest.raises(ValueError):
        tools.validate_chart_spec({"chart_type": "donut", "series": []})
