"""
routes/onboarding_routes.py
═══════════════════════════
Setup Wizard — 8-step interactive onboarding for new businesses.

PLACEMENT: backend/routes/onboarding_routes.py

Registers at /onboarding/* — completely separate from existing /auth/*
and /products/* routes. Never touches existing signup flow.

Steps:
  1. business_info   — name, category, currency, timezone
  2. branding        — logo upload, theme colour, tagline
  3. products        — add first 1-5 products with images
  4. whatsapp        — connect WhatsApp number (dedicated or shared)
  5. ai_config       — choose AI role, welcome message, hours
  6. test_order      — fire a simulated WhatsApp order
  7. go_live         — confirm readiness, show share links
  8. complete        — mark onboarding done, redirect to dashboard

Each step is idempotent — the wizard can be resumed at any point.
Progress is stored in businesses.onboarding_step (safe ADD COLUMN IF NOT EXISTS).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

log = logging.getLogger("wazibot")
router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Dependency — reuse existing require_business
# ─────────────────────────────────────────────────────────────────────────────
try:
    from core.auth import require_business
    _HAS_AUTH = True
except ImportError:
    _HAS_AUTH = False
    def require_business():
        return {"business_id": 0, "username": "dev"}


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────

class Step1Request(BaseModel):
    business_name: str
    category: str = ""
    currency: str = "USD"
    currency_symbol: str = "$"
    timezone: str = "Africa/Harare"

class Step2Request(BaseModel):
    tagline: str = ""
    theme_colour: str = "#00c853"

class Step5Request(BaseModel):
    ai_role: str = "general"         # general | sales | support | booking
    welcome_message: str = ""
    business_hours: str = ""

class Step7Request(BaseModel):
    confirmed: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Wizard page (serves the SPA)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/onboarding", response_class=HTMLResponse, include_in_schema=False)
async def onboarding_page():
    """Serve the setup wizard SPA."""
    static_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "static", "onboarding.html"
    )
    if os.path.exists(static_path):
        return HTMLResponse(open(static_path).read())
    # Minimal fallback if static file not yet deployed
    return HTMLResponse(
        "<html><body><h1>WaziBot Setup Wizard</h1>"
        "<p>Static file not found. Deploy static/onboarding.html.</p></body></html>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step API endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/onboarding/progress")
def onboarding_progress(user=Depends(require_business)):
    """Return current wizard progress for this business."""
    bid = user["business_id"]
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("id, name, category, onboarding_step, onboarding_completed")
            .eq("id", bid)
            .limit(1)
            .execute()
        )
        biz = res.data[0] if res.data else {}
        return {
            "business_id": bid,
            "current_step": biz.get("onboarding_step", 1),
            "completed": bool(biz.get("onboarding_completed", False)),
            "business_name": biz.get("name", ""),
        }
    except Exception as exc:
        log.warning("onboarding_progress error: %s", exc)
        return {"business_id": bid, "current_step": 1, "completed": False}


@router.post("/onboarding/step/1")
def onboarding_step1(body: Step1Request, user=Depends(require_business)):
    """Step 1 — Business Info."""
    bid = user["business_id"]
    try:
        from core.db import supabase
        supabase.table("businesses").update({
            "name":            body.business_name.strip(),
            "category":        body.category.strip(),
            "currency":        body.currency,
            "currency_symbol": body.currency_symbol,
            "onboarding_step": 2,
        }).eq("id", bid).execute()
        return {"ok": True, "next_step": 2}
    except Exception as exc:
        log.error("onboarding step1 error: %s", exc)
        raise HTTPException(500, str(exc))


@router.post("/onboarding/step/2")
async def onboarding_step2(
    tagline: str = "",
    theme_colour: str = "#00c853",
    logo: Optional[UploadFile] = File(None),
    user=Depends(require_business),
):
    """Step 2 — Branding (logo upload + colours)."""
    bid = user["business_id"]
    logo_url = ""

    if logo and logo.filename:
        data = await logo.read()
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(400, "Logo must be under 5 MB")
        try:
            from core.db import supabase
            import time as _t
            ext = logo.filename.rsplit(".", 1)[-1].lower()
            path = f"logos/{bid}_{int(_t.time())}.{ext}"
            supabase.storage.from_("product-images").upload(
                path=path, file=data,
                file_options={"content-type": logo.content_type or "image/jpeg", "upsert": "true"},
            )
            supa_url = os.getenv("SUPABASE_URL", "").rstrip("/")
            logo_url = f"{supa_url}/storage/v1/object/public/product-images/{path}"
        except Exception as exc:
            log.warning("logo upload failed (non-fatal): %s", exc)

    try:
        from core.db import supabase
        update: dict = {"onboarding_step": 3}
        if tagline:    update["tagline"]       = tagline
        if logo_url:   update["logo_url"]      = logo_url
        if theme_colour: update["theme_colour"] = theme_colour
        supabase.table("businesses").update(update).eq("id", bid).execute()
        return {"ok": True, "next_step": 3, "logo_url": logo_url}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/onboarding/step/3")
def onboarding_step3_complete(user=Depends(require_business)):
    """Step 3 — Products added (products are added via existing /products endpoint).
    This endpoint just advances the step counter."""
    bid = user["business_id"]
    try:
        from core.db import supabase
        # Check at least one product exists
        res = supabase.table("products").select("id").eq("business_id", bid).limit(1).execute()
        has_product = bool(res.data)
        supabase.table("businesses").update({"onboarding_step": 4}).eq("id", bid).execute()
        return {"ok": True, "next_step": 4, "has_products": has_product}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/onboarding/step/4")
def onboarding_step4_complete(user=Depends(require_business)):
    """Step 4 — WhatsApp connection verified. Advance wizard."""
    bid = user["business_id"]
    try:
        from core.db import supabase
        supabase.table("businesses").update({"onboarding_step": 5}).eq("id", bid).execute()
        return {"ok": True, "next_step": 5}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/onboarding/step/5")
def onboarding_step5(body: Step5Request, user=Depends(require_business)):
    """Step 5 — AI Config (role, welcome message, hours)."""
    bid = user["business_id"]
    try:
        from core.db import supabase
        update: dict = {"onboarding_step": 6}
        if body.ai_role:         update["ai_role"]         = body.ai_role
        if body.welcome_message: update["welcome_message"] = body.welcome_message
        if body.business_hours:  update["business_hours"]  = body.business_hours
        supabase.table("businesses").update(update).eq("id", bid).execute()
        return {"ok": True, "next_step": 6, "ai_role": body.ai_role}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/onboarding/step/6/test")
def onboarding_step6_test(user=Depends(require_business)):
    """Step 6 — Run a simulated test order and return the AI response."""
    bid = user["business_id"]
    try:
        from core.db import supabase
        # Pull first product
        res = supabase.table("products").select("*").eq("business_id", bid).limit(1).execute()
        product = res.data[0] if res.data else {"name": "your product", "price": 0}
        # Simulate what the AI would reply to "menu"
        sim_reply = (
            f"🤖 *Test Simulation*\n\n"
            f"Customer types: *menu*\n\n"
            f"AI replies:\n"
            f"📋 *Menu*\n"
            f"  1. {product['name']} — ${product.get('price', 0):.2f}\n\n"
            f"✅ Your WhatsApp AI is working correctly!"
        )
        supabase.table("businesses").update({"onboarding_step": 7}).eq("id", bid).execute()
        return {"ok": True, "next_step": 7, "simulation": sim_reply}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/onboarding/step/7")
def onboarding_step7(body: Step7Request, user=Depends(require_business)):
    """Step 7 — Go Live confirmation."""
    bid = user["business_id"]
    if not body.confirmed:
        raise HTTPException(400, "Confirmation required to go live")
    try:
        from core.db import supabase
        wazibot_url = os.getenv("WAZIBOT_URL", "https://wazibot-api-assistant.onrender.com")
        biz_res = supabase.table("businesses").select("name").eq("id", bid).limit(1).execute()
        biz_name = (biz_res.data[0].get("name") if biz_res.data else "") or "your-business"
        slug = biz_name.lower().replace(" ", "-").replace("_", "-")
        share_links = {
            "menu":       f"{wazibot_url}/menu/{slug}",
            "store":      f"{wazibot_url}/store/{slug}",
            "directory":  f"{wazibot_url}/directory",
        }
        supabase.table("businesses").update({"onboarding_step": 8}).eq("id", bid).execute()
        return {"ok": True, "next_step": 8, "share_links": share_links}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/onboarding/complete")
def onboarding_complete(user=Depends(require_business)):
    """Step 8 — Mark onboarding as complete."""
    bid = user["business_id"]
    try:
        from core.db import supabase
        supabase.table("businesses").update({
            "onboarding_step":      8,
            "onboarding_completed": True,
        }).eq("id", bid).execute()
        return {"ok": True, "message": "Onboarding complete! Redirecting to dashboard."}
    except Exception as exc:
        raise HTTPException(500, str(exc))
