"""
crud/admin_audit.py — SuperAdmin 2.0: audit log, admin notes, risk flags.

Requires the `admin_audit_logs`, `business_admin_notes` and
`business_risk_flags` tables (schema in admin_audit_log_migration.sql at
the repo root — run it once in the Supabase SQL editor, the same
optional-table pattern as ai_usage_log_migration.sql).

Every function here is best-effort and NEVER raises: a missing table (not
migrated yet), a Supabase outage, or any other DB error is caught, logged
at debug level, and treated as "nothing recorded" / "no history yet".
Audit logging must never be able to break the admin action it is
recording — an admin who suspends a business must have that suspension
succeed even if the audit-log insert fails.

Never pass secrets (passwords, tokens, Stripe/WhatsApp keys) into
`metadata` — callers are responsible for only passing safe, already
non-sensitive context (e.g. old/new tier, old/new is_active).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.db import supabase

log = logging.getLogger(__name__)


def log_admin_action(
    *,
    actor_username: Optional[str],
    action: str,
    target_type: str = "business",
    target_id: Optional[Any] = None,
    business_id: Optional[int] = None,
    reason: Optional[str] = None,
    metadata: Optional[dict] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    actor_role: str = "superadmin",
) -> None:
    """Best-effort insert of one audit-log row. Never raises."""
    try:
        row = {
            "actor_username": actor_username,
            "actor_role":     actor_role,
            "action":         action,
            "target_type":    target_type,
            "target_id":      str(target_id) if target_id is not None else None,
            "business_id":    business_id,
            "reason":         reason,
            "metadata":       metadata or {},
            "ip_address":     ip_address,
            "user_agent":     user_agent,
        }
        supabase.table("admin_audit_logs").insert(row).execute()
    except Exception as exc:
        log.debug("log_admin_action skipped (%s) — table may not exist yet", exc)


def list_audit_logs(
    limit: int = 50,
    offset: int = 0,
    business_id: Optional[int] = None,
    action: Optional[str] = None,
) -> list[dict]:
    """Recent audit-log rows, newest first. Returns [] on any error."""
    try:
        q = (
            supabase.table("admin_audit_logs")
            .select("*")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
        )
        if business_id is not None:
            q = q.eq("business_id", business_id)
        if action:
            q = q.eq("action", action)
        res = q.execute()
        return res.data or []
    except Exception as exc:
        log.debug("list_audit_logs failed (%s) — treating as empty", exc)
        return []


def add_admin_note(business_id: int, author_username: Optional[str], note: str) -> Optional[dict]:
    """Best-effort insert of one private admin note. Returns the row or None."""
    try:
        res = (
            supabase.table("business_admin_notes")
            .insert({
                "business_id":     business_id,
                "author_username": author_username,
                "note":            note,
            })
            .execute()
        )
        return (res.data or [None])[0]
    except Exception as exc:
        log.debug("add_admin_note skipped (%s) — table may not exist yet", exc)
        return None


def list_admin_notes(business_id: int, limit: int = 50) -> list[dict]:
    """Private admin notes for a business, newest first. Returns [] on any error."""
    try:
        res = (
            supabase.table("business_admin_notes")
            .select("*")
            .eq("business_id", business_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.debug("list_admin_notes failed (%s) — treating as empty", exc)
        return []


def add_risk_flag(business_id: int, risk_level: str, reason: str, evidence: Optional[dict] = None) -> Optional[dict]:
    """Best-effort insert of one transparent risk flag. Returns the row or None."""
    try:
        res = (
            supabase.table("business_risk_flags")
            .insert({
                "business_id": business_id,
                "risk_level":  risk_level,
                "reason":      reason,
                "evidence":    evidence or {},
                "status":      "open",
            })
            .execute()
        )
        return (res.data or [None])[0]
    except Exception as exc:
        log.debug("add_risk_flag skipped (%s) — table may not exist yet", exc)
        return None


def list_risk_flags(business_id: Optional[int] = None, status: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Risk flags, newest first. Returns [] on any error."""
    try:
        q = supabase.table("business_risk_flags").select("*").order("created_at", desc=True).limit(limit)
        if business_id is not None:
            q = q.eq("business_id", business_id)
        if status:
            q = q.eq("status", status)
        res = q.execute()
        return res.data or []
    except Exception as exc:
        log.debug("list_risk_flags failed (%s) — treating as empty", exc)
        return []


def resolve_risk_flag(flag_id: int, reviewed_by: Optional[str], status: str, action_taken: Optional[str] = None) -> Optional[dict]:
    """Mark a risk flag resolved/actioned. Returns the updated row or None."""
    try:
        res = (
            supabase.table("business_risk_flags")
            .update({
                "status":       status,
                "resolved_at":  datetime.now(timezone.utc).isoformat(),
                "reviewed_by":  reviewed_by,
                "action_taken": action_taken,
            })
            .eq("id", flag_id)
            .execute()
        )
        return (res.data or [None])[0]
    except Exception as exc:
        log.debug("resolve_risk_flag failed (%s)", exc)
        return None
