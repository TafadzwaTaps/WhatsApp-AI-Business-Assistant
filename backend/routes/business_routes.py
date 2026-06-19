"""
routes/business_routes.py — Business profile, products, orders, conversations,
broadcast, customers, CRM, campaigns, templates, analytics, and voice endpoints.

Routes: /me, /me/*, /products, /orders, /conversations, /customers, /broadcast,
        /crm/*, /campaigns/*, /templates/*, /analytics/*, /voice/transcribe
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Query, UploadFile, File
from pydantic import BaseModel, validator

import crud
from core.auth import require_business, get_current_user
from core.plan_guard import require_plan
from core.crypto import TokenDecryptionError
from services.ai import generate_reply
from services.invoice_service import generate_invoice_text
from services.security import check as _rate_check
from workflows.order_lifecycle import (
    create_order_supabase, update_order_status_supabase, get_order, VALID_STATUSES,
)

log = logging.getLogger("wazibot")
router = APIRouter()

# Runtime config — set by main.py
send_whatsapp  = None
SHARED_WA_TOKEN = ""
SHARED_PHONE_NUMBER_ID = ""


# ── Business profile ──────────────────────────────────────────────────────────

class BusinessUpdate(BaseModel):
    name:              Optional[str]  = None
    whatsapp_phone_id: Optional[str]  = None
    whatsapp_token:    Optional[str]  = None
    is_active:         Optional[bool] = None
    payment_number:    Optional[str]  = None
    payment_name:      Optional[str]  = None
    ecocash_number:    Optional[str]  = None
    ecocash_name:      Optional[str]  = None
    paypal_email:      Optional[str]  = None
    # Per-business AI customisation
    welcome_message:   Optional[str]  = None  # Custom greeting on "hi" / first contact
    currency:          Optional[str]  = None  # e.g. "USD", "ZWL", "ZAR"
    currency_symbol:   Optional[str]  = None  # e.g. "$", "R", "ZWL$"
    menu_header:       Optional[str]  = None  # Custom header shown above menu items
    # H3 fix: allow growth automation and other feature flags to be persisted
    features_json:     Optional[dict] = None  # arbitrary feature flags, stored as JSONB
    # H5: allow business owners to add/update their contact email
    owner_email:       Optional[str]  = None


@router.get("/me")
def get_me(user=Depends(require_business)):
    b = crud.get_business_by_id(user["business_id"])
    if not b: raise HTTPException(404, "Not found")
    b.pop("owner_password", None)
    b.pop("whatsapp_token", None)
    # H2: provide safe defaults for columns that may not exist if the schema
    # migration hasn't been run yet — prevents frontend undefined checks
    b.setdefault("subscription_tier",  "free")
    b.setdefault("billing_status",     "free")
    b.setdefault("trial_ends_at",      None)
    b.setdefault("features_json",      {})
    b.setdefault("onboarding_step",    1)
    b.setdefault("onboarding_completed", False)
    b.setdefault("owner_email",        None)
    b.setdefault("contact_phone",      None)
    b.setdefault("use_shared_number",  False)
    b.setdefault("ecocash_number",     None)
    b.setdefault("paypal_email",       None)
    b.setdefault("cash_enabled",       False)
    return b


@router.patch("/me")
def update_me(data: BusinessUpdate, user=Depends(require_business)):
    safe = data.dict(exclude_none=True)
    safe.pop("is_active", None)
    new_phone_id = safe.get("whatsapp_phone_id")
    if new_phone_id:
        existing = crud.get_business_by_phone_id(new_phone_id)
        if existing and existing["id"] != user["business_id"]:
            raise HTTPException(400, "That WhatsApp Phone Number ID is already registered.")
    class _D:
        def dict(self, **_): return safe
    b = crud.update_business(user["business_id"], _D())
    if not b: raise HTTPException(404, "Not found")
    b.pop("owner_password", None)
    b.pop("whatsapp_token", None)
    return b


@router.get("/me/test-whatsapp")
def test_whatsapp_connection(user=Depends(require_business)):
    import requests as http_requests
    b = crud.get_business_by_id(user["business_id"])
    if not b or not b.get("whatsapp_phone_id"):
        return {"ok": False, "reason": "No Phone Number ID saved"}
    try:
        token = crud.get_decrypted_token(b)
    except TokenDecryptionError as exc:
        return {"ok": False, "reason": f"Token decryption failed — {exc}"}
    if not token:
        return {"ok": False, "reason": "No access token saved"}
    try:
        resp = http_requests.get(
            f"https://graph.facebook.com/v18.0/{b['whatsapp_phone_id']}",
            headers={"Authorization": f"Bearer {token}"}, timeout=5,
        )
        if resp.status_code == 200:
            return {"ok": True, "reason": "Connected to Meta API ✅"}
        return {"ok": False, "reason": resp.json().get("error", {}).get("message", "Unknown")}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


# ── Payment settings ──────────────────────────────────────────────────────────

class EcoCashSettingsUpdate(BaseModel):
    ecocash_number: str
    ecocash_name:   str

class PayPalSettingsUpdate(BaseModel):
    paypal_email: str

class PaymentSettingsUpdate(BaseModel):
    ecocash_number: Optional[str] = None
    ecocash_name:   Optional[str] = None
    paypal_email:   Optional[str] = None


@router.get("/me/payment-settings")
def get_payment_settings(user=Depends(require_business)):
    b = crud.get_business_by_id(user["business_id"])
    if not b: raise HTTPException(404, "Business not found")
    ecocash_number = b.get("ecocash_number") or b.get("payment_number") or ""
    ecocash_name   = b.get("ecocash_name")   or b.get("payment_name")  or ""
    paypal_email   = b.get("paypal_email") or ""
    return {
        "business_name": b.get("name", ""),
        "ecocash_number": ecocash_number, "ecocash_name": ecocash_name,
        "ecocash_configured": bool(ecocash_number),
        "paypal_email": paypal_email, "paypal_configured": bool(paypal_email),
        "payment_number": ecocash_number, "payment_name": ecocash_name,
    }


@router.post("/me/payment-settings")
def update_payment_settings(data: PaymentSettingsUpdate, user=Depends(require_business)):
    bid, updates, errors = user["business_id"], {}, []
    if data.ecocash_number is not None:
        n = data.ecocash_number.strip()
        if n and len(n) < 7: errors.append("EcoCash number too short — include country code")
        elif n: updates["ecocash_number"] = n; updates["payment_number"] = n
        else:   updates["ecocash_number"] = None; updates["payment_number"] = None
    if data.ecocash_name is not None:
        n = data.ecocash_name.strip()
        updates["ecocash_name"] = n or None; updates["payment_name"] = n or None
    if data.paypal_email is not None:
        e = data.paypal_email.strip().lower()
        if e and "@" not in e: errors.append("PayPal email is invalid — must contain @")
        elif e: updates["paypal_email"] = e
        else:   updates["paypal_email"] = None
    if errors: raise HTTPException(422, "; ".join(errors))
    if not updates: raise HTTPException(422, "No valid fields provided")
    class _D:
        def dict(self, **_): return updates
    b = crud.update_business(bid, _D())
    if not b: raise HTTPException(500, "Failed to update payment settings")
    ecocash_number = b.get("ecocash_number") or b.get("payment_number") or ""
    ecocash_name   = b.get("ecocash_name")   or b.get("payment_name")  or ""
    paypal_email   = b.get("paypal_email") or ""
    return {
        "ok": True, "message": "Payment settings saved successfully.",
        "ecocash_number": ecocash_number, "ecocash_name": ecocash_name,
        "paypal_email": paypal_email,
        "ecocash_configured": bool(ecocash_number), "paypal_configured": bool(paypal_email),
    }


@router.post("/me/payment-settings/ecocash")
def update_ecocash_settings(data: EcoCashSettingsUpdate, user=Depends(require_business)):
    number, name = data.ecocash_number.strip(), data.ecocash_name.strip()
    if not number: raise HTTPException(422, "EcoCash number is required")
    if len(number) < 7: raise HTTPException(422, "EcoCash number too short — include country code")
    if not name: raise HTTPException(422, "Business name is required")
    class _D:
        def dict(self, **_): return {"ecocash_number": number, "ecocash_name": name, "payment_number": number, "payment_name": name}
    b = crud.update_business(user["business_id"], _D())
    if not b: raise HTTPException(500, "Failed to save EcoCash settings")
    return {"ok": True, "message": f"EcoCash number saved. Customers will send money to {number}.", "ecocash_number": number, "ecocash_name": name}


@router.post("/me/payment-settings/paypal")
def update_paypal_settings(data: PayPalSettingsUpdate, user=Depends(require_business)):
    email = data.paypal_email.strip().lower()
    if not email: raise HTTPException(422, "PayPal email is required")
    if "@" not in email or "." not in email.split("@")[-1]: raise HTTPException(422, "Invalid PayPal email")
    class _D:
        def dict(self, **_): return {"paypal_email": email}
    b = crud.update_business(user["business_id"], _D())
    if not b: raise HTTPException(500, "Failed to save PayPal settings")
    return {"ok": True, "message": f"PayPal email saved. Customers will send money to {email}.", "paypal_email": email}


@router.get("/me/payment")
def get_payment_settings_legacy(user=Depends(require_business)):
    return get_payment_settings(user)


@router.post("/me/payment")
def update_payment_legacy():
    raise HTTPException(410, "Deprecated. Use POST /me/payment-settings.")


# ── Products ──────────────────────────────────────────────────────────────────

class ProductCreate(BaseModel):
    name:                str
    price:               float
    image_url:           Optional[str] = None
    stock:               int           = 0
    low_stock_threshold: int           = 5


@router.post("/products/upload-image")
async def upload_product_image(
    file:    UploadFile = File(...),
    user=Depends(require_business),
):
    """
    Upload a product image to Supabase Storage.
    Returns the public HTTPS URL that WhatsApp can access.
    Uses the service_role key server-side — never exposes it to the frontend.
    """
    import os, mimetypes
    from core.db import supabase

    # Validate file type
    allowed = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    content_type = file.content_type or "image/jpeg"
    if content_type not in allowed:
        raise HTTPException(400, f"Unsupported file type: {content_type}. Use JPG, PNG, or WebP.")

    # Max 5MB
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(400, "File too large. Maximum size is 5MB.")

    # Build storage path: products/{business_id}_{timestamp}.ext
    ext        = mimetypes.guess_extension(content_type) or ".jpg"
    ext        = ext.replace(".jpe", ".jpg")  # normalise
    bid        = user["business_id"]
    timestamp  = int(__import__("time").time() * 1000)
    safe_name  = f"products/{bid}_{timestamp}{ext}"

    # Upload to Supabase Storage using the storage client
    try:
        # supabase-py storage upload
        res = supabase.storage.from_("product-images").upload(
            path        = safe_name,
            file        = data,
            file_options= {"content-type": content_type, "upsert": "true"},
        )
    except Exception as exc:
        log.error("storage upload error: %s", exc)
        # Fallback: try raw HTTP with service_role key
        try:
            import httpx
            supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
            supabase_key = os.getenv("SUPABASE_KEY", "")
            upload_url   = f"{supabase_url}/storage/v1/object/product-images/{safe_name}"
            resp = httpx.put(
                upload_url,
                content  = data,
                headers  = {
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type":  content_type,
                    "x-upsert":      "true",
                },
                timeout  = 20,
            )
            if resp.status_code not in (200, 201):
                raise HTTPException(500, f"Upload failed: {resp.text[:200]}")
        except HTTPException:
            raise
        except Exception as exc2:
            log.error("storage upload fallback error: %s", exc2)
            raise HTTPException(500, "Image upload failed. Check Supabase Storage is configured.")

    # Build the public URL
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    public_url   = f"{supabase_url}/storage/v1/object/public/product-images/{safe_name}"

    log.info("product image uploaded  biz=%s  path=%s", bid, safe_name)
    return {"url": public_url, "path": safe_name}


@router.get("/products")
def get_products(user=Depends(require_business)):
    return crud.get_products(user["business_id"])


@router.post("/products", status_code=201)
def create_product(product: ProductCreate, user=Depends(require_business)):
    bid = user["business_id"]
    if not product.name or not product.name.strip(): raise HTTPException(422, "Product name cannot be empty")
    if product.price < 0: raise HTTPException(422, "Product price must be non-negative")
    # Sprint 2: Plan-based product limit (edit/delete/import are NOT affected)
    try:
        from core.plan_guard import check_product_limit
        limit_error = check_product_limit(bid)
        if limit_error:
            raise HTTPException(403, detail=limit_error)
    except HTTPException:
        raise
    except Exception as _lim_exc:
        log.warning("product limit check failed (non-blocking): %s", _lim_exc)
    try:
        created = crud.create_product(bid, product)
    except ValueError as exc: raise HTTPException(422, str(exc))
    except RuntimeError as exc: raise HTTPException(500, str(exc))
    return created


@router.patch("/products/{product_id}")
def update_product(product_id: int, data: dict, user=Depends(require_business)):
    data.pop("business_id", None); data.pop("id", None)
    if not data: raise HTTPException(422, "No fields to update")
    updated = crud.update_product(product_id, user["business_id"], data)
    if not updated: raise HTTPException(404, f"Product {product_id} not found")
    return updated


@router.delete("/products/{product_id}")
def delete_product(product_id: int, user=Depends(require_business)):
    try:
        p = crud.delete_product(product_id, user["business_id"])
    except RuntimeError as exc: raise HTTPException(500, str(exc))
    if not p: raise HTTPException(404, f"Product {product_id} not found or access denied")
    return {"deleted": product_id, "name": p.get("name", "")}


# ── Orders ────────────────────────────────────────────────────────────────────

class OrderCreateRequest(BaseModel):
    customer_phone: str
    items: list

class OrderStatusUpdate(BaseModel):
    status: str
    @validator("status")
    def status_valid(cls, v):
        if v not in VALID_STATUSES: raise ValueError(f"status must be one of {VALID_STATUSES}")
        return v


@router.get("/orders")
def get_orders(user=Depends(require_business)):
    return crud.get_orders(user["business_id"])


@router.post("/orders", status_code=201)
def create_order_api(data: OrderCreateRequest, user=Depends(require_business)):
    try:
        order = create_order_supabase(business_id=user["business_id"],
                                      customer_phone=data.customer_phone, cart=data.items)
    except ValueError as exc: raise HTTPException(400, str(exc))
    # Sprint 1: Notify business owner — fail silently
    try:
        from crud.orders import notify_owner_new_order
        notify_owner_new_order(user["business_id"], order)
    except Exception:
        pass
    return {"message": "Order created", "order_id": order.get("id"),
            "order": order, "invoice": generate_invoice_text(order)}


@router.put("/orders/{order_id}/status")
def update_order_status_api(order_id: int, data: OrderStatusUpdate, user=Depends(require_business)):
    existing = crud.get_order_by_id(order_id, user["business_id"])
    if not existing: raise HTTPException(404, "Order not found")
    try:
        order = update_order_status_supabase(order_id, data.status)
    except ValueError as exc: raise HTTPException(400, str(exc))
    return {"message": "Status updated", "order_id": order_id, "status": order["status"]}


@router.get("/orders/{order_id}/invoice")
def get_invoice_text(order_id: int, user=Depends(require_business)):
    order = crud.get_order_by_id(order_id, user["business_id"])
    if not order: raise HTTPException(404, "Order not found")
    return {"invoice": generate_invoice_text(order)}


# ── Legacy conversations ──────────────────────────────────────────────────────

@router.get("/conversations")
def get_conversations(user=Depends(require_business)):
    return crud.get_conversations(user["business_id"])


@router.get("/conversations/{phone}")
def get_chat(phone: str, user=Depends(require_business)):
    return crud.get_messages_for_phone(user["business_id"], phone)


# ── Broadcast ─────────────────────────────────────────────────────────────────

class BroadcastRequest(BaseModel):
    message:      str
    phone_filter: list[str] | None = None
    @validator("message")
    def msg_valid(cls, v):
        v = v.strip()
        if len(v) < 3:    raise ValueError("Message too short")
        if len(v) > 1024: raise ValueError("Message too long (max 1024 chars)")
        return v


@router.post("/broadcast")
def broadcast(body: BroadcastRequest, request: Request, user=Depends(require_business), _plan=Depends(require_plan("STARTER"))):
    _rate_check("broadcast", request)
    bid      = user["business_id"]
    business = crud.get_business_by_id(bid)
    try:
        token = crud.get_decrypted_token(business)
    except TokenDecryptionError as exc:
        raise HTTPException(503, "WhatsApp token cannot be decrypted. Re-enter it in Settings.")
    if not token: raise HTTPException(400, "WhatsApp token not configured.")
    if not business.get("whatsapp_phone_id"): raise HTTPException(400, "WhatsApp Phone Number ID not configured.")

    all_phones = crud.get_all_customer_phones(bid)
    if not all_phones: return {"sent": 0, "failed": 0, "total": 0, "message": "No customers found"}

    if body.phone_filter:
        filter_set = {p.strip().lstrip("+") for p in body.phone_filter if p}
        phones = [p for p in all_phones if p.strip().lstrip("+") in filter_set]
    else:
        phones = all_phones

    if not phones:
        return {"sent": 0, "failed": 0, "total": len(all_phones), "message": "No recipients matched"}

    sent, failed, failed_phones = 0, 0, []
    for phone in phones:
        try:
            result = send_whatsapp(business["whatsapp_phone_id"], token, phone, body.message)
            if "error" in result: raise RuntimeError(result["error"])
            crud.log_message(bid, phone, "out", f"[BROADCAST] {body.message}")
            sent += 1
        except Exception as exc:
            failed += 1
            failed_phones.append(phone)

    return {"sent": sent, "failed": failed, "total": len(phones), "failed_numbers": failed_phones}


@router.get("/customers")
def get_customers(user=Depends(require_business)):
    phones = crud.get_all_customer_phones(user["business_id"])
    return {"phones": phones, "total": len(phones)}


# ── CRM segments ──────────────────────────────────────────────────────────────

@router.get("/crm/segments")
def crm_segments(user=Depends(require_business)):
    return crud.get_segment_summary(user["business_id"])


@router.get("/crm/segments/{segment}")
def crm_segment_customers(segment: str, user=Depends(require_business)):
    valid = {"vip", "loyal", "regular", "new", "prospect", "all"}
    if segment not in valid: raise HTTPException(400, f"Invalid segment. Use: {', '.join(sorted(valid))}")
    return crud.get_customers_by_segment(user["business_id"], segment)


@router.get("/crm/inactive")
def crm_inactive(days: int = 30, user=Depends(require_business)):
    if days < 1 or days > 365: raise HTTPException(400, "days must be between 1 and 365")
    return crud.get_inactive_customers(user["business_id"], inactive_days=days)


# ── Campaigns ─────────────────────────────────────────────────────────────────

class CampaignRequest(BaseModel):
    audience:        str
    message:         str
    phone_list:      list[str] | None = None
    personalise_msg: bool = True
    dry_run:         bool = False


@router.post("/campaigns/send")
async def campaign_send(body: CampaignRequest, request: Request, user=Depends(require_business), _plan=Depends(require_plan("STARTER"))):
    _rate_check("campaign", request)
    from services.campaign_service import CampaignService, AUDIENCE_INFO
    bid = user["business_id"]
    if body.audience not in AUDIENCE_INFO: raise HTTPException(400, f"Unknown audience: {list(AUDIENCE_INFO.keys())}")
    if len(body.message.strip()) < 3: raise HTTPException(400, "Message too short")
    if len(body.message) > 1024: raise HTTPException(400, "Message too long (max 1024 chars)")
    result = CampaignService.run(business_id=bid, audience=body.audience, message=body.message,
                                  phone_list=body.phone_list, personalise_msg=body.personalise_msg, dry_run=body.dry_run)
    if not body.dry_run and result.get("sent", 0) > 0:
        try:
            from services.events import Events
            Events.emit("broadcast_sent", {"business_id": bid, "audience": body.audience,
                                            "sent": result["sent"], "failed": result.get("failed", 0)})
        except Exception: pass
    return result


@router.get("/campaigns/audiences")
def campaign_audiences():
    from services.campaign_service import AUDIENCE_INFO
    return AUDIENCE_INFO


@router.post("/campaigns/preview")
async def campaign_preview(body: CampaignRequest, user=Depends(require_business)):
    from services.campaign_service import CampaignService
    return CampaignService.run(business_id=user["business_id"], audience=body.audience,
                                message=body.message, phone_list=body.phone_list,
                                personalise_msg=body.personalise_msg, dry_run=True)


# ── Templates ─────────────────────────────────────────────────────────────────

@router.get("/templates")
def list_templates():
    from services.templates import list_templates as _list
    return _list()


@router.get("/templates/{template_id}")
def get_template(template_id: str):
    from services.templates import get_template as _get, TEMPLATES
    if template_id not in TEMPLATES: raise HTTPException(404, f"Template '{template_id}' not found")
    return _get(template_id).to_dict()


# ── Payment reminders ─────────────────────────────────────────────────────────

@router.get("/payments/reminders/pending")
def reminders_pending(user=Depends(require_business)):
    from workflows.payment_reminder import FIRST_REMINDER_HOURS, get_reminder_tier
    stale = crud.get_stale_payment_orders(user["business_id"], older_than_hours=FIRST_REMINDER_HOURS)
    enriched = [{
        "order_id": o.get("id"), "customer_phone": o.get("customer_phone"),
        "total_price": float(o.get("total_price") or 0),
        "payment_method": o.get("payment_method", ""), "payment_status": o.get("payment_status", ""),
        "payment_reference": o.get("payment_reference", ""), "created_at": o.get("created_at", ""),
        "reminder_tier": get_reminder_tier(o),
    } for o in stale]
    return {"count": len(enriched), "orders": enriched}


@router.post("/payments/reminders/send")
async def reminders_send(dry_run: bool = False, user=Depends(require_business)):
    from workflows.payment_reminder import run_reminders_for_business
    return run_reminders_for_business(user["business_id"], dry_run=dry_run)


@router.post("/payments/reminders/{order_id}/nudge")
async def reminder_nudge(order_id: int, dry_run: bool = False, user=Depends(require_business)):
    from workflows.payment_reminder import send_reminder, get_reminder_tier, _last_reminder_sent
    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid: raise HTTPException(404, f"Order {order_id} not found")
    pstatus = order.get("payment_status", "")
    if pstatus not in ("awaiting_payment", "payment_review", "pending_cash"):
        raise HTTPException(422, f"Order {order_id} has payment_status={pstatus!r}")
    business = crud.get_business_by_id(bid)
    if not business: raise HTTPException(404, "Business not found")
    tier = get_reminder_tier(order) or 1
    _last_reminder_sent.pop(order_id, None)
    return send_reminder(order, business, tier, dry_run=dry_run)


@router.get("/payments/reminders/{order_id}/preview")
def reminder_preview(order_id: int, user=Depends(require_business)):
    from workflows.payment_reminder import build_reminder_message, get_reminder_tier
    bid   = user["business_id"]
    order = get_order(order_id)
    if not order or order.get("business_id") != bid: raise HTTPException(404, f"Order {order_id} not found")
    business = crud.get_business_by_id(bid)
    biz_name = business.get("name", "WaziBot") if business else "WaziBot"
    tier = get_reminder_tier(order) or 1
    return {"order_id": order_id, "tier": tier, "customer_phone": order.get("customer_phone", ""),
            "payment_status": order.get("payment_status", ""),
            "preview_message": build_reminder_message(order, biz_name, tier)}


# ── Analytics ─────────────────────────────────────────────────────────────────

@router.get("/analytics/stats")
def analytics_stats(user=Depends(require_business)):
    # Sprint 4: use 60s cached version to reduce Supabase load
    try:
        from crud.analytics import get_business_stats_cached
        return get_business_stats_cached(user["business_id"])
    except Exception:
        return crud.get_business_stats(user["business_id"])


@router.get("/analytics/top-customers")
def analytics_top_customers(limit: int = 10, user=Depends(require_business)):
    return crud.get_top_customers(user["business_id"], limit=limit)


@router.get("/analytics/low-stock")
def analytics_low_stock(user=Depends(require_business)):
    return crud.get_low_stock_products(user["business_id"])


@router.post("/analytics/notify-low-stock")
async def notify_low_stock_to_owner(user=Depends(require_business)):
    bid  = user["business_id"]
    biz  = crud.get_business_by_id(bid)
    if not biz: raise HTTPException(404, "Business not found")
    owner_phone = biz.get("contact_phone", "").strip()
    if not owner_phone: return {"ok": False, "message": "No contact_phone set."}
    low = crud.get_low_stock_products(bid)
    if not low: return {"ok": True, "message": "All products are well-stocked! ✅"}
    lines = [f"  • *{p['name']}* — {p.get('stock', 0)} left" for p in low]
    msg = f"⚠️ *Low Stock Alert — {biz.get('name', 'Your Store')}*\n\n" + "\n".join(lines) + "\n\n_Please restock soon._"
    try:
        token    = crud.get_decrypted_token(biz)
        phone_id = biz.get("whatsapp_phone_id", "")
        if not token or not phone_id:
            if SHARED_WA_TOKEN and SHARED_PHONE_NUMBER_ID:
                token, phone_id = SHARED_WA_TOKEN, SHARED_PHONE_NUMBER_ID
            else:
                return {"ok": False, "message": "No WhatsApp token configured."}
        result = send_whatsapp(phone_id, token, owner_phone, msg)
        return {"ok": "error" not in result, "products_alerted": len(low)}
    except Exception as exc:
        raise HTTPException(500, str(exc))




# NOTE: /analytics/{business_id} (parameterized int route) was moved below
# all literal /analytics/* routes. FastAPI matches routes in registration
# order — having {business_id}: int registered first caused every literal
# path like /analytics/repeat-customers and /analytics/satisfaction to be
# captured as business_id="repeat-customers", fail int coercion, and return
# 422 Unprocessable Entity instead of reaching the real handler.


# ── Voice transcribe ──────────────────────────────────────────────────────────

class VoiceTranscribeRequest(BaseModel):
    customer_id: int
    audio_url:   str
    phone:       str
    language:    str = "en"


@router.post("/voice/transcribe")
async def voice_transcribe(body: VoiceTranscribeRequest, user=Depends(require_business)):
    import os
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(body.customer_id, bid)
    if not customer: raise HTTPException(404, "Customer not found")

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    transcript = None

    if openai_key:
        try:
            import httpx
            biz   = crud.get_business_by_id(bid)
            token = crud.get_decrypted_token(biz) if biz else ""
            async with httpx.AsyncClient() as client:
                r = await client.get(body.audio_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
                r.raise_for_status()
                audio_bytes = r.content
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    files={"file": ("voice.ogg", audio_bytes, "audio/ogg")},
                    data={"model": "whisper-1", "language": body.language if body.language != "auto" else ""},
                    timeout=60,
                )
                resp.raise_for_status()
                transcript = resp.json().get("text", "").strip()
        except Exception as exc:
            log.warning("voice_transcribe whisper error: %s", exc)

    if not transcript:
        return {"ok": False, "transcript": None,
                "reply": "🎤 Sorry, I couldn't process your voice note. Could you type your order instead? Type *menu* to get started! 😊"}

    biz      = crud.get_business_by_id(bid)
    products = crud.get_products(bid)
    reply    = generate_reply(
        message=transcript, phone=body.phone, business_id=bid,
        business_name=biz.get("name", "") if biz else "",
        products=products, voice_transcript=transcript,
    )
    return {"ok": True, "transcript": transcript, "reply": reply}
@router.get("/analytics/handoff-stats")
def analytics_handoff_stats(user=Depends(require_business)):
    """
    #12 — Dashboard: Human Handoff & Agent Activity Analytics.

    Returns:
      pending_handoffs:  list of active handoffs (from get_pending_handoffs)
      agent_activity:    list of agents who sent messages, with last-reply time + count
      avg_wait_seconds:  average time customers wait before agent replies
      total_today:       number of handoffs initiated today
    All data derived from existing tables — no new schema required.
    """
    import time as _time
    import datetime as _dt
    from core.db import supabase as _sb

    bid = user["business_id"]

    # ── Active pending handoffs (from existing function) ──────────────────────
    from workflows.human_handoff import get_pending_handoffs
    pending = get_pending_handoffs(bid)

    # ── Average wait time across active handoffs ───────────────────────────────
    wait_times   = [p["wait_seconds"] for p in pending if p.get("wait_seconds") is not None]
    avg_wait     = round(sum(wait_times) / len(wait_times)) if wait_times else 0

    # ── Handoffs initiated today ───────────────────────────────────────────────
    total_today  = 0
    try:
        today_start = _dt.datetime.now(_dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()
        res = (
            _sb.table("messages")
            .select("id, sender_name, sender_type, created_at")
            .eq("business_id", bid)
            .eq("direction", "outgoing")
            .eq("sender_type", "agent")
            .gte("created_at", today_start)
            .execute()
        )
        agent_msgs_today = res.data or []
    except Exception as exc:
        log.warning("handoff_stats: messages query failed: %s", exc)
        agent_msgs_today = []

    # ── Agent activity: unique agents, message counts, last-reply time ─────────
    agent_activity_map: dict = {}
    for msg in agent_msgs_today:
        name = msg.get("sender_name") or "Agent"
        if name not in agent_activity_map:
            agent_activity_map[name] = {"agent_name": name, "messages_today": 0, "last_reply_at": None}
        agent_activity_map[name]["messages_today"] += 1
        t = msg.get("created_at")
        if t and (agent_activity_map[name]["last_reply_at"] is None
                  or t > agent_activity_map[name]["last_reply_at"]):
            agent_activity_map[name]["last_reply_at"] = t
    agent_activity = sorted(agent_activity_map.values(),
                            key=lambda a: a["last_reply_at"] or "", reverse=True)

    # ── Handoffs initiated today (approximate via first-handoff-ack messages) ──
    # Count messages with text starting with the handoff ack emoji as a proxy
    try:
        res2 = (
            _sb.table("messages")
            .select("id")
            .eq("business_id", bid)
            .eq("direction", "outgoing")
            .ilike("text", "🙋%")
            .gte("created_at", today_start)
            .execute()
        )
        total_today = len(res2.data or [])
    except Exception:
        total_today = len(pending)

    return {
        "pending_handoffs": pending,
        "agent_activity":   agent_activity,
        "avg_wait_seconds": avg_wait,
        "total_today":      total_today,
        "active_count":     len(pending),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Feature 2 — REPEAT CUSTOMER ANALYTICS
# Simple endpoint: reads user_memory.order_count. No new tables.
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/analytics/repeat-customers")
def analytics_repeat_customers(user=Depends(require_business)):
    """
    Return repeat customer metrics for the dashboard overview card.
    Repeat customer = has order_count > 1 in user_memory.
    """
    bid = user["business_id"]
    try:
        from core.db import supabase as _sb
        res = (
            _sb.table("user_memory")
            .select("phone, order_count")
            .eq("business_id", bid)
            .execute()
        )
        rows             = res.data or []
        total_customers  = len(rows)
        repeat_customers = sum(1 for r in rows if (r.get("order_count") or 0) > 1)
        repeat_rate      = round(repeat_customers / total_customers * 100, 1) if total_customers else 0
        return {
            "total_customers":   total_customers,
            "repeat_customers":  repeat_customers,
            "repeat_rate_pct":   repeat_rate,
        }
    except Exception as exc:
        log.warning("analytics_repeat_customers error: %s", exc)
        return {"total_customers": 0, "repeat_customers": 0, "repeat_rate_pct": 0}


# ═══════════════════════════════════════════════════════════════════════════
# Feature 4 — CSV PRODUCT IMPORT
# Accepts a CSV file, parses it, inserts valid rows. Skips invalid ones.
# Uses existing products table and validation patterns.
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/products/import-csv")
async def import_products_csv(
    file: UploadFile = File(...),
    user=Depends(require_business),
):
    """
    Bulk product import via CSV.
    Expected columns: name, price, description (optional), image_url (optional)
    Skips rows with missing name or non-numeric price.
    Returns summary: {imported, skipped, errors}
    """
    import csv
    import io

    bid       = user["business_id"]
    imported  = 0
    skipped   = 0
    row_errors: list[str] = []

    try:
        content  = await file.read()
        text     = content.decode("utf-8-sig", errors="replace")  # handle BOM
        reader   = csv.DictReader(io.StringIO(text))

        # Normalise column names to lowercase stripped
        rows = []
        for row in reader:
            rows.append({k.strip().lower(): (v or "").strip() for k, v in row.items()})

        if not rows:
            raise HTTPException(400, "CSV file is empty or has no rows.")

        from core.db import supabase as _sb
        import time as _t

        for i, row in enumerate(rows, start=2):   # start=2 (row 1 = header)
            name  = row.get("name") or row.get("product_name") or ""
            price = row.get("price") or ""

            if not name:
                skipped += 1
                row_errors.append(f"Row {i}: missing name — skipped")
                continue

            try:
                price_f = float(price)
                if price_f < 0:
                    raise ValueError("negative price")
            except (ValueError, TypeError):
                skipped += 1
                row_errors.append(f"Row {i}: invalid price '{price}' — skipped")
                continue

            payload = {
                "business_id": bid,
                "name":        name[:200],
                "price":       price_f,
                "description": (row.get("description") or "")[:1000],
                "stock":       None,
            }
            img = (row.get("image_url") or row.get("image") or "").strip()
            if img and img.startswith("http"):
                payload["image_url"] = img[:500]

            try:
                _sb.table("products").insert(payload).execute()
                imported += 1
            except Exception as db_exc:
                skipped += 1
                row_errors.append(f"Row {i}: DB error — {str(db_exc)[:80]}")

        log.info(
            "csv_import: biz=%s imported=%d skipped=%d",
            bid, imported, skipped,
        )
        return {
            "ok":       True,
            "imported": imported,
            "skipped":  skipped,
            "errors":   row_errors[:20],   # cap error list
        }

    except HTTPException:
        raise
    except Exception as exc:
        log.error("import_products_csv error: %s", exc)
        raise HTTPException(500, f"Import failed: {exc}")

@router.get("/analytics/satisfaction")
def analytics_satisfaction(user=Depends(require_business)):
    """Sprint 5 — Customer satisfaction score from user_memory.last_rating."""
    from crud.analytics import get_satisfaction_score
    return get_satisfaction_score(user["business_id"])


# Parameterized route registered LAST among /analytics/* paths on purpose —
# see note above. Must come after every literal /analytics/<word> route or
# it will shadow them and cause 422s on valid string-suffixed paths.
@router.get("/analytics/{business_id}")
def get_analytics(business_id: int, user=Depends(get_current_user)):
    if user["role"] != "superadmin" and user.get("business_id") != business_id:
        raise HTTPException(403, "Access denied")
    business = crud.get_business_by_id(business_id)
    if not business: raise HTTPException(404, "Business not found")
    try:
        stats = crud.get_dashboard_stats(business_id)
        stats["business_name"] = business.get("name", "")
        return stats
    except Exception as exc:
        raise HTTPException(500, "Failed to load analytics")


@router.get("/trial/status")
def trial_status(user=Depends(require_business)):
    """
    Return trial status for the dashboard banner.
    Used by dashboard.js to show/hide the trial banner and upgrade prompts.
    """
    from core.plan_guard import get_trial_status_response
    return get_trial_status_response(user["business_id"])

@router.post("/crm/backfill-from-chats")
def crm_backfill_from_chats(user=Depends(require_business)):
    """
    One-time fix: create user_memory rows for customers who appear in the
    "customers" table (this is the actual source of the Conversations page —
    crud.get_chat_conversations() reads from "customers", not "chat_messages")
    but have no user_memory row yet. These customers had active conversations
    but were invisible in the CRM/Customers page because the CRM only reads
    user_memory.

    Safe to call multiple times — upsert on (phone, business_id), only fills
    in missing rows, never overwrites existing order/spend data.
    """
    bid = user["business_id"]
    try:
        from core.db import supabase
        from datetime import datetime, timezone

        # Get all customers for this business — this is the same table the
        # Conversations page reads from (crud.get_chat_conversations).
        cust_res = (
            supabase.table("customers")
            .select("phone, created_at, last_seen")
            .eq("business_id", bid)
            .execute()
        )
        customer_rows = cust_res.data or []

        # Get phones that already have a user_memory row
        mem_res = (
            supabase.table("user_memory")
            .select("phone")
            .eq("business_id", bid)
            .execute()
        )
        existing_phones = {r["phone"] for r in (mem_res.data or [])}

        created = 0
        skipped_no_phone = 0
        for row in customer_rows:
            phone = row.get("phone")
            if not phone:
                skipped_no_phone += 1
                continue
            if phone in existing_phones:
                continue
            try:
                seen_at = row.get("last_seen") or row.get("created_at") or datetime.now(timezone.utc).isoformat()
                # Fix: was upsert(on_conflict="phone,business_id") which fails
                # with PostgREST error 42P10 ("no unique or exclusion
                # constraint matching the ON CONFLICT specification") because
                # user_memory has no UNIQUE(phone, business_id) constraint.
                # This caused every single backfill insert to fail silently,
                # producing "Synced 0 customers from chats" even with real
                # customers present.
                #
                # Since `phone` was already filtered against existing_phones
                # above, every row reaching here is confirmed new — a plain
                # insert() is correct and avoids the broken ON CONFLICT path
                # entirely. (Permanent fix: add the unique constraint via
                # `ALTER TABLE user_memory ADD CONSTRAINT
                # user_memory_phone_business_id_key UNIQUE (phone,
                # business_id);` — once that's run, on_conflict-based
                # upserts elsewhere in the app will also work correctly.)
                supabase.table("user_memory").insert({
                    "phone":          phone,
                    "business_id":    bid,
                    "frequent_items": {},
                    "last_orders":    [],
                    "customer_name":  "",
                    "total_spent":    0.0,
                    "order_count":    0,
                    "last_seen":      seen_at,
                    "updated_at":     datetime.now(timezone.utc).isoformat(),
                }).execute()
                created += 1
            except Exception as row_exc:
                log.warning("backfill: failed for phone=%s: %s", phone, row_exc)

        # Clear cache so the Customers page reflects new rows immediately
        try:
            from crud.analytics import _cache_invalidate_business
            _cache_invalidate_business(bid)
        except Exception:
            pass

        return {
            "ok":                True,
            "total_customers":   len(customer_rows),
            "already_existed":   len(existing_phones),
            "created":           created,
            "skipped_no_phone":  skipped_no_phone,
        }
    except Exception as exc:
        log.error("crm_backfill_from_chats error: %s", exc)
        raise HTTPException(500, f"Backfill failed: {exc}")
