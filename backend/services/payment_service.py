"""
payments.py — WaziBot payment stack with PayPal auto-verification.

PAYMENT METHODS & VERIFICATION MODEL:
  ┌─────────────────────────────────────────────────────────────────┐
  │ Method    │ Verification    │ Proof required │ Confirmation    │
  ├─────────────────────────────────────────────────────────────────┤
  │ EcoCash   │ Manual          │ Yes (txn ID / │ Staff confirms  │
  │           │                 │  screenshot)   │                 │
  ├─────────────────────────────────────────────────────────────────┤
  │ PayPal    │ Automatic ✅    │ No             │ Webhook fires   │
  │           │ (Orders API +   │                │ auto-confirms   │
  │           │  Webhook)       │                │                 │
  ├─────────────────────────────────────────────────────────────────┤
  │ Cash      │ Instant ✅      │ No             │ Immediate       │
  └─────────────────────────────────────────────────────────────────┘

PAYPAL FLOW (auto-verified):
  1. create_paypal_order(order)
       → POST /v2/checkout/orders  →  returns { paypal_order_id, approval_url }
       → paypal_order_id stored in orders.paypal_order_id
  2. Customer taps approval_url in WhatsApp, pays on PayPal.
  3. PayPal fires POST /payments/paypal/webhook event PAYMENT.CAPTURE.COMPLETED
  4. verify_paypal_webhook_signature()  validates PAYPAL-TRANSMISSION-SIG
  5. Order marked paid, WhatsApp confirmation sent.

PAYPAL ENV VARS:
  PAYPAL_CLIENT_ID   — Live app client ID
  PAYPAL_SECRET      — Live app secret
  PAYPAL_WEBHOOK_ID  — Webhook ID from PayPal Developer Dashboard (required
                       for signature verification)
  PAYPAL_RETURN_URL  — Browser redirect after payment approval
  PAYPAL_CANCEL_URL  — Browser redirect on cancel
  PAYPAL_MODE        — "live" (default) or "sandbox" (dev only)

ECOCASH / CASH ENV VARS (platform-wide fallbacks):
  ECOCASH_NUMBER, ECOCASH_NAME, PAYPAL_EMAIL

RETURN CONTRACT — every gateway function returns:
  {
    "method":          str,   # "ecocash" | "paypal" | "cash"
    "message":         str,   # WhatsApp-ready instruction text
    "url":             str,   # PayPal approval URL (empty for manual methods)
    "reference":       str,   # "ORDER-{id}"
    "paypal_order_id": str,   # PayPal's order ID (empty for non-PayPal)
    "auto_verified":   bool,  # True = webhook will confirm, no proof needed
    "status":          str,   # "awaiting_payment" | "error"
    "error":           str,   # non-empty if something failed
  }
"""

import os
import base64
import hashlib
import hmac
import logging
import json
from typing import Optional

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
        "method":          method,
        "message":         "",
        "url":             "",
        "reference":       _ref(order),
        "paypal_order_id": "",
        "auto_verified":   False,
        "status":          "awaiting_payment",
        "error":           "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# SETTINGS RESOLVERS — DB-first, ENV fallback
# ─────────────────────────────────────────────────────────────────────────────

def _biz_ecocash(order: dict) -> tuple[str, str]:
    """Return (ecocash_number, account_name) for this order's business."""
    number = (
        order.get("ecocash_number", "")
        or order.get("payment_number", "")
        or _env("ECOCASH_NUMBER")
    )
    name = (
        order.get("ecocash_name", "")
        or order.get("payment_name", "")
        or _env("ECOCASH_NAME")
        or order.get("business_name", "WaziBot Business")
    )
    return number.strip(), name.strip()


def _biz_paypal_email(order: dict) -> tuple[str, str]:
    """Return (paypal_email, business_name) for this order's business."""
    email = (
        order.get("paypal_email", "")
        or _env("PAYPAL_EMAIL")
    )
    name = order.get("business_name", "WaziBot Business")
    return email.strip(), name


# ─────────────────────────────────────────────────────────────────────────────
# AVAILABILITY — which methods are configured for THIS business?
# ─────────────────────────────────────────────────────────────────────────────

def available_methods(order: dict = None) -> list[str]:
    """
    Return ordered list of configured payment methods for this business.
    Cash is always included. Order: EcoCash → PayPal → Cash.
    """
    order   = order or {}
    methods: list[str] = []

    eco_number, _ = _biz_ecocash(order)
    if eco_number:
        methods.append("ecocash")

    paypal_email, _ = _biz_paypal_email(order)
    if paypal_email or _env("PAYPAL_CLIENT_ID"):
        methods.append("paypal")

    methods.append("cash")
    return methods


# ─────────────────────────────────────────────────────────────────────────────
# PAYPAL API — base URL and OAuth token
# ─────────────────────────────────────────────────────────────────────────────

def _paypal_base() -> str:
    """Returns PayPal API base URL. sandbox mode for dev, live for production."""
    mode = _env("PAYPAL_MODE", "live")
    if mode == "sandbox":
        return "https://api-m.sandbox.paypal.com"
    return "https://api-m.paypal.com"


def _paypal_token() -> str:
    """
    Fetch a short-lived OAuth2 Bearer token from PayPal.
    Uses PAYPAL_CLIENT_ID + PAYPAL_SECRET from environment.
    Raises ValueError if credentials are not set.
    """
    client_id = _env("PAYPAL_CLIENT_ID")
    secret    = _env("PAYPAL_SECRET")

    if not client_id or not secret:
        raise ValueError("PAYPAL_CLIENT_ID and PAYPAL_SECRET must be set in environment")

    creds = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    resp  = http.post(
        f"{_paypal_base()}/v1/oauth2/token",
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
        raise ValueError("PayPal OAuth did not return an access_token")
    return token


# ─────────────────────────────────────────────────────────────────────────────
# 1. PAYPAL ORDERS API — create + capture + get order details
# ─────────────────────────────────────────────────────────────────────────────

def create_paypal_order(order: dict) -> dict:
    """
    Create a PayPal checkout order via the Orders v2 API.

    This is the PRIMARY PayPal entry point (replaces old create_paypal_checkout).
    Returns the full result dict with:
      - url             → approval URL to send to customer
      - paypal_order_id → PayPal's order ID (must be stored in DB)
      - auto_verified   → True (webhook will confirm automatically)

    On any API failure, falls back to generate_paypal_email_instructions()
    so the customer always gets a usable payment option.
    """
    result     = _base("paypal", order)
    ref        = _ref(order)
    total      = _total(order)
    return_url = _env("PAYPAL_RETURN_URL", "https://wazibothq.com/payments/paypal/success")
    cancel_url = _env("PAYPAL_CANCEL_URL", "https://wazibothq.com/payments/paypal/cancel")
    biz_name   = order.get("business_name", "WaziBot")

    try:
        token = _paypal_token()

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": ref,
                "description":  f"WaziBot {ref}",
                "custom_id":    str(order.get("id", "")),   # internal order ID for webhook lookup
                "amount": {
                    "currency_code": "USD",
                    "value":         f"{total:.2f}",
                },
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
            f"{_paypal_base()}/v2/checkout/orders",
            headers={
                "Authorization":    f"Bearer {token}",
                "Content-Type":     "application/json",
                "PayPal-Request-Id": ref,   # idempotency key
            },
            json=payload,
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()

        paypal_order_id = data.get("id", "")
        approval_url    = next(
            (lnk["href"] for lnk in data.get("links", []) if lnk.get("rel") == "approve"),
            "",
        )

        if not paypal_order_id or not approval_url:
            raise ValueError(f"PayPal API returned incomplete response: {data}")

        result.update({
            "url":             approval_url,
            "paypal_order_id": paypal_order_id,
            "auto_verified":   True,
            "message": (
                f"🌍 *Pay with PayPal*\n"
                f"{'─' * 28}\n"
                f"  Order  : *{ref}*\n"
                f"  Amount : *${total:.2f} USD*\n"
                f"{'─' * 28}\n"
                f"👆 *Tap to pay securely:*\n"
                f"{approval_url}\n\n"
                f"✅ Your order will be confirmed *automatically* once payment is received.\n"
                f"_No need to reply — we'll message you when it's done!_\n\n"
                f"_Cards, PayPal balance & Buy Now Pay Later accepted._\n"
                f"_Type *cancel* to cancel this order._"
            ),
        })

        log.info("create_paypal_order  ref=%s  paypal_id=%s  total=%.2f",
                 ref, paypal_order_id, total)
        return result

    except Exception as exc:
        log.warning("create_paypal_order failed (%s) — falling back to email instructions", exc)
        fallback = generate_paypal_email_instructions(order)
        fallback["error"]         = str(exc)
        fallback["auto_verified"] = False
        return fallback


def capture_paypal_order(paypal_order_id: str) -> dict:
    """
    Capture an approved PayPal order (called from /payments/paypal/success).
    Returns: { paid, reference, internal_order_id, amount, status, error }
    """
    try:
        token = _paypal_token()
        resp  = http.post(
            f"{_paypal_base()}/v2/checkout/orders/{paypal_order_id}/capture",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=12,
        )
        resp.raise_for_status()
        data   = resp.json()
        status = data.get("status", "")
        paid   = (status == "COMPLETED")

        pu       = (data.get("purchase_units") or [{}])[0]
        ref      = pu.get("reference_id", "")           # our ORDER-{id}
        custom   = pu.get("custom_id", "")              # our internal order id
        captures = pu.get("payments", {}).get("captures", [{}])
        capture  = captures[0] if captures else {}
        amount   = float(capture.get("amount", {}).get("value", 0))
        currency = capture.get("amount", {}).get("currency_code", "USD")

        log.info("capture_paypal_order  paypal_id=%s  paid=%s  ref=%s  amount=%.2f %s",
                 paypal_order_id, paid, ref, amount, currency)

        return {
            "paid":              paid,
            "reference":         ref,
            "internal_order_id": int(custom) if custom.isdigit() else None,
            "amount":            amount,
            "currency":          currency,
            "status":            status,
            "error":             "",
        }
    except Exception as exc:
        log.exception("capture_paypal_order error: %s", exc)
        return {
            "paid": False, "reference": "", "internal_order_id": None,
            "amount": 0, "currency": "USD", "error": str(exc), "status": "error",
        }


def get_paypal_order_details(paypal_order_id: str) -> dict:
    """
    Fetch the current status of a PayPal order from the API.
    Used to check if payment completed when user says "paid" before webhook fires.
    Returns: { status, paid, amount, currency, reference, internal_order_id, error }
    """
    try:
        token = _paypal_token()
        resp  = http.get(
            f"{_paypal_base()}/v2/checkout/orders/{paypal_order_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data   = resp.json()
        status = data.get("status", "")
        paid   = status == "COMPLETED"

        pu       = (data.get("purchase_units") or [{}])[0]
        ref      = pu.get("reference_id", "")
        custom   = pu.get("custom_id", "")
        captures = pu.get("payments", {}).get("captures", [{}])
        capture  = captures[0] if captures else {}
        amount   = float(capture.get("amount", {}).get("value", 0)) if capture else 0
        currency = capture.get("amount", {}).get("currency_code", "USD") if capture else "USD"

        return {
            "status":            status,
            "paid":              paid,
            "amount":            amount,
            "currency":          currency,
            "reference":         ref,
            "internal_order_id": int(custom) if custom.isdigit() else None,
            "error":             "",
        }
    except Exception as exc:
        log.error("get_paypal_order_details error: %s", exc)
        return {"paid": False, "status": "unknown", "amount": 0, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# PAYPAL WEBHOOK VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def verify_paypal_webhook_signature(
    headers: dict,
    raw_body: bytes,
    webhook_id: str,
) -> bool:
    """
    Verify a PayPal webhook using the PayPal /v1/notifications/verify-webhook-signature API.

    Required headers from PayPal:
      PAYPAL-TRANSMISSION-ID
      PAYPAL-TRANSMISSION-TIME
      PAYPAL-CERT-URL
      PAYPAL-AUTH-ALGO
      PAYPAL-TRANSMISSION-SIG

    webhook_id must match the Webhook ID from PayPal Developer Dashboard.
    Returns True if signature is valid, False otherwise.
    """
    required = [
        "PAYPAL-TRANSMISSION-ID",
        "PAYPAL-TRANSMISSION-TIME",
        "PAYPAL-CERT-URL",
        "PAYPAL-AUTH-ALGO",
        "PAYPAL-TRANSMISSION-SIG",
    ]
    # Headers may come in any case — normalise to upper
    headers_upper = {k.upper(): v for k, v in headers.items()}

    missing = [h for h in required if h not in headers_upper]
    if missing:
        log.warning("verify_paypal_webhook: missing headers: %s", missing)
        return False

    try:
        token = _paypal_token()
        verify_payload = {
            "auth_algo":         headers_upper["PAYPAL-AUTH-ALGO"],
            "cert_url":          headers_upper["PAYPAL-CERT-URL"],
            "transmission_id":   headers_upper["PAYPAL-TRANSMISSION-ID"],
            "transmission_sig":  headers_upper["PAYPAL-TRANSMISSION-SIG"],
            "transmission_time": headers_upper["PAYPAL-TRANSMISSION-TIME"],
            "webhook_id":        webhook_id,
            "webhook_event":     json.loads(raw_body),
        }
        resp = http.post(
            f"{_paypal_base()}/v1/notifications/verify-webhook-signature",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            json=verify_payload,
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json().get("verification_status", "")
        ok = result == "SUCCESS"
        if not ok:
            log.warning("PayPal webhook verification_status=%s", result)
        return ok

    except Exception as exc:
        log.error("verify_paypal_webhook_signature error: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# PAYPAL SMART DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

def paypal_payment(order: dict) -> dict:
    """
    Smart PayPal dispatcher:
      - API credentials set (PAYPAL_CLIENT_ID + PAYPAL_SECRET)
        → create_paypal_order() → auto-verified via webhook
      - No API credentials → generate_paypal_email_instructions() → manual proof

    Always falls back to email on API failure.
    """
    if _env("PAYPAL_CLIENT_ID") and _env("PAYPAL_SECRET"):
        return create_paypal_order(order)
    return generate_paypal_email_instructions(order)


# ─────────────────────────────────────────────────────────────────────────────
# 2. PAYPAL EMAIL — Manual, instruction-based (no API)
# ─────────────────────────────────────────────────────────────────────────────

def generate_paypal_email_instructions(order: dict) -> dict:
    """
    Simple PayPal instructions — customer sends to the business's real PayPal email.
    Used when PAYPAL_CLIENT_ID is not configured, or as fallback on API error.
    Requires proof of payment (same as EcoCash).
    """
    result = _base("paypal", order)
    total  = _total(order)
    ref    = _ref(order)

    email, name = _biz_paypal_email(order)

    if not email:
        log.warning("generate_paypal_email_instructions: no PayPal email configured for biz=%s",
                    order.get("business_id", "?"))
        result["error"]  = "PayPal email not configured"
        result["status"] = "error"
        result["message"] = (
            "⚠️ PayPal isn't configured yet for this business.\n"
            "Please choose EcoCash or Cash on Delivery instead."
        )
        return result

    result["auto_verified"] = False   # manual mode — proof required
    result["message"] = (
        f"🌍 *Pay via PayPal (Email)*\n"
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
        f"  4️⃣  Amount *${total:.2f}* — Add note: *{ref}*\n"
        f"{'─' * 28}\n"
        f"🔒 Verified merchant: *{name}*\n\n"
        f"Once sent, reply *paid* and send your transaction ID or screenshot. ✅"
    )
    log.info("paypal email instructions  ref=%s  total=%.2f  email=%s", ref, total, email)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 3. ECOCASH — Manual, instruction-based
# ─────────────────────────────────────────────────────────────────────────────

def generate_ecocash_instructions(order: dict) -> dict:
    """
    EcoCash payment instructions. Always requires proof of payment.
    Uses business's own EcoCash number from DB settings.
    """
    result = _base("ecocash", order)
    total  = _total(order)
    ref    = _ref(order)

    number, name = _biz_ecocash(order)

    if not number:
        log.warning("generate_ecocash_instructions: no EcoCash number for biz=%s",
                    order.get("business_id", "?"))
        result["error"]  = "EcoCash number not configured"
        result["status"] = "error"
        result["message"] = (
            "⚠️ EcoCash details aren't set up yet.\n"
            "Please contact us to arrange payment directly."
        )
        return result

    result["auto_verified"] = False   # always requires manual proof
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
        f"Once sent, reply *paid* and send your transaction ID or screenshot. 📸\n"
        f"_Keep your receipt screenshot just in case!_"
    )
    log.info("ecocash instructions  ref=%s  total=%.2f  number=%s", ref, total, number)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 4. CASH — Instant confirmation, no proof needed
# ─────────────────────────────────────────────────────────────────────────────

def generate_cash_instructions(order: dict) -> dict:
    """
    Cash on delivery / pickup. Confirmed immediately — no proof required.
    """
    result = _base("cash", order)
    total  = _total(order)
    ref    = _ref(order)
    name   = order.get("business_name", "WaziBot Business")

    result["auto_verified"] = True   # instant — no proof needed
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
        f"We'll be in touch to arrange delivery or pickup. 📦"
    )
    log.info("cash instructions  ref=%s  total=%.2f", ref, total)
    return result
