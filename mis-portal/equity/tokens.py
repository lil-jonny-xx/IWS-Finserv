import json
import os
from pathlib import Path

_TOKEN_FILE = Path(__file__).parent / "equity_tokens.json"


def _load() -> dict:
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def get(key: str) -> str | None:
    return _load().get(key)


def save(key: str, value: str) -> None:
    data = _load()
    data[key] = value
    _TOKEN_FILE.write_text(json.dumps(data, indent=2))
