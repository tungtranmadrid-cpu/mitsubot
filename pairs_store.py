"""Persistent storage for the auto-refreshed PAIRS list.

Stores pairs in a JSON file so the list survives restarts without needing to
mutate environment variables. The path is controlled by the PAIRS_FILE env var
(default: ./data/pairs.json) — on Railway, point this at a mounted Volume.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("pairs_store")


def get_pairs_file() -> Path:
    return Path(os.getenv("PAIRS_FILE", "data/pairs.json"))


def load_pairs() -> list[str] | None:
    """Return the saved pairs list, or None if no file / invalid content."""
    path = get_pairs_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pairs = data.get("pairs") if isinstance(data, dict) else data
        if not isinstance(pairs, list):
            return None
        cleaned = [str(p).strip().upper() for p in pairs if str(p).strip()]
        return cleaned or None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to read {path}: {e}")
        return None


def save_pairs(pairs: list[str]) -> None:
    """Persist pairs to the JSON file (creates parent dirs as needed)."""
    path = get_pairs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pairs": [p.strip().upper() for p in pairs if p.strip()]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    logger.info(f"Saved {len(payload['pairs'])} pairs to {path}")
