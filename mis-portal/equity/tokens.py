"""
Shared token store for broker access tokens.
Persists to equity_tokens.json alongside this file.
Not committed to git — add equity_tokens.json to .gitignore.
"""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKENS_FILE = Path(__file__).parent / "equity_tokens.json"


def load() -> dict:
    if _TOKENS_FILE.exists():
        return json.loads(_TOKENS_FILE.read_text())
    return {}


def get(key: str, default: str = "") -> str:
    return load().get(key, default)


def save(key: str, value: str) -> None:
    data = load()
    data[key] = value
    _TOKENS_FILE.write_text(json.dumps(data, indent=2))
    os.chmod(_TOKENS_FILE, 0o600)
    logger.debug(f"Token saved: {key}")
