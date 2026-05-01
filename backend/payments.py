"""
payments.py — Simple, trust-based payment stack for WaziBot.

Methods supported:
  1. EcoCash        — Manual. Customer dials *151#, sends money, replies "paid".
  2. PayPal email   — Simple. Customer sends to business PayPal email, replies "paid".
  3. PayPal API     — Optional. Auto-generates a checkout link if credentials exist.
  4. Cash           — Pay on delivery or pickup. No gateway needed.

Environment variables:
  # EcoCash (required for EcoCash option to show)
  ECOCASH_NUMBER=+263771234567
  ECOCASH_NAME=Flavoury Foods

  # PayPal simple (required for PayPal option)
  PAYPAL_EMAIL=payments@yourbusiness.com

  # PayPal API (optional — upgrades PayPal to auto-link mode)
  PAYPAL_CLIENT_ID=your_client_id
  PAYPAL_SECRET=your_secret
  PAYPAL_RETURN_URL=https://your-api.onrender.com/payments/paypal/success
  PAYPAL_CANCEL_URL=https://your-api.onrender.com/payments/paypal/cancel
  PAYPAL_MODE=sandbox   # or 'live'

Return contract — every function returns:
  {
    "method":   str,    # "ecocash" | "paypal" | "cash"
    "message":  str,    # WhatsApp-ready instruction text
    "url":      str,    # payment URL (empty for manual methods)
    "reference":str,    # ORDER-{id}
    "status":   str,    # "awaiting_payment" | "error"
    "error":    str,    # non-empty if something failed
  }
"""

import os
import base64
import logging

import requests as http

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _ref(order: dict) -> str:
    return f"ORDER-{order.get('id', 'X')}"


def _total(order: dict) -> float:
    return float(order.get("total_price") or order.get("total") or 0)


def _base(method: str, order: dict) -> dict:
    return {
        "method":    method,
        "message":   "",
        "url":       "",
        "reference": _ref(order),
        "status":    "awaiting_payment",
        "error":     "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# AVAILABILITY CHECK — which methods are configured?
# ─────────────────────────────────────────────────────────────────────────────

def available_methods() -> list[str]:
    """
    Return ordered list of payment methods that are configured.
    Always includes 'cash'. Others require env vars to be set.
    """
    methods = []
    if _env("ECOCASH_NUMBER"):
        methods.append("ecocash")
    if _env("PAYPAL_EMAIL") or _env("PAYPAL_CLIENT_ID"):
        methods.append("paypal")
    methods.append("cash")   # always available
    return methods


# ─────────────────────────────────────────────────────────────────────────────
# 1. ECOCASH — Manual, instruction-based
# ─────────────────────────────────────────────────────────────────────────────

def generate_ecocash_instructions(order: dict) -> dict:
    """
    Build WhatsApp-ready EcoCash payment instructions.
    No API call required — pure text generation.
    """
    result = _base("ecocash", order)
    total  = _total(order)
    ref    = _ref(order)

    # Number: env var → business payment_number stored on the order dict
    number = (
        _env("ECOCASH_NUMBER")
        or order.get("payment_number", "")
        or order.get("ecocash_number", "")
    )
    name = (
        _env("ECOCASH_NAME")
        or order.get("payment_name", "")
        or order.get("business_name", "WaziBot Business")
    )

    if not number:
        log.warning("generate_ecocash_instructions: ECOCASH_NUMBER not set")
        result["error"]   = "EcoCash number not configured"
        result["status"]  = "error"
        result["message"] = (
            "⚠️ EcoCash details aren't set up yet.\n"
            "Please contact us to arrange payment directly."
        )
        return result

    result["message"] = (
        f"💚 *Pay via EcoCash*\n"
        f"{'─' * 28}\n"
        f"  Send to  : *{number}*\n"
        f"  Name     : *{name}*\n"
        f"  Amount   : *${total:.2f}*\n"
        f"  Ref      : *{ref}*\n"
        f"{'─' * 28}\n"
        f"📱 *How to send (dial *151#)*\n"
        f"  1️⃣  Select _Send Money_\n"
        f"  2️⃣  Enter number: *{number}*\n"
        f"  3️⃣  Enter amount: *${total:.2f}*\n"
        f"  4️⃣  Use reference: *{ref}*\n"
        f"{'─' * 28}\n"
        f"🔒 Verified merchant: *{name}*\n\n"
        f"Once sent, reply *paid* and we'll confirm your order. 📸\n"
        f"_Keep your receipt screenshot just in case!_"
    )
    log.info("ecocash instructions  ref=%s  total=%.2f", ref, total)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 2. PAYPAL — Simple email mode (no API needed)
# ─────────────────────────────────────────────────────────────────────────────

def generate_paypal_email_instructions(order: dict) -> dict:
    """
    Simple PayPal instructions — customer sends money to the business email.
    No API key required. Works for any PayPal account.
    """
    result = _base("paypal", order)
    total  = _total(order)
    ref    = _ref(order)
    email  = _env("PAYPAL_EMAIL")
    name   = _env("ECOCASH_NAME") or order.get("business_name", "WaziBot Business")

    if not email:
        result["error"]   = "PayPal email not configured"
        result["status"]  = "error"
        result["message"] = (
            "⚠️ PayPal isn't set up yet.\n"
            "Please choose EcoCash or Cash on Delivery."
        )
        return result

    result["message"] = (
        f"🌍 *Pay via PayPal*\n"
        f"{'─' * 28}\n"
        f"  Send to  : *{email}*\n"
        f"  Name     : *{name}*\n"
        f"  Amount   : *${total:.2f} USD*\n"
        f"  Note/Ref : *{ref}*\n"
        f"{'─' * 28}\n"
        f"💡 *Steps:*\n"
        f"  1️⃣  Open PayPal app or paypal.com\n"
        f"  2️⃣  Tap _Send Money_\n"
        f"  3️⃣  Enter email: *{email}*\n"
        f"  4️⃣  Amount: *${total:.2f}* → Add note: *{ref}*\n"
        f"{'─' * 28}\n"
        f"🔒 Verified merchant: *{name}*\n\n"
        f"Once sent, reply *paid* and we'll confirm your order. ✅\n"
        f"_Cards, PayPal balance & Buy Now Pay Later accepted._"
    )
    log.info("paypal email instructions  ref=%s  total=%.2f  email=%s", ref, total, email)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 3. PAYPAL API — Auto-link mode (optional upgrade)
# ─────────────────────────────────────────────────────────────────────────────

def _paypal_base_url() -> str:
    mode = _env("PAYPAL_MODE", "sandbox")
    return (
        "https://api-m.sandbox.paypal.com"
        if mode == "sandbox"
        else "https://api-m.paypal.com"
    )


def _paypal_token() -> str:
    """Get a short-lived PayPal OAuth2 access token."""
    client_id = _env("PAYPAL_CLIENT_ID")
    secret    = _env("PAYPAL_SECRET")
    if not client_id or not secret:
        raise ValueError("PAYPAL_CLIENT_ID / PAYPAL_SECRET not set")
    creds = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    resp  = http.post(
        f"{_paypal_base_url()}/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials",
        timeout=12,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError("PayPal returned no access token")
    return token


def create_paypal_checkout(order: dict) -> dict:
    """
    Optional: generate a PayPal checkout link automatically.
    Only called when PAYPAL_CLIENT_ID + PAYPAL_SECRET are set.
    Falls back to email instructions on any failure.
    """
    result     = _base("paypal", order)
    ref        = _ref(order)
    total      = _total(order)
    return_url = _env("PAYPAL_RETURN_URL", "https://example.com/payments/paypal/success")
    cancel_url = _env("PAYPAL_CANCEL_URL", "https://example.com/payments/paypal/cancel")
    biz_name   = order.get("business_name", "WaziBot")

    try:
        token = _paypal_token()
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": ref,
                "description":  f"WaziBot {ref}",
                "amount": {"currency_code": "USD", "value": f"{total:.2f}"},
            }],
            "application_context": {
                "return_url":          return_url + f"?reference={ref}",
                "cancel_url":          cancel_url + f"?reference={ref}",
                "brand_name":          biz_name,
                "landing_page":        "BILLING",
                "user_action":         "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            },
        }
        resp = http.post(
            f"{_paypal_base_url()}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()

        approval_url = next(
            (lnk["href"] for lnk in data.get("links", []) if lnk.get("rel") == "approve"),
            "",
        )
        paypal_order_id = data.get("id", "")

        if not approval_url:
            raise ValueError("No approval URL in PayPal response")

        result["url"]      = approval_url
        result["poll_url"] = paypal_order_id
        result["message"]  = (
            f"🌍 *Pay via PayPal*\n"
            f"{'─' * 28}\n"
            f"  Order  : *{ref}*\n"
            f"  Amount : *${total:.2f} USD*\n"
            f"{'─' * 28}\n"
            f"👆 Tap to pay securely:\n{approval_url}\n\n"
            f"Your order is confirmed automatically after payment.\n"
            f"_Cards, PayPal balance & BNPL accepted._"
        )
        log.info("paypal checkout created  ref=%s  paypal_id=%s", ref, paypal_order_id)
        return result

    except Exception as exc:
        log.warning("create_paypal_checkout failed (%s) — falling back to email", exc)
        # Graceful fallback to email instructions
        return generate_paypal_email_instructions(order)


def paypal_payment(order: dict) -> dict:
    """
    Smart dispatcher:
    - If PAYPAL_CLIENT_ID + PAYPAL_SECRET exist → generate checkout link
    - Otherwise → return email instructions
    Always works, never crashes.
    """
    if _env("PAYPAL_CLIENT_ID") and _env("PAYPAL_SECRET"):
        return create_paypal_checkout(order)
    return generate_paypal_email_instructions(order)


def capture_paypal_order(paypal_order_id: str) -> dict:
    """
    Capture a PayPal order after user approves.
    Called from /payments/paypal/success endpoint.
    Returns: { paid, reference, amount, status, error }
    """
    try:
        token = _paypal_token()
        resp  = http.post(
            f"{_paypal_base_url()}/v2/checkout/orders/{paypal_order_id}/capture",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=12,
        )
        resp.raise_for_status()
        data     = resp.json()
        status   = data.get("status", "")
        paid     = status == "COMPLETED"
        pu       = (data.get("purchase_units") or [{}])[0]
        ref      = pu.get("reference_id", "")
        captures = pu.get("payments", {}).get("captures", [{}])
        amount   = float((captures[0] if captures else {}).get("amount", {}).get("value", 0))
        log.info("paypal capture  id=%s  paid=%s  ref=%s  amount=%.2f", paypal_order_id, paid, ref, amount)
        return {"paid": paid, "reference": ref, "amount": amount, "status": status, "error": ""}
    except Exception as exc:
        log.exception("capture_paypal_order error: %s", exc)
        return {"paid": False, "reference": "", "amount": 0, "error": str(exc), "status": "error"}


# ─────────────────────────────────────────────────────────────────────────────
# 4. CASH — Pay on delivery / pickup
# ─────────────────────────────────────────────────────────────────────────────

def generate_cash_instructions(order: dict) -> dict:
    """
    Cash on delivery / pickup instructions.
    No API, no credentials required.
    """
    result = _base("cash", order)
    total  = _total(order)
    ref    = _ref(order)
    name   = order.get("business_name", "WaziBot Business")

    result["message"] = (
        f"💵 *Cash on Delivery / Pickup*\n"
        f"{'─' * 28}\n"
        f"  Order  : *{ref}*\n"
        f"  Amount : *${total:.2f}*\n"
        f"{'─' * 28}\n"
        f"Your order is confirmed! 🎉\n\n"
        f"Please have *${total:.2f}* ready when your order\n"
        f"is delivered or when you come to collect.\n"
        f"{'─' * 28}\n"
        f"🔒 Merchant: *{name}*\n\n"
        f"We'll contact you to arrange delivery or pickup. 📦"
    )
    log.info("cash instructions  ref=%s  total=%.2f", ref, total)
    return result
