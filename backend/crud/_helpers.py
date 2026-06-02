"""
crud/_helpers.py — Shared helpers used across all crud sub-modules.

Keep this file small: only _now() and _one() live here.
Every other crud module imports from this file.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from core.db import supabase  # noqa: F401 — re-exported for convenience

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _one(table: str, res) -> Optional[dict]:
    data = res.data
    return data[0] if data else None
