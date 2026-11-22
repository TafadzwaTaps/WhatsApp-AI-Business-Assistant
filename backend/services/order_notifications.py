"""
services/order_notifications.py: tell the customer when their order's status really changes.

One shared notifier for every path that moves an order (dashboard status change, the lifecycle push, ...).

Rules
  * Meaningful changes only: confirmed, preparing, ready, out for delivery, delivered/collected, cancelled. Pickup and
    delivery get different wording, and a step that does not exist for that kind of order (out for delivery on a
    pickup order) is not announced.
  * Once per order AND status: the claim is recorded in the existing notification ledger, so a repeated click, a retry
    or a second path never sends the same update twice.
  * WhatsApp rules are respected: inside the 24-hour window the update is sent as normal text; outside it ONLY an
    approved "order_update" utility template may be used. With no approved template nothing is sent (never free text
    outside the window), the claim is released, and the skip is recorded.
  * Never raises and never blocks the status change. Tenant scope comes from the order row, never from the caller.
  * Nothing is promised that cannot be delivered: `updates_promise_line` gives honest wording for the places that
    used to say "we'll notify you when it's ready".

Records: every skip/failure is written to the ledger as kind "order_status_skipped" (once per reason) and logged with
the order id, status and a short reason code only (no message text, no phone numbers, no tokens).
"""
from __future__ import annotations

import logging
import os

from services.money import order_money

log = logging.getLogger("wazibot")

NOTIFIABLE = ("confirmed", "preparing", "ready", "out_for_delivery", "delivered", "completed", "cancelled")


def _ref(order: dict, business: dict | None) -> str:
    return f"{'BOOKING' if (business or {}).get('is_service_business') else 'ORDER'}-{order.get('id')}"


def _kind(order: dict) -> str:
    fm = str(order.get("fulfillment_method") or "").strip().lower()
    return fm if fm in ("pickup", "delivery") else "unknown"


def status_text(order: dict, new_status: str, business: dict | None = None, note: str | None = None) -> str | None:
    """The message for this status, or None when the step does not apply to this kind of order."""
    s = str(new_status or "").strip().lower()
    if s not in NOTIFIABLE:
        return None
    kind = _kind(order)
    ref = _ref(order, business)
    biz_name = (business or {}).get("name") or "us"
    addr = str((business or {}).get("address") or "").strip()

    if s == "confirmed":
        return f"✅ *{ref} is confirmed.*\nThank you for ordering from *{biz_name}*!"
    if s == "preparing":
        return f"👨‍🍳 *{ref} is being prepared.*"
    if s == "ready":
        if kind == "pickup":
            return f"🎉 *{ref} is ready for pickup!*" + (f"\n📍 {addr}" if addr else "")
        if kind == "delivery":
            return f"📦 *{ref} is ready* and will go out for delivery shortly."
        return f"🎉 *{ref} is ready!*"
    if s == "out_for_delivery":
        if kind == "pickup":
            return None
        return f"🛵 *{ref} is on its way.*\nPlease be available to receive it."
    if s == "delivered":
        if kind == "pickup":
            return None                                      # a pickup is finished by "completed" (collected)
        return f"📦 *{ref} was delivered.*\nThank you for ordering from *{biz_name}*! 🙏"
    if s == "completed":
        if kind == "pickup":
            return f"✅ *{ref} was collected.*\nThank you for ordering from *{biz_name}*! 🙏"
        if kind == "delivery":
            return None                                      # "delivered" already closed it
        return f"✅ *{ref} is complete.*\nThank you for ordering from *{biz_name}*! 🙏"
    # cancelled
    paid = str(order.get("payment_status") or "").lower() in ("paid", "confirmed")
    nxt = ("If you already paid, we'll contact you about your refund." if paid
           else "No payment is due for this order.")
    extra = f"\n_{note}_" if note else ""
    return f"❌ *{ref} was cancelled.*{extra}\n{nxt}\nType *menu* to place a new order, or message us if this is a mistake."


def notifications_enabled(business: dict | None) -> bool:
    """Automatic status messages are OFF by default. A business turns them on explicitly with
    features_json.order_status_notifications = true (rollout step, after staging verification)."""
    fj = (business or {}).get("features_json")
    return isinstance(fj, dict) and fj.get("order_status_notifications") is True


def updates_promised(business: dict | None) -> bool:
    """True when this business can really message customers about order progress (updates switched on, and it
    has WhatsApp credentials or the shared number)."""
    business = business or {}
    if not notifications_enabled(business):
        return False
    if business.get("whatsapp_phone_id"):
        return True
    return bool(os.getenv("SHARED_PHONE_NUMBER_ID", "").strip() and os.getenv("SHARED_WA_TOKEN", "").strip())


def updates_promise_line(business: dict | None, ready_word: str = "ready") -> str:
    """Honest wording for 'we will tell you': a promise only when updates can be sent."""
    if updates_promised(business):
        return (f"We'll message you here when your order is {ready_word}. "
                f"You can also ask *where is my order* any time.")
    return "Ask *where is my order* any time to check its progress."


def _record_skip(business_id, order_id, status, reason) -> None:
    try:
        from crud import notification_ledger as ledger
        ledger.claim(business_id, "order_status_skipped", f"{order_id}:{status}:{reason}", table_only=True)
    except Exception as exc:
        log.debug("order_notifications: skip not recorded: %s", type(exc).__name__)
    log.info("order status notification not sent  order=%s status=%s reason=%s", order_id, status, reason)


def _outcome(outcome: str, reason: str = "") -> dict:
    # `notified` = WhatsApp's API ACCEPTED the message. It does not mean delivered or read (those arrive later as
    # status webhooks and are not tracked here). skipped_* = nothing was attempted; failed = attempted, not accepted.
    accepted = outcome in ("sent", "sent_template")
    stage = "accepted_by_whatsapp" if accepted else ("failed" if outcome == "failed" else
                                                      "duplicate" if outcome == "duplicate" else "not_attempted")
    return {"outcome": outcome, "reason": reason, "notified": accepted, "stage": stage}


def notify_order_status(order: dict, new_status: str, *, business: dict | None = None, source: str = "dashboard",
                        note: str | None = None, text_override: str | None = None, sender=None) -> dict:
    """Send the customer an update for `new_status`, once. Returns {'outcome', 'reason', 'notified'}. Never raises.

    outcome: sent | sent_template | duplicate | skipped_disabled | skipped_not_notifiable | skipped_no_phone |
             skipped_no_credentials | skipped_window_no_template | failed
    sent / sent_template mean WhatsApp ACCEPTED the message (stage "accepted_by_whatsapp"), not that it was delivered.
    `text_override` (the lifecycle push's owner-written message) replaces the generated text but goes through the same
    once-only / window / template rules. `sender(phone_id, token, phone, text)` is the test seam for free text."""
    try:
        order = order or {}
        status = str(new_status or "").strip().lower()
        order_id, biz_id = order.get("id"), order.get("business_id")
        if status not in NOTIFIABLE or not order_id or not biz_id:
            return _outcome("skipped_not_notifiable", "status")
        if business is None:
            try:
                import crud
                business = crud.get_business_by_id(biz_id)
            except Exception as exc:
                log.debug("order_notifications: business lookup failed: %s", type(exc).__name__)
        if not notifications_enabled(business):               # default OFF: explicit per-business enablement
            return _outcome("skipped_disabled", "disabled")
        text = text_override or status_text(order, status, business, note)
        if not text:
            return _outcome("skipped_not_notifiable", "not_applicable")
        phone = str(order.get("customer_phone") or "").strip()
        if not phone:
            _record_skip(biz_id, order_id, status, "no_phone")
            return _outcome("skipped_no_phone", "no_phone")

        # once per order + status (table_only: never touches the business settings row)
        from crud import notification_ledger as ledger
        key = f"{order_id}:{status}"
        try:
            if not ledger.claim(biz_id, "order_status", key, table_only=True, strict=True):
                return _outcome("duplicate", "already_sent")
        except ledger.LedgerUnavailable:                      # table not created yet: send nothing, never risk repeats
            log.warning("order_notifications: notification_log table missing, nothing sent order=%s", order_id)
            return _outcome("failed", "ledger_unavailable")
        except Exception as exc:                              # fail closed: a customer must not get repeats
            log.warning("order_notifications: claim failed order=%s: %s", order_id, type(exc).__name__)
            return _outcome("failed", "claim_failed")

        def _give_back(reason: str, outcome: str) -> dict:
            try:
                ledger.release(biz_id, "order_status", key)
            except Exception as exc:
                log.debug("order_notifications: release failed: %s", type(exc).__name__)
            _record_skip(biz_id, order_id, status, reason)
            return _outcome(outcome, reason)

        from services import wa_templates
        phone_id, token = wa_templates.resolve_credentials(business)
        if not phone_id or not token:
            return _give_back("no_credentials", "skipped_no_credentials")

        window_closed = False
        try:
            from services.wa_window import closed_phones
            window_closed = phone in closed_phones(biz_id, [phone])
        except Exception as exc:                              # unknown = attempt; Meta's 131047 is handled below
            log.debug("order_notifications: window check skipped: %s", type(exc).__name__)

        def _template() -> dict:
            from services.wa_outbound import template_for_closed_window
            ctx = {"business_name": (business or {}).get("name") or "", "order_id": _ref(order, business),
                   "status": status.replace("_", " "),
                   "amount": order_money(order, business)}
            return template_for_closed_window(business, biz_id, phone, "order_update", ctx,
                                              ref=f"order:{order_id}:{status}", strict_ledger=True)

        if window_closed:
            tr = _template()
            if tr.get("ok"):
                return _outcome("sent_template")
            reason = tr.get("skipped") or tr.get("reason") or "template_failed"
            return _give_back(f"window_closed_{reason}", "skipped_window_no_template" if tr.get("skipped") else "failed")

        if sender is None:
            try:
                from main import send_whatsapp as sender       # lazy: main imports the routes that import this module
            except Exception as exc:
                log.warning("order_notifications: sender unavailable: %s", type(exc).__name__)
                return _give_back("sender_unavailable", "failed")
        try:
            res = sender(phone_id, token, phone, text) or {}
        except Exception as exc:
            log.warning("order_notifications: send raised order=%s: %s", order_id, type(exc).__name__)
            return _give_back("send_exception", "failed")
        if isinstance(res, dict) and "error" in res:
            code = None
            try:
                err = res["error"]
                code = (err.get("code") if isinstance(err, dict) else None) or res.get("code")
            except Exception:
                code = None
            if str(code) == "131047" or "131047" in str(res.get("error")):
                tr = _template()                                # window had actually closed: only a template may be used
                if tr.get("ok"):
                    return _outcome("sent_template")
                return _give_back(f"window_closed_{tr.get('skipped') or tr.get('reason') or 'template_failed'}",
                                  "skipped_window_no_template" if tr.get("skipped") else "failed")
            return _give_back("send_error", "failed")
        log.info("order status notification sent  order=%s status=%s source=%s", order_id, status, source)
        return _outcome("sent")
    except Exception as exc:                                    # the status change must never fail because of this
        log.warning("order_notifications: unexpected error: %s", type(exc).__name__)
        return _outcome("failed", "unexpected")
