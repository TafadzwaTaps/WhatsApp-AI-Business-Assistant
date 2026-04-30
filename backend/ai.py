"""
ai.py — Universal Autonomous Business AI Engine

WHAT THIS IS:
- Business-agnostic AI commerce + service brain
- Works for ANY industry (barber, shop, salon, SaaS, repairs, etc.)
- Handles ordering, carts, recommendations
- Learns user behavior over time (lightweight memory layer)

CHECKOUT FLOW:
  detect "checkout"
    → create_order_supabase()   (reduces stock)
    → generate_invoice_text()   (WhatsApp text invoice)
    → generate_pdf_invoice()    (PDF file)
    → send_whatsapp_document()  (PDF sent to customer)
    → return text invoice as WA reply
  Stock is reduced atomically; insufficient stock blocks checkout with a friendly message.
"""

import re
import logging
from difflib import get_close_matches
import crud

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
# PDF + WHATSAPP DOCUMENT HELPER
# ─────────────────────────────────────────────

def _send_pdf_invoice(order: dict, phone: str, business_id: int) -> None:
    """
    Generate PDF invoice and send it via WhatsApp.
    Silently logs errors — never crashes the checkout flow.
    """
    try:
        from pdf_invoice import generate_pdf_invoice
        pdf_path = generate_pdf_invoice(order)
    except Exception as exc:
        log.error("_send_pdf_invoice: PDF generation failed — %s", exc)
        return

    try:
        business = crud.get_business_by_id(business_id)
        if not business:
            log.warning("_send_pdf_invoice: business %s not found", business_id)
            return

        token = crud.get_decrypted_token(business)
        phone_number_id = business.get("whatsapp_phone_id")

        if not token or not phone_number_id:
            log.warning("_send_pdf_invoice: missing token or phone_id for business %s", business_id)
            return

        from whatsapp import send_whatsapp_document
        result = send_whatsapp_document(
            phone=phone,
            file_path=pdf_path,
            access_token=token,
            phone_number_id=phone_number_id,
            caption=f"📄 Your invoice for ORDER-{order.get('id', '?')}",
        )
        if "error" in result:
            log.error("_send_pdf_invoice: WhatsApp send failed — %s", result["error"])
        else:
            log.info("_send_pdf_invoice: ✅ PDF sent  order=%s  phone=%s", order.get("id"), phone)

    except Exception as exc:
        log.exception("_send_pdf_invoice: unexpected error — %s", exc)


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

    # Load cart (list of {name, qty, price})
    cart_raw = crud.get_cart(phone, business_id)
    if isinstance(cart_raw, dict):
        cart = cart_raw.get("items") or []
        if isinstance(cart, dict):
            cart = list(cart.values())
    elif isinstance(cart_raw, list):
        cart = cart_raw
    else:
        cart = []

    # ─────────────────────────────
    # HELP
    # ─────────────────────────────
    if intent == "help":
        return (
            f"👋 Welcome to *{business_name}*!\n\n"
            f"Here's what you can do:\n"
            f"  • *menu* — browse our catalog\n"
            f"  • Add items (e.g. '2 burgers')\n"
            f"  • *cart* — view your cart\n"
            f"  • *checkout* — place your order\n"
            f"  • *remove [item]* — remove from cart\n\n"
            f"I learn your preferences over time 🤖"
        )

    # ─────────────────────────────
    # BROWSE
    # ─────────────────────────────
    if intent == "browse":
        if not products:
            return f"📋 *{business_name}*\n\nNo items available yet. Check back soon! 🙏"

        menu_lines = "\n".join(
            [f"  {i+1}. {p['name']} — ${float(p['price']):.2f}" for i, p in enumerate(products)]
        )

        recs = _recommend(phone, business_id, products)
        rec_text = ""
        if recs:
            rec_text = "\n\n⭐ *Recommended for you:*\n" + "\n".join(
                [f"  • {r['name']}" for r in recs]
            )

        return (
            f"📋 *{business_name} Menu*\n\n"
            f"{menu_lines}{rec_text}\n\n"
            f"_Type item name to add to cart, or 'checkout' to place order._"
        )

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
            return "🛒 Your cart is empty. Add some items first!"

        try:
            from order_lifecycle import create_order_supabase
            from invoice import generate_invoice_text

            order = create_order_supabase(
                business_id=business_id,
                customer_phone=phone,
                cart=cart,
            )

            _update_memory(phone, business_id, cart)
            crud.clear_cart(phone, business_id)

            # Generate text invoice for WhatsApp reply
            invoice = generate_invoice_text(order)

            # Enrich order with business name for PDF header
            order["business_name"] = business_name

            # Generate PDF + send via WhatsApp (non-blocking — errors are logged only)
            _send_pdf_invoice(order, phone, business_id)

            return invoice

        except ValueError as e:
            log.warning("checkout blocked — %s", e)
            return f"⚠️ Could not place order:\n{e}\n\nPlease adjust your cart and try again."

        except Exception as e:
            log.exception("checkout error: %s", e)
            return "❌ Something went wrong placing your order. Please try again."

    # ─────────────────────────────
    # REMOVE ITEM
    # ─────────────────────────────
    if intent == "remove":
        text_lower = text.lower()
        for item in list(cart):
            if item["name"].lower() in text_lower:
                cart.remove(item)
                crud.save_cart(phone, business_id, cart)
                return f"❌ Removed *{item['name']}* from cart.\n\n{_format_cart(cart)}"

        return "⚠️ Item not found in cart.\n\n" + _format_cart(cart)

    # ─────────────────────────────
    # ADD / ORDER
    # ─────────────────────────────
    product = _find_product(text, products)

    if product:
        # Force fresh stock from DB
        fresh = crud.get_product_by_name(business_id, product["name"])
        if fresh:
            product = fresh

        qty = _qty(text)

        available_stock = product.get("stock")
        if available_stock is not None:
            in_cart = next((i["qty"] for i in cart if i["name"] == product["name"]), 0)
            if in_cart + qty > available_stock:
                if available_stock == 0:
                    return f"😔 Sorry, *{product['name']}* is out of stock."
                return (
                    f"⚠️ Only *{available_stock}* unit(s) of *{product['name']}* available "
                    f"(you already have {in_cart} in cart)."
                )

        for item in cart:
            if item["name"] == product["name"]:
                item["qty"] += qty
                break
        else:
            cart.append({
                "name":  product["name"],
                "qty":   qty,
                "price": float(product["price"]),
            })

        crud.save_cart(phone, business_id, cart)

        recs = _recommend(phone, business_id, products)
        msg = f"👍 Added *{product['name']}* x{qty} to cart.\n\n{_format_cart(cart)}"

        if recs:
            msg += "\n\n⭐ *You may also like:*\n" + "\n".join(
                [f"  • {r['name']}" for r in recs]
            )

        msg += "\n\n_Type 'checkout' to place your order._"
        return msg

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
