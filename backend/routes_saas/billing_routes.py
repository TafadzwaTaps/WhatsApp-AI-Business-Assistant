"""
routes/billing_routes.py — WaziBot Stripe Billing API

Endpoints:
  GET  /billing/status            — current tier + features for logged-in business
  POST /billing/checkout          — create Stripe Checkout Session
  POST /billing/cancel            — cancel subscription at period end
  GET  /billing/tiers             — list all available tiers + pricing
  POST /billing/webhook           — Stripe webhook receiver (public, sig-verified)
  GET  /billing/success           — landing page after successful checkout
  GET  /billing/cancel-page       — landing page after cancelled checkout

None of these routes modify existing WhatsApp flows, orders, or AI behaviour.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
import logging

log = logging.getLogger(__name__)
router = APIRouter()



# ── Pydantic models ───────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    tier:           str              # "pro" | "business" | "enterprise"
    billing_period: str = "monthly"  # "monthly" | "annual"
    success_url:    Optional[str] = None
    cancel_url:     Optional[str] = None


class CancelRequest(BaseModel):
    confirm: bool = False


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/billing/status")
def billing_status(user=Depends(require_business)):
    """Return current subscription tier and feature flags for this business."""
    from billing.stripe_service import get_subscription_status
    return get_subscription_status(user["business_id"])


@router.get("/billing/tiers")
def billing_tiers():
    """Return all subscription tiers and their pricing — public endpoint."""
    from billing.stripe_service import TIERS
    # Exclude internal stripe_price_id fields from public response
    public_tiers = {}
    for k, v in TIERS.items():
        public_tiers[k] = {f: val for f, val in v.items()
                           if not f.startswith("stripe_price_id")}
    return public_tiers


@router.post("/billing/checkout")
def billing_checkout(body: CheckoutRequest, user=Depends(require_business)):
    """
    Create a Stripe Checkout Session for upgrading to a paid tier.
    Returns {"url": "https://checkout.stripe.com/..."} for the frontend to redirect to.
    """
    from billing.stripe_service import create_checkout_session
    import crud
    # Get business owner email for pre-filling Stripe checkout
    try:
        biz   = crud.get_business_by_id(user["business_id"])
        email = biz.get("owner_email", "") if biz else ""
    except Exception:
        email = ""

    result = create_checkout_session(
        business_id    = user["business_id"],
        tier           = body.tier,
        billing_period = body.billing_period,
        success_url    = body.success_url or "",
        cancel_url     = body.cancel_url  or "",
        customer_email = email,
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/billing/cancel")
def billing_cancel(body: CancelRequest, user=Depends(require_business)):
    """Cancel the business's Stripe subscription at end of current period."""
    if not body.confirm:
        raise HTTPException(400, "Set confirm=true to cancel your subscription")
    from billing.stripe_service import cancel_subscription
    result = cancel_subscription(user["business_id"])
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/billing/portal")
def billing_portal(user=Depends(require_business)):
    """Create a Stripe Customer Portal session so users can manage their subscription,
    update payment method, view invoices — all without leaving WaziBot."""
    from billing.stripe_service import _stripe, get_subscription_status
    import os
    stripe = _stripe()
    if not stripe:
        raise HTTPException(503, "Stripe not configured on this server")
    status = get_subscription_status(user["business_id"])
    customer_id = status.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(400, "No Stripe customer found — upgrade first to create one")
    base = os.getenv("WAZIBOT_URL", "https://wazibot-api-assistant.onrender.com")
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{base}/static/dashboard.html",
        )
        return {"url": session.url}
    except Exception as exc:
        log.error("Stripe portal error: %s", exc)
        raise HTTPException(500, str(exc))


@router.post("/billing/webhook")
async def stripe_webhook(request: Request):
    """
    Stripe webhook endpoint — receives events from Stripe.
    Signature is verified inside handle_stripe_webhook() using STRIPE_WEBHOOK_SECRET.
    This endpoint MUST receive the raw request body (not parsed JSON).
    """
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    from billing.stripe_service import handle_stripe_webhook
    result = handle_stripe_webhook(payload, sig_header)

    if result.get("status") == 400 or "error" in result:
        raise HTTPException(result.get("status", 400), result.get("error", "Webhook error"))
    return {"received": True}


@router.get("/billing/success")
def billing_success(session_id: str = ""):
    """
    Landing page after successful Stripe checkout.
    Returns JSON — the frontend dashboard should redirect here and show a success toast.
    """
    return {
        "ok":         True,
        "message":    "Subscription activated! Your plan has been upgraded.",
        "session_id": session_id,
    }


@router.get("/billing/cancel-page")
def billing_cancel_page():
    """Landing page after user cancels out of Stripe checkout."""
    return {"ok": False, "message": "Checkout cancelled. Your plan was not changed."}
