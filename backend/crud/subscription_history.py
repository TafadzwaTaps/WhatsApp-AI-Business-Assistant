"""
crud/subscription_history.py — SuperAdmin 2.0 (Phase 9): cohort & churn
analytics support.

Requires the `subscription_events` table (schema in
subscription_events_migration.sql — run it once in the Supabase SQL
editor, same optional-table pattern as admin_audit_log_migration.sql).

Every function here is best-effort and NEVER raises: a missing table (not
migrated yet), a Supabase outage, or any other DB error is caught, logged
at debug level, and treated as "no events recorded" — never fabricated.
Logging a subscription event must never be able to break the billing
webhook or admin action that triggered it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.db import supabase

log = logging.getLogger(__name__)


def log_subscription_event(
    *,
    business_id: int,
    previous_tier: Optional[str],
    new_tier: Optional[str],
    previous_status: Optional[str],
    new_status: Optional[str],
    source: str = "stripe_webhook",
) -> None:
    """Best-effort insert of one subscription transition row. Never raises."""
    try:
        supabase.table("subscription_events").insert({
            "business_id":     business_id,
            "previous_tier":   previous_tier,
            "new_tier":        new_tier,
            "previous_status": previous_status,
            "new_status":      new_status,
            "source":          source,
        }).execute()
    except Exception as exc:
        log.debug("log_subscription_event skipped (%s) — table may not exist yet", exc)


def list_subscription_events(
    business_id: Optional[int] = None,
    since_iso: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    """Subscription transition rows, newest first. Returns [] on any error."""
    try:
        q = supabase.table("subscription_events").select("*").order("created_at", desc=True).limit(limit)
        if business_id is not None:
            q = q.eq("business_id", business_id)
        if since_iso:
            q = q.gte("created_at", since_iso)
        res = q.execute()
        return res.data or []
    except Exception as exc:
        log.debug("list_subscription_events failed (%s) — treating as empty", exc)
        return []


def earliest_event_timestamp() -> Optional[str]:
    """
    The created_at of the oldest row in subscription_events, i.e. when
    transition tracking actually started. Used to honestly caveat any
    churn-timing figure — a cancellation that happened before this
    timestamp existed as an event is invisible to us. Returns None if the
    table is empty or missing (never guesses a date).
    """
    try:
        res = (
            supabase.table("subscription_events")
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
