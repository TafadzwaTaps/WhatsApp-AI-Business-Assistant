"""services/security_center.py — read-only figures and explanations for the Super Admin
Security Center (security audit Stage 4b).

Everything shown is counted from stored rows. When something cannot be read (database change not
applied, table missing, database down) the value is None and says so: never a made-up zero. There
is deliberately no overall "security score".
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

from core.safelog import redact

log = logging.getLogger(__name__)

# event type -> (name, why it was recorded, what the application did, what to check)
_CATALOG = {
    "ip_login_flagged": ("IP flagged for failed sign-ins", "One address failed to sign in too many times.",
                         "Sign-ins from it were refused for a while.",
                         "Check whether the address also reached any account successfully."),
    "account_lockout": ("Account locked", "Too many failed sign-ins against one account name.",
                        "Sign-ins for that name were refused for a while.",
                        "Confirm with the owner if it repeats."),
    "webhook_invalid_signature": ("Bad webhook signature", "A payment or WhatsApp notification failed verification.",
                                  "The request was rejected.",
                                  "Repeats can mean forgery, or a wrong webhook secret."),
    "signup_success_limit_exceeded": ("Signup limit reached", "One address tried to create more accounts than allowed.",
                                      "The signup was refused.", "Look for related accounts."),
    "upload_blocked_content": ("Upload blocked", "A file failed the content or format checks.",
                               "The file was not stored.", "Review the business if it repeats."),
    "account_deletion_requested": ("Account deletion requested", "An owner asked for their account to be deleted.",
                                   "The request was recorded.", "No action unless the owner disputes it."),
    "login_failed": ("Failed sign-in", "A wrong password or an unknown account name.", "The sign-in was refused.",
                     "Single failures are normal; look for bursts."),
    "superadmin_login": ("Super Admin sign-in", "The Super Admin signed in.", "The sign-in was allowed.",
                         "Confirm it was you."),
    "rate_limit_exceeded": ("Rate limit reached", "Too many requests in a short time.", "The request was refused.",
                            "Look for one address hitting many routes."),
    "refresh_token_reuse": ("Session token reused", "A sign-in token was used again after it had been replaced.",
                            "The token was revoked.",
                            "Investigate: a copy of the token may exist. Consider a password reset."),
    "revoked_token_used": ("Ended session used", "A signed-out session token was presented.", "The request was refused.",
                           "Usually an old browser tab; look for repeats from new addresses."),
    "cross_tenant_blocked": ("Cross-business access blocked", "An account asked for another business's data.",
                             "The request was refused.", "Investigate which account did it and why."),
    "support_write_blocked": ("Support session blocked", "A view-as session tried something that is not read-only.",
                              "The request was refused.", "Confirm the support user's intent."),
    "prompt_injection_blocked": ("Prompt injection blocked", "A customer message tried to manipulate the AI.",
                                 "A fixed refusal was sent.", "No action unless it repeats."),
    "ai_quota_reached": ("AI quota reached", "A business used up its daily AI allowance.",
                         "The AI call was skipped.", "Check for unusual volume."),
    "data_export": ("Data exported", "Data was downloaded.", "The export was allowed.",
                    "Confirm the export was expected."),
    "admin_action": ("Admin action", "A Super Admin action was performed.", "It was allowed.",
                     "See the audit trail."),
}
_UNKNOWN = ("Other event", "Recorded by the application.", "Not recorded.", "Review the details.")

_REVIEW_SEVERITIES = ("high", "critical")
_OVERVIEW_COUNTS = {
    "failed_logins": ("login_failed",),
    "lockouts_and_flagged_ips": ("account_lockout", "ip_login_flagged"),
    "rate_limited": ("rate_limit_exceeded",),
    "cross_tenant": ("cross_tenant_blocked",),
    "webhook_failures": ("webhook_invalid_signature",),
    "ai_blocks": ("prompt_injection_blocked", "ai_quota_reached"),
    "session_token_reuse": ("refresh_token_reuse",),
    "blocked_uploads": ("upload_blocked_content",),
    "exports": ("data_export",),
}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def describe(row: dict) -> dict:
    """One stored event as the dashboard shows it, with the reason it was recorded."""
    from crud.security_events import EVENT_RULES, occurrences_of
    event_type = row.get("event_type") or "unknown"
    name, why, action, follow_up = _CATALOG.get(event_type, _UNKNOWN)
    default_severity, default_outcome = EVENT_RULES.get(event_type, ("info", None))
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    # A row without an outcome was written by a plain insert, so its stored severity is only the
    # column default.
    classified = bool(row.get("outcome"))
    return {
        "id": row.get("id"),
        "first_seen": row.get("created_at"),
        "last_seen": row.get("last_seen_at") or row.get("created_at"),
        "event_type": event_type,
        "name": name,
        "severity": (row.get("severity") if classified else None) or default_severity,
        "outcome": row.get("outcome") or default_outcome,
        "occurrences": occurrences_of(row),
        "ip": row.get("ip"),
        "username": row.get("username"),
        "business_id": row.get("business_id"),
        "endpoint": row.get("endpoint") or meta.get("endpoint"),
        "rule": row.get("rule") or meta.get("rule"),
        "user_agent": row.get("user_agent"),
        "incident_id": row.get("incident_id"),
        "details": redact(meta),
        "why": why,
        "action_taken": row.get("action_taken") or action,
        "follow_up": follow_up,
    }


def _rows(table: str, columns: str) -> Optional[list]:
    """All rows (up to 1000) of one Security Center table, or None if it cannot be read."""
    try:
        from core.db import supabase
        return supabase.table(table).select(columns).limit(1000).execute().data or []
    except Exception as exc:
        log.warning("security_center: %s unreadable (%s)", table, type(exc).__name__)
        return None


def _open_incidents() -> Optional[dict]:
    rows = _rows("security_incidents", "severity,status")
    if rows is None:
        return None
    open_rows = [r for r in rows if r.get("status") not in ("resolved", "false_positive")]
    return {"total": len(open_rows),
            "critical": sum(r.get("severity") == "critical" for r in open_rows),
            "high": sum(r.get("severity") == "high" for r in open_rows)}


def _active_ip_restrictions() -> Optional[int]:
    rows = _rows("security_restrictions", "kind,expires_at,revoked_at")
    if rows is None:
        return None
    now = _now()

    def active(row) -> bool:
        if row.get("kind") != "ip" or row.get("revoked_at"):
            return False
        try:
            return _dt.datetime.fromisoformat(str(row.get("expires_at")).replace("Z", "+00:00")) > now
        except (ValueError, TypeError):
            return True                       # unreadable expiry: show it rather than hide it

    return sum(active(r) for r in rows)


def _open_vulnerabilities() -> Optional[int]:
    rows = _rows("security_vulnerabilities", "status")
    if rows is None:
        return None
    return sum(r.get("status") in ("open", "acknowledged", "investigating") for r in rows)


def _suspicious_accounts() -> Optional[dict]:
    try:
        from services.security import get_security_snapshot
        snapshot = get_security_snapshot()
        from crud.admin_audit import list_risk_flags
        return {"locked_accounts": len(snapshot.get("locked_accounts") or []),
                "flagged_ips": len(snapshot.get("flagged_login_ips") or []),
                "open_risk_flags": len(list_risk_flags(status="open"))}
    except Exception as exc:
        log.warning("security_center: account snapshot unavailable (%s)", type(exc).__name__)
        return None


def _event_figures(days: int) -> dict:
    """Counts over the window. `available` False means the events could not be read."""
    from crud.security_events import list_all_security_events_since
    since = (_now() - _dt.timedelta(days=days)).isoformat()
    try:
        rows, truncated = list_all_security_events_since(since, raise_errors=True)
    except Exception as exc:
        log.warning("security_center: events unreadable (%s)", type(exc).__name__)
        return {"available": False, "counts": None, "by_severity": None, "blocked": None,
                "truncated": False, "last_event_at": None, "classified": None, "to_review": []}
    events = [describe(r) for r in rows]
    counts = {name: sum(e["occurrences"] for e in events if e["event_type"] in types)
              for name, types in _OVERVIEW_COUNTS.items()}
    by_severity = {s: sum(e["occurrences"] for e in events if e["severity"] == s)
                   for s in ("critical", "high", "medium", "low", "info")}
    review: dict = {}
    for e in events:
        if e["severity"] in _REVIEW_SEVERITIES:
            item = review.setdefault(e["event_type"], {"event_type": e["event_type"], "name": e["name"],
                                                       "severity": e["severity"], "occurrences": 0})
            item["occurrences"] += e["occurrences"]
    return {
        "available": True,
        "counts": counts,
        "by_severity": by_severity,
        "blocked": sum(e["occurrences"] for e in events if e["outcome"] == "blocked"),
        "truncated": truncated,
        "last_event_at": events[0]["last_seen"] if events else None,
        # Whether the newest event carries a stored classification (database change applied).
        "classified": bool(rows[0].get("outcome")) if rows else None,
        "to_review": sorted(review.values(), key=lambda i: -i["occurrences"]),
    }


def overview(days: int) -> dict:
    from services import security_events as recorder
    thread = recorder._thread
    return {
        "window_days": days,
        "generated_at": _now().isoformat(),
        "events": _event_figures(days),
        "open_incidents": _open_incidents(),
        "active_ip_restrictions": _active_ip_restrictions(),
        "open_vulnerabilities": _open_vulnerabilities(),
        "accounts_now": _suspicious_accounts(),
        "monitoring": {
            "writer_running": bool(thread and thread.is_alive()),
            "events_waiting": sum(1 for e in list(recorder._pending.values()) if e["count"]),
            "retention_days": recorder.retention_days(),
            "last_security_scan": None,       # no scan results are recorded by the application yet
        },
        "catalog": [{"event_type": key, "name": value[0]} for key, value in _CATALOG.items()],
    }
