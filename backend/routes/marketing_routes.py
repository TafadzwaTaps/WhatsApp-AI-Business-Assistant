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
from fastapi.responses import Response
import logging

from core.auth import require_business

log    = logging.getLogger(__name__)
router = APIRouter()


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
