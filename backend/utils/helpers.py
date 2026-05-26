"""
utils/helpers.py — Shared utility functions for WaziBot.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def truncate(text: str, max_len: int = 80) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def log_event(event_type: str, **fields) -> None:
    """Log a structured business event. Never raises."""
    try:
        kv = "  ".join(f"{k}={v!r}" for k, v in fields.items())
        log.info("EVENT %s  %s", event_type, kv)
    except Exception:
        pass
