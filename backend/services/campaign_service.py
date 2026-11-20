"""
services/campaign_service.py — Campaign & Broadcast Engine (Phase 4)

PURPOSE
───────
Adds audience targeting on top of the existing broadcast system.
Instead of "send to all", you can now send to:
  • inactive customers (win-back)
  • VIP / loyal customers (retention)
  • new customers (onboarding)
  • high spenders (upsell)
  • customers with unpaid orders (payment nudge)

Uses ONLY the existing send_whatsapp() sender and crud helpers.
Does NOT modify WhatsApp core logic.

USAGE
─────
From main.py endpoints:

    from services.campaign_service import CampaignService

    result = await CampaignService.run(
        business_id = 3,
        audience    = "inactive_30d",
        message     = "Hey! We miss you at Flavoury Foods. Come back for 10% off today!",
        dry_run     = False,
    )

AUDIENCES
─────────
  "all"              — every customer with any interaction
  "vip"              — VIP segment (≥10 orders or ≥$50 spent)
  "loyal"            — Loyal segment (≥5 orders or ≥$20 spent)
  "regular"          — Regular segment (2-4 orders)
  "new"              — New customers (exactly 1 order)
  "inactive_30d"     — Not seen in 30 days, ordered at least once
  "inactive_14d"     — Not seen in 14 days
  "inactive_7d"      — Not seen in 7 days
  "high_spenders"    — total_spent ≥ $30
  "unpaid"           — customers with stale payment orders
  "custom"           — pass phone_list explicitly
"""

from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# Minimum gap between two campaigns sent to the same phone (minutes)
# Prevents accidental double-sending when endpoint is called twice quickly.
CAMPAIGN_COOLDOWN_MINUTES = 30

# In-process dedup: {(business_id, phone): last_sent_unix}
_campaign_sent: dict[tuple, float] = {}


def _on_cooldown(business_id: int, phone: str) -> bool:
    key  = (business_id, phone)
    last = _campaign_sent.get(key)
    if last is None:
        return False
    return (time.time() - last) < (CAMPAIGN_COOLDOWN_MINUTES * 60)


def _mark_sent(business_id: int, phone: str) -> None:
    _campaign_sent[(business_id, phone)] = time.time()


# ─────────────────────────────────────────────────────────────────────────────
# AUDIENCE RESOLVER
# ─────────────────────────────────────────────────────────────────────────────

def resolve_audience(
    business_id: int,
    audience:    str,
    phone_list:  list[str] | None = None,
) -> list[dict]:
    """
    Return list of {phone, customer_name, ...} for the given audience.
    Each dict is a user_memory row.

    For "custom" audience, pass phone_list explicitly.
    """
    import crud

    if audience == "custom":
        if not phone_list:
            return []
        # Fetch memory rows for given phones. Numbers typed by the owner are put into the international
        # form using the business's default dialing code (never an assumed country); unparseable ones are
        # used exactly as given, as before.
        from services.phone_utils import business_dialing_code, normalise_phone
        try:
            _dial = business_dialing_code(crud.get_business_by_id(business_id))
        except Exception:
            _dial = None
        result = []
        for phone in phone_list:
            phone = normalise_phone(phone, _dial) or phone
            mem = crud.get_user_memory(phone, business_id) or {}
            mem["phone"] = phone
            result.append(mem)
        return result

    if audience == "all":
        return crud.get_customers_by_segment(business_id, "all")

    if audience in ("vip", "loyal", "regular", "new", "prospect"):
        return crud.get_customers_by_segment(business_id, audience)

    if audience.startswith("inactive_"):
        # e.g. "inactive_30d" → 30 days
        try:
            days = int(audience.replace("inactive_", "").replace("d", ""))
        except ValueError:
            days = 30
        return crud.get_inactive_customers(business_id, inactive_days=days)

    if audience == "high_spenders":
        rows = crud.get_customers_by_segment(business_id, "all")
        return [r for r in rows if float(r.get("total_spent") or 0) >= 30]

    if audience == "unpaid":
        orders = crud.get_stale_payment_orders(business_id, older_than_hours=0.5)
        seen   = set()
        result = []
        for o in orders:
            phone = o.get("customer_phone", "")
            if phone and phone not in seen:
                seen.add(phone)
                mem = crud.get_user_memory(phone, business_id) or {}
                mem["phone"] = phone
                result.append(mem)
        return result

    log.warning("campaign: unknown audience=%r", audience)
    return []


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE PERSONALISER
# ─────────────────────────────────────────────────────────────────────────────

def personalise(message: str, memory: dict, business_name: str = "", currency_symbol: str = "$") -> str:
    """
    Replace template variables in a campaign message.

    Supported variables:
      {name}     — customer_name or "there"
      {business} — business name
      {orders}   — order_count
      {spent}    — total_spent formatted

    Example:
      "Hi {name}, thank you for your {orders} orders at {business}!"
      → "Hi Tafadzwa, thank you for your 7 orders at Flavoury Foods!"
    """
    name   = (memory.get("customer_name") or "").strip() or "there"
    orders = int(memory.get("order_count") or 0)
    spent  = float(memory.get("total_spent") or 0)

    return (
        message
        .replace("{name}",     name)
        .replace("{business}", business_name)
        .replace("{orders}",   str(orders))
        .replace("{spent}",    f"{currency_symbol or '$'}{spent:.2f}")
    )


# ─────────────────────────────────────────────────────────────────────────────
# CAMPAIGN RUNNER
# ─────────────────────────────────────────────────────────────────────────────

class CampaignService:
    """
    Executes targeted WhatsApp campaigns against a resolved audience.
    Uses the existing send_whatsapp() from main.py — no new HTTP logic.
    """

    @staticmethod
    def run(
        business_id:  int,
        audience:     str,
        message:      str,
        *,
        phone_list:   list[str] | None = None,
        personalise_msg: bool = True,
        dry_run:      bool = False,
    ) -> dict:
        """
        Send a campaign message to a resolved audience.

        Parameters
        ──────────
        business_id      Tenant
        audience         Audience key (see module docstring)
        message          Message text — supports {name}, {business}, {orders}, {spent}
        phone_list       Explicit phones for "custom" audience
        personalise_msg  If True, replaces {name} etc. in the message per recipient
        dry_run          If True, resolve audience and preview but do NOT send

        Returns
        ───────
        {
          audience:   str,
          total:      int,   # audience size
          sent:       int,
          skipped:    int,   # cooldown / no phone
          failed:     int,
          dry_run:    bool,
          previews:   list   # first 3 personalised messages (always shown)
        }
        """
        import crud

        business = crud.get_business_by_id(business_id)
        if not business:
            return {"ok": False, "error": f"Business {business_id} not found"}

        biz_name  = business.get("name", "")
        recipients = resolve_audience(business_id, audience, phone_list)

        if not recipients:
            return {
                "ok": True, "audience": audience, "total": 0,
                "sent": 0, "skipped": 0, "failed": 0,
                "dry_run": dry_run,
                "message": f"No customers found for audience '{audience}'",
            }

        # Build previews (first 3 personalised messages)
        previews = []
        for r in recipients[:3]:
            phone = r.get("phone", "")
            msg   = personalise(message, r, biz_name, business.get("currency_symbol") or "$") if personalise_msg else message
            previews.append({"phone": phone, "message": msg})

        if dry_run:
            log.info(
                "campaign DRY RUN  biz=%s  audience=%s  recipients=%d",
                business_id, audience, len(recipients),
            )
            return {
                "ok": True, "audience": audience,
                "total": len(recipients),
                "sent": 0, "skipped": 0, "failed": 0,
                "dry_run": True, "previews": previews,
            }

        # Resolve WhatsApp credentials
        try:
            token = crud.get_decrypted_token(business)
        except Exception:
            token = ""

        phone_id = business.get("whatsapp_phone_id", "")

        # Fall back to shared number
        if not phone_id or not token:
            import os
            phone_id = os.getenv("SHARED_PHONE_NUMBER_ID", "").strip()
            token    = os.getenv("SHARED_WA_TOKEN", "").strip()

        if not phone_id or not token:
            return {"ok": False, "error": "No WhatsApp credentials configured"}

        # Send
        sent    = 0
        skipped = 0
        failed  = 0
        failed_numbers: list = []
        first_error = ""
        first_error_reason = ""

        try:
            from main import send_whatsapp as _send
        except ImportError:
            return {"ok": False, "error": "Cannot import send_whatsapp from main"}

        # A campaign is MARKETING. In order: (1) marketing consent: customers who have not opted in (or have
        # opted out) are not sent anything; (2) WhatsApp's 24-hour window: customers who last wrote more than
        # 24 hours ago cannot be sent free text, only an approved marketing template, and are skipped when none
        # is registered. Inactive pieces (migration not applied, no template) leave today's behaviour unchanged.
        from services.wa_window import WINDOW_NOTE
        from services.wa_outbound import plan_marketing, run_reference, template_for_closed_window, CONSENT_NOTE
        _plan = plan_marketing(business_id, [(r.get("phone") or "").strip() for r in recipients])
        _ref = run_reference(message)
        skipped_window = skipped_no_consent = skipped_opted_out = skipped_cap = sent_template = 0

        for recipient in recipients:
            phone = (recipient.get("phone") or "").strip()
            if not phone:
                skipped += 1
                continue

            if phone in _plan["opted_out"]:
                skipped_opted_out += 1
                continue
            if phone in _plan["no_consent"]:
                skipped_no_consent += 1
                continue

            if _on_cooldown(business_id, phone):
                log.debug("campaign: cooldown  phone=%s", phone)
                skipped += 1
                continue

            if phone in _plan["closed"]:
                tr = template_for_closed_window(
                    business, business_id, phone, "campaign",
                    {"customer_name": (recipient.get("name") or "").split(" ")[0], "business_name": biz_name},
                    _ref, sent_in_run=sent_template)
                if tr.get("ok"):
                    _mark_sent(business_id, phone)
                    sent += 1
                    sent_template += 1
                elif tr.get("skipped") == "no_template":
                    skipped_window += 1
                elif tr.get("skipped") == "cap":
                    skipped_cap += 1
                elif tr.get("skipped") == "duplicate":
                    skipped += 1
                elif tr.get("skipped") in ("no_consent", "opted_out"):
                    skipped_no_consent += 1
                else:
                    failed += 1
                    failed_numbers.append(phone)
                    if not first_error:
                        first_error = _friendly_send_error(RuntimeError(tr.get("error") or {}))
                        first_error_reason = tr.get("reason") or "meta"
                continue

            msg = personalise(message, recipient, biz_name, business.get("currency_symbol") or "$") if personalise_msg else message

            try:
                result = _send(phone_id, token, phone, msg)
                if "error" in result:
                    raise RuntimeError(result["error"])
                _mark_sent(business_id, phone)
                sent += 1
            except Exception as exc:
                log.warning("campaign: send failed  phone=%s  exc=%s", phone, exc)
                failed += 1
                failed_numbers.append(phone)
                if not first_error:
                    first_error = _friendly_send_error(exc)
                    first_error_reason = _error_reason(exc)

        if not sent and not first_error:
            if skipped_window:
                first_error = WINDOW_NOTE
            elif skipped_no_consent or skipped_opted_out:
                first_error = CONSENT_NOTE
        log.info(
            "campaign done  biz=%s  audience=%s  total=%d  sent=%d  skipped=%d  skipped_window=%d  "
            "skipped_no_consent=%d  skipped_opted_out=%d  sent_template=%d  failed=%d",
            business_id, audience, len(recipients), sent, skipped, skipped_window,
            skipped_no_consent, skipped_opted_out, sent_template, failed,
        )
        return {
            "ok":       True,
            "audience": audience,
            "total":    len(recipients),
            "sent":     sent,
            "skipped":  skipped,
            "failed":   failed,
            "skipped_window": skipped_window,
            "window_note": WINDOW_NOTE if skipped_window else "",
            "skipped_no_consent": skipped_no_consent,
            "skipped_opted_out": skipped_opted_out,
            "skipped_cap": skipped_cap,
            "sent_template": sent_template,
            "consent_note": CONSENT_NOTE if (skipped_no_consent or skipped_opted_out) else "",
            "failed_numbers": failed_numbers,
            "first_error": first_error,
            "first_error_reason": first_error_reason,
            "dry_run":  False,
            "previews": previews,
        }


# ── structured outcome for scheduled campaigns (Stage 5) ─────────────────────────────────────────────
# Safe, fixed vocabulary: a code the dashboard can translate plus a short sentence. Never raw provider
# messages, tokens or phone numbers.
FAILURE_DETAILS = {
    "window_closed": "Customers last wrote more than 24 hours ago, so WhatsApp only allows an approved template. None is set up for campaigns.",
    "no_consent": "No recipient has agreed to receive promotional messages (marketing consent is required).",
    "opted_out": "Every eligible recipient has opted out of promotional messages.",
    "rate_limited": "WhatsApp limited how fast messages can be sent. Try again later or send in smaller batches.",
    "template_error": "WhatsApp rejected the message template. Check that the template is approved and its fields are right.",
    "recipient_invalid": "WhatsApp could not deliver to these phone numbers (not on WhatsApp or invalid).",
    "meta_error": "WhatsApp rejected the messages. Check your WhatsApp connection in Settings.",
    "internal_error": "The campaign could not be processed. Please try rescheduling it.",
    "no_recipients": "The chosen audience has no customers with a phone number.",
    "no_eligible_recipients": "No recipient was eligible to receive this message right now.",
    "send_cap": "The per-run template limit was reached before anyone could be sent a message.",
    "partial": "Some recipients were sent the message; others were skipped or failed.",
}
_REASON_TO_CODE = {"window": "window_closed", "rate_limit": "rate_limited", "template": "template_error",
                   "recipient": "recipient_invalid", "opt_out": "opted_out", "meta": "meta_error",
                   "internal": "internal_error"}


def _error_reason(exc) -> str:
    """Safe reason (wa_templates.classify_error vocabulary) for a send exception; never raises."""
    try:
        d = exc.args[0] if getattr(exc, "args", None) else None
        if isinstance(d, dict):
            from services.wa_templates import classify_error
            return classify_error(d)
    except Exception as e2:
        log.debug("campaign error classification failed: %s", type(e2).__name__)
    return "internal"


def outcome_from_result(result: dict | None, *, error: bool = False) -> dict:
    """Turn CampaignService.run's result (or a crash) into the structured fields stored on a scheduled campaign:
    outcome (delivered | partial | failed), failure_code, failure_detail and the counts. A campaign is only
    'delivered' when somebody was reached AND nobody eligible was left out; zero reached is always 'failed'."""
    if error or not isinstance(result, dict) or result.get("ok") is False:
        return {"outcome": "failed", "failure_code": "internal_error", "failure_detail": FAILURE_DETAILS["internal_error"],
                "sent_count": 0, "failed_count": 0, "skipped_count": 0, "total_recipients": 0}
    def n(key):
        try:
            return max(int(result.get(key) or 0), 0)
        except (TypeError, ValueError):
            return 0
    sent, failed, total = n("sent"), n("failed"), n("total")
    window, no_consent, opted_out, cap = n("skipped_window"), n("skipped_no_consent"), n("skipped_opted_out"), n("skipped_cap")
    skipped = n("skipped") + window + no_consent + opted_out + cap
    counts = {"sent_count": sent, "failed_count": failed, "skipped_count": skipped, "total_recipients": total}
    if sent > 0:
        if failed or window or no_consent or opted_out or cap:
            dominant = max(((failed, _REASON_TO_CODE.get(str(result.get("first_error_reason") or "meta"), "meta_error")),
                            (window, "window_closed"), (no_consent, "no_consent"), (opted_out, "opted_out"),
                            (cap, "send_cap")), key=lambda x: x[0])
            return {"outcome": "partial", "failure_code": "partial", "failure_detail":
                    FAILURE_DETAILS["partial"] + " Main reason: " + FAILURE_DETAILS[dominant[1]], **counts}
        return {"outcome": "delivered", "failure_code": None, "failure_detail": None, **counts}
    if total == 0:
        code = "no_recipients"
    elif failed:
        code = _REASON_TO_CODE.get(str(result.get("first_error_reason") or "meta"), "meta_error")
    else:
        top = max(((window, "window_closed"), (no_consent, "no_consent"), (opted_out, "opted_out"), (cap, "send_cap")),
                  key=lambda x: x[0])
        code = top[1] if top[0] else "no_eligible_recipients"
    return {"outcome": "failed", "failure_code": code, "failure_detail": FAILURE_DETAILS[code], **counts}


def _friendly_send_error(exc) -> str:
    """Turn a WhatsApp API error into a plain sentence the owner can act on."""
    raw = ""
    code = sub = None
    try:
        d = exc.args[0] if getattr(exc, "args", None) else None
        if isinstance(d, dict):
            code, sub, raw = d.get("code"), d.get("error_subcode"), str(d.get("message") or "")
        else:
            raw = str(exc)
    except Exception:
        raw = str(exc)
    if code == 100 and sub == 33:
        return ("Your WhatsApp Phone Number ID is not valid for your access token. "
                "Open Settings and re-enter it, or switch to the shared WaziBot number.")
    if code == 190:
        return "Your WhatsApp access token has expired. Reconnect WhatsApp in Settings."
    if code == 131047 or "24 hour" in raw or "re-engage" in raw.lower():
        return ("WhatsApp only allows free-text messages to customers who messaged you in the last 24 hours. "
                "Use an approved template for older customers.")
    if "paused" in raw:
        return "Sending is temporarily paused platform-wide. Please try again shortly."
    return raw[:160] or "WhatsApp rejected the message."


# ─────────────────────────────────────────────────────────────────────────────
# AUDIENCE DESCRIPTIONS (for dashboard display)
# ─────────────────────────────────────────────────────────────────────────────

AUDIENCE_INFO: dict[str, dict] = {
    "all":           {"label": "All Customers",           "icon": "📱", "desc": "Every customer who has ever messaged"},
    "vip":           {"label": "VIP Customers",           "icon": "⭐", "desc": "10+ orders or $50+ spent"},
    "loyal":         {"label": "Loyal Customers",         "icon": "💚", "desc": "5+ orders or $20+ spent"},
    "regular":       {"label": "Regular Customers",       "icon": "👍", "desc": "2–4 orders placed"},
    "new":           {"label": "New Customers",           "icon": "👋", "desc": "Exactly 1 order placed"},
    "inactive_7d":   {"label": "Inactive (7 days)",       "icon": "😴", "desc": "No activity in last 7 days"},
    "inactive_14d":  {"label": "Inactive (14 days)",      "icon": "😴", "desc": "No activity in last 14 days"},
    "inactive_30d":  {"label": "Inactive (30 days)",      "icon": "🔴", "desc": "No activity in last 30 days"},
    "high_spenders": {"label": "High Spenders",           "icon": "💰", "desc": "Customers with $30+ total spend"},
    "unpaid":        {"label": "Unpaid Orders",           "icon": "⏳", "desc": "Customers with pending payments"},
    "custom":        {"label": "Custom Selection",        "icon": "✏️",  "desc": "Manually selected phones"},
}
