"""
Model routing — default to Sonnet, escalate to Opus for genuinely hard turns.

Cost rationale: Opus output (incl. thinking) is 5x Sonnet's. Most messages are lookups
("what's my equity value", "list my funds") that Sonnet answers perfectly; only advisory /
analytical questions ("should I rebalance", "why is pharma lagging", "is this worth buying")
need Opus-grade judgment. We choose the model ONCE per user turn (the agentic loop reuses it
across internal tool turns) using a deterministic heuristic — no extra classifier API call,
no added latency. Env vars let ops retune the model ids without a code change.
"""

from __future__ import annotations

import os
import re

# Tunable via env so model ids can change without a deploy.
DEFAULT_MODEL    = os.getenv("ASSISTANT_MODEL_DEFAULT",    "claude-sonnet-4-6")
ESCALATION_MODEL = os.getenv("ASSISTANT_MODEL_ESCALATION", "claude-opus-4-8")

# Reasoning effort per tier. Thinking tokens bill as output, so this is the second
# biggest cost dial after model choice. "medium" gives advisory-grade reasoning at a
# meaningful discount to "high"; lookups on the default tier need little deliberation.
DEFAULT_EFFORT    = os.getenv("ASSISTANT_EFFORT_DEFAULT",    "medium")
ESCALATION_EFFORT = os.getenv("ASSISTANT_EFFORT_ESCALATION", "medium")

# Long, multi-part questions tend to need deeper reasoning even without a keyword hit.
_LONG_MESSAGE_CHARS = 320

# Signals of advisory / analytical depth → escalate to Opus.
_ESCALATION_RE = re.compile(
    r"""
    \b(
        recommend | advice | advise | suggest |
        should \s i | should \s we | worth \s (buying|holding|selling|it) |
        better \s to | ought \s to |
        rebalanc | reallocat | re-?allocat | diversif |
        over-?weight | under-?weight | allocat |
        valuation | valued | intrinsic | fair \s value | over-?valued | under-?valued |
        fundamental | thesis | moat | outlook | forecast | project(ed|ion)? |
        scenario | what \s if | stress | downside | upside |
        compare | comparison | versus | vs\b |
        under-?perform | out-?perform | lagging | dragging | why \s (is|are|did|has|have) |
        risk | hedge | exposure | concentrat | drawdown |
        tax | capital \s gain | harvest |
        opportunit | sector \s rotation | macro | interest \s rate | inflation |
        buy | sell | exit | enter | add \s more | trim |
        good \s (idea|investment|time) | bad \s (idea|investment|time)
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def choose_model(user_text: str, history: list[dict] | None = None) -> str:
    """Return the model id for this user turn.

    Escalate to Opus when the message shows advisory/analytical intent or is a long,
    multi-part question; otherwise use the cheaper default (Sonnet).
    """
    text = (user_text or "").strip()
    if not text:
        return DEFAULT_MODEL
    if _ESCALATION_RE.search(text):
        return ESCALATION_MODEL
    if len(text) >= _LONG_MESSAGE_CHARS:
        return ESCALATION_MODEL
    return DEFAULT_MODEL


def effort_for(model: str) -> str:
    """Reasoning effort to pair with the chosen model tier."""
    return ESCALATION_EFFORT if model == ESCALATION_MODEL else DEFAULT_EFFORT
