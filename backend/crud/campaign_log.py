"""
crud/campaign_log.py — SuperAdmin 2.0 (Phase 10): campaign-abuse detection
support.

Requires the `campaign_log` table (schema in campaign_log_migration.sql —
run it once in the Supabase SQL editor, same optional-table pattern as
every other SuperAdmin 2.0 table).

Every function here is best-effort and NEVER raises: a missing table (not
migrated yet), a Supabase outage, or any other DB error is caught, logged
at debug level, and treated as "no campaigns recorded" — never fabricated.
Logging a campaign send must never be able to break the actual send.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.db import supabase

log = logging.getLogger(__name__)


def log_campaign_send(
    *,
    business_id: int,
    audience: Optional[str],
    recipient_count: int,
    message_length: Optional[int] = None,
) -> None:
    """Best-effort insert of one campaign-send row. Never raises."""
    try:
        supabase.table("campaign_log").insert({
            "business_id":     business_id,
            "audience":        audience,
            "recipient_count": recipient_count,
            "message_length":  message_length,
        }).execute()
    except Exception as exc:
        log.debug("log_campaign_send skipped (%s) — table may not exist yet", exc)


def list_campaign_sends(
    business_id: Optional[int] = None,
    since_iso: Optional[str] = None,
    limit: int = 1000,
) -> list[dict]:
    """Campaign-send rows, newest first. Returns [] on any error."""
    try:
        q = supabase.table("campaign_log").select("*").order("created_at", desc=True).limit(limit)
        if business_id is not None:
            q = q.eq("business_id", business_id)
        if since_iso:
            q = q.gte("created_at", since_iso)
        res = q.execute()
        return res.data or []
    except Exception as exc:
        log.debug("list_campaign_sends failed (%s) — treating as empty", exc)
        return []
