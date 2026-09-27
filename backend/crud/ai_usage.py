"""
crud/ai_usage.py — Phase 12: AI request/cost tracking.

Requires the `ai_usage_log` table (schema + create statement in
ai_usage_log_migration.sql at the repo root — run it once in the
Supabase SQL editor, the same way this project's other optional tables,
like `ratings`, are added: see crud/analytics.py's get_satisfaction_score
for that established pattern).

Every function here is best-effort and NEVER raises: a missing table (not
migrated yet), a Supabase outage, or any other DB error is caught, logged
at debug level, and treated as "nothing recorded" / "0 recent requests".
Tracking must never be able to break the customer-facing reply pipeline,
and a business's usage cap (core/plan_guard.check_ai_usage_limit) must
never be enforced against data this module failed to read — that would
turn an observability outage into a customer-facing outage, which is
strictly worse.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from core.db import supabase

log = logging.getLogger(__name__)


def insert_ai_usage_log(row: dict) -> None:
    """Best-effort insert of one AI-request record. Never raises."""
    try:
        supabase.table("ai_usage_log").insert(row).execute()
    except Exception as exc:
        log.debug("insert_ai_usage_log skipped (%s) — table may not exist yet", exc)


def count_recent_ai_requests(business_id: int, hours: float = 24.0) -> int:
    """
    Count this business's ai_usage_log rows in the last `hours` hours.
    Returns 0 (never blocks) on any error, including the table not
    existing yet — a business with no tracking data has not used up any
    quota, by definition.
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = (
            supabase.table("ai_usage_log")
            .select("id")
            .eq("business_id", business_id)
            .gte("created_at", cutoff)
            .execute()
        )
        return len(res.data or [])
    except Exception as exc:
        log.debug("count_recent_ai_requests failed (%s) — treating as 0", exc)
        return 0


def get_ai_usage_summary(business_id: int, hours: float = 24.0) -> dict:
    """
    Lightweight aggregate for a future dashboard card: request count,
    total tokens, and total estimated cost over the window. Returns all
    zeros on any error rather than raising.
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = (
            supabase.table("ai_usage_log")
            .select("total_tokens, estimated_cost")
            .eq("business_id", business_id)
            .gte("created_at", cutoff)
            .execute()
        )
        rows = res.data or []
        return {
            "requests":       len(rows),
            "total_tokens":   sum(int(r.get("total_tokens") or 0) for r in rows),
            "estimated_cost": round(sum(float(r.get("estimated_cost") or 0) for r in rows), 4),
        }
    except Exception as exc:
        log.debug("get_ai_usage_summary failed (%s) — returning zeros", exc)
        return {"requests": 0, "total_tokens": 0, "estimated_cost": 0.0}
