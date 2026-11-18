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


def fetch_all(make_query, page: int = 1000, max_pages: int = 200) -> list:
    """Read every row of a query in pages of `page` (PostgREST silently truncates a plain
    select at its row cap). `make_query` must return a fresh query with a deterministic
    .order(). Falls back to a single plain execute if the client has no .range()."""
    rows: list = []
    for i in range(max_pages):
        q = make_query()
        if not hasattr(q, "range"):
            return list(q.execute().data or [])
        data = q.range(i * page, (i + 1) * page - 1).execute().data or []
        rows.extend(data)
        if len(data) < page:
            break
    else:
        log.warning("fetch_all hit max_pages=%d (%d rows): result truncated", max_pages, len(rows))
    return rows


def _one(table: str, res) -> Optional[dict]:
    data = res.data
    return data[0] if data else None
