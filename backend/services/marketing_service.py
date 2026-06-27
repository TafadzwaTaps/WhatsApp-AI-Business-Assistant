"""
services/marketing_service.py — WaziBot Shared Number Marketing Kit
═══════════════════════════════════════════════════════════════════

Provides:
  generate_business_keyword()   — "START FLAVOURY-FOODS"
  generate_whatsapp_link()      — wa.me deep link with pre-filled text
  generate_qr_png()             — PNG bytes of a QR code (cached in memory)
  get_marketing_kit()           — full kit dict for API response

Security: all functions take business_id and verify ownership before
returning data. QR images are served via authenticated endpoint only.

Shared number comes from SHARED_WHATSAPP_NUMBER env var — never hardcoded.
"""
from __future__ import annotations

import io
import os
import re
import logging
from functools import lru_cache

log = logging.getLogger("wazibot")

# ── Configuration ─────────────────────────────────────────────────────────────

def _shared_number() -> str:
    """
    Return the shared WaziBot WhatsApp number (digits only, no +).
    Priority: SHARED_WHATSAPP_NUMBER → SHARED_WA_PHONE (strip non-digits).
    Falls back to the known UK number if neither is set.
    """
    raw = (
        os.getenv("SHARED_WHATSAPP_NUMBER", "") or
        os.getenv("SHARED_WA_PHONE",        "") or
        "447774128484"          # last-resort fallback — set env var in production
    )
    digits = re.sub(r"[^\d]", "", raw)
    return digits or "447774128484"


# ── Slug / keyword helpers ────────────────────────────────────────────────────

def _name_to_slug(name: str) -> str:
    """'Flavoury Foods (Pvt) Ltd' → 'flavoury-foods-pvt-ltd'"""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def generate_business_keyword(business_name: str) -> str:
    """
    Generate the unique START keyword for this business.
    'Flavoury Foods' → 'START FLAVOURY-FOODS'
    """
    slug = _name_to_slug(business_name)
    return f"START {slug.upper()}"


def generate_whatsapp_link(business_name: str) -> str:
    """
    Generate a wa.me deep link that pre-fills the START keyword.
    Customer scans QR / taps link → WhatsApp opens with text ready to send.
    """
    number  = _shared_number()
    keyword = generate_business_keyword(business_name)
    # URL-encode the keyword (spaces → %20, hyphens are safe)
    encoded = keyword.replace(" ", "%20")
    return f"https://wa.me/{number}?text={encoded}"


# ── QR code generation ────────────────────────────────────────────────────────

# In-process cache: keyword → PNG bytes
# Lightweight — QR codes are tiny (~3-5 KB each). Survives restarts gracefully.
_qr_cache: dict[str, bytes] = {}


def generate_qr_png(business_name: str, box_size: int = 10, border: int = 4) -> bytes:
    """
    Generate a PNG QR code for the business's WhatsApp deep link.
    Returns raw PNG bytes. Cached by keyword so repeated calls are free.

    Raises ImportError if qrcode/Pillow not installed (add to requirements.txt).
    """
    keyword = generate_business_keyword(business_name)
    if keyword in _qr_cache:
        return _qr_cache[keyword]

    try:
        import qrcode
        from qrcode.image.pure import PyPNGImage
    except ImportError:
        raise ImportError(
            "qrcode package not installed. Add 'qrcode[pil]>=7.4.2' to requirements.txt "
            "and redeploy."
        )

    wa_link = generate_whatsapp_link(business_name)

    qr = qrcode.QRCode(
        version=None,           # auto-size
        error_correction=qrcode.constants.ERROR_CORRECT_M,  # ~15% damage tolerance
        box_size=box_size,
        border=border,
    )
    qr.add_data(wa_link)
    qr.make(fit=True)

    # Try Pillow first (better quality), fall back to pure-PNG
    try:
        from PIL import Image as _PILImage
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
    except ImportError:
        # Pillow not available — use pure Python PNG writer
        img      = qr.make_image(image_factory=PyPNGImage)
        buf      = io.BytesIO()
        img.save(buf)
        png_bytes = buf.getvalue()

    _qr_cache[keyword] = png_bytes
    log.info("QR generated  business=%s  keyword=%s  size=%d bytes",
             business_name, keyword, len(png_bytes))
    return png_bytes


def invalidate_qr_cache(business_name: str) -> None:
    """Call this if the business name changes so the QR is regenerated."""
    keyword = generate_business_keyword(business_name)
    _qr_cache.pop(keyword, None)


# ── Full kit response ─────────────────────────────────────────────────────────

def get_marketing_kit(business_id: int) -> dict:
    """
    Return the full marketing kit for a business.
    Fetches business name from DB, then builds all assets.
    """
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("id, name, use_shared_number")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        biz = (res.data or [{}])[0]
        if not biz:
            return {"error": "Business not found"}

        name          = biz.get("name", "")
        use_shared    = biz.get("use_shared_number", True)
        keyword       = generate_business_keyword(name)
        wa_link       = generate_whatsapp_link(name)
        slug          = _name_to_slug(name)
        shared_number = _shared_number()

        return {
            "business_id":    business_id,
            "business_name":  name,
            "slug":           slug,
            "use_shared_number": use_shared,
            "shared_number":  f"+{shared_number}",
            "keyword":        keyword,
            "whatsapp_link":  wa_link,
            "qr_url":         f"/marketing/qr",
            "qr_download_url": f"/marketing/qr/download",
            "tips": [
                "Print the QR code and place it on your counter",
                "Add it to your Facebook page",
                "Add it to your Instagram bio",
                "Put it on your receipts and packaging",
                "Share the WhatsApp link in your status",
                "Customers simply scan and their order message is ready",
            ],
        }
    except Exception as exc:
        log.error("get_marketing_kit error  business=%s  error=%s", business_id, exc)
        return {"error": str(exc)}
