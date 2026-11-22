"""
services/wa_outbound.py: one place that decides HOW a business-initiated WhatsApp message may go out.

For MARKETING (campaigns, broadcasts, cart recovery, re-engagement), in this order:
  1. marketing consent  (services/wa_consent.py)  : not opted in / opted out  -> not sent, counted separately;
  2. the 24-hour window (services/wa_window.py)   : open or unknown           -> free text as before;
                                                    positively closed         -> an approved MARKETING template
                                                    (services/wa_templates.py) if one is registered, else skipped.
For UTILITY / transactional messages (order, booking and payment updates) there is no marketing-consent check:
  window open or unknown -> free text as before; positively closed -> an approved UTILITY template if one is
  registered, else skipped.
Free-form text is never sent outside the window, and nothing is sent unless it is allowed by every rule above.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

CONSENT_NOTE = ("These customers have not opted in to promotional WhatsApp messages (or have opted out), so they "
                "were skipped. Customers opt in by replying SUBSCRIBE or through a consent you record for them "
                "in the dashboard; they can reply STOP at any time.")


def plan_marketing(business_id: int, phones) -> dict:
    """Classify recipients for a MARKETING send. Sets of raw (stripped) phone strings."""
    from services import wa_consent, wa_window
    raw = [str(p).strip() for p in (phones or []) if p and str(p).strip()]
    c = wa_consent.split_marketing(business_id, raw)
    closed = wa_window.closed_phones(business_id, c["allowed"])
    return {"allowed": set(c["allowed"]), "closed": closed, "no_consent": set(c["no_consent"]),
            "opted_out": set(c["opted_out"])}


def run_reference(text: str) -> str:
    """Idempotency reference for a campaign-like send: same text on the same UTC day is the same send."""
    h = hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:10]
    return f"{h}:{datetime.now(timezone.utc).strftime('%Y%m%d')}"


def template_for_closed_window(business, business_id: int, phone: str, purpose: str, context: dict, ref: str,
                               sent_in_run: int = 0, sender=None, strict_ledger: bool = False) -> dict:
    """The recipient's window is closed: send the approved template for `purpose` if there is one.
    Returns send_template's result; {'ok': False, 'skipped': 'no_template'} means "nothing could be sent"."""
    from services import wa_templates
    return wa_templates.send_template(business_id, phone, purpose, context, ref=ref, business=business,
                                      sent_in_run=sent_in_run, sender=sender, strict_ledger=strict_ledger)
