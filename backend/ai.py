"""
ai.py — Universal Autonomous Business AI Engine

WHAT THIS IS:
- Business-agnostic AI commerce + service brain
- Works for ANY industry (barber, shop, salon, SaaS, repairs, etc.)
- Handles ordering, carts, recommendations
- Learns user behavior over time (lightweight memory layer)

CHECKOUT FLOW:
  detect "checkout" → create_order() → generate PDF → send via WhatsApp
  Stock is reduced atomically; insufficient stock blocks checkout with a friendly message.
"""

import re
import logging
from difflib import get_close_matches
import crud

from order_lifecycle import create_order
from pdf_invoice import generate_pdf_invoice
from whatsapp import send_whatsapp_document

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# MEMORY (USER BEHAVIOR LAYER)
# ─────────────────────────────────────────────

def _get_memory(phone: str, business_id: int) -> dict:
    return crud.get_user_memory(phone, business_id) or {
        "frequent_items": {},
        "last_orders": [],
    }


def _update_memory(phone: str, business_id: int, cart: list) -> None:
    mem = _get_memory(phone, business_id)

    for item in cart:
        name = item["name"]
        mem["frequent_items"][name] = mem["frequent_items"].get(name, 0) + item["qty"]

    mem["last_orders"].append([i["name"] for i in cart])
    mem["last_orders"] = mem["last_orders"][-10:]

    crud.save_user_memory(phone, business_id, mem)


# ─────────────────────────────────────────────
# INTENT ENGINE
# ─────────────────────────────────────────────

def _intent(text: str) -> str:
    t = text.lower()

    if any(w in t for w in ["menu", "list", "browse", "show", "catalog"]):
        return "browse"
    if any(w in t for w in ["my cart", "view cart", "show cart", "cart"]):
        return "cart"
    if any(w in t for w in ["checkout", "pay", "confirm order", "place order", "done", "finish"]):
        return "checkout"
    if t.startswith("remove") or t.startswith("delete"):
        return "remove"
    if any(w in t for w in ["help", "hi ", "hello", "hey", "start"]) or t in ("hi", "hello", "hey"):
        return "help"

    return "order"


# ─────────────────────────────────────────────
# SMART MATCHING
# ─────────────────────────────────────────────

def _find_product(text: str, products: list) -> dict | None:
    if not products:
        return None

    names = [p["name"].lower() for p in products]

    match = get_close_matches(text.lower(), names, n=1, cutoff=0.55)
    if match:
        for p in products:
            if p["name"].lower() == match[0]:
                return p

    for word in text.split():
        if len(word) < 3:
            continue
        match = get_close_matches(word, names, n=1, cutoff=0.6)
        if match:
            for p in products:
                if p["name"].lower() == match[0]:
                    return p

    return None


# ─────────────────────────────────────────────
# QUANTITY ENGINE
# ─────────────────────────────────────────────

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1,
    "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
    "couple": 2, "few": 3,
}


def _qty(text: str) -> int:
    t = text.lower()

    m = re.search(r"x\s*(\d+)", t)
    if m:
        return max(1, int(m.group(1)))

    m = re.search(r"\b(\d+)\b", t)
    if m:
        return max(1, int(m.group(1)))

    for w in t.split():
        if w in NUMBER_WORDS:
            return NUMBER_WORDS[w]

    return 1


# ─────────────────────────────────────────────
# RECOMMENDATION ENGINE
# ─────────────────────────────────────────────

def _recommend(phone: str, business_id: int, products: list) -> list:
    mem = _get_memory(phone, business_id)
    freq = mem.get("frequent_items", {})
    if not freq:
        return []

    top = max(freq, key=freq.get)
    suggestions = [p for p in products if p["name"].lower() != top.lower()]
    return suggestions[:2]


# ─────────────────────────────────────────────
# CART FORMATTER
# ─────────────────────────────────────────────

def _format_cart(cart: list) -> str:
    if not cart:
        return "🛒 Your cart is empty."

    total = 0.0
    lines = []

    for i in cart:
        subtotal = i["qty"] * float(i["price"])
        total += subtotal
        lines.append(f"  • {i['name']} x{i['qty']}  — ${subtotal:.2f}")

    return (
        "🛒 *Your Cart:*\n"
        + "\n".join(lines)
        + f"\n\n💰 *Total: ${total:.2f}*"
    )


# ─────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────

def generate_reply(
    message: str,
    phone: str,
    business_id: int,
    business_name: str,
    products: list,
) -> str:

    text = message.strip()
    intent = _intent(text)

    cart_raw = crud.get_cart(phone, business_id)

    if isinstance(cart_raw, dict):
        cart = cart_raw.get("items") or []
        if isinstance(cart, dict):
            cart = list(cart.values())
    elif isinstance(cart_raw, list):
        cart = cart_raw
    else:
        cart = []

    # ───────── HELP ─────────
    if intent == "help":
        return (
            f"👋 Welcome to *{business_name}*!\n\n"
            f"• *menu* — browse catalog\n"
            f"• Add items (e.g. '2 burgers')\n"
            f"• *cart* — view cart\n"
            f"• *checkout* — place order\n"
            f"• *remove [item]* — remove item\n"
        )

    # ───────── BROWSE ─────────
    if intent == "browse":
        if not products:
            return "No items available."

        menu = "\n".join([f"{p['name']} — ${p['price']}" for p in products])
        return f"📋 *Menu*\n\n{menu}"

    # ───────── CART ─────────
    if intent == "cart":
        return _format_cart(cart)

    # ───────── CHECKOUT (🔥 FULLY WIRED) ─────────
    if intent == "checkout":
        if not cart:
            return "🛒 Your cart is empty."

        try:
            order = create_order(
                supabase=crud.supabase,
                business_id=business_id,
                items=[
                    {
                        "product_id": i["id"],
                        "quantity": i["qty"],
                        "price": i["price"]
                    }
                    for i in cart
                ]
            )

            _update_memory(phone, business_id, cart)
            crud.clear_cart(phone, business_id)

            pdf_path = generate_pdf_invoice(order)

            send_whatsapp_document(
                phone=phone,
                file_path=pdf_path,
                token=crud.WHATSAPP_TOKEN,
                phone_id=crud.PHONE_NUMBER_ID
            )

            return (
                f"✅ Order placed!\n"
                f"📄 Invoice sent\n"
                f"Reference: ORDER-{order['id']}"
            )

        except Exception as e:
            log.exception("checkout error")
            return f"❌ Checkout failed: {str(e)}"

    # ───────── REMOVE ─────────
    if intent == "remove":
        text_lower = text.lower()
        for item in list(cart):
            if item["name"].lower() in text_lower:
                cart.remove(item)
                crud.save_cart(phone, business_id, cart)
                return f"Removed {item['name']}"

        return "Item not found."

    # ───────── ADD ITEM (🔥 FIXED WITH ID) ─────────
    product = _find_product(text, products)

    if product:
        qty = _qty(text)

        for item in cart:
            if item["id"] == product["id"]:
                item["qty"] += qty
                break
        else:
            cart.append({
                "id": product["id"],   # 🔥 CRITICAL FIX
                "name": product["name"],
                "qty": qty,
                "price": float(product["price"]),
            })

        crud.save_cart(phone, business_id, cart)

        return f"Added {product['name']} x{qty}\n\n{_format_cart(cart)}"

    # ─────────────────────────────
    # FALLBACK
    # ─────────────────────────────
    return (
        f"🤖 I didn't quite understand that.\n\n"
        f"Try:\n"
        f"  • *menu* — see what we offer\n"
        f"  • '2 burgers' — add items\n"
        f"  • *cart* — view your cart\n"
        f"  • *checkout* — place your order\n"
    )

