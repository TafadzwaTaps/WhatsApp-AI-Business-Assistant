"""
crud/stripe_webhook_events.py — Stripe webhook idempotency.

Requires the `stripe_webhook_events` table (schema in
stripe_webhook_events_migration.sql — run it once in the Supabase SQL
editor, same optional-table pattern as every other table added this way
in this project).

Fails OPEN on any DB error (including the table not existing yet, if the
migration hasn't been run): billing/stripe_service.py treats a failure
here as "not yet processed" and goes ahead with the webhook, which is
exactly today's behavior before this table existed. That means duplicate-
delivery protection is only actually enforced once the migration has run
— but a missing optional table must never turn into Stripe webhooks being
rejected outright, which would be a far worse outcome than an occasional
duplicate.
"""

from __future__ import annotations

import logging

from core.db import supabase

log = logging.getLogger(__name__)


def already_processed(event_id: str) -> bool:
    """True only if we can positively confirm this event_id was already
    recorded. Any error (including a missing table) returns False, i.e.
    "treat as not yet processed" — see module docstring."""
    try:
        res = (
            supabase.table("stripe_webhook_events")
            .select("event_id")
            .eq("event_id", event_id)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception as exc:
        log.debug("stripe_webhook_events lookup failed (%s) — treating as not processed", exc)
        return False


def mark_processed(event_id: str, event_type: str) -> None:
    """Best-effort record of a processed event. Never raises — see module
    docstring; a failure here just means the next retry of the same event
    won't be caught either, same as before this table existed."""
    try:
        supabase.table("stripe_webhook_events").insert({
            "event_id": event_id,
            "event_type": event_type,
        }).execute()
    except Exception as exc:
        log.debug("stripe_webhook_events insert failed for event_id=%s (non-fatal): %s", event_id, exc)
