"""
services/data_export.py — SuperAdmin 2.0 (Phase 12): controlled data
export.

PURPOSE
───────
A single, explicit ALLOWLIST of exportable datasets for SuperAdmin use —
not a generic "export any table" tool. Each dataset names exactly which
columns leave the system; nothing is exported by default just because a
table exists. Two rules this module exists to enforce:

  1. NEVER export secrets. businesses.owner_password (hashed, but still
     never leaves this system) and businesses.whatsapp_token (Fernet-
     encrypted at rest, but the ciphertext itself is still a credential)
     are excluded from the "businesses" dataset's field list — not
     filtered out after the fact, but never selected from the DB in the
     first place.
  2. Every export is bounded (EXPORT_ROW_LIMIT) and meant to be
     audit-logged by the caller (routes/saas_admin_routes.py does this) —
     this is a SuperAdmin reporting/backup tool, not a raw DB dump
     endpoint. A SuperAdmin can already see all of this data through the
     existing dashboard endpoints; this just lets them take a bounded,
     traceable copy of it.

Adding a new exportable dataset means adding one explicit entry to
EXPORT_DATASETS below — reviewing exactly which fields it exposes — never
a blanket "select *".
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)

EXPORT_ROW_LIMIT = 5000  # hard cap per export — a reporting tool, not a bulk-data pipe


def _fetch_businesses(limit: int) -> list[dict]:
    from core.db import supabase
    # Explicit column allowlist — see module docstring. Never
    # owner_password or whatsapp_token.
    fields = (
        "id, name, owner_username, owner_email, contact_phone, category, "
        "currency, subscription_tier, billing_status, is_active, created_at"
    )
    res = supabase.table("businesses").select(fields).limit(limit).execute()
    return res.data or []


def _fetch_audit_logs(limit: int) -> list[dict]:
    from crud.admin_audit import list_audit_logs
    return list_audit_logs(limit=limit)


def _fetch_risk_flags(limit: int) -> list[dict]:
    from crud.admin_audit import list_risk_flags
    return list_risk_flags(limit=limit)


def _fetch_subscription_events(limit: int) -> list[dict]:
    from crud.subscription_history import list_subscription_events
    return list_subscription_events(limit=limit)


def _fetch_campaign_log(limit: int) -> list[dict]:
    from crud.campaign_log import list_campaign_sends
    return list_campaign_sends(limit=limit)


def _fetch_security_events(limit: int) -> list[dict]:
    from crud.security_events import list_security_events
    return list_security_events(limit=limit)


# name -> (human description, fetch(limit) -> rows, explicit field order for CSV)
EXPORT_DATASETS: dict[str, dict] = {
    "businesses": {
        "description": "Tenant roster — safe fields only, never owner_password or whatsapp_token.",
        "fetch": _fetch_businesses,
        "fields": ["id", "name", "owner_username", "owner_email", "contact_phone", "category",
                   "currency", "subscription_tier", "billing_status", "is_active", "created_at"],
    },
    "audit_logs": {
        "description": "SuperAdmin admin-action audit log.",
        "fetch": _fetch_audit_logs,
        "fields": ["id", "created_at", "actor_username", "actor_role", "action", "target_type",
                   "target_id", "business_id", "reason", "metadata", "ip_address"],
    },
    "risk_flags": {
        "description": "Abuse-scan risk flags (open, cleared, and actioned).",
        "fetch": _fetch_risk_flags,
        "fields": ["id", "created_at", "business_id", "risk_level", "reason", "evidence",
                   "status", "resolved_at", "reviewed_by", "action_taken"],
    },
    "subscription_events": {
        "description": "Subscription tier/status transition log (Phase 9).",
        "fetch": _fetch_subscription_events,
        "fields": ["id", "created_at", "business_id", "previous_tier", "new_tier",
                   "previous_status", "new_status", "source"],
    },
    "campaign_log": {
        "description": "Real (non-dry-run) campaign/broadcast sends (Phase 10).",
        "fetch": _fetch_campaign_log,
        "fields": ["id", "created_at", "business_id", "audience", "recipient_count", "message_length"],
    },
    "security_events": {
        "description": "Persisted security-event history (Phase 11).",
        "fetch": _fetch_security_events,
        "fields": ["id", "created_at", "event_type", "ip", "username", "business_id", "metadata"],
    },
}


def list_datasets() -> list[dict]:
    return [{"name": name, "description": d["description"], "fields": d["fields"]}
            for name, d in EXPORT_DATASETS.items()]


def fetch_dataset(name: str, limit: int = EXPORT_ROW_LIMIT) -> list[dict]:
    """
    Returns up to `limit` rows (capped at EXPORT_ROW_LIMIT regardless of
    what's requested). Raises KeyError for an unknown dataset name — the
    caller is expected to have validated against EXPORT_DATASETS first.
    Each underlying fetch is already fail-soft (crud modules return []
    on error) so this never raises for a DB/table problem, only for a
    genuinely unknown dataset name.
    """
    if name not in EXPORT_DATASETS:
        raise KeyError(name)
    capped = max(1, min(limit, EXPORT_ROW_LIMIT))
    return EXPORT_DATASETS[name]["fetch"](capped)


def to_csv(rows: list[dict], fields: list[str]) -> str:
    """Serialize rows to CSV using the dataset's fixed field order. A dict/
    list value (e.g. metadata, evidence) is serialized as compact JSON in
    its cell rather than Python's str() repr, so the CSV stays valid JSON
    inside a cell rather than single-quoted Python syntax."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        flat = dict(row)
        for k, v in flat.items():
            if isinstance(v, (dict, list)):
                flat[k] = json.dumps(v)
        writer.writerow(flat)
    return buf.getvalue()
