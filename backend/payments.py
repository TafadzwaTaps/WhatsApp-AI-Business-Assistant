"""
payments.py — Multi-payment gateway abstraction for WaziBot.

Supports:
  1. EcoCash   — Instruction-based. No API. Returns formatted payment text.
  2. Paynow    — Zimbabwe processor. Generates a redirect URL via Paynow API.
  3. PayPal    — International. Creates a PayPal checkout session + capture.

Required environment variables (Render → Environment or .env):
  # EcoCash
  ECOCASH_NUMBER=+263771234567
  ECOCASH_NAME=Flavoury Foods

  # Paynow
  PAYNOW_INTEGRATION_ID=your_id
  PAYNOW_INTEGRATION_KEY=your_key
  PAYNOW_RETURN_URL=https://your-api.onrender.com/payments/paynow/return
  PAYNOW_RESULT_URL=https://your-api.onrender.com/payments/paynow/callback

  # PayPal
  PAYPAL_CLIENT_ID=your_paypal_client_id
  PAYPAL_SECRET=your_paypal_secret
  PAYPAL_RETURN_URL=https://your-api.onrender.com/payments/paypal/success
  PAYPAL_CANCEL_URL=https://your-api.onrender.com/payments/paypal/cancel
  PAYPAL_MODE=sandbox   # change to 'live' for production

Return contract — every function returns:
  {
    "method":    str,   # "ecocash" | "paynow" | "paypal"
    "url":       str,   # payment URL (empty for EcoCash)
    "reference": str,   # e.g. "ORDER-42"
    "status":    str,   # "pending_payment" | "error"
    "message":   str,   # WhatsApp-ready text to send to customer
    "poll_url":  str,   # Paynow poll URL | PayPal order ID (for others: "")
    "error":     str,   # non-empty string if something failed
  }
"""

import os
import hmac
import hashlib
import logging
import base64

import requests as http

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    """Read an env var, strip whitespace, never raise."""
    return os.getenv(key, default).strip()


def _reference(order: dict) -> str:
    return f"ORDER-{order.get('id', 'X')}"


def _total(order: dict) -> float:
    return float(order.get("total_price") or order.get("total") or 0)


def _empty_result(method: str, order: dict) -> dict:
    """Baseline result dict — every gateway function starts from this."""
    return {
        "method":    method,
        "url":       "",
        "reference": _reference(order),
        "status":    "pending_payment",
        "message":   "",
        "poll_url":  "",
        "error":     "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. ECOCASH — Instruction-based, no external API
# ─────────────────────────────────────────────────────────────────────────────

def generate_ecocash_instructions(order: dict) -> dict:
    """
    Build WhatsApp-ready EcoCash payment instructions.

    Number priority:
      ECOCASH_NUMBER env var → order["payment_number"] → fallback warning
    """
    result = _empty_result("ecocash", order)
    total  = _total(order)
    ref    = _reference(order)

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
        log.warning("generate_ecocash_instructions: ECOCASH_NUMBER not configured")
        result["error"]   = "EcoCash number not configured"
        result["message"] = (
            "⚠️ EcoCash payment details are not set up yet.\n"
            "Please contact us directly to arrange payment."
        )
        return result

    result["message"] = (
        f"💚 *EcoCash Payment*\n"
        f"{'─' * 30}\n"
        f"  Send to  : *{number}*\n"
        f"  Name     : *{name}*\n"
        f"  Amount   : *${total:.2f}*\n"
        f"  Ref      : *{ref}*\n"
        f"{'─' * 30}\n"
        f"📋 *Steps (dial *151#)*\n"
        f"  1️⃣  Select _Send Money_\n"
        f"  2️⃣  Enter: *{number}*\n"
        f"  3️⃣  Amount: *${total:.2f}*\n"
        f"  4️⃣  Reference: *{ref}*\n"
        f"{'─' * 30}\n"
        f"🔒 Verified merchant: *{name}*\n\n"
        f"Your order will be confirmed once payment is received.\n"
        f"_Keep your receipt screenshot!_ 📸"
    )
    log.info("ecocash instructions  ref=%s  total=%.2f  number=%s", ref, total, number)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 2. PAYNOW (Zimbabwe)
# ─────────────────────────────────────────────────────────────────────────────

_PAYNOW_URL = "https://www.paynow.co.zw/interface/remotetransaction"


def _paynow_hash(fields: dict, key: str) -> str:
    """SHA512 HMAC of all field values (excluding 'hash') joined as one string."""
    values = "".join(str(v) for k, v in fields.items() if k.lower() != "hash")
    return hmac.new(key.encode(), values.encode(), hashlib.sha512).hexdigest().upper()


def _parse_paynow_response(text: str) -> dict:
    """Parse Paynow's URL-encoded response into a dict."""
    result: dict = {}
    for part in text.split("&"):
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip().lower()] = v.strip()
    return result


def create_paynow_payment(order: dict) -> dict:
    """
    Initiate a Paynow web payment.
    Returns result with 'url' = Paynow redirect link and 'poll_url' for status checks.
    """
    result = _empty_result("paynow", order)

    integration_id  = _env("PAYNOW_INTEGRATION_ID")
    integration_key = _env("PAYNOW_INTEGRATION_KEY")
    return_url      = _env("PAYNOW_RETURN_URL", "https://example.com/payments/paynow/return")
    result_url      = _env("PAYNOW_RESULT_URL", "https://example.com/payments/paynow/callback")

    if not integration_id or not integration_key:
        log.error("create_paynow_payment: PAYNOW credentials not set")
        result["error"]   = "Paynow not configured"
        result["message"] = "⚠️ Paynow is not configured. Please choose EcoCash or PayPal."
        return result

    ref   = _reference(order)
    total = _total(order)
    phone = order.get("customer_phone", "")
    email = f"{phone.replace('+', '')}@wazibot.app"

    fields = {
        "id":             integration_id,
        "reference":      ref,
        "amount":         f"{total:.2f}",
        "additionalinfo": f"WaziBot {ref}",
        "returnurl":      return_url,
        "resulturl":      result_url,
        "authemail":      email,
        "status":         "Message",
    }
    fields["hash"] = _paynow_hash(fields, integration_key)

    try:
        resp = http.post(_PAYNOW_URL, data=fields, timeout=15)
        resp.raise_for_status()

        data        = _parse_paynow_response(resp.text)
        status      = data.get("status", "").lower()
        browser_url = data.get("browserurl", "")
        poll_url    = data.get("pollurl", "")

        log.debug("paynow response: %s", data)

        if status != "ok" or not browser_url:
            err = data.get("error", data.get("status", "Unknown error from Paynow"))
            raise ValueError(err)

        result["url"]      = browser_url
        result["poll_url"] = poll_url
        result["message"]  = (
            f"💳 *Paynow Payment*\n"
            f"{'─' * 30}\n"
            f"  Order  : *{ref}*\n"
            f"  Amount : *${total:.2f}*\n"
            f"{'─' * 30}\n"
            f"👆 Click to pay:\n{browser_url}\n\n"
            f"Your order is confirmed automatically after payment.\n"
            f"_Link valid for 30 minutes._"
        )
        log.info("paynow payment created  ref=%s  url=%s", ref, browser_url[:60])
        return result

    except Exception as exc:
        log.exception("create_paynow_payment failed: %s", exc)
        result["error"]   = str(exc)
        result["status"]  = "error"
        result["message"] = (
            f"⚠️ Could not create a Paynow link right now.\n"
            f"Please try *EcoCash* or *PayPal* instead, or try again shortly."
        )
        return result


def verify_paynow_callback(post_data: dict) -> dict:
    """
    Verify an incoming Paynow IPN callback (POST to /payments/paynow/callback).
    Returns: { paid, reference, status, amount, error }
    """
    integration_key = _env("PAYNOW_INTEGRATION_KEY")

    # Pop hash before verifying so it's not included in the recalculation
    incoming_hash   = post_data.pop("hash", "").upper()
    expected_hash   = _paynow_hash(post_data, integration_key)

    if incoming_hash != expected_hash:
        log.warning("paynow callback hash mismatch  incoming=%s  expected=%s",
                    incoming_hash[:16], expected_hash[:16])
        return {"paid": False, "error": "Hash mismatch", "reference": "", "amount": 0}

    status    = post_data.get("status", "").lower()
    paid      = status in ("paid", "awaiting delivery")
    reference = post_data.get("reference", "")
    amount    = float(post_data.get("amount", 0))

    log.info("paynow callback  ref=%s  status=%s  paid=%s  amount=%.2f",
             reference, status, paid, amount)
    return {"paid": paid, "reference": reference, "status": status, "amount": amount, "error": ""}


def poll_paynow_status(poll_url: str) -> dict:
    """Poll a Paynow poll URL to check live payment status."""
    if not poll_url:
        return {"paid": False, "error": "No poll URL provided"}
    try:
        resp = http.get(poll_url, timeout=10)
        resp.raise_for_status()
        data   = _parse_paynow_response(resp.text)
        status = data.get("status", "").lower()
        return {
            "paid":      status in ("paid", "awaiting delivery"),
            "status":    status,
            "reference": data.get("reference", ""),
            "amount":    float(data.get("amount", 0)),
            "error":     "",
        }
    except Exception as exc:
        log.error("poll_paynow_status error: %s", exc)
        return {"paid": False, "error": str(exc), "status": "unknown"}


# ─────────────────────────────────────────────────────────────────────────────
# 3. PAYPAL (International)
# ─────────────────────────────────────────────────────────────────────────────

def _paypal_base() -> str:
    mode = _env("PAYPAL_MODE", "sandbox")
    return (
        "https://api-m.sandbox.paypal.com"
        if mode == "sandbox"
        else "https://api-m.paypal.com"
    )


def _paypal_token() -> str:
    """Fetch a short-lived OAuth2 access token from PayPal."""
    client_id = _env("PAYPAL_CLIENT_ID")
    secret    = _env("PAYPAL_SECRET")

    if not client_id or not secret:
        raise ValueError("PAYPAL_CLIENT_ID / PAYPAL_SECRET not set")

    creds = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    resp  = http.post(
        f"{_paypal_base()}/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials",
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError("PayPal did not return an access token")
    return token


def create_paypal_payment(order: dict) -> dict:
    """
    Create a PayPal checkout order and return the user approval URL.
    Result 'poll_url' carries the PayPal order ID for capture later.
    """
    result = _empty_result("paypal", order)
    ref    = _reference(order)
    total  = _total(order)

    return_url = _env("PAYPAL_RETURN_URL", "https://example.com/payments/paypal/success")
    cancel_url = _env("PAYPAL_CANCEL_URL", "https://example.com/payments/paypal/cancel")

    try:
        token = _paypal_token()

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": ref,
                "description":  f"WaziBot {ref}",
                "amount": {
                    "currency_code": "USD",
                    "value":         f"{total:.2f}",
                },
            }],
            "application_context": {
                "return_url":          return_url + f"?reference={ref}",
                "cancel_url":          cancel_url + f"?reference={ref}",
                "brand_name":          order.get("business_name", "WaziBot"),
                "landing_page":        "BILLING",
                "user_action":         "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
            },
        }

        resp = http.post(
            f"{_paypal_base()}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        paypal_order_id = data.get("id", "")
        approval_url = next(
            (lnk["href"] for lnk in data.get("links", []) if lnk.get("rel") == "approve"),
            "",
        )

        if not approval_url:
            raise ValueError("PayPal did not return an approval URL")

        result["url"]      = approval_url
        result["poll_url"] = paypal_order_id      # stored for capture step
        result["message"]  = (
            f"🌍 *PayPal Payment*\n"
            f"{'─' * 30}\n"
            f"  Order  : *{ref}*\n"
            f"  Amount : *${total:.2f} USD*\n"
            f"{'─' * 30}\n"
            f"👆 Pay securely via PayPal:\n{approval_url}\n\n"
            f"Order confirmed automatically after payment.\n"
            f"_Cards, PayPal balance & Buy Now Pay Later accepted._"
        )
        log.info("paypal payment created  ref=%s  paypal_id=%s", ref, paypal_order_id)
        return result

    except Exception as exc:
        log.exception("create_paypal_payment failed: %s", exc)
        result["error"]   = str(exc)
        result["status"]  = "error"
        result["message"] = (
            "⚠️ Could not create a PayPal link right now.\n"
            "Please try *EcoCash* or *Paynow* instead, or try again shortly."
        )
        return result


def capture_paypal_order(paypal_order_id: str) -> dict:
    """
    Capture an approved PayPal order.
    Call this from the /payments/paypal/success endpoint after user approves.
    Returns: { paid, reference, amount, status, error }
    """
    try:
        token = _paypal_token()
        resp  = http.post(
            f"{_paypal_base()}/v2/checkout/orders/{paypal_order_id}/capture",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data   = resp.json()
        status = data.get("status", "")
        paid   = (status == "COMPLETED")

        pu       = (data.get("purchase_units") or [{}])[0]
        ref      = pu.get("reference_id", "")
        captures = pu.get("payments", {}).get("captures", [{}])
        amount   = float((captures[0] if captures else {}).get("amount", {}).get("value", 0))

        log.info("paypal capture  paypal_id=%s  paid=%s  ref=%s  amount=%.2f",
                 paypal_order_id, paid, ref, amount)
        return {"paid": paid, "reference": ref, "amount": amount, "status": status, "error": ""}

    except Exception as exc:
        log.exception("capture_paypal_order failed: %s", exc)
        return {"paid": False, "reference": "", "amount": 0, "error": str(exc), "status": "error"}
