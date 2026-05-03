"""
payments.py — Trust-based payment stack for WaziBot.

PAYMENT METHODS:
  1. EcoCash   — Manual. Customer dials *151#, sends to business number, replies "paid".
  2. PayPal    — Email-based. Customer sends to business PayPal email, replies "paid".
               — Optional API mode: auto-generates a live PayPal checkout link if
                 PAYPAL_CLIENT_ID + PAYPAL_SECRET are set (always live, never sandbox).
  3. Cash      — Pay on delivery or pickup. No gateway needed.

SETTINGS PRIORITY (per gateway):
  Each payment method reads its config in this order:
    1. Business DB record  (set via /me/payment-settings — the recommended way)
    2. Environment variable (global fallback for deployments with one business)
    3. Empty string → method hidden from customers

  This means:
    • Businesses set their own EcoCash number and PayPal email via the settings page.
    • The system works for multi-tenant SaaS (each business has its own credentials).
    • ENV vars act as a platform-wide fallback if no per-business config is set.

HOW available_methods() WORKS:
  Returns only the methods that have credentials configured for a given business.
  Always called with the order dict so business-specific settings are used.
  Cash is always available.

RETURN CONTRACT — every function returns:
  {
    "method":    str,   # "ecocash" | "paypal" | "cash"
    "message":   str,   # WhatsApp-ready instruction text
    "url":       str,   # PayPal checkout URL (empty for manual methods)
    "reference": str,   # "ORDER-{id}"
    "status":    str,   # "awaiting_payment" | "error"
    "error":     str,   # non-empty if something failed
  }

REQUIRED ENV VARS (platform-wide fallbacks):
  ECOCASH_NUMBER=+263771234567    # fallback EcoCash number if biz hasn't set one
  ECOCASH_NAME=Flavoury Foods     # fallback account name

  PAYPAL_EMAIL=pay@yourbiz.com    # fallback PayPal email if biz hasn't set one

  # Optional — enables auto-link mode (live PayPal, NOT sandbox):
  PAYPAL_CLIENT_ID=your_live_client_id
  PAYPAL_SECRET=your_live_secret
  PAYPAL_RETURN_URL=https://your-api.onrender.com/payments/paypal/success
  PAYPAL_CANCEL_URL=https://your-api.onrender.com/payments/paypal/cancel

  NOTE: PAYPAL_MODE is intentionally removed. The system always uses the live
  PayPal API. Use sandbox credentials in PAYPAL_CLIENT_ID/SECRET during
  development if you must test, but real businesses always go live.
"""

import os
import base64
import logging

import requests as http

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    """Read env var, strip whitespace, never raise."""
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
# SETTINGS RESOLVER — reads per-business DB config, falls back to ENV
# ─────────────────────────────────────────────────────────────────────────────

def _biz_ecocash(order: dict) -> tuple[str, str]:
    """
    Return (ecocash_number, ecocash_name) for this order's business.
    Priority: DB record → ENV var → empty string.
    """
    number = (
        order.get("ecocash_number", "")      # injected from businesses.ecocash_number
        or order.get("payment_number", "")    # legacy field
        or _env("ECOCASH_NUMBER")             # platform-wide fallback
    )
    name = (
        order.get("ecocash_name", "")         # injected from businesses.ecocash_name
        or order.get("payment_name", "")      # legacy field
        or _env("ECOCASH_NAME")               # platform-wide fallback
        or order.get("business_name", "WaziBot Business")
    )
    return number.strip(), name.strip()


def _biz_paypal_email(order: dict) -> tuple[str, str]:
    """
    Return (paypal_email, business_name) for this order's business.
    Priority: DB record → ENV var → empty string.
    """
    email = (
        order.get("paypal_email", "")         # injected from businesses.paypal_email
        or _env("PAYPAL_EMAIL")               # platform-wide fallback
    )
    name = order.get("business_name", "WaziBot Business")
    return email.strip(), name


# ─────────────────────────────────────────────────────────────────────────────
# AVAILABILITY — which methods are configured for THIS business?
# ─────────────────────────────────────────────────────────────────────────────

def available_methods(order: dict = None) -> list[str]:
    """
    Return ordered list of payment methods that are configured.

    Pass the order dict so per-business DB settings are respected.
    Falls back to ENV vars when order is None (e.g. during menu building).
    Cash is always available.
    """
    order = order or {}
    methods: list[str] = []

    eco_number, _ = _biz_ecocash(order)
    if eco_number:
        methods.append("ecocash")

    paypal_email, _ = _biz_paypal_email(order)
    if paypal_email or _env("PAYPAL_CLIENT_ID"):
        methods.append("paypal")

    methods.append("cash")   # always available — no config needed
    return methods


# ─────────────────────────────────────────────────────────────────────────────
# 1. ECOCASH — Manual, instruction-based
# ─────────────────────────────────────────────────────────────────────────────

def generate_ecocash_instructions(order: dict) -> dict:
    """
    Build WhatsApp-ready EcoCash payment instructions.
    Uses the business's own EcoCash number from their settings.
    No external API call required.
    """
    result = _base("ecocash", order)
    total  = _total(order)
    ref    = _ref(order)

    number, name = _biz_ecocash(order)

    if not number:
        log.warning("generate_ecocash_instructions: no EcoCash number for business=%s",
                    order.get("business_id", "?"))
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
    log.info("ecocash instructions  ref=%s  total=%.2f  number=%s", ref, total, number)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 2. PAYPAL EMAIL — Simple, instruction-based (default mode)
# ─────────────────────────────────────────────────────────────────────────────

def generate_paypal_email_instructions(order: dict) -> dict:
    """
    Simple PayPal instructions — customer sends money to the business's real PayPal email.
    No API key required. Works for any PayPal account.
    This is the default PayPal mode — business receives money directly.
    """
    result = _base("paypal", order)
    total  = _total(order)
    ref    = _ref(order)

    email, name = _biz_paypal_email(order)

    if not email:
        log.warning("generate_paypal_email_instructions: no PayPal email for business=%s",
                    order.get("business_id", "?"))
        result["error"]   = "PayPal email not configured"
        result["status"]  = "error"
        result["message"] = (
            "⚠️ PayPal isn't set up yet for this business.\n"
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
        f"  4️⃣  Amount *${total:.2f}* — Add note: *{ref}*\n"
        f"{'─' * 28}\n"
        f"🔒 Verified merchant: *{name}*\n\n"
        f"Once sent, reply *paid* and we'll confirm your order. ✅\n"
        f"_Cards, PayPal balance & Buy Now Pay Later accepted._"
    )
    log.info("paypal email instructions  ref=%s  total=%.2f  email=%s", ref, total, email)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 3. PAYPAL API — Auto-link mode (optional, always LIVE)
# ─────────────────────────────────────────────────────────────────────────────

def _paypal_api_base() -> str:
    """Always returns the LIVE PayPal API endpoint. No sandbox."""
    return "https://api-m.paypal.com"


def _paypal_token() -> str:
    """Fetch a short-lived OAuth2 access token from the live PayPal API."""
    client_id = _env("PAYPAL_CLIENT_ID")
    secret    = _env("PAYPAL_SECRET")

    if not client_id or not secret:
        raise ValueError("PAYPAL_CLIENT_ID / PAYPAL_SECRET not set in environment")

    creds = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    resp  = http.post(
        f"{_paypal_api_base()}/v1/oauth2/token",
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
        raise ValueError("PayPal API did not return an access token")
    return token


def create_paypal_checkout(order: dict) -> dict:
    """
    Generate a live PayPal checkout link using the PayPal Orders v2 API.
    Only called when PAYPAL_CLIENT_ID + PAYPAL_SECRET are configured.

    IMPORTANT: Always uses the live PayPal API (api-m.paypal.com).
    Use your live credentials — sandbox is not supported in production.

    Falls back to email instructions on any failure so the customer
    always gets a usable payment option.
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
            f"{_paypal_api_base()}/v2/checkout/orders",
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
            raise ValueError("No approval URL in PayPal API response")

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
        log.info("paypal checkout link created  ref=%s  paypal_id=%s", ref, paypal_order_id)
        return result

    except Exception as exc:
        log.warning("create_paypal_checkout failed (%s) — falling back to email", exc)
        return generate_paypal_email_instructions(order)


def paypal_payment(order: dict) -> dict:
    """
    Smart PayPal dispatcher:
      - PAYPAL_CLIENT_ID + PAYPAL_SECRET set → generate live checkout link
      - Otherwise → email-based instructions (money goes straight to business email)

    Always falls back to email on API failure.
    """
    if _env("PAYPAL_CLIENT_ID") and _env("PAYPAL_SECRET"):
        return create_paypal_checkout(order)
    return generate_paypal_email_instructions(order)


def capture_paypal_order(paypal_order_id: str) -> dict:
    """
    Capture an approved PayPal order (live API).
    Called from the /payments/paypal/success endpoint after user approves.
    Returns: { paid, reference, amount, status, error }
    """
    try:
        token = _paypal_token()
        resp  = http.post(
            f"{_paypal_api_base()}/v2/checkout/orders/{paypal_order_id}/capture",
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
        log.info("paypal capture  id=%s  paid=%s  ref=%s  amount=%.2f",
                 paypal_order_id, paid, ref, amount)
        return {"paid": paid, "reference": ref, "amount": amount, "status": status, "error": ""}
    except Exception as exc:
        log.exception("capture_paypal_order error: %s", exc)
        return {"paid": False, "reference": "", "amount": 0, "error": str(exc), "status": "error"}


# ─────────────────────────────────────────────────────────────────────────────
# 4. CASH — Pay on delivery or pickup
# ─────────────────────────────────────────────────────────────────────────────

def generate_cash_instructions(order: dict) -> dict:
    """
    Cash on delivery / pickup instructions.
    No API, no credentials required. Always available.
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
        f"We'll be in touch to arrange delivery or pickup. 📦"
    )
    log.info("cash instructions  ref=%s  total=%.2f", ref, total)
    return result
