"""
routes/admin_routes.py — Superadmin, debug, order lifecycle, and platform endpoints.

Routes: /debug/*, /admin/*, /orders/{id}/lifecycle, /orders/{id}/approve-payment,
        /orders/{id}/reject-proof, /orders/{id}/cancel, /orders/{id}/refund,
        /orders/{id}/status, /platform/*
"""

import os
import logging
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

import crud
from core.auth import (
    require_superadmin, require_business, get_current_user,
)
from core.crypto import TokenDecryptionError
from workflows.order_lifecycle import (
    update_order_status_supabase, get_order,
    format_order_status, get_progress_bar, next_order_stage,
)

log = logging.getLogger("wazibot")
router = APIRouter()

# Runtime config — injected by main.py
send_whatsapp        = None
WHATSAPP_APP_SECRET  = ""
SHARED_PHONE_NUMBER_ID = ""


# ── Debug endpoints ───────────────────────────────────────────────────────────

@router.get("/debug/env")
def debug_env():
    fernet_key = os.getenv("FERNET_KEY", "")
    secret_key = os.getenv("SECRET_KEY", "")
    supa_url   = os.getenv("SUPABASE_URL", "")
    supa_key   = os.getenv("SUPABASE_KEY", "")
    return {
        "FERNET_KEY":   f"{fernet_key[:8]}…({len(fernet_key)} chars)" if fernet_key else "❌ NOT SET",
        "SECRET_KEY":   f"{secret_key[:4]}…({len(secret_key)} chars)" if secret_key else "❌ NOT SET",
        "SUPABASE_URL": f"{supa_url[:30]}…" if supa_url else "❌ NOT SET",
        "SUPABASE_KEY": f"{supa_key[:8]}…({len(supa_key)} chars)" if supa_key else "❌ NOT SET",
        "VERIFY_TOKEN": "✅ set" if os.getenv("VERIFY_TOKEN") else "⚠ using default",
    }


@router.get("/debug/security")
def debug_security():
    return {
        "webhook_signature_verification": "enabled" if WHATSAPP_APP_SECRET else "disabled (WHATSAPP_APP_SECRET not set)",
        "rate_limiting":   "enabled",
        "security_headers": "enabled",
        "password_policy": "min 8 chars, letter+digit required",
        "note": "Set WHATSAPP_APP_SECRET in Render env vars to enable webhook signature verification",
    }


@router.get("/debug/supabase")
def debug_supabase():
    import re
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    issues = []
    if not url: issues.append("SUPABASE_URL is not set")
    elif "xxxx" in url.lower(): issues.append(f"SUPABASE_URL has placeholder: {url!r}")
    elif not url.startswith("https://"): issues.append(f"SUPABASE_URL must start with https://")
    elif not re.search(r"https://[a-z0-9]+\.supabase\.co", url.rstrip("/")): issues.append(f"SUPABASE_URL format invalid")
    if not key: issues.append("SUPABASE_KEY is not set")
    elif not key.startswith("eyJ"): issues.append(f"SUPABASE_KEY doesn't look like a JWT")
    if issues:
        return {"ok": False, "issues": issues,
                "action": "Fix in Render → Environment, then redeploy."}
    try:
        from core.db import supabase as _sb
        res = _sb.table("businesses").select("id").limit(1).execute()
        return {"ok": True, "message": "Supabase connection working ✅",
                "url": url[:50] + "…", "rows": len(res.data)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url[:50] + "…",
                "action": "Check Supabase project is active at https://supabase.com/dashboard"}


@router.get("/debug/token")
def debug_token():
    from core.crypto import encrypt_token, decrypt_token
    from fastapi.responses import JSONResponse
    test = "wazibot-test-12345"
    try:
        ct = encrypt_token(test)
        pt = decrypt_token(ct)
        ok = pt == test
        return {"ok": ok, "ciphertext_prefix": ct[:12] + "…",
                "round_trip": "✅ PASS" if ok else "❌ FAIL"}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@router.get("/debug/webhook")
def debug_webhook(user=Depends(require_business)):
    import requests as http_requests
    bid      = user["business_id"]
    business = crud.get_business_by_id(bid)
    if not business:
        return {"ok": False, "step": 1, "error": "Business not found"}
    steps = {
        "business_id": bid, "business_name": business.get("name"),
        "whatsapp_phone_id": business.get("whatsapp_phone_id"),
        "has_phone_id": bool(business.get("whatsapp_phone_id")),
        "has_token_stored": bool(business.get("whatsapp_token")),
    }
    token = ""
    try:
        token = crud.get_decrypted_token(business)
        steps["token_decrypts"] = bool(token)
        steps["token_tail"]     = "…" + token[-6:] if token else "empty"
    except Exception as exc:
        steps["token_decrypts"] = False
        steps["token_error"]    = str(exc)
    try:
        products = crud.get_products(bid)
        steps["products_count"] = len(products)
    except Exception as exc:
        steps["products_count"] = 0
        steps["products_error"] = str(exc)
    if token and business.get("whatsapp_phone_id"):
        try:
            resp = http_requests.get(
                f"https://graph.facebook.com/v18.0/{business['whatsapp_phone_id']}",
                headers={"Authorization": f"Bearer {token}"}, timeout=5,
            )
            steps["whatsapp_api_status"] = resp.status_code
            steps["whatsapp_api_ok"]     = resp.status_code == 200
        except Exception as exc:
            steps["whatsapp_api_ok"]    = False
            steps["whatsapp_api_error"] = str(exc)
    steps["overall"] = "✅ ALL GOOD" if all([
        steps["has_phone_id"], steps.get("token_decrypts"),
        steps.get("products_count", 0) > 0, steps.get("whatsapp_api_ok"),
    ]) else "❌ FIX ISSUES ABOVE"
    return steps


@router.get("/debug/schema")
def debug_schema(user=Depends(require_business)):
    from workflows.order_lifecycle import _get_orders_columns, _invalidate_column_cache
    _invalidate_column_cache()
    cols     = _get_orders_columns()
    optional = ["items", "payment_status", "payment_reference"]
    return {
        "all_columns":      sorted(cols),
        "optional_columns": {c: (c in cols) for c in optional},
        "migration_needed": not all(c in cols for c in optional),
        "action": "✅ Schema up to date" if all(c in cols for c in optional) else "⚠️ Run MIGRATION.sql",
    }


# ── Superadmin ────────────────────────────────────────────────────────────────

@router.get("/admin/businesses")
def list_businesses(_=Depends(require_superadmin)):
    return crud.get_all_businesses()


@router.patch("/admin/businesses/{business_id}")
def admin_update_business(business_id: int, data: dict, _=Depends(require_superadmin)):
    class _D:
        def dict(self, **_): return data
    b = crud.update_business(business_id, _D())
    if not b: raise HTTPException(404, "Business not found")
    return b


@router.delete("/admin/businesses/{business_id}")
def admin_delete_business(business_id: int, _=Depends(require_superadmin)):
    b = crud.delete_business(business_id)
    if not b: raise HTTPException(404, "Business not found")
    return {"deleted": business_id}


@router.get("/admin/stats")
def admin_stats(_=Depends(require_superadmin)):
    return crud.get_admin_stats()


# ── Order lifecycle ───────────────────────────────────────────────────────────

_LIFECYCLE_MESSAGES = {
    "preparing": "👨‍🍳 *Your order is being prepared!*\n\n📦 Order: *{ref}*\n\nWe're working on it now. Estimated: *{eta}*\n\n_We'll let you know when it's ready! 😊_",
    "ready": "🎉 *Your order is ready!*\n\n📦 Order: *{ref}*\n\nReady for pickup or dispatch. 🙌",
    "out_for_delivery": "🛵 *Your order is on the way!*\n\n📦 Order: *{ref}*\n\n{eta}\n_Please be available to receive your order. 😊_",
    "delivered": "✅ *Order delivered!*\n\n📦 Order: *{ref}*\n\nThank you for ordering from *{biz}*! 🙏\n\n_Type *menu* to order again._",
    "completed": "✅ *Order completed!*\n\n📦 Order: *{ref}*\n\nThank you from *{biz}*! 🙏\n\n_Type *menu* to order again._",
}


class OrderLifecycleUpdate(BaseModel):
    order_id:          int
    status:            str
    message:           Optional[str] = None
    estimated_minutes: Optional[int] = None


@router.post("/orders/{order_id}/lifecycle")
async def push_lifecycle_update(
    order_id: int,
    data: OrderLifecycleUpdate,
    user=Depends(require_business),
):
    bid   = user["business_id"]
    order = get_order(order_id)
    if not order: raise HTTPException(404, f"Order {order_id} not found")
    if order.get("business_id") != bid: raise HTTPException(403, "Access denied")

    new_status = data.status.lower().strip()
    valid = {"preparing", "ready", "out_for_delivery", "delivered", "completed"}
    if new_status not in valid:
        raise HTTPException(422, f"Invalid status. Valid: {sorted(valid)}")

    try: crud.update_order_payment(order_id, bid, {"payment_status": "paid"})
    except Exception: pass

    status_map = {
        "preparing": "confirmed", "ready": "confirmed",
        "out_for_delivery": "confirmed", "delivered": "delivered", "completed": "delivered",
    }
    try: update_order_status_supabase(order_id, status_map.get(new_status, "confirmed"))
    except Exception as exc: log.warning("lifecycle: db update failed: %s", exc)

    ref      = f"ORDER-{order_id}"
    biz_name = ""
    try:
        biz_row  = crud.get_business_by_id(bid)
        biz_name = biz_row.get("name", "") if biz_row else ""
    except Exception: pass

    if data.message:
        customer_msg = data.message
    else:
        template = _LIFECYCLE_MESSAGES.get(new_status, "📦 Order *{ref}* status updated.")
        eta_text = f"*{data.estimated_minutes} minutes*" if data.estimated_minutes and new_status == "preparing" else ("*shortly*" if new_status == "preparing" else (f"Estimated: *{data.estimated_minutes} min* ⏱\n" if data.estimated_minutes else ""))
        customer_msg = template.format(ref=ref, biz=biz_name, eta=eta_text)

    phone     = order.get("customer_phone", "")
    wa_result = {}
    if phone:
        try:
            biz_row  = crud.get_business_by_id(bid)
            token    = crud.get_decrypted_token(biz_row) if biz_row else ""
            phone_id = biz_row.get("whatsapp_phone_id", "") if biz_row else ""
            if token and phone_id:
                wa_result = send_whatsapp(phone_id, token, phone, customer_msg)
        except Exception as exc:
            wa_result = {"error": str(exc)}

    if new_status in ("delivered", "completed") and phone:
        try:
            from services._ai_state import _set_survey_state
            _set_survey_state(phone, bid)
        except Exception as exc:
            log.warning("lifecycle: survey trigger failed: %s", exc)

    return {"ok": True, "order_id": order_id, "new_status": new_status,
            "customer_notified": bool(phone and not wa_result.get("error")),
            "message_sent": customer_msg[:80] + "..." if len(customer_msg) > 80 else customer_msg}


class OrderAdminAction(BaseModel):
    note: Optional[str] = None


async def _notify_customer_payment(order: dict, message: str) -> None:
    if not order: return
    biz_id = order.get("business_id")
    phone  = order.get("customer_phone", "")
    if not biz_id or not phone: return
    try:
        business = crud.get_business_by_id(biz_id)
        if not business: return
        token    = crud.get_decrypted_token(business)
        phone_id = business.get("whatsapp_phone_id")
        if token and phone_id:
            send_whatsapp(phone_id, token, phone, message)
    except Exception as exc:
        log.error("_notify_customer_payment error: %s", exc)


@router.post("/orders/{order_id}/approve-payment")
async def admin_approve_payment(order_id: int, data: OrderAdminAction, user=Depends(require_business)):
    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")

    crud.update_order_payment(order_id, bid, {"payment_status": "paid"})
    try: update_order_status_supabase(order_id, "confirmed")
    except Exception as exc: log.warning("approve_payment: status update failed: %s", exc)

    ref   = f"ORDER-{order_id}"
    total = float(order.get("total_price") or 0)
    note_line = f"\n_Note: {data.note}_" if data.note else ""
    await _notify_customer_payment(order,
        f"✅ *Payment Confirmed!*\n\nYour payment for *{ref}* has been verified.\n\n"
        f"💰 Amount: ${total:.2f}\n📍 Status: *CONFIRMED*{note_line}\n\nWe're now preparing your order! 🙌"
    )
    phone = order.get("customer_phone", "")
    if phone and not order.get("fulfillment_method"):
        try:
            from services._ai_state import _set_awaiting_fulfillment, _get_state
            if _get_state(phone, bid) == "browsing":
                _set_awaiting_fulfillment(phone, bid, order_id=order_id, reference=ref)
                biz      = crud.get_business_by_id(bid)
                token    = crud.get_decrypted_token(biz) if biz else ""
                phone_id = biz.get("whatsapp_phone_id", "") if biz else ""
                if token and phone_id:
                    send_whatsapp(phone_id, token, phone,
                        f"🚚 *One more step!*\n\nHow would you like to receive *{ref}*?\n\n"
                        f"  1️⃣  *Delivery* — we bring it to you\n  2️⃣  *Pickup* — collect from us\n\n"
                        f"_Reply *1* or *delivery* / *2* or *pickup*_"
                    )
        except Exception as exc:
            log.warning("approve_payment: fulfillment trigger failed: %s", exc)

    return {"ok": True, "order_id": order_id, "status": "confirmed",
            "message": f"Payment for {ref} approved and customer notified."}


@router.post("/orders/{order_id}/reject-proof")
async def admin_reject_proof(order_id: int, data: OrderAdminAction, user=Depends(require_business)):
    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")
    crud.update_order_payment(order_id, bid, {"payment_status": "awaiting_payment"})
    ref    = f"ORDER-{order_id}"
    reason = data.note or "The proof was unclear or did not match the order amount."
    await _notify_customer_payment(order,
        f"⚠️ *Payment Proof Not Accepted*\n\nWe could not verify your payment for *{ref}*.\n\n"
        f"Reason: _{reason}_\n\nPlease send a clearer screenshot or the correct transaction ID.\n"
        f"_Reply *paid* to submit new proof._"
    )
    return {"ok": True, "order_id": order_id, "message": "Proof rejected, customer notified."}


@router.post("/orders/{order_id}/cancel")
async def admin_cancel_order(order_id: int, data: OrderAdminAction, user=Depends(require_business)):
    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")
    try: update_order_status_supabase(order_id, "cancelled")
    except Exception as exc: raise HTTPException(422, str(exc))
    crud.update_order_payment(order_id, bid, {"payment_status": "cancelled"})
    ref    = f"ORDER-{order_id}"
    reason = data.note or "Your order has been cancelled."
    await _notify_customer_payment(order,
        f"❌ *Order Cancelled*\n\n*{ref}* has been cancelled.\n\n_{reason}_\n\nType *menu* to place a new order. 😊"
    )
    return {"ok": True, "order_id": order_id, "message": f"{ref} cancelled."}


@router.post("/orders/{order_id}/refund")
async def admin_refund_order(order_id: int, data: OrderAdminAction, user=Depends(require_business)):
    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")
    try: update_order_status_supabase(order_id, "refunded")
    except Exception as exc: raise HTTPException(422, str(exc))
    crud.update_order_payment(order_id, bid, {"payment_status": "refunded"})
    ref   = f"ORDER-{order_id}"
    note  = data.note or "Your refund has been processed."
    total = float(order.get("total_price") or 0)
    await _notify_customer_payment(order,
        f"💳 *Refund Processed*\n\n*{ref}* — ${total:.2f}\n\n_{note}_\n\nPlease allow 3–5 business days. 🙏"
    )
    return {"ok": True, "order_id": order_id, "message": f"{ref} marked refunded."}


@router.get("/orders/{order_id}/status")
def get_order_status(order_id: int, user=Depends(require_business)):
    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid:
        raise HTTPException(404, f"Order {order_id} not found")
    status  = order.get("status", "pending")
    payment = order.get("payment_status", "pending")
    return {
        "order_id": order_id, "status": status,
        "status_label": format_order_status(status),
        "payment_status": payment, "payment_label": format_order_status(payment),
        "progress_bar": get_progress_bar(status), "next_stage": next_order_stage(status),
        "fulfillment_method": order.get("fulfillment_method", ""),
        "delivery_address": order.get("delivery_address", ""),
        "total_price": float(order.get("total_price") or 0),
        "customer_phone": order.get("customer_phone", ""),
        "created_at": order.get("created_at", ""),
    }


# ── Platform ──────────────────────────────────────────────────────────────────

@router.get("/platform/businesses")
def platform_list_businesses(user=Depends(require_superadmin)):
    businesses = crud.get_all_businesses()
    return {"count": len(businesses), "businesses": businesses}


@router.get("/platform/businesses/active")
def platform_active_businesses():
    businesses = crud.get_active_businesses()
    return [{"id": b["id"], "name": b["name"], "category": b.get("category", "")}
            for b in businesses]


class BusinessStatusUpdate(BaseModel):
    is_active:         Optional[bool] = None
    use_shared_number: Optional[bool] = None
    display_order:     Optional[int]  = None
    category:          Optional[str]  = None


@router.patch("/platform/businesses/{business_id}")
def platform_update_business(business_id: int, data: BusinessStatusUpdate, user=Depends(require_superadmin)):
    biz = crud.get_business_by_id(business_id)
    if not biz: raise HTTPException(404, f"Business {business_id} not found")
    updates: dict = {}
    if data.is_active is not None: updates["is_active"] = data.is_active
    if data.use_shared_number is not None: updates["use_shared_number"] = data.use_shared_number
    if data.display_order is not None: updates["display_order"] = data.display_order
    if data.category is not None: updates["category"] = data.category.strip()
    if not updates: raise HTTPException(422, "No fields to update")
    class _D:
        def dict(self, **_): return updates
    crud.update_business(business_id, _D())
    return {"ok": True, "business_id": business_id, "updates": updates}


@router.post("/platform/businesses/{business_id}/suspend")
def platform_suspend_business(business_id: int, user=Depends(require_superadmin)):
    biz = crud.get_business_by_id(business_id)
    if not biz: raise HTTPException(404, f"Business {business_id} not found")
    class _D:
        def dict(self, **_): return {"is_active": False}
    crud.update_business(business_id, _D())
    return {"ok": True, "message": f"Business {business_id} suspended."}


@router.post("/platform/businesses/{business_id}/activate")
def platform_activate_business(business_id: int, user=Depends(require_superadmin)):
    biz = crud.get_business_by_id(business_id)
    if not biz: raise HTTPException(404, f"Business {business_id} not found")
    class _D:
        def dict(self, **_): return {"is_active": True}
    crud.update_business(business_id, _D())
    return {"ok": True, "message": f"Business {business_id} activated."}


@router.get("/platform/stats")
def platform_stats(user=Depends(require_superadmin)):
    try:
        from core.db import supabase as _sb
        businesses  = crud.get_all_businesses()
        active_biz  = [b for b in businesses if b.get("is_active")]
        orders_res  = _sb.table("orders").select("id,total_price,status,payment_status,business_id").execute()
        orders      = orders_res.data or []
        revenue     = sum(float(o.get("total_price") or 0) for o in orders if o.get("payment_status") == "paid")
        customers_r = _sb.table("customers").select("id").execute()
        return {
            "total_businesses":  len(businesses),
            "active_businesses": len(active_biz),
            "total_orders":      len(orders),
            "total_revenue":     round(revenue, 2),
            "total_customers":   len(customers_r.data or []),
            "shared_number_id":  SHARED_PHONE_NUMBER_ID or "not configured",
        }
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.get("/platform/customer/{phone}/session")
def platform_customer_session(phone: str, user=Depends(require_superadmin)):
    from services.tenant_router import get_selected_business_id, get_selected_business_name
    return {
        "phone": phone,
        "selected_business_id":   get_selected_business_id(phone),
        "selected_business_name": get_selected_business_name(phone),
        "has_selection": get_selected_business_id(phone) is not None,
    }


@router.delete("/platform/customer/{phone}/session")
def platform_clear_customer_session(phone: str, user=Depends(require_superadmin)):
    from services.tenant_router import clear_selected_business
    clear_selected_business(phone)
    return {"ok": True, "phone": phone, "message": "Session cleared."}
