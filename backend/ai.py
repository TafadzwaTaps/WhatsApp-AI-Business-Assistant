"""
ai.py — Final Universal Autonomous Business AI Engine

WHAT THIS IS:
- A business-agnostic AI commerce + service brain
- Works for ANY industry (barber, shop, salon, SaaS, repairs, etc.)
- Handles ordering, booking-style services, carts, and recommendations
- Learns user behavior over time (lightweight memory layer)

CORE IDEA:
Everything = "catalog item"
User intent = "action on catalog"
AI = "decision engine + memory + recommendation layer"
"""

import re
from difflib import get_close_matches
import crud


# ─────────────────────────────────────────────
# MEMORY (USER BEHAVIOR LAYER)
# ─────────────────────────────────────────────

def _get_memory(phone, business_id):
    return crud.get_user_memory(phone, business_id) or {
        "frequent_items": {},
        "last_orders": []
    }


def _update_memory(phone, business_id, cart):
    mem = _get_memory(phone, business_id)

    for item in cart:
        name = item["name"]
        mem["frequent_items"][name] = mem["frequent_items"].get(name, 0) + item["qty"]

    mem["last_orders"].append([i["name"] for i in cart])
    mem["last_orders"] = mem["last_orders"][-10:]

    crud.save_user_memory(phone, business_id, mem)


# ─────────────────────────────────────────────
# INTENT ENGINE (UNIVERSAL)
# ─────────────────────────────────────────────

def _intent(text: str):
    text = text.lower()

    if any(w in text for w in ["menu", "list", "browse", "show"]):
        return "browse"
    if any(w in text for w in ["cart", "my cart"]):
        return "cart"
    if any(w in text for w in ["checkout", "pay", "order", "done"]):
        return "checkout"
    if text.startswith("remove"):
        return "remove"
    if any(w in text for w in ["help"]):
        return "help"

    return "order"


# ─────────────────────────────────────────────
# SMART MATCHING (NO INDUSTRY ASSUMPTIONS)
# ─────────────────────────────────────────────

def _find_product(text, products):
    names = [p["name"].lower() for p in products]
    match = get_close_matches(text.lower(), names, n=1, cutoff=0.55)

    if match:
        for p in products:
            if p["name"].lower() == match[0]:
                return p

    # fallback word scan
    for word in text.split():
        match = get_close_matches(word, names, n=1, cutoff=0.6)
        if match:
            for p in products:
                if p["name"].lower() == match[0]:
                    return p

    return None


# ─────────────────────────────────────────────
# QUANTITY ENGINE (UNIVERSAL)
# ─────────────────────────────────────────────

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1,
    "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
    "couple": 2, "few": 3
}


def _qty(text: str):
    text = text.lower()

    m = re.search(r"x(\d+)", text)
    if m:
        return int(m.group(1))

    m = re.search(r"\b(\d+)\b", text)
    if m:
        return int(m.group(1))

    for w in text.split():
        if w in NUMBER_WORDS:
            return NUMBER_WORDS[w]

    return 1


# ─────────────────────────────────────────────
# RECOMMENDATION ENGINE (LEARNING LAYER)
# ─────────────────────────────────────────────

def _recommend(phone, business_id, products):
    mem = _get_memory(phone, business_id)

    freq = mem.get("frequent_items", {})
    if not freq:
        return []

    top = max(freq, key=freq.get)

    suggestions = []
    for p in products:
        if p["name"].lower() != top.lower():
            suggestions.append(p)

    return suggestions[:2]


# ─────────────────────────────────────────────
# CART FORMATTER
# ─────────────────────────────────────────────

def _format_cart(cart):
    if not cart:
        return "🛒 Cart is empty."

    total = 0
    lines = []

    for i in cart:
        subtotal = i["qty"] * i["price"]
        total += subtotal
        lines.append(f"• {i['name']} x{i['qty']} — ${subtotal:.2f}")

    return "🛒 Cart:\n\n" + "\n".join(lines) + f"\n\n💰 Total: ${total:.2f}"


# ─────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────

def generate_reply(message, phone, business_id, business_name, products):
    text = message.lower().strip()
    intent = _intent(text)

    cart = crud.get_cart(phone, business_id) or []

    # ─────────────────────────────
    # HELP
    # ─────────────────────────────
    if intent == "help":
        return (
            f"👋 Welcome to *{business_name}*\n\n"
            f"You can:\n"
            f"• Add items naturally (e.g. '2 haircut')\n"
            f"• View cart\n"
            f"• Checkout\n\n"
            f"I learn your preferences over time 🤖"
        )

    # ─────────────────────────────
    # BROWSE
    # ─────────────────────────────
    if intent == "browse":
        menu = "\n".join([f"{p['name']} — ${p['price']}" for p in products])

        recs = _recommend(phone, business_id, products)
        rec_text = ""

        if recs:
            rec_text = "\n\n⭐ Recommended for you:\n" + "\n".join(
                [f"• {r['name']}" for r in recs]
            )

        return f"📋 *{business_name}*\n\n{menu}{rec_text}"

    # ─────────────────────────────
    # CART
    # ─────────────────────────────
    if intent == "cart":
        return _format_cart(cart)

    # ─────────────────────────────
    # CHECKOUT
    # ─────────────────────────────
    if intent == "checkout":
        if not cart:
            return "🛒 Cart is empty."

        total = sum(i["qty"] * i["price"] for i in cart)

        for item in cart:
            crud.create_order(
                business_id,
                type("Order", (), {
                    "customer_phone": phone,
                    "product_name": item["name"],
                    "quantity": item["qty"]
                })
            )

        _update_memory(phone, business_id, cart)
        crud.clear_cart(phone, business_id)

        return f"✅ Order placed!\n💰 Total: ${total:.2f}"

    # ─────────────────────────────
    # REMOVE ITEM
    # ─────────────────────────────
    if intent == "remove":
        for item in cart:
            if item["name"].lower() in text:
                cart.remove(item)
                crud.save_cart(phone, business_id, cart)
                return f"❌ Removed {item['name']}\n\n{_format_cart(cart)}"

        return "Item not found."

    # ─────────────────────────────
    # ADD / ORDER (UNIVERSAL)
    # ─────────────────────────────
    product = _find_product(text, products)

    if product:
        qty = _qty(text)

        for item in cart:
            if item["name"] == product["name"]:
                item["qty"] += qty
                break
        else:
            cart.append({
                "name": product["name"],
                "qty": qty,
                "price": float(product["price"])
            })

        crud.save_cart(phone, business_id, cart)

        recs = _recommend(phone, business_id, products)

        msg = f"👍 Added {product['name']} x{qty}\n\n" + _format_cart(cart)

        if recs:
            msg += "\n\n⭐ You may also like:\n" + "\n".join(
                [f"• {r['name']}" for r in recs]
            )

        return msg

    # ─────────────────────────────
    # FALLBACK
    # ─────────────────────────────
    return (
        f"🤖 I didn’t fully understand that.\n\n"
        f"Try:\n"
        f"• 'add item name'\n"
        f"• '2 of item name'\n"
        f"• 'show cart'\n"
        f"• 'checkout'\n"
    )