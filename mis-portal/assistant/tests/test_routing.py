"""
Tests for assistant.routing.choose_model — the Sonnet-default / Opus-escalation heuristic.

Run:  python -m pytest assistant/tests/test_routing.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from assistant import routing  # noqa: E402
from assistant.routing import (  # noqa: E402
    choose_model,
    effort_for,
    DEFAULT_MODEL,
    ESCALATION_MODEL,
    DEFAULT_EFFORT,
    ESCALATION_EFFORT,
)


# Simple lookups → cheap default model.
SIMPLE = [
    "What's my equity value?",
    "List my mutual funds",
    "How much is in IWS Fincorp?",
    "Show my holdings",
    "What is the NAV of my Parag Parikh fund?",
    "Total portfolio value please",
]

# Advisory / analytical questions → escalate to Opus.
HARD = [
    "Should I rebalance my portfolio?",
    "Is HDFC Bank worth buying at this valuation?",
    "Why is my pharma exposure lagging?",
    "Recommend how to reduce concentration risk",
    "Compare my large-cap funds versus the index",
    "What's the fundamental outlook for my IT holdings?",
    "Would it be a good idea to trim my gold position?",
]


@pytest.mark.parametrize("msg", SIMPLE)
def test_simple_uses_default(msg):
    assert choose_model(msg) == DEFAULT_MODEL


@pytest.mark.parametrize("msg", HARD)
def test_hard_escalates(msg):
    assert choose_model(msg) == ESCALATION_MODEL


def test_effort_pairs_with_tier():
    assert effort_for(ESCALATION_MODEL) == ESCALATION_EFFORT
    assert effort_for(DEFAULT_MODEL) == DEFAULT_EFFORT
    # Default tuning keeps both at medium (the second-biggest cost dial).
    assert effort_for(ESCALATION_MODEL) == "medium"
    assert effort_for("some-unknown-model") == DEFAULT_EFFORT


def test_empty_uses_default():
    assert choose_model("") == DEFAULT_MODEL
    assert choose_model("   ") == DEFAULT_MODEL


def test_long_multipart_escalates():
    long_msg = (
        "Here is some context about my situation and a few things I'm trying to "
        "figure out across my accounts this quarter, walking through each holding "
        "one by one and what I've been thinking about lately in quite some detail. "
        "I want to lay it all out clearly so you have the full picture before we "
        "start, including how the various accounts relate to each other over time."
    )
    assert len(long_msg) >= routing._LONG_MESSAGE_CHARS
    assert choose_model(long_msg) == ESCALATION_MODEL


def test_env_override(monkeypatch):
    # Model ids are env-tunable; reload the module so the constants re-read os.environ.
    monkeypatch.setenv("ASSISTANT_MODEL_DEFAULT", "claude-haiku-4-5-20251001")
    import importlib
    reloaded = importlib.reload(routing)
    try:
        assert reloaded.choose_model("List my funds") == "claude-haiku-4-5-20251001"
    finally:
        monkeypatch.delenv("ASSISTANT_MODEL_DEFAULT", raising=False)
        importlib.reload(routing)
