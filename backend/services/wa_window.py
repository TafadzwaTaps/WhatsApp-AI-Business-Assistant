"""
services/wa_window.py: WhatsApp 24-hour customer-service window (Phase 6.4).

Meta rule: a business may send FREE-FORM messages only within 24 hours of the customer's last INBOUND
message. Outside that window only an approved message template may be sent. WaziBot has no template
sender yet (a separate, approved piece of work), so a business-initiated free-form send to a customer
whose window is positively closed can only fail with error 131047, and each failure costs a call and
can hurt the number's quality rating.

This module answers one question for a batch of recipients: whose window is positively CLOSED?
  * closed  = we have this customer's last inbound message and it is more than 24 hours old.
  * open or unknown = everyone else. UNKNOWN (no inbound row found, a lookup failure, a customer we
    cannot match) is deliberately NOT treated as closed: the send is attempted and Meta decides, so
    this guard can only remove sends that are certain to fail; it can never block a send that would
    have worked. It is a guard, not a workaround: nothing here tries to reopen a window.
Inbound replies (webhook AI replies, staff replies in the inbox) never go through this guard.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger("wazibot")

WINDOW = timedelta(hours=24)
_CHUNK = 100
_PHONE_OK = re.compile(r"^\+?[0-9]{6,16}$")   # only plain numbers reach the query filter


def _parse(value):
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def last_inbound_map(business_id: int, phones) -> dict:
    """{phone: datetime of the customer's last inbound message}. Phones with no known inbound message
    are simply absent. Any failure returns what was gathered so far (never raises)."""
    out: dict = {}
    phones = [p for p in dict.fromkeys(str(p).strip() for p in (phones or []) if p) if _PHONE_OK.match(p)]
    # (anything else cannot be matched safely, so it is simply "unknown": the send is attempted)
    if not phones:
        return out
    try:
        from core.db import supabase
        for i in range(0, len(phones), _CHUNK):
            batch = phones[i:i + _CHUNK]
            cust = (supabase.table("customers").select("id,phone")
                    .eq("business_id", business_id).in_("phone", batch).execute().data) or []
            by_id = {c["id"]: str(c.get("phone") or "").strip() for c in cust if c.get("id") is not None}
            if not by_id:
                continue
            msgs = (supabase.table("messages").select("customer_id,created_at")
                    .eq("business_id", business_id).eq("direction", "incoming")
                    .in_("customer_id", list(by_id)).order("created_at", desc=True)
                    .limit(max(1000, len(by_id) * 20)).execute().data) or []
            for m in msgs:
                phone = by_id.get(m.get("customer_id"))
                if phone and phone not in out:
                    ts = _parse(m.get("created_at"))
                    if ts:
                        out[phone] = ts
    except Exception as exc:
        log.warning("wa_window: lookup failed (treated as unknown): %s", type(exc).__name__)
    return out


def closed_phones(business_id: int, phones, now: datetime | None = None) -> set:
    """The subset of `phones` whose 24-hour window is positively closed."""
    now = now or datetime.now(timezone.utc)
    seen = last_inbound_map(business_id, phones)
    return {p for p, ts in seen.items() if now - ts > WINDOW}


def split(business_id: int, phones, now: datetime | None = None):
    """(sendable, closed) lists, order preserved. Sendable = open or unknown."""
    phones = list(phones or [])
    closed = closed_phones(business_id, phones, now)
    return [p for p in phones if str(p).strip() not in closed], [p for p in phones if str(p).strip() in closed]


WINDOW_NOTE = ("These customers last wrote to you more than 24 hours ago. WhatsApp only allows a free-text "
               "message within 24 hours of the customer's last message; an approved message template is "
               "required for anyone older, so they were skipped instead of failing.")
