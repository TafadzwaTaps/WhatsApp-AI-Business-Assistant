"""
routes/expansion_routes.py — Business Expansion Endpoints
(Phases 1-9: bookings, calendar, trial, referrals, affiliates, marketing)

All routes are ADDITIVE — no existing endpoints modified.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, validator

import crud
from core.auth import require_business, require_superadmin

log = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 — SERVICE BUSINESS MODE
# ─────────────────────────────────────────────────────────────────────────────

class ServiceModeUpdate(BaseModel):
    is_service_business: bool
    default_slot_mins:   int  = 60
    booking_lead_hrs:    int  = 1
    working_hours_start: str  = "08:00"
    working_hours_end:   str  = "17:00"


@router.patch("/me/service-mode")
def update_service_mode(data: ServiceModeUpdate, user=Depends(require_business)):
    """Enable or disable service/booking mode for this business."""
    bid = user["business_id"]
    class _D:
        def dict(self, **_):
            return {
                "is_service_business": data.is_service_business,
                "default_slot_mins":   data.default_slot_mins,
                "booking_lead_hrs":    data.booking_lead_hrs,
                "working_hours_start": data.working_hours_start,
                "working_hours_end":   data.working_hours_end,
            }
    b = crud.update_business(bid, _D())
    if not b: raise HTTPException(500, "Update failed")
    return {
        "ok": True,
        "is_service_business": b.get("is_service_business"),
        "message": "Service mode enabled ✓" if data.is_service_business else "Service mode disabled",
    }


@router.get("/me/service-mode")
def get_service_mode(user=Depends(require_business)):
    """Return current service mode configuration."""
    b = crud.get_business_by_id(user["business_id"])
    if not b: raise HTTPException(404, "Business not found")
    return {
        "is_service_business": bool(b.get("is_service_business", False)),
        "default_slot_mins":   b.get("default_slot_mins", 60),
        "booking_lead_hrs":    b.get("booking_lead_hrs", 1),
        "working_hours_start": b.get("working_hours_start", "08:00"),
        "working_hours_end":   b.get("working_hours_end", "17:00"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHASES 2-3 — BOOKING MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

class BookingCreate(BaseModel):
    customer_phone: str
    booking_date:   str   # YYYY-MM-DD
    start_time:     str   # HH:MM
    duration_hrs:   float = 1.0
    service_name:   str   = ""
    notes:          str   = ""

    @validator("booking_date")
    def date_valid(cls, v):
        try: datetime.strptime(v, "%Y-%m-%d")
        except: raise ValueError("booking_date must be YYYY-MM-DD")
        return v

    @validator("start_time")
    def time_valid(cls, v):
        parts = v.split(":")
        if len(parts) != 2: raise ValueError("start_time must be HH:MM")
        return v


@router.get("/bookings")
def list_bookings(
    upcoming_only: bool = True,
    date:          Optional[str] = None,   # filter by date YYYY-MM-DD
    user=Depends(require_business),
):
    """List all bookings for this business."""
    from services.booking_service import get_bookings
    bookings = get_bookings(user["business_id"], upcoming_only=upcoming_only)
    if date:
        bookings = [b for b in bookings if b.get("booking_date") == date]
    return {"count": len(bookings), "bookings": bookings}


@router.post("/bookings", status_code=201)
def create_booking_api(data: BookingCreate, user=Depends(require_business)):
    """Manually create a booking from the dashboard."""
    from services.booking_service import create_booking, check_availability
    bid = user["business_id"]

    avail = check_availability(bid, data.booking_date, data.start_time, data.duration_hrs)
    if not avail["available"]:
        raise HTTPException(409, f"Slot not available: {avail['reason']}")

    booking = create_booking(
        business_id=bid, customer_phone=data.customer_phone,
        booking_date=data.booking_date, start_time=data.start_time,
        duration_hrs=data.duration_hrs, service_name=data.service_name,
        notes=data.notes,
    )
    if not booking: raise HTTPException(500, "Failed to create booking")

    # Optional: send WhatsApp confirmation
    return {"ok": True, "booking": booking}


@router.patch("/bookings/{booking_id}/status")
def update_booking_status(
    booking_id: int,
    status:     str,
    user=Depends(require_business),
):
    """Update booking status: confirmed | completed | cancelled | rescheduled"""
    valid = {"confirmed", "completed", "cancelled", "rescheduled", "pending"}
    if status not in valid:
        raise HTTPException(400, f"status must be one of {valid}")
    try:
        from core.db import supabase
        res = (
            supabase.table("bookings").update({"status": status})
            .eq("id", booking_id).eq("business_id", user["business_id"])
            .execute()
        )
        if not res.data: raise HTTPException(404, "Booking not found")
        return {"ok": True, "booking": res.data[0]}
    except HTTPException: raise
    except Exception as exc: raise HTTPException(500, str(exc))


@router.delete("/bookings/{booking_id}")
def cancel_booking_api(booking_id: int, user=Depends(require_business)):
    """Cancel a booking."""
    from services.booking_service import cancel_booking
    result = cancel_booking(booking_id, user["business_id"])
    if not result: raise HTTPException(404, "Booking not found")
    return {"ok": True, "cancelled": booking_id}


@router.post("/bookings/reminders/run")
async def run_booking_reminders(user=Depends(require_business)):
    """
    Send WhatsApp reminders for upcoming bookings.
    Call from Render cron or dashboard.
    Sends for bookings within next 24h (not already reminded).
    """
    from services.booking_service import get_upcoming_reminders, format_reminder_message
    from core.db import supabase
    bid = user["business_id"]
    biz = crud.get_business_by_id(bid)
    if not biz: raise HTTPException(404, "Business not found")
    biz_name = biz.get("name", "")

    reminders = get_upcoming_reminders(bid, window_hours=24.5)
    sent, skipped = 0, 0

    # Runtime send_whatsapp — imported from routes context
    from routes.webhook_routes import send_whatsapp
    if not send_whatsapp:
        raise HTTPException(503, "WhatsApp sender not initialised")

    try:
        token    = crud.get_decrypted_token(biz)
        phone_id = biz.get("whatsapp_phone_id", "")
    except Exception:
        token, phone_id = "", ""

    import os
    if not token or not phone_id:
        phone_id = os.getenv("SHARED_PHONE_NUMBER_ID", "").strip()
        token    = os.getenv("SHARED_WA_TOKEN", "").strip()

    if not token or not phone_id:
        return {"ok": False, "error": "No WhatsApp credentials"}

    for b in reminders:
        if b.get("reminder_24h_sent"): skipped += 1; continue
        phone = b.get("customer_phone", "")
        if not phone: continue

        msg = format_reminder_message(b, biz_name)
        result = send_whatsapp(phone_id, token, phone, msg)
        if "error" not in result:
            supabase.table("bookings").update({"reminder_24h_sent": True}).eq("id", b["id"]).execute()
            sent += 1
        else:
            skipped += 1

    return {"ok": True, "sent": sent, "skipped": skipped}


@router.get("/bookings/availability")
def check_slot_availability(
    booking_date: str  = Query(..., description="YYYY-MM-DD"),
    start_time:   str  = Query(..., description="HH:MM"),
    duration_hrs: float = 1.0,
    user=Depends(require_business),
):
    """Check if a slot is available."""
    from services.booking_service import check_availability
    return check_availability(user["business_id"], booking_date, start_time, duration_hrs)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4 — CALENDAR STATUS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/calendar/status")
def calendar_status(user=Depends(require_business)):
    """Return Google Calendar integration status."""
    from services.calendar_service import gcal_status
    return gcal_status()


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 6 — TRIAL MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me/trial")
def get_trial(user=Depends(require_business)):
    """Return trial status for the current business."""
    from services.growth_service import get_trial_status
    biz = crud.get_business_by_id(user["business_id"])
    if not biz: raise HTTPException(404, "Business not found")
    return get_trial_status(biz)


@router.post("/me/trial/start")
def start_trial_endpoint(user=Depends(require_business)):
    """
    Initialise trial. Called automatically at signup.
    Safe to call multiple times — won't reset an existing trial.
    """
    from services.growth_service import start_trial
    return start_trial(user["business_id"])


@router.post("/admin/trials/reminders")
async def send_trial_reminders(_=Depends(require_superadmin)):
    """
    Send trial expiry reminders. Call from Render cron daily.
    Sends to businesses at 7d, 3d, 1d, and expired milestones.
    """
    from services.growth_service import get_trial_reminders_due
    due = get_trial_reminders_due()

    MESSAGES = {
        "7d":      "📬 Your WaziBot free trial has 7 days remaining! Reply UPGRADE to continue after your trial.",
        "3d":      "⚠️ 3 days left in your WaziBot trial. Upgrade now to keep your business running seamlessly.",
        "1d":      "🔴 Last day of your WaziBot trial! Upgrade today to avoid any interruption.",
        "expired": "⏰ Your WaziBot trial has expired. Upgrade to continue automating your WhatsApp business.",
    }

    sent = 0
    for row in due:
        phone = row.get("contact_phone", "").strip()
        tier  = row.get("reminder_tier", "")
        if not phone or tier not in MESSAGES: continue
        msg = MESSAGES[tier]
        # Best effort — no hard fail
        try:
            import os
            from services.whatsapp_service import send_message
            send_message(phone, msg)
            sent += 1
        except Exception as exc:
            log.warning("trial reminder send failed: %s", exc)

    return {"ok": True, "due": len(due), "sent": sent}


# ─────────────────────────────────────────────────────────────────────────────
# PHASES 7-9 — REFERRAL, AFFILIATE, COMMISSIONS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me/referral")
def get_referral(user=Depends(require_business)):
    """Get referral code, link, and stats for this business."""
    from services.growth_service import get_referral_stats
    return get_referral_stats(user["business_id"])


# ── Referral credit withdrawal ────────────────────────────────────────────────

class WithdrawRequest(BaseModel):
    paypal_email:  str
    amount:        float  # must be >= 5.00

@router.get("/me/referral/balance")
def get_referral_balance(user=Depends(require_business)):
    """Return current referral credit balance for this business."""
    from services.growth_service import get_referral_stats
    stats = get_referral_stats(user["business_id"])
    return {
        "available_balance":   stats.get("available_balance", 0.0),
        "pending_balance":     stats.get("pending_balance",   0.0),
        "total_withdrawn":     stats.get("total_withdrawn",   0.0),
        "can_withdraw":        stats.get("can_withdraw",      False),
        "min_withdrawal":      5.00,
        "credit_per_referral": 0.20,
    }


@router.post("/me/referral/withdraw")
def request_referral_withdrawal(body: WithdrawRequest, user=Depends(require_business)):
    """
    Request a PayPal payout of referral credits.
    Rules:
      - Minimum withdrawal: $5.00
      - Only available credits (not pending) can be withdrawn
      - Payouts are processed manually within 3-5 business days
    """
    from core.db import supabase
    from services.growth_service import get_referral_stats

    bid = user["business_id"]

    # Validate email
    if not body.paypal_email or "@" not in body.paypal_email:
        raise HTTPException(400, "Valid PayPal email required")

    # Validate amount
    if body.amount < 5.00:
        raise HTTPException(400, f"Minimum withdrawal is $5.00. You requested ${body.amount:.2f}.")

    # Check available balance
    stats = get_referral_stats(bid)
    available = stats.get("available_balance", 0.0)

    if available < 5.00:
        raise HTTPException(400,
            f"Insufficient balance. Available: ${available:.2f}. Minimum: $5.00. "
            f"Keep referring to build your balance — each referral earns you $0.20.")

    if body.amount > available:
        raise HTTPException(400,
            f"Requested ${body.amount:.2f} exceeds available balance of ${available:.2f}.")

    try:
        from datetime import datetime, timezone as tz
        now = datetime.now(tz.utc).isoformat()

        # Create withdrawal request
        withdrawal = supabase.table("referral_withdrawals").insert({
            "business_id":  bid,
            "amount":       round(body.amount, 2),
            "paypal_email": body.paypal_email.strip().lower(),
            "status":       "pending",
            "requested_at": now,
            "admin_note":   None,
        }).execute()

        # Mark credits as withdrawn (up to the requested amount)
        credits_res = (
            supabase.table("referral_credits")
            .select("id, amount")
            .eq("business_id", bid)
            .eq("status", "available")
            .order("created_at", desc=False)
            .execute()
        )
        remaining = round(body.amount, 2)
        for credit in (credits_res.data or []):
            if remaining <= 0:
                break
            credit_amt = float(credit.get("amount", 0))
            if credit_amt <= remaining:
                supabase.table("referral_credits").update(
                    {"status": "withdrawn", "withdrawn_at": now}
                ).eq("id", credit["id"]).execute()
                remaining -= credit_amt
            else:
                # Partial — split not needed at $0.20 granularity, just mark whole credit
                supabase.table("referral_credits").update(
                    {"status": "withdrawn", "withdrawn_at": now}
                ).eq("id", credit["id"]).execute()
                remaining = 0

        log.info("Withdrawal requested  business=%s  amount=%.2f  paypal=%s",
                 bid, body.amount, body.paypal_email)

        return {
            "ok":           True,
            "amount":       round(body.amount, 2),
            "paypal_email": body.paypal_email.strip().lower(),
            "status":       "pending",
            "message":      f"Withdrawal of ${body.amount:.2f} requested. "
                            f"We'll send it to {body.paypal_email} within 3-5 business days.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.error("referral withdrawal error  business=%s  error=%s", bid, exc)
        raise HTTPException(500, "Failed to process withdrawal request. Please try again.")


@router.post("/affiliates", status_code=201)
def create_affiliate_endpoint(
    name:           str,
    email:          str,
    affiliate_type: str   = "creator",
    commission_pct: float = 15.0,
    _=Depends(require_superadmin),
):
    """Register a new affiliate (creator, coach, consultant, influencer)."""
    from services.growth_service import create_affiliate
    aff = create_affiliate(name, email, affiliate_type, commission_pct)
    if not aff: raise HTTPException(500, "Failed to create affiliate")
    return {"ok": True, "affiliate": aff}


@router.get("/affiliates/{affiliate_id}/stats")
def affiliate_stats(affiliate_id: int, _=Depends(require_superadmin)):
    """Return performance stats for an affiliate."""
    from services.growth_service import get_affiliate_stats
    return get_affiliate_stats(affiliate_id)


@router.patch("/admin/commissions/{commission_id}")
def update_commission(
    commission_id: int,
    status:        str,
    _=Depends(require_superadmin),
):
    """Approve or mark as paid a commission."""
    from services.growth_service import update_commission_status
    result = update_commission_status(commission_id, status)
    if not result: raise HTTPException(404, f"Commission {commission_id} not found")
    return {"ok": True, "commission": result}


# ─────────────────────────────────────────────────────────────────────────────
# PHASES 10-11 — MARKETING CONTENT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/marketing/copy")
def get_marketing_copy(
    business_type: str = "general",
    tone:          str = "friendly",
    user=Depends(require_business),
):
    """Get all marketing copy variations for this business type."""
    from services.marketing_service import get_all_copy_variations
    from services.growth_service    import get_referral_stats

    bid      = user["business_id"]
    biz      = crud.get_business_by_id(bid)
    biz_name = biz.get("name", "") if biz else ""
    category = (biz.get("category") or business_type).lower()

    try:
        ref = get_referral_stats(bid)
        referral_code = ref.get("referral_code", "")
        referral_link = ref.get("referral_link", "")
    except Exception:
        referral_code, referral_link = "", ""

    return get_all_copy_variations(
        business_type=category,
        referral_code=referral_code,
        referral_link=referral_link,
        business_name=biz_name,
    )


@router.get("/marketing/launch")
def get_launch_copy(user=Depends(require_business)):
    """Get a launch announcement message for the business to send to their customers."""
    from services.marketing_service import generate_launch_copy
    biz      = crud.get_business_by_id(user["business_id"])
    biz_name = biz.get("name", "Our Business") if biz else "Our Business"
    category = (biz.get("category") or "general").lower() if biz else "general"
    return generate_launch_copy(biz_name, category)


@router.get("/marketing/referral-message")
def get_referral_message(user=Depends(require_business)):
    """Get a ready-to-share referral message for the business owner."""
    from services.marketing_service import generate_referral_copy
    from services.growth_service    import get_referral_stats

    bid   = user["business_id"]
    biz   = crud.get_business_by_id(bid)
    biz_name = biz.get("name", "") if biz else ""
    ref  = get_referral_stats(bid)
    return generate_referral_copy(
        referral_code=ref.get("referral_code", ""),
        referral_link=ref.get("referral_link", ""),
        business_name=biz_name,
    )
