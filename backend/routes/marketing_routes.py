"""
routes/marketing_routes.py — WaziBot Marketing Kit API

Endpoints (all require business auth — Feature 8 security):
  GET  /marketing/kit          — full kit: keyword, link, QR URL, tips
  GET  /marketing/qr           — serve QR PNG (for <img> tag in dashboard)
  GET  /marketing/qr/download  — download QR PNG with proper filename
  GET  /marketing/keyword      — just the START keyword

All endpoints return only the authenticated business's own data.
Never exposes another business's QR or keyword.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, RedirectResponse
import logging
import re

from core.auth import require_business

log    = logging.getLogger(__name__)
router = APIRouter()


# ── Public redirect endpoints — track then redirect ───────────────────────────
# These are the URLs embedded in QR codes and website buttons.
# They record the event silently then redirect instantly to WhatsApp.
# Public — no auth required (customers don't have WaziBot accounts).

def _slug_to_business_id(slug: str) -> tuple[int, str]:
    """
    Look up a business by slug (converted from name).
    Returns (business_id, business_name) or (0, "").
    """
    try:
        from core.db import supabase
        # Convert slug back to name pattern (flavoury-foods → flavoury foods)
        name_pattern = slug.replace("-", " ")
        res = (
            supabase.table("businesses")
            .select("id, name")
            .ilike("name", f"%{name_pattern}%")
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        if res.data:
            b = res.data[0]
            return b["id"], b.get("name", "")
    except Exception as exc:
        log.debug("_slug_to_business_id error slug=%s err=%s", slug, exc)
    return 0, ""


def _build_wa_redirect(business_name: str) -> str:
    """Build the WhatsApp redirect URL for a business."""
    import os
    number  = re.sub(r"[^\d]", "", os.getenv("SHARED_WHATSAPP_NUMBER", "447774128484"))
    keyword = f"START {re.sub(r'[^a-z0-9]+', '-', (business_name or '').lower()).strip('-').upper()}"
    encoded = keyword.replace(" ", "%20")
    return f"https://wa.me/{number}?text={encoded}"


@router.get("/qr/{slug}", include_in_schema=False)
def qr_redirect(slug: str):
    """
    QR code redirect endpoint.
    QR codes point here instead of directly to WhatsApp.
    Records qr_scan event, then redirects instantly to WhatsApp.
    No login required — this is a public customer-facing URL.
    """
    business_id, name = _slug_to_business_id(slug)
    if business_id:
        # Fire-and-forget — never block the redirect
        try:
            from services.acquisition_service import record_qr_scan
            record_qr_scan(business_id)
        except Exception:
            pass
        wa_url = _build_wa_redirect(name)
    else:
        # Slug not found — redirect to WaziBot homepage
        wa_url = "https://wa.me/447774128484"

    # 302 = temporary redirect (not cached by browsers)
    return RedirectResponse(url=wa_url, status_code=302)


@router.get("/go/{slug}", include_in_schema=False)
def link_click_redirect(slug: str):
    """
    WhatsApp button click redirect endpoint.
    Website 'Chat on WhatsApp' buttons point here.
    Records whatsapp_click event, then redirects instantly.
    """
    business_id, name = _slug_to_business_id(slug)
    if business_id:
        try:
            from services.acquisition_service import record_whatsapp_click
            record_whatsapp_click(business_id)
        except Exception:
            pass
        wa_url = _build_wa_redirect(name)
    else:
        wa_url = "https://wa.me/447774128484"

    return RedirectResponse(url=wa_url, status_code=302)


# ── Acquisition stats endpoint — authenticated ────────────────────────────────

@router.get("/analytics/acquisition")
def get_acquisition_analytics(user=Depends(require_business)):
    """
    Return customer acquisition funnel stats for the authenticated business.
    {qr_scans, whatsapp_clicks, conversations_started, orders, conversion_rate,
     today: {...}, this_month: {...}}
    """
    from services.acquisition_service import get_acquisition_stats
    return get_acquisition_stats(user["business_id"])


@router.get("/marketing/kit")
def get_marketing_kit_endpoint(user=Depends(require_business)):
    """Return the full marketing kit for the authenticated business."""
    from services.marketing_service import get_marketing_kit
    kit = get_marketing_kit(user["business_id"])
    if "error" in kit:
        raise HTTPException(404, kit["error"])
    return kit


@router.get("/marketing/keyword")
def get_keyword(user=Depends(require_business)):
    """Return just the START keyword for this business."""
    from services.marketing_service import get_marketing_kit
    kit = get_marketing_kit(user["business_id"])
    if "error" in kit:
        raise HTTPException(404, kit["error"])
    return {
        "keyword":      kit["keyword"],
        "whatsapp_link": kit["whatsapp_link"],
        "shared_number": kit["shared_number"],
    }


@router.get("/marketing/qr")
def get_qr_image(user=Depends(require_business)):
    """
    Serve the QR code PNG for the authenticated business.
    Returns image/png — use as <img src="/marketing/qr"> with auth header.
    Security: authenticated endpoint — only serves the caller's own QR.
    """
    from services.marketing_service import get_marketing_kit, generate_qr_png
    kit = get_marketing_kit(user["business_id"])
    if "error" in kit:
        raise HTTPException(404, kit["error"])
    try:
        png_bytes = generate_qr_png(kit["business_name"])
    except ImportError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        log.error("QR generation failed  business=%s  error=%s", user["business_id"], exc)
        raise HTTPException(500, "QR generation failed — please try again")
    return Response(content=png_bytes, media_type="image/png")


@router.get("/marketing/qr/download")
def download_qr(user=Depends(require_business)):
    """
    Download QR PNG with a descriptive filename.
    Feature 4: 'flavoury-foods-whatsapp-qr.png'
    """
    from services.marketing_service import get_marketing_kit, generate_qr_png, _name_to_slug
    kit = get_marketing_kit(user["business_id"])
    if "error" in kit:
        raise HTTPException(404, kit["error"])
    try:
        png_bytes = generate_qr_png(kit["business_name"])
    except ImportError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        raise HTTPException(500, "QR generation failed")

    slug     = kit["slug"]
    filename = f"{slug}-whatsapp-qr.png"
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
