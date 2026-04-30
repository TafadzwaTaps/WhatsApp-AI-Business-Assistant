"""
ai.py — Universal Autonomous Business AI Engine

ChatGPT-style WhatsApp ordering system.
Reliable cart + checkout + product matching.

PRIORITY ORDER:
  1. Checkout
  2. Remove item
  3. Add to cart  (NLP product match — highest priority for product inputs)
  4. Cart view
  5. Browse menu
  6. Help / greeting
  7. Fallback LAST
"""

import re
import logging
from difflib import get_close_matches
import crud

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# MEMORY
# ─────────────────────────────────────────────

def _get_memory(phone: str, business_id: int) -> dict:
    try:
        return crud.get_user_memory(phone, business_id) or {
            "frequent_items": {}, "last_orders": [],
        }
    except Exception as exc:
        log.warning("_get_memory failed: %s", exc)
        return {"frequent_items": {}, "last_orders": []}


def _update_memory(phone: str, business_id: int, cart: list) -> None:
    try:
        mem = _get_memory(phone, business_id)
        for item in cart:
            name = item["name"]
            mem["frequent_items"][name] = mem["frequent_items"].get(name, 0) + item["qty"]
        mem["last_orders"].append([i["name"] for i in cart])
        mem["last_orders"] = mem["last_orders"][-10:]
        crud.save_user_memory(phone, business_id, mem)
    except Exception as exc:
        log.warning("_update_memory failed: %s", exc)


# ─────────────────────────────────────────────
# CART HELPERS — always safe, always list
# ─────────────────────────────────────────────

def _load_cart(phone: str, business_id: int) -> list:
    """Always returns a clean list of {name, qty, price} dicts."""
    try:
        raw = crud.get_cart(phone, business_id)
    except Exception as exc:
        log.error("_load_cart error: %s", exc)
        return []

    if raw is None:
        return []
    if isinstance(raw, list):
        return [i for i in raw if isinstance(i, dict) and "name" in i]
    if isinstance(raw, dict):
        items = raw.get("items") or []
        if isinstance(items, dict):
            items = list(items.values())
        return [i for i in items if isinstance(i, dict) and "name" in i]
    return []


def _save_cart(phone: str, business_id: int, cart: list) -> None:
    try:
        crud.save_cart(phone, business_id, cart)
        log.debug("_save_cart: %d item(s) for %s", len(cart), phone)
    except Exception as exc:
        log.error("_save_cart error: %s", exc)


# ─────────────────────────────────────────────
# INTENT ENGINE
# ─────────────────────────────────────────────

def _intent(text: str) -> str:
    t = text.lower().strip()

    if any(w in t for w in [
        "checkout", "pay", "confirm order", "place order",
        "done", "finish", "complete order", "i'm done", "im done",
        "order now", "submit order",
    ]):
        return "checkout"

    if t.startswith("remove") or t.startswith("delete") or "remove " in t:
        return "remove"

    if any(w in t for w in [
        "my cart", "view cart", "show cart", "whats in cart",
        "what's in cart", "cart", "my order so far",
    ]):
        return "cart"

    if any(w in t for w in [
        "menu", "list", "browse", "show me", "catalog",
        "what do you have", "what do you sell", "products",
        "whats available", "what's available",
    ]):
        return "browse"

    if (any(w in t for w in ["help", "hi ", "hello", "hey ", "start", "hie", "howzit"])
            or t in ("hi", "hello", "hey", "hie", "yo", "sup", "howzit")):
        return "help"

    return "order"


# ─────────────────────────────────────────────
# SMART PRODUCT MATCHING
# ─────────────────────────────────────────────

def _find_product(text: str, products: list) -> dict | None:
    """
    Multi-strategy matcher.
    exact → stripped → close-phrase → word-by-word → substring → reverse-word
    """
    if not products:
        return None

    t = text.lower().strip()
    name_map = {p["name"].lower(): p for p in products}
    names = list(name_map.keys())

    # 1. Exact full match
    if t in name_map:
        return name_map[t]

    # 2. Strip intent prefixes + leading qty, then exact match
    stripped = t
    for prefix in [
        r"^(i want|i'd like|give me|add|order|get me|can i have|can i get|please)\s+",
    ]:
        stripped = re.sub(prefix, "", stripped, flags=re.IGNORECASE).strip()
    # Remove leading qty: "2 sadza" → "sadza", "x2 sadza" → "sadza"
    stripped = re.sub(r"^(x\s*)?\d+\s+", "", stripped).strip()
    if stripped and stripped != t and stripped in name_map:
        return name_map[stripped]

    # 3. Close phrase match (full cleaned text)
    for candidate in (t, stripped):
        if not candidate:
            continue
        match = get_close_matches(candidate, names, n=1, cutoff=0.55)
        if match:
            return name_map[match[0]]

    # 4. Word-by-word close match
    for word in t.split():
        if len(word) < 3:
            continue
        match = get_close_matches(word, names, n=1, cutoff=0.60)
        if match:
            return name_map[match[0]]

    # 5. Substring: product name appears in user text
    for name, product in name_map.items():
        if name in t:
            return product

    # 6. Reverse: any word of product name appears in user text
    for name, product in name_map.items():
        for part in name.split():
            if len(part) >= 3 and part in t:
                return product

    return None


# ─────────────────────────────────────────────
# QUANTITY ENGINE
# ─────────────────────────────────────────────

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3,
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

def _recommend(phone: str, business_id: int, products: list, exclude: str = "") -> list:
    mem = _get_memory(phone, business_id)
    freq = mem.get("frequent_items", {})
    recs = [p for p in products if p["name"].lower() != exclude.lower()]
    if freq:
        recs.sort(key=lambda p: freq.get(p["name"], 0), reverse=True)
    return recs[:2]


# ─────────────────────────────────────────────
# CART FORMATTER
# ─────────────────────────────────────────────

def _format_cart(cart: list) -> str:
    if not cart:
        return "🛒 Your cart is empty. Type *menu* to see what we have!"

    total = 0.0
    lines = []
    for i in cart:
        subtotal = i["qty"] * float(i["price"])
        total += subtotal
        lines.append(f"  • {i['name']} ×{i['qty']}  —  ${subtotal:.2f}")

    return (
        "🛒 *Your Cart:*\n"
        + "\n".join(lines)
        + f"\n\n💰 *Total: ${total:.2f}*"
    )


# ─────────────────────────────────────────────
# PDF SEND HELPER (non-blocking)
# ─────────────────────────────────────────────

def _send_pdf_invoice(order: dict, phone: str, business_id: int) -> None:
    try:
        from pdf_invoice import generate_pdf_invoice
        pdf_path = generate_pdf_invoice(order)
    except Exception as exc:
        log.error("PDF generation failed: %s", exc)
        return
    try:
        business = crud.get_business_by_id(business_id)
        if not business:
            return
        token = crud.get_decrypted_token(business)
        phone_number_id = business.get("whatsapp_phone_id")
        if not token or not phone_number_id:
            return
        from whatsapp import send_whatsapp_document
        result = send_whatsapp_document(
            phone=phone, file_path=pdf_path,
            access_token=token, phone_number_id=phone_number_id,
            caption=f"📄 Invoice for ORDER-{order.get('id', '?')}",
        )
        if "error" not in result:
            log.info("PDF invoice sent  order=%s  phone=%s", order.get("id"), phone)
    except Exception as exc:
        log.exception("_send_pdf_invoice error: %s", exc)


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
    text   = message.strip()
    intent = _intent(text)

    log.info("▶ reply  phone=%s  biz=%s  intent=%s  msg=%r", phone, business_id, intent, text[:80])

    cart = _load_cart(phone, business_id)
    log.debug("cart_before  size=%d  %s", len(cart), cart)

    # ══════════════════════════════════════════
    # 1. CHECKOUT
    # ══════════════════════════════════════════
    if intent == "checkout":
        if not cart:
            return (
                "🛒 Your cart is empty!\n\n"
                "Type *menu* to see what we offer, then add an item — e.g. _\"Sadza\"_ or _\"2 Beef\"_"
            )
        try:
            from order_lifecycle import create_order_supabase
            from invoice import generate_invoice_text

            log.info("checkout  phone=%s  cart=%s", phone, cart)
            order = create_order_supabase(
                business_id=business_id,
                customer_phone=phone,
                cart=cart,
            )
            log.info("order created  id=%s", order.get("id", "?"))

            _update_memory(phone, business_id, cart)
            crud.clear_cart(phone, business_id)
            order["business_name"] = business_name
            invoice = generate_invoice_text(order)
            _send_pdf_invoice(order, phone, business_id)
            return invoice

        except ValueError as e:
            log.warning("checkout ValueError: %s", e)
            return (
                f"⚠️ Couldn't place your order:\n_{e}_\n\n"
                "Please adjust your cart and try *checkout* again."
            )
        except Exception as e:
            log.exception("checkout error: %s", e)
            return (
                "❌ Something went wrong placing your order.\n\n"
                "Please try again in a moment."
            )

    # ══════════════════════════════════════════
    # 2. REMOVE ITEM
    # ══════════════════════════════════════════
    if intent == "remove":
        t_lower = text.lower()
        for item in list(cart):
            if item["name"].lower() in t_lower:
                cart.remove(item)
                _save_cart(phone, business_id, cart)
                return f"🗑️ Removed *{item['name']}* from your cart.\n\n{_format_cart(cart)}"
        return f"⚠️ I couldn't find that item in your cart.\n\n{_format_cart(cart)}"

    # ══════════════════════════════════════════
    # 3. ADD / ORDER — always try product match
    # ══════════════════════════════════════════
    if intent == "order":
        product = _find_product(text, products)

        if product:
            # Refresh stock from DB
            try:
                fresh = crud.get_product_by_name(business_id, product["name"])
                if fresh:
                    product = fresh
            except Exception as exc:
                log.warning("stock refresh failed: %s", exc)

            qty          = _qty(text)
            product_name = product["name"]

            # Stock guard
            available = product.get("stock")
            if available is not None:
                in_cart = next((i["qty"] for i in cart if i["name"] == product_name), 0)
                if in_cart + qty > available:
                    if available == 0:
                        return (
                            f"😔 *{product_name}* is currently out of stock.\n\n"
                            "Type *menu* to see available items."
                        )
                    return (
                        f"⚠️ Only *{available}* unit(s) of *{product_name}* available "
                        f"(you already have {in_cart} in your cart)."
                    )

            # Update cart in-place
            found = False
            for item in cart:
                if item["name"] == product_name:
                    item["qty"] += qty
                    found = True
                    break
            if not found:
                cart.append({
                    "name":  product_name,
                    "qty":   qty,
                    "price": float(product["price"]),
                })

            _save_cart(phone, business_id, cart)
            log.info("added  %s ×%d  phone=%s", product_name, qty, phone)

            recs = _recommend(phone, business_id, products, exclude=product_name)
            qty_label = f" ×{qty}" if qty > 1 else ""
            msg = (
                f"👍 Nice choice! Added *{product_name}*{qty_label} to your cart.\n\n"
                f"{_format_cart(cart)}"
            )
            if recs:
                rec_str = " or ".join(f"*{r['name']}*" for r in recs)
                msg += f"\n\n💡 You might also enjoy {rec_str}."
            msg += "\n\n_Type *checkout* to place your order, or keep adding items._"
            return msg

        # No product match → fall through to fallback

    # ══════════════════════════════════════════
    # 4. CART VIEW
    # ══════════════════════════════════════════
    if intent == "cart":
        reply = _format_cart(cart)
        if cart:
            reply += "\n\n_Ready? Type *checkout* to place your order._"
        return reply

    # ══════════════════════════════════════════
    # 5. BROWSE MENU
    # ══════════════════════════════════════════
    if intent == "browse":
        if not products:
            return f"📋 *{business_name}*\n\nNo items available yet. Check back soon! 🙏"

        lines = []
        for i, p in enumerate(products):
            stock_note = ""
            s = p.get("stock")
            if s is not None and s <= 5:
                stock_note = f"  ⚠️ _only {s} left_"
            lines.append(f"  {i+1}. *{p['name']}* — ${float(p['price']):.2f}{stock_note}")

        recs = _recommend(phone, business_id, products)
        rec_text = ""
        if recs:
            rec_text = "\n\n⭐ *You usually order:*\n" + "\n".join(
                f"  • {r['name']}" for r in recs
            )

        return (
            f"📋 *{business_name} Menu*\n\n"
            + "\n".join(lines)
            + rec_text
            + "\n\n_Type a product name to add it — e.g. \"Sadza\" or \"2 Beef\"_"
        )

    # ══════════════════════════════════════════
    # 6. HELP / GREETING
    # ══════════════════════════════════════════
    if intent == "help":
        hint = f"*{products[0]['name']}*" if products else "an item"
        return (
            f"👋 Hey! Welcome to *{business_name}*!\n\n"
            f"Here's what you can do:\n"
            f"  📋 *menu* — see everything we offer\n"
            f"  🛍️ Just type a name — e.g. _{hint}_\n"
            f"  🛒 *cart* — see what you've added\n"
            f"  ✅ *checkout* — place your order\n"
            f"  ❌ *remove [item]* — remove something\n\n"
            f"What can I get you today? 😊"
        )

    # ══════════════════════════════════════════
    # 7. FALLBACK — last resort
    # ══════════════════════════════════════════
    # One more product-match attempt before giving up
    product = _find_product(text, products)
    if product:
        return generate_reply(
            message=product["name"],
            phone=phone,
            business_id=business_id,
            business_name=business_name,
            products=products,
        )

    hint = f"e.g. _{products[0]['name']}_" if products else "e.g. _Burger_"
    return (
        f"🤖 Hmm, I didn't quite understand that.\n\n"
        f"Try:\n"
        f"  📋 *menu* — browse products\n"
        f"  🛍️ Type a product name — {hint}\n"
        f"  🛒 *cart* — view your cart\n"
        f"  ✅ *checkout* — place your order\n"
    )
