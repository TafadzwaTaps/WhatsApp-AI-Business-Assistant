"""
crud/stripe_webhook_events.py — durable Stripe webhook event ledger.

Requires the `stripe_webhook_events` table (backend/migrations/
billing_lifecycle_hardening.sql). One row per Stripe event id:

    event_id (PK) · event_type · status ('processing'|'processed'|'failed')
    attempts · last_error · created_at · updated_at · processed_at

Lifecycle used by billing/stripe_service.handle_stripe_webhook:
    already_processed()  → skip events that fully succeeded
    claim_event()        → atomically take the event (PK insert) or detect a
                           concurrent / completed delivery
    mark_processed()     → only after every required DB update succeeded
    mark_failed()        → keep the event retryable (Stripe will redeliver)

Failure policy: if the ledger table is missing or unreadable we FAIL OPEN
(process the event). The billing handlers are state-convergent (they re-read
Stripe and write the result), so an occasional duplicate is harmless; refusing
webhooks outright would be worse. Only the stored `error` text (type + short
message, never payloads or card data) is persisted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from core.db import supabase

log = logging.getLogger(__name__)

# A 'processing' claim older than this is considered abandoned (worker crashed).
_STALE_CLAIM = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def already_processed(event_id: str) -> bool:
    """True only if this event_id is positively recorded as fully processed."""
    try:
        res = (
            supabase.table("stripe_webhook_events")
            .select("event_id, status")
            .eq("event_id", event_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return False
        status = rows[0].get("status")
        return status in (None, "processed")   # None = row from the pre-status schema
    except Exception as exc:
        log.debug("stripe_webhook_events lookup failed (%s) — treating as not processed", exc)
        return False


def claim_event(event_id: str, event_type: str) -> str:
    """Returns 'process' | 'duplicate' | 'in_progress'."""
    try:
        supabase.table("stripe_webhook_events").insert({
            "event_id": event_id, "event_type": event_type,
            "status": "processing", "attempts": 1,
        }).execute()
        return "process"
    except Exception as insert_exc:
        try:
            res = (
                supabase.table("stripe_webhook_events")
                .select("status, attempts, updated_at")
                .eq("event_id", event_id)
                .limit(1)
                .execute()
            )
            rows = res.data or []
        except Exception as exc:
            log.warning("stripe_webhook_events unavailable (%s) — processing without dedup", type(exc).__name__)
            return "process"
        if not rows:
            # Insert failed for a reason other than "already exists" (e.g. table missing).
            log.warning("stripe_webhook_events insert failed (%s) — processing without dedup",
                        type(insert_exc).__name__)
            return "process"
        row = rows[0]
        status = row.get("status")
        if status in (None, "processed"):
            return "duplicate"
        fresh = True
        try:
            ts = datetime.fromisoformat(str(row.get("updated_at")).replace("Z", "+00:00"))
            fresh = (_now() - ts) < _STALE_CLAIM
        except Exception:
            fresh = False
        if status == "processing" and fresh:
            return "in_progress"
        # failed, or abandoned claim → take it over (conditional on the status we saw)
        try:
            supabase.table("stripe_webhook_events").update({
                "status": "processing", "attempts": int(row.get("attempts") or 1) + 1,
                "updated_at": _now().isoformat(),
            }).eq("event_id", event_id).eq("status", status).execute()
        except Exception as exc:
            log.debug("stripe_webhook_events takeover failed: %s", exc)
        return "process"


def mark_processed(event_id: str, event_type: str) -> None:
    """Record success. Never raises (a failure here only loses dedup for a retry)."""
    try:
        supabase.table("stripe_webhook_events").upsert({
            "event_id": event_id, "event_type": event_type, "status": "processed",
            "last_error": None, "updated_at": _now().isoformat(),
            "processed_at": _now().isoformat(),
        }).execute()
    except Exception as exc:
        log.debug("stripe_webhook_events mark_processed failed for event_id=%s (non-fatal): %s", event_id, exc)


def mark_failed(event_id: str, event_type: str, error: str) -> None:
    """Record a failed attempt so it stays retryable and visible to reconciliation."""
    try:
        supabase.table("stripe_webhook_events").upsert({
            "event_id": event_id, "event_type": event_type, "status": "failed",
            "last_error": (error or "")[:300], "updated_at": _now().isoformat(),
        }).execute()
    except Exception as exc:
        log.debug("stripe_webhook_events mark_failed failed for event_id=%s (non-fatal): %s", event_id, exc)


def list_failed_events(limit: int = 50) -> list:
    """Events that failed processing (for the admin reconciliation report)."""
    try:
        res = (
            supabase.table("stripe_webhook_events")
            .select("event_id, event_type, attempts, last_error, updated_at")
            .eq("status", "failed")
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.debug("list_failed_events failed (%s)", exc)
        return []
