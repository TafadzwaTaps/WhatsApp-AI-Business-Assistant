"""
crud/security_events.py — SuperAdmin 2.0 (Phase 11): persisted security
event history, on top of the Phase 6 live in-process snapshot.

Requires the `security_events` table (schema in
security_events_migration.sql — run it once in the Supabase SQL editor,
same optional-table pattern as every other SuperAdmin 2.0 table).

Every function here is best-effort and NEVER raises: a missing table (not
migrated yet), a Supabase outage, or any other DB error is caught, logged
at debug level, and treated as "nothing recorded". Logging a security
event must never be able to affect the actual security check it's
attached to — a login lockout still locks, a bad webhook signature is
still rejected, whether or not this write succeeds.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.db import supabase

log = logging.getLogger(__name__)

EVENT_TYPES = (
    "ip_login_flagged",
    "account_lockout",
    "webhook_invalid_signature",
    "signup_success_limit_exceeded",
)


def log_security_event(
    *,
    event_type: str,
    ip: Optional[str] = None,
    username: Optional[str] = None,
    business_id: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Best-effort insert of one security-event row. Never raises."""
    try:
        supabase.table("security_events").insert({
            "event_type":  event_type,
            "ip":          ip,
            "username":    username,
            "business_id": business_id,
            "metadata":    metadata or {},
        }).execute()
    except Exception as exc:
        log.debug("log_security_event skipped (%s) — table may not exist yet", exc)


def list_security_events(
    event_type: Optional[str] = None,
    since_iso: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    """Security-event rows, newest first. Returns [] on any error."""
    try:
        q = supabase.table("security_events").select("*").order("created_at", desc=True).limit(limit)
        if event_type:
            q = q.eq("event_type", event_type)
        if since_iso:
            q = q.gte("created_at", since_iso)
        res = q.execute()
        return res.data or []
    except Exception as exc:
        log.debug("list_security_events failed (%s) — treating as empty", exc)
        return []


def earliest_event_timestamp() -> Optional[str]:
    """
    created_at of the oldest security_events row — when historical
    tracking actually started. Used to honestly caveat trend figures
    rather than presenting a synthetic history. Returns None if the table
    is empty or missing.
    """
    try:
        res = (
            supabase.table("security_events")
            .select("created_at")
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0]["created_at"] if rows else None
    except Exception as exc:
        log.debug("earliest_event_timestamp failed (%s)", exc)
        return None
