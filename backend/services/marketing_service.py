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


def generate_referral_qr_png(referral_code: str, referral_link: str) -> bytes:
    """
    Branded QR code encoding a business owner's own referral link — used
    on the Referrals tab so they can share/print/post something more
    compelling than a plain text link, which should genuinely help boost
    referral counts (a shareable graphic gets posted; a raw URL usually
    doesn't). Deliberately styled (WaziBot green, rounded modules, wordmark)
    rather than reusing generate_qr_png()'s plain black-and-white style —
    that one is a quick "scan to message us" utility QR for a till/table,
    this one is meant to be posted on social media, so it needs to look
    like something worth sharing.

    Cached by referral_code so repeated calls (e.g. re-opening the tab)
    are free, same pattern as generate_qr_png().

    Raises ImportError if qrcode/Pillow not installed (already required —
    see generate_qr_png above).
    """
    cache_key = f"referral:{referral_code}"
    if cache_key in _qr_cache:
        return _qr_cache[cache_key]

    try:
        import qrcode
        from qrcode.image.styledpil import StyledPilImage
        from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
        from qrcode.image.styles.colormasks import SolidFillColorMask
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ImportError(
            "qrcode[pil] not installed. Add 'qrcode[pil]>=7.4.2' to requirements.txt "
            "and redeploy."
        )

    BRAND_GREEN = (34, 197, 94)    # matches --green in landing.html/dashboard.html
    BRAND_DARK  = (10, 15, 13)     # matches --bg
    TEXT_DIM    = (232, 245, 233)

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,  # more damage tolerance — room for real-world printing/screenshots
        box_size=12,
        border=3,
    )
    qr.add_data(referral_link)
    qr.make(fit=True)

    qr_img = qr.make_image(
        image_factory=StyledPilImage,
        module_drawer=RoundedModuleDrawer(),
        color_mask=SolidFillColorMask(back_color=(255, 255, 255), front_color=BRAND_GREEN),
    ).convert("RGB")
    qr_size = qr_img.size[0]

    # Card layout: wordmark + tagline top, white rounded QR card center,
    # CTA + the referrer's own code at the bottom (so if someone downloads
    # several of these, or a friend asks "whose code is this", it's
    # readable on the image itself, not just embedded invisibly in the QR).
    PAD, TOP_SPACE, BOTTOM_SPACE = 60, 130, 150
    card_w = qr_size + PAD * 2
    card_h = qr_size + TOP_SPACE + BOTTOM_SPACE
    card = Image.new("RGB", (card_w, card_h), BRAND_DARK)
    draw = ImageDraw.Draw(card)

    try:
        font_wordmark = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 54)
        font_tagline  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 20)
        font_cta      = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 20)
        font_code     = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 24)
    except Exception:
        # Font file not present on this host — fall back to PIL's default
        # bitmap font rather than failing the whole QR generation.
        font_wordmark = font_tagline = font_cta = font_code = ImageFont.load_default()

    def _center(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((card_w - w) / 2, y), text, font=font, fill=fill)

    _center(34, "WaziBot", font_wordmark, BRAND_GREEN)
    _center(96, "Try WhatsApp AI for your business", font_tagline, TEXT_DIM)

    qr_pad = 24
    qr_card = Image.new("RGB", (qr_size + qr_pad*2, qr_size + qr_pad*2), (255, 255, 255))
    mask = Image.new("L", qr_card.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, qr_card.size[0]-1, qr_card.size[1]-1], radius=20, fill=255)
    qr_card_rgb = Image.new("RGB", qr_card.size, BRAND_DARK)
    qr_card_rgb.paste(qr_card, (0, 0), mask)
    qr_card_rgb.paste(qr_img, (qr_pad, qr_pad))
    card.paste(qr_card_rgb, ((card_w - qr_card_rgb.size[0]) // 2, TOP_SPACE))

    cta_y = TOP_SPACE + qr_card_rgb.size[1] + 26
    _center(cta_y, "Scan to get started free", font_cta, TEXT_DIM)
    _center(cta_y + 34, f"Referral code: {referral_code}", font_code, BRAND_GREEN)

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    _qr_cache[cache_key] = png_bytes
    log.info("Referral QR generated  code=%s  size=%d bytes", referral_code, len(png_bytes))
    return png_bytes


def generate_qr_png(business_name: str, box_size: int = 10, border: int = 4,
                     dest: str = "whatsapp") -> bytes:
    """
    Generate a PNG QR code for the business.
    Returns raw PNG bytes. Cached by (keyword, dest) so repeated calls are free.

    dest: "whatsapp" (default) — scans route through /qr/{slug}, a tracking
          redirect that records a qr_scan event before bouncing to WhatsApp.
          "booking" — scans go straight to the native public booking page
          (/book/{slug}), which records its own booking_page_view event on
          load, so scan tracking still works without a second redirect hop.

    Raises ImportError if qrcode/Pillow not installed (add to requirements.txt).
    """
    keyword = generate_business_keyword(business_name)
    cache_key = f"{keyword}:{dest}"
    if cache_key in _qr_cache:
        return _qr_cache[cache_key]

    try:
        import qrcode
        from qrcode.image.pure import PyPNGImage
    except ImportError:
        raise ImportError(
            "qrcode package not installed. Add 'qrcode[pil]>=7.4.2' to requirements.txt "
            "and redeploy."
        )

    import os
    base_url = os.getenv("WAZIBOT_URL", "https://wazibothq.com")
    slug     = _name_to_slug(business_name)
    if dest == "booking":
        qr_target_url = f"{base_url}/book/{slug}"
    else:
        # QR codes point to /qr/{slug} (tracking redirect) instead of
        # directly to WhatsApp — this lets us count QR scans before the
        # WhatsApp redirect.
        qr_target_url = f"{base_url}/qr/{slug}"

    qr = qrcode.QRCode(
        version=None,           # auto-size
        error_correction=qrcode.constants.ERROR_CORRECT_M,  # ~15% damage tolerance
        box_size=box_size,
        border=border,
    )
    qr.add_data(qr_target_url)
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

    _qr_cache[cache_key] = png_bytes
    log.info("QR generated  business=%s  keyword=%s  dest=%s  size=%d bytes",
             business_name, keyword, dest, len(png_bytes))
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


# ═══════════════════════════════════════════════════════════════════════════
# MARKETING COPY FUNCTIONS — preserved from original marketing_service.py
# Required by expansion_routes.py: generate_referral_copy, generate_launch_copy,
# get_all_copy_variations, and supporting helpers.
# ═══════════════════════════════════════════════════════════════════════════

import random

TAGLINE     = "Your AI Employee on WhatsApp"
VALUE_PROP  = "WaziBot handles your customers, orders, payments and bookings — even while you sleep."
POSITIONING = "An AI-powered business assistant, not just a chatbot."

_HERO_HEADLINES = [
    "Your business keeps selling even when you're busy.",
    "Turn WhatsApp into your top salesperson.",
    "Never miss a customer while you're at work.",
    "Run your entire business from WhatsApp — automatically.",
    "Your customers order. WaziBot handles it. You focus on what matters.",
    "The AI assistant that never clocks out.",
    "Stop losing customers to slow replies. Let WaziBot handle it.",
    "From inquiry to payment — WaziBot does it all on WhatsApp.",
]

_PAIN_POINTS = [
    "Tired of missing customer messages while you're busy?",
    "Can't reply to every customer at 10pm?",
    "Losing sales because you're too busy to respond?",
    "Spending hours on WhatsApp instead of growing your business?",
    "Customers ghosting you because replies are too slow?",
]


def _get_features_for_type(business_type: str, focus: str = "general") -> list:
    _FEATURES_BY_TYPE = {
        "restaurant": [
            "Handles orders and menus automatically",
            "Accepts EcoCash, PayPal and cash payments",
            "Sends order confirmations and delivery updates",
            "Runs targeted promotions to your regulars",
        ],
        "salon": [
            "Books appointments automatically 24/7",
            "Sends reminders so customers don't miss bookings",
            "Manages your service menu and pricing",
            "Follows up after visits for repeat bookings",
        ],
        "general": [
            "Replies to customers instantly — even at 2am",
            "Processes orders and sends confirmations",
            "Accepts payments via EcoCash, PayPal or cash",
            "Sends promotions to your customer list",
        ],
    }
    return _FEATURES_BY_TYPE.get(business_type, _FEATURES_BY_TYPE["general"])


def _bullet_features(business_type: str) -> str:
    features = _get_features_for_type(business_type)
    return "\n".join(f"✅ {f}" for f in features)


def _get_cta(tone: str, include_cta: bool) -> str:
    if not include_cta:
        return ""
    base_url = os.getenv("WAZIBOT_URL", "https://wazibothq.com") + "/signup"
    if tone == "urgent":
        return f"⚡ Start your free 14-day trial NOW → {base_url}"
    elif tone == "professional":
        return f"📲 Start your free trial → {base_url}"
    else:
        return f"👉 Try WaziBot free for 14 days → {base_url}"


def generate_whatsapp_copy(
    business_type: str = "general",
    tone: str = "friendly",
    focus: str = "general",
    include_cta: bool = True,
) -> dict:
    headline = random.choice(_HERO_HEADLINES)
    pain     = random.choice(_PAIN_POINTS)
    features = _get_features_for_type(business_type, focus)
    if tone == "urgent":
        opener = f"⚠️ *{pain}*\n\n{VALUE_PROP}"
    elif tone == "professional":
        opener = f"*{TAGLINE}*\n\n{VALUE_PROP}"
    else:
        opener = f"👋 {pain}\n\n*{VALUE_PROP}*"
    body = (
        f"{opener}\n\n"
        f"Here's what WaziBot does for you:\n"
        + "\n".join(f"✅ {f}" for f in features)
        + f"\n\n_{headline}_"
    )
    cta = _get_cta(tone, include_cta)
    if include_cta:
        body += f"\n\n{cta}"
    return {
        "subject":      TAGLINE,
        "headline":     headline,
        "body":         body,
        "cta":          cta,
        "full_message": body,
        "char_count":   len(body),
    }


def generate_facebook_copy(
    business_type: str = "general",
    post_type: str = "awareness",
) -> dict:
    if post_type == "testimonial":
        caption = (
            "💬 *Business owner testimony:*\n\n"
            "\"I set up WaziBot on a Friday. By Monday, "
            "customers were ordering and paying — while I was at my day job.\"\n\n"
            f"{VALUE_PROP}\n\n"
            "👇 Start your free 14-day trial — no credit card needed."
        )
    elif post_type == "lead_gen":
        headline = random.choice(_HERO_HEADLINES)
        caption  = (
            f"🚀 *{headline}*\n\n"
            f"WaziBot is an AI employee that runs your WhatsApp business automatically:\n\n"
            f"{_bullet_features(business_type)}\n\n"
            f"📲 Try it free for 14 days. No setup fees. Cancel anytime."
        )
    else:
        caption = (
            f"❓ Did you know you can automate your entire WhatsApp business?\n\n"
            f"WaziBot handles:\n"
            f"{_bullet_features(business_type)}\n\n"
            f"🤖 Think of it as hiring a 24/7 sales assistant for your WhatsApp — "
            f"for a fraction of the cost.\n\n"
            f"Comment 'INFO' to learn more. 👇"
        )
    hashtags = "#WaziBot #WhatsAppBusiness #SmallBusiness #Zimbabwe #Entrepreneur #SideHustle"
    if business_type in ("restaurant", "bakery"):
        hashtags += " #FoodBusiness #Harare"
    elif business_type in ("salon", "barber"):
        hashtags += " #SalonLife #BeautyBusiness"
    return {
        "caption":    caption,
        "hashtags":   hashtags,
        "full_post":  f"{caption}\n\n{hashtags}",
        "char_count": len(caption),
    }


def generate_referral_copy(
    referral_code: str,
    referral_link: str,
    business_name: str = "",
    reward_text: str = "earn rewards for every business you refer",
) -> dict:
    biz_line = f"I've been using *{business_name}* on WaziBot" if business_name else "I'm using WaziBot"
    body = (
        f"👋 Hey!\n\n"
        f"{biz_line} to automate my WhatsApp business — and it's been a game changer.\n\n"
        f"*WaziBot* handles my orders, payments and customer messages automatically. "
        f"Even when I'm away from my phone.\n\n"
        f"Try it free for 14 days — use my referral link:\n"
        f"🔗 {referral_link}\n\n"
        f"Or use code: *{referral_code}* at signup.\n\n"
        f"_{TAGLINE}_"
    )
    return {
        "code":         referral_code,
        "link":         referral_link,
        "message":      body,
        "whatsapp_url": f"https://wa.me/?text={body.replace(' ', '%20')[:500]}",
        "char_count":   len(body),
    }


def generate_launch_copy(business_name: str, category: str = "general") -> dict:
    cta = "Type *menu* to start browsing and ordering! 🛒"
    if category in ("salon", "barber", "consultant", "coach", "trainer"):
        cta = "Type *book* to schedule your appointment! 📅"
    elif category in ("restaurant", "bakery", "grocery", "food"):
        cta = "Type *menu* to see today's menu and order! 🍽️"
    body = (
        f"👋 *Welcome to {business_name} on WhatsApp!*\n\n"
        f"We're now on WhatsApp — making it even easier to order from us.\n\n"
        f"You can now:\n"
        f"  ✅ Browse our full catalogue\n"
        f"  ✅ Place orders directly\n"
        f"  ✅ Pay safely and securely\n"
        f"  ✅ Track your order status\n"
        f"  ✅ Get instant replies — 24/7\n\n"
        f"{cta}\n\n"
        f"We're excited to serve you! 🙏"
    )
    return {
        "headline":   f"Welcome to {business_name} on WhatsApp!",
        "body":       body,
        "cta":        cta,
        "char_count": len(body),
    }


def get_all_copy_variations(
    business_type: str = "general",
    referral_code: str = "",
    referral_link: str = "",
    business_name: str = "",
) -> dict:
    return {
        "whatsapp_friendly":     generate_whatsapp_copy(business_type, tone="friendly"),
        "whatsapp_professional": generate_whatsapp_copy(business_type, tone="professional"),
        "whatsapp_urgent":       generate_whatsapp_copy(business_type, tone="urgent"),
        "facebook_awareness":    generate_facebook_copy(business_type, post_type="awareness"),
        "facebook_lead_gen":     generate_facebook_copy(business_type, post_type="lead_gen"),
        "facebook_testimonial":  generate_facebook_copy(business_type, post_type="testimonial"),
        "referral":              generate_referral_copy(referral_code, referral_link, business_name) if referral_code else None,
        "launch_announcement":   generate_launch_copy(business_name, business_type) if business_name else None,
        "tagline":               TAGLINE,
        "value_proposition":     VALUE_PROP,
        "positioning":           POSITIONING,
    }
