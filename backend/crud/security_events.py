"""
crud/security_events.py — SuperAdmin 2.0 (Phase 11): persisted security
event history, on top of the Phase 6 live in-process snapshot.

Requires the `security_events` table. Severity, repeat counting and retention
additionally need the Security Operations Center database changes (part 1, then
migrations/soc_04_security_event_counts.sql); without them, events are written
with the original five columns, and without part 1 nothing is deleted. Report
new events through services.security_events.emit(), which queues and batches
them.

Every function here is best-effort and NEVER raises: a missing table (not
migrated yet), a Supabase outage, or any other DB error is caught, logged
(a dropped write at warning level), and treated as "nothing recorded". Logging a security
event must never be able to affect the actual security check it's
attached to — a login lockout still locks, a bad webhook signature is
still rejected, whether or not this write succeeds.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from core.db import supabase
from core.safelog import record_dropped, redact

log = logging.getLogger(__name__)

# event type -> (default severity, default outcome). Every reported type is listed here.
EVENT_RULES = {
    "ip_login_flagged":              ("medium", "blocked"),
    "account_lockout":               ("medium", "blocked"),
    "webhook_invalid_signature":     ("medium", "blocked"),
    "signup_success_limit_exceeded": ("low",    "blocked"),
    "upload_blocked_content":        ("medium", "blocked"),
    "account_deletion_requested":    ("info",   "allowed"),
    "login_failed":                  ("low",    "blocked"),
    "superadmin_login":              ("info",   "allowed"),
    "rate_limit_exceeded":           ("low",    "blocked"),
    "refresh_token_reuse":           ("high",   "blocked"),
    "revoked_token_used":            ("low",    "blocked"),
    "cross_tenant_blocked":          ("high",   "blocked"),
    "support_write_blocked":         ("medium", "blocked"),
    "prompt_injection_blocked":      ("low",    "blocked"),
    "ai_quota_reached":              ("info",   "blocked"),
    "data_export":                   ("info",   "allowed"),
    "admin_action":                  ("info",   "allowed"),
}
EVENT_TYPES = tuple(EVENT_RULES)
_SEVERITIES = ("info", "low", "medium", "high", "critical")
_OUTCOMES = ("blocked", "allowed", "flagged")

_RPC_RETRY_SECONDS = 600
_rpc_retry_at = 0.0         # set while the migration's function is known to be missing


def _function_missing(exc: Exception) -> bool:
    return str(getattr(exc, "code", "")) in ("PGRST202", "42883") or "Could not find the function" in str(exc)


def _record_with_function(params: dict) -> bool:
    """Write through record_security_event() (severity and repeat counting). False if not possible."""
    global _rpc_retry_at
    if time.monotonic() < _rpc_retry_at:
        return False
    try:
        supabase.rpc("record_security_event", params).execute()
        return True
    except Exception as exc:
        if _function_missing(exc):
            _rpc_retry_at = time.monotonic() + _RPC_RETRY_SECONDS
        else:
            log.debug("record_security_event failed (%s) — using a plain insert", type(exc).__name__)
        return False


def log_security_event(
    *,
    event_type: str,
    ip: Optional[str] = None,
    username: Optional[str] = None,
    business_id: Optional[int] = None,
    metadata: Optional[dict] = None,
    severity: Optional[str] = None,
    outcome: Optional[str] = None,
    rule: Optional[str] = None,
    endpoint: Optional[str] = None,
    user_agent: Optional[str] = None,
    dedup_key: Optional[str] = None,
    occurrences: int = 1,
) -> None:
    """Best-effort write of one security event, or of `occurrences` repeats of it. Never raises."""
    try:
        default_severity, default_outcome = EVENT_RULES.get(event_type, ("info", None))
        severity = severity if severity in _SEVERITIES else default_severity
        outcome = outcome if outcome in _OUTCOMES else default_outcome
        meta = redact(metadata or {})
        if _record_with_function({
            "p_event_type": event_type, "p_dedup_key": dedup_key, "p_severity": severity,
            "p_outcome": outcome, "p_rule": rule, "p_endpoint": endpoint, "p_ip": ip,
            "p_user_agent": user_agent, "p_username": username, "p_business_id": business_id,
            "p_metadata": meta, "p_count": occurrences,
        }):
            return
        extra = {k: v for k, v in (("endpoint", endpoint), ("rule", rule)) if v}
        if occurrences > 1:
            extra["occurrences"] = occurrences
        supabase.table("security_events").insert({
            "event_type":  event_type,
            "ip":          ip,
            "username":    username,
            "business_id": business_id,
            "metadata":    {**meta, **extra} if extra else meta,
        }).execute()
    except Exception as exc:
        record_dropped("security_events", event_type, exc)


def occurrences_of(row: dict) -> int:
    """How many occurrences one stored row stands for (either write path)."""
    meta = row.get("metadata")
    counts = [row.get("occurrence_count"), meta.get("occurrences") if isinstance(meta, dict) else None]
    return max([c for c in counts if isinstance(c, int) and c > 0], default=1)


def prune_security_events(days: int) -> Optional[int]:
    """Remove events older than `days` (the database enforces at least 30 and keeps events linked
    to an incident). Returns the number removed, or None if the migration is not applied."""
    try:
        return supabase.rpc("prune_security_events", {"p_days": days}).execute().data
    except Exception as exc:
        if not _function_missing(exc):
            log.warning("security_events: retention run failed (%s)", type(exc).__name__)
        return None


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


def list_all_security_events_since(since_iso: str, page: int = 1000, max_rows: int = 20000,
                                   raise_errors: bool = False) -> tuple:
    """(rows, truncated): every event since `since_iso`, newest first, read in pages because the
    database API returns at most 1000 rows per request by default. On error: ([], False), or the
    exception when `raise_errors` (so a failure is not mistaken for "nothing happened")."""
    rows: list = []
    try:
        while len(rows) < max_rows:
            batch = (
                supabase.table("security_events").select("*")
                .gte("created_at", since_iso)
                .order("created_at", desc=True).order("id", desc=True)
                .range(len(rows), len(rows) + page - 1)
                .execute()
            ).data or []
            rows.extend(batch)
            if len(batch) < page:
                return rows, False
        return rows, True
    except Exception as exc:
        if raise_errors:
            raise
        log.warning("list_all_security_events_since failed (%s)", type(exc).__name__)
        return rows, bool(rows)


def _ilike_text(value: str) -> str:
    """The value as literal text inside an ilike pattern ("*" cannot be escaped, so it is removed)."""
    return value.replace("*", "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def query_security_events(
    *, since_iso: Optional[str] = None, before_iso: Optional[str] = None, severity: Optional[str] = None,
    event_type: Optional[str] = None, business_id: Optional[int] = None, username: Optional[str] = None,
    endpoint: Optional[str] = None, ip: Optional[str] = None, incident_id: Optional[int] = None,
    search: Optional[str] = None, offset: int = 0, limit: int = 25,
) -> tuple:
    """(rows, has_more, problem) for one filtered page, newest first. `problem` is None on success.
    `search` must already be restricted to harmless characters (see the route)."""
    try:
        q = supabase.table("security_events").select("*")
        if since_iso:
            q = q.gte("created_at", since_iso)
        if before_iso:
            q = q.lt("created_at", before_iso)
        for column, value in (("severity", severity), ("event_type", event_type), ("ip", ip),
                              ("business_id", business_id), ("incident_id", incident_id)):
            if value is not None and value != "":
                q = q.eq(column, value)
        for column, value in (("username", username), ("endpoint", endpoint)):
            if value:
                q = q.ilike(column, f"%{_ilike_text(value)}%")
        if search:
            # Only columns that exist both before and after the database changes.
            q = q.or_(",".join(f'{column}.ilike."*{search}*"' for column in ("event_type", "username", "ip")))
        rows = (q.order("created_at", desc=True).order("id", desc=True)
                .range(offset, offset + limit).execute()).data or []
        return rows[:limit], len(rows) > limit, None
    except Exception as exc:
        log.warning("query_security_events failed (%s)", type(exc).__name__)
        missing = str(getattr(exc, "code", "")) in ("42703", "42P01", "PGRST204", "PGRST205")
        return [], False, ("This filter needs a database change that has not been applied." if missing
                           else "Security events could not be read.")


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
