"""
Unit tests for assistant.analytics — exact expected numbers on known datasets.

Run from mis-portal/:  python -m pytest assistant/tests/test_analytics.py -q
(or:                   /var/www/.venv/bin/python -m pytest assistant/tests/test_analytics.py -q)
"""

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from assistant import analytics  # noqa: E402


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------

def test_hhi_equal_weights():
    # four equal positions -> hhi = 4 * (0.25^2) = 0.25, effective_n = 4
    r = analytics.hhi_concentration([25, 25, 25, 25])
    assert r["hhi"] == pytest.approx(0.25)
    assert r["hhi_index"] == pytest.approx(2500.0)
    assert r["effective_n"] == pytest.approx(4.0)
    assert r["top_weight_pct"] == pytest.approx(25.0)
    assert r["n"] == 4


def test_hhi_single_position():
    r = analytics.hhi_concentration([100])
    assert r["hhi"] == pytest.approx(1.0)
    assert r["effective_n"] == pytest.approx(1.0)
    assert r["top_weight_pct"] == pytest.approx(100.0)


def test_hhi_ignores_zero_and_none():
    r = analytics.hhi_concentration([50, 50, 0, None])
    assert r["n"] == 2
    assert r["hhi"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def test_max_drawdown_basic():
    # peak 100 -> trough 50 is the worst: -50%
    r = analytics.max_drawdown([100, 120, 60, 80, 50, 70])
    # running peak at index of value 50 is 120; (50-120)/120 = -58.333%
    assert r["max_drawdown_pct"] == pytest.approx(-58.3333, abs=1e-3)
    assert r["trough_value"] == pytest.approx(50.0)
    assert r["peak_value"] == pytest.approx(120.0)


def test_max_drawdown_monotonic_increase():
    r = analytics.max_drawdown([10, 20, 30, 40])
    assert r["max_drawdown_pct"] == pytest.approx(0.0)


def test_max_drawdown_too_short():
    assert analytics.max_drawdown([100])["max_drawdown_pct"] == 0.0


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------

def test_annualized_volatility_known():
    # returns of +10%, -10%, +10%... alternating from a known series
    values = [100, 110, 99, 108.9, 98.01]
    rets = np.diff(np.array(values)) / np.array(values)[:-1]
    expected_period = float(np.std(rets, ddof=1))
    r = analytics.annualized_volatility(values, periods_per_year=252)
    assert r["period_vol_pct"] == pytest.approx(expected_period * 100, abs=1e-4)
    assert r["annualized_vol_pct"] == pytest.approx(
        expected_period * math.sqrt(252) * 100, abs=1e-3
    )
    assert r["n_returns"] == 4


def test_annualized_volatility_constant_series_is_zero():
    r = analytics.annualized_volatility([50, 50, 50, 50])
    assert r["annualized_vol_pct"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def test_correlation_perfectly_correlated():
    a = [10, 11, 12, 13, 14]
    b = [20, 22, 24, 26, 28]  # exactly 2x -> returns identical -> corr 1
    r = analytics.correlation_matrix({"A": a, "B": b})
    assert r["labels"] == ["A", "B"]
    # diagonal is 1, off-diagonal is 1 (perfectly correlated returns)
    assert r["matrix"][0][1] == pytest.approx(1.0, abs=1e-6)


def test_correlation_inverse():
    # a has genuinely varying returns; b is built so each return is the exact
    # negative of a's -> perfect inverse correlation (-1).
    a = [100, 120, 90, 150, 100]
    a_rets = np.diff(np.array(a, dtype=float)) / np.array(a, dtype=float)[:-1]
    b = [100.0]
    for ar in a_rets:
        b.append(b[-1] * (1.0 - ar))
    r = analytics.correlation_matrix({"A": a, "B": b})
    assert r["matrix"][0][1] == pytest.approx(-1.0, abs=1e-6)


def test_correlation_insufficient_series():
    r = analytics.correlation_matrix({"A": [1, 2, 3]})
    assert r["matrix"] == []


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------

def test_sector_exposure():
    positions = [
        {"group": "Financials", "value": 60},
        {"group": "Tech", "value": 30},
        {"group": "Financials", "value": 10},
    ]
    r = analytics.sector_exposure(positions)
    assert r["total"] == pytest.approx(100.0)
    fin = next(b for b in r["breakdown"] if b["group"] == "Financials")
    assert fin["value"] == pytest.approx(70.0)
    assert fin["pct"] == pytest.approx(70.0)
    # sorted desc by value -> Financials first
    assert r["breakdown"][0]["group"] == "Financials"


def test_sector_exposure_unclassified():
    r = analytics.sector_exposure([{"group": None, "value": 10}])
    assert r["breakdown"][0]["group"] == "Unclassified"


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------

def test_scenario_shock_single_group():
    positions = [
        {"group": "Gold", "value": 100},
        {"group": "Equity", "value": 400},
    ]
    r = analytics.scenario_shock(positions, {"Gold": -10.0})
    # gold drops 10% -> -10 absolute on 500 total = -2%
    assert r["base_value"] == pytest.approx(500.0)
    assert r["post_value"] == pytest.approx(490.0)
    assert r["impact"] == pytest.approx(-10.0)
    assert r["impact_pct"] == pytest.approx(-2.0)


def test_scenario_shock_wildcard():
    positions = [{"group": "A", "value": 100}, {"group": "B", "value": 100}]
    r = analytics.scenario_shock(positions, {"*": -5.0, "A": -20.0})
    # A: -20 on 100 = -20; B falls through to wildcard -5 on 100 = -5
    assert r["impact"] == pytest.approx(-25.0)
    assert r["impact_pct"] == pytest.approx(-12.5)


# ---------------------------------------------------------------------------
# Laggards
# ---------------------------------------------------------------------------

def test_laggard_ranking():
    holdings = [
        {"name": "X", "returns_pct": 5.0},
        {"name": "Y", "returns_pct": -12.0},
        {"name": "Z", "returns_pct": -3.0},
        {"name": "W", "returns_pct": None},
    ]
    r = analytics.laggard_ranking(holdings, "returns_pct", n=2)
    assert [h["name"] for h in r] == ["Y", "Z"]
    assert r[0]["returns_pct"] == pytest.approx(-12.0)
