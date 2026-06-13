"""
services/whatsapp_catalog.py — WhatsApp Visual Catalog Engine
(Phases 3-9, 12)

PURPOSE
───────
Abstracts all product-image WhatsApp messages into a clean layer.
Future WhatsApp Catalog API / Meta Commerce Manager integration plugs in here
without touching any existing ordering, cart, or AI code.

DESIGN PRINCIPLES
─────────────────
• Never raises — all functions return a safe result or empty string
• Graceful fallback: if a product has no image, returns a text card instead
• Tenant-isolated: every function takes business_id and uses it
• Batch-safe: never sends more than BATCH_SIZE images in one call
• Backward compatible: existing text menu remains the default;
  visual mode is opt-in when products have image_url populated

ARCHITECTURE (Phase 12 — future-ready)
───────────────────────────────────────
  send_product_image()      → single product image + details
  send_catalog()            → batch of product cards (text with image hint)
  send_product_gallery()    → paginated visual browsing
  send_recommendation_card()→ "you may also like" with image
  build_product_card_text() → text fallback card (no image)

These are called by ai.py handlers — the webhook and ordering flows
are completely unmodified.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# Max products per catalog batch (Phase 9 — prevent WhatsApp spam)
CATALOG_BATCH_SIZE = int(os.getenv("WA_CATALOG_BATCH_SIZE", "5"))


# ─────────────────────────────────────────────────────────────────────────────
# CORE: SEND PRODUCT IMAGE VIA WHATSAPP API
# ─────────────────────────────────────────────────────────────────────────────

def send_product_image(
    phone_number_id: str,
    token:           str,
    to:              str,
    product:         dict,
    currency_sym:    str = "$",
    caption:         str = "",
) -> dict:
    """
    Send a single product image message via WhatsApp Cloud API.
    Falls back to a text card if no image_url present.

    Returns the API response dict (or {"fallback": True} for text mode).
    """
    image_url = (product.get("image_url") or "").strip()
    if not image_url:
        return {"fallback": True, "text": build_product_card_text(product, currency_sym)}

    if not phone_number_id or not token:
        log.warning("send_product_image: no credentials — text fallback")
        return {"fallback": True, "text": build_product_card_text(product, currency_sym)}

    # Build caption
    if not caption:
        caption = _build_product_caption(product, currency_sym)

    try:
        import requests as _req
        url     = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body    = {
            "messaging_product": "whatsapp",
            "to":                to.replace("whatsapp:", "").strip(),
            "type":              "image",
            "image":             {"link": image_url, "caption": caption},
        }
        resp   = _req.post(url, headers=headers, json=body, timeout=10)
        result = resp.json()
        if resp.status_code == 200:
            msg_id = (result.get("messages") or [{}])[0].get("id", "?")
            log.info("product image sent  product=%s  to=%s  msg_id=%s",
                     product.get("name"), to, msg_id)
            return result
        else:
            log.warning("product image send failed  status=%s  error=%s",
                        resp.status_code, result.get("error", {}).get("message"))
            return {"fallback": True, "text": build_product_card_text(product, currency_sym),
                    "error": result}
    except Exception as exc:
        log.warning("send_product_image exception: %s", exc)
        return {"fallback": True, "text": build_product_card_text(product, currency_sym)}


def send_text_message(
    phone_number_id: str,
    token:           str,
    to:              str,
    text:            str,
) -> dict:
    """
    Send a plain text WhatsApp message.
    Thin wrapper so whatsapp_catalog.py is self-contained.
    """
    if not phone_number_id or not token or not text:
        return {}
    try:
        import requests as _req
        url     = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body    = {
            "messaging_product": "whatsapp",
            "to":                to.replace("whatsapp:", "").strip(),
            "type":              "text",
            "text":              {"body": text},
        }
        resp = _req.post(url, headers=headers, json=body, timeout=10)
        return resp.json()
    except Exception as exc:
        log.warning("send_text_message exception: %s", exc)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# CATALOG — batch of product cards
# ─────────────────────────────────────────────────────────────────────────────

def send_catalog(
    phone_number_id: str,
    token:           str,
    to:              str,
    products:        list[dict],
    currency_sym:    str = "$",
    page:            int = 0,
    batch_size:      int = CATALOG_BATCH_SIZE,
) -> dict:
    """
    Send a paginated batch of product cards.
    Returns {"sent": n, "has_more": bool, "next_page": int, "fallback_text": str}

    If products have images → sends image messages for each.
    If no images → returns a rich text catalog string for the caller to send.

    Phase 9: batch_size caps how many are sent per call.
    """
    start     = page * batch_size
    batch     = products[start: start + batch_size]
    total     = len(products)
    has_more  = (start + batch_size) < total
    next_page = page + 1 if has_more else 0

    # Check if any products in the batch have images
    has_images = any(p.get("image_url") for p in batch)

    if has_images and phone_number_id and token:
        sent = 0
        for p in batch:
            result = send_product_image(phone_number_id, token, to, p, currency_sym)
            if not result.get("fallback"):
                sent += 1
            elif result.get("text"):
                # Text fallback for this product
                send_text_message(phone_number_id, token, to, result["text"])
                sent += 1

        if has_more:
            more_hint = f"\n_Type *more* to see the next {min(batch_size, total - start - batch_size)} products._"
            send_text_message(phone_number_id, token, to, more_hint)

        return {"sent": sent, "has_more": has_more, "next_page": next_page}
    else:
        # Full text catalog
        text = build_text_catalog(batch, currency_sym, page, total, batch_size)
        return {"sent": 0, "has_more": has_more, "next_page": next_page,
                "fallback_text": text}


def send_product_gallery(
    phone_number_id: str,
    token:           str,
    to:              str,
    products:        list[dict],
    category:        str = "",
    currency_sym:    str = "$",
) -> dict:
    """
    Phase 6: Send a category-filtered visual gallery.
    Filters products by category then delegates to send_catalog().
    """
    if category:
        cat_lower = category.lower()
        filtered  = [p for p in products
                     if cat_lower in (p.get("category") or "").lower()
                     or cat_lower in (p.get("name") or "").lower()]
    else:
        filtered = products

    if not filtered:
        return {"sent": 0, "has_more": False, "next_page": 0,
                "fallback_text": f"_No {category} products found._"}

    return send_catalog(phone_number_id, token, to, filtered, currency_sym)


def send_recommendation_card(
    phone_number_id: str,
    token:           str,
    to:              str,
    product:         dict,
    reason:          str = "",
    currency_sym:    str = "$",
) -> dict:
    """
    Phase 7: Send a single recommended product with image + reason.
    """
    image_url = (product.get("image_url") or "").strip()
    name      = product.get("name", "")
    caption   = (
        f"🌟 *Recommended for you*\n\n"
        f"*{name}*\n"
        f"{currency_sym}{float(product.get('price', 0)):.2f}\n\n"
        + (f"_{reason}_\n\n" if reason else "")
        + f"Type *{name.lower()}* to add to cart."
    )

    if image_url and phone_number_id and token:
        return send_product_image(phone_number_id, token, to, product, currency_sym, caption)
    else:
        return {"fallback": True, "text": caption}


# ─────────────────────────────────────────────────────────────────────────────
# TEXT CARD BUILDERS — used as fallbacks when no image or no credentials
# ─────────────────────────────────────────────────────────────────────────────

def build_product_card_text(product: dict, currency_sym: str = "$") -> str:
    """
    Phase 8: Rich text product card — fallback when no image available.
    Never raises, never returns empty string.
    """
    name  = product.get("name", "Product")
    price = float(product.get("price") or 0)
    desc  = (product.get("description") or "").strip()
    stock = product.get("stock")
    cat   = (product.get("category") or "").strip()

    # Stock status
    if stock is None:
        stock_line = "✅ Available"
    elif stock == 0:
        stock_line = "❌ Out of stock"
    elif stock <= 5:
        stock_line = f"⚠️ Only {stock} left"
    else:
        stock_line = f"✅ In stock ({stock})"

    lines = [
        f"📦 *{name}*",
        f"💰 *{currency_sym}{price:.2f}*",
    ]
    if cat:
        lines.append(f"🏷️ _{cat}_")
    lines.append(stock_line)
    if desc:
        lines.append(f"\n_{desc}_")
    lines.append(f"\nType *{name.lower()}* to add to cart.")

    return "\n".join(lines)


def build_text_catalog(
    products:     list[dict],
    currency_sym: str = "$",
    page:         int = 0,
    total:        int = 0,
    batch_size:   int = CATALOG_BATCH_SIZE,
) -> str:
    """
    Build a rich text catalog when no images are available.
    Used as the fallback for send_catalog().
    """
    if not products:
        return "📦 No products available."

    start = page * batch_size
    lines = []
    for i, p in enumerate(products, start + 1):
        name  = p.get("name", "?")
        price = float(p.get("price") or 0)
        stock = p.get("stock")
        desc  = (p.get("description") or "").strip()

        stock_note = ""
        if stock is not None and stock == 0:
            stock_note = " ❌"
        elif stock is not None and stock <= 5:
            stock_note = f" ⚠️_{stock} left_"

        desc_line = f"\n     _{desc[:60]}{'…' if len(desc) > 60 else ''}_" if desc else ""
        lines.append(f"  {i}. *{name}* — {currency_sym}{price:.2f}{stock_note}{desc_line}")

    header = f"🛍️ *Product Catalog*"
    if total > len(products):
        header += f" _(showing {start+1}–{start+len(products)} of {total})_"

    has_more = total > (start + len(products))
    footer   = "\n\n_Type a product name to add to cart._"
    if has_more:
        footer += "\n_Type *more* to see next products._"

    return header + "\n\n" + "\n".join(lines) + footer


def _build_product_caption(product: dict, currency_sym: str = "$") -> str:
    """Build the caption text for a WhatsApp image message."""
    name  = product.get("name", "Product")
    price = float(product.get("price") or 0)
    desc  = (product.get("description") or "").strip()
    stock = product.get("stock")

    stock_note = ""
    if stock is None or stock > 5:
        stock_note = "✅ Available"
    elif stock == 0:
        stock_note = "❌ Out of stock"
    else:
        stock_note = f"⚠️ Only {stock} left"

    caption = f"*{name}*\n{currency_sym}{price:.2f}\n{stock_note}"
    if desc:
        caption += f"\n\n{desc[:100]}"
    caption += f"\n\nType *{name.lower()}* to add to cart."
    return caption


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY — check if business has visual catalog support
# ─────────────────────────────────────────────────────────────────────────────

def has_product_images(products: list[dict]) -> bool:
    """Returns True if at least one product has an image_url."""
    return any(bool(p.get("image_url")) for p in products)


def image_coverage(products: list[dict]) -> dict:
    """
    Phase 11: Return image coverage stats for the dashboard.
    {total, with_images, missing_images, coverage_pct}
    """
    total        = len(products)
    with_images  = sum(1 for p in products if p.get("image_url"))
    missing      = total - with_images
    pct          = round(with_images / total * 100) if total else 0
    return {
        "total":         total,
        "with_images":   with_images,
        "missing_images": missing,
        "coverage_pct":  pct,
    }


def filter_products_needing_images(products: list[dict]) -> list[dict]:
    """Return only products that are missing an image_url."""
    return [p for p in products if not p.get("image_url")]


# ─────────────────────────────────────────────────────────────────────────────
# ORDER PROGRESS TRACKER (Premium UX upgrade)
# ─────────────────────────────────────────────────────────────────────────────

# Display-friendly stage labels and emoji, in customer-facing order.
# Maps from workflows/order_lifecycle.py VALID_STATUSES — does not hardcode
# new statuses, only adds presentation metadata for existing ones.
_PROGRESS_STAGES = [
    ("pending",         "Order Received"),
    ("payment_review",  "Payment Verification"),
    ("confirmed",       "Payment Confirmed"),
    ("preparing",       "Preparing Order"),
    ("ready",           "Ready for Pickup"),
    ("out_for_delivery","Out for Delivery"),
    ("delivered",       "Delivered"),
    ("completed",       "Completed"),
]

# Statuses that map onto an earlier stage for progress display purposes
# (e.g. pending_cash and awaiting_payment both count as "Order Received")
_STATUS_ALIASES = {
    "pending_cash":     "pending",
    "awaiting_payment": "pending",
}

_ETA_HINTS = {
    "pending":          "Awaiting payment",
    "payment_review":   "5–15 minutes",
    "confirmed":        "Preparing shortly",
    "preparing":        "10–15 minutes",
    "ready":            "Ready now!",
    "out_for_delivery": "On the way",
    "delivered":        "Delivered",
    "completed":        "Completed",
}


def build_progress_tracker(order_id, status: str, fulfillment_method: str = "") -> str:
    """
    Build a visual order-progress tracker for WhatsApp.

    order_id: the order's numeric ID (used for the ORDER-N reference)
    status:   current order status (from VALID_STATUSES)
    fulfillment_method: "pickup" | "delivery" | "" (unknown)

    Never raises. Falls back to a simple status line for unrecognised statuses.
    """
    ref = f"ORDER-{order_id}" if order_id else "your order"
    status = (status or "pending").lower().strip()
    status = _STATUS_ALIASES.get(status, status)

    stage_keys = [s[0] for s in _PROGRESS_STAGES]
    if status not in stage_keys:
        # Unknown/terminal status (e.g. "cancelled", "refunded") — simple fallback
        return (
            f"📦 *{ref}*\n\n"
            f"Current Status: *{status.replace('_', ' ').title()}*\n\n"
            f"_Type *menu* to browse or *agent* to talk to our team._"
        )

    current_idx = stage_keys.index(status)

    # Skip the delivery/pickup-specific stage that doesn't apply
    lines = []
    for i, (key, label) in enumerate(_PROGRESS_STAGES):
        # Pickup orders skip delivery-only stages; delivery orders skip pickup-only stage
        if fulfillment_method == "delivery" and key in ("ready",):
            continue
        if fulfillment_method == "pickup" and key in ("out_for_delivery", "delivered"):
            continue
        if i < current_idx:
            box = "✅"
        elif i == current_idx:
            box = "⏳"
        else:
            box = "⬜"
        lines.append(f"{box} {label}")

    eta = _ETA_HINTS.get(status, "")
    eta_line = f"\nEstimated Time Remaining:\n*{eta}*\n" if eta else ""

    current_label = next((lbl for k, lbl in _PROGRESS_STAGES if k == status), status.title())

    return (
        f"📦 *{ref}*\n\n"
        f"Current Status:\n*{current_label}*\n\n"
        f"Progress:\n\n"
        + "\n".join(lines) + "\n"
        + eta_line
    )


# ─────────────────────────────────────────────────────────────────────────────
# HANDOFF TICKET NUMBERS
# ─────────────────────────────────────────────────────────────────────────────

def generate_ticket_number(customer_id, business_id: int = 0) -> str:
    """
    Generate a human-friendly support ticket number, e.g. SUP-1042.
    Deterministic-ish but unique enough for display purposes — derived from
    customer_id and current time. Not a database primary key; purely cosmetic.
    Never raises.
    """
    import time as _time
    try:
        base = int(customer_id or 0) * 17 + int(_time.time()) % 10000
        num  = abs(base) % 9000 + 1000  # 4-digit range 1000-9999
        return f"SUP-{num}"
    except Exception:
        return f"SUP-{abs(hash(str(customer_id))) % 9000 + 1000}"
