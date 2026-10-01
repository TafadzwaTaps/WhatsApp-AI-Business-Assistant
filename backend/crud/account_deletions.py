"""
crud/account_deletions.py — self-service account deletion records.

Requires the `account_deletions` table (schema in
account_deletions_migration.sql — run it once in the Supabase SQL editor,
same optional-table pattern as every other table added this way in this
project).

Unlike crud/security_events.py (pure telemetry, fine to lose), this table
is the only durable record of WHERE a business's pre-deletion data backup
lives and WHEN it becomes eligible for permanent purge — so unlike that
module, insert_deletion_record() here is allowed to raise: if we can't
record where the backup went, services/account_deletion.py must not tell
the business their data is safely backed up. The read-side functions
still fail soft (return [] / None), since a listing/lookup failing should
not itself break anything — only a write silently vanishing would.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.db import supabase

log = logging.getLogger(__name__)


def insert_deletion_record(
    *,
    business_id: int,
    business_name: str,
    owner_username: str,
    owner_email: str,
    reason: str,
    backup_path: str,
    backup_format: str,
    purge_after_iso: str,
) -> dict:
    """Raises on failure — see module docstring for why this one doesn't fail-soft."""
    res = supabase.table("account_deletions").insert({
        "business_id":    business_id,
        "business_name":  business_name,
        "owner_username": owner_username,
        "owner_email":    owner_email,
        "reason":         reason or None,
        "backup_path":    backup_path,
        "backup_format":  backup_format,
        "status":         "pending_purge",
        "purge_after":    purge_after_iso,
    }).execute()
    rows = res.data or []
    return rows[0] if rows else {}


def get_pending_for_business(business_id: int) -> Optional[dict]:
    try:
        res = (
            supabase.table("account_deletions")
            .select("*")
            .eq("business_id", business_id)
            .eq("status", "pending_purge")
            .order("requested_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        log.debug("get_pending_for_business failed (%s) — treating as none", exc)
        return None


def list_due_for_purge(now_iso: str, limit: int = 200) -> list[dict]:
    try:
        res = (
            supabase.table("account_deletions")
            .select("*")
            .eq("status", "pending_purge")
            .lte("purge_after", now_iso)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.debug("list_due_for_purge failed (%s) — treating as empty", exc)
        return []


def mark_purged(record_id: int) -> None:
    try:
        from datetime import datetime, timezone
        supabase.table("account_deletions").update({
            "status": "purged",
            "purged_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", record_id).execute()
    except Exception as exc:
        log.warning("mark_purged failed for record_id=%s: %s", record_id, exc)


def mark_reactivated(business_id: int) -> None:
    """Best-effort — called when support reactivates a business that had a
    pending deletion, so it drops out of the purge queue. Never raises:
    this is a courtesy cleanup, not the thing that actually reactivates
    the account (routes/admin_routes.py's activate endpoint does that)."""
    try:
        from datetime import datetime, timezone
        supabase.table("account_deletions").update({
            "status": "reactivated",
            "reactivated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("business_id", business_id).eq("status", "pending_purge").execute()
    except Exception as exc:
        log.debug("mark_reactivated failed for business_id=%s (non-fatal): %s", business_id, exc)
