"""
crud/handoff_log.py — Phase 14: durable human-handoff event log.

Requires the `handoff_log` table (schema in handoff_log_migration.sql at
the repo root) — same optional-table convention as crud/ai_usage.py
(Phase 12) and the `ratings` table (crud/analytics.py). Every function
here is best-effort and NEVER raises: a missing table, a Supabase outage,
or any other DB error is caught, logged at debug level, and treated as
"nothing recorded" / "0 events". Human handoff itself (Phase 9) does not
read from this table at all — it only gains a durable, purpose-built
record of WHEN and WHY each escalation happened, in place of the older
"count outgoing messages starting with the handoff emoji" proxy still
used by the /analytics/handoff-stats "today" figure.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from core.db import supabase

log = logging.getLogger(__name__)


def insert_handoff_event(business_id: int, phone: str, reason: str) -> None:
    """Best-effort record of one handoff event. Never raises."""
    try:
        supabase.table("handoff_log").insert({
            "business_id": business_id,
            "phone":       phone,
            "reason":      reason or "",
        }).execute()
    except Exception as exc:
        log.debug("insert_handoff_event skipped (%s) — table may not exist yet", exc)


def get_handoff_events(business_id: int, hours: float = 720.0) -> list[dict]:
    """
    Return this business's handoff_log rows in the last `hours` hours
    (default 30 days). Returns [] on any error, including the table not
    existing yet.
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = (
            supabase.table("handoff_log")
            .select("phone, reason, created_at")
            .eq("business_id", business_id)
            .gte("created_at", cutoff)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.debug("get_handoff_events failed (%s) — returning []", exc)
        return []
