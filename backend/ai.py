"""
ai.py — Universal Autonomous Business AI Engine  (v4 — multi-payment)

CHECKOUT FLOW (new):
  User: "checkout"
    → Bot shows cart summary + asks for payment method
  User: "1" / "ecocash" / "2" / "paynow" / "3" / "paypal"
    → Order is created in Supabase
    → Payment gateway is called
    → Customer receives payment instructions / URL
    → Cart is cleared ONLY after order is successfully created

PRIORITY ORDER:
  1. Checkout initiation  — asks for payment method
  2. Payment method reply — handles 1/2/3 or ecocash/paynow/paypal
  3. Remove item
  4. Add to cart  (NLP product match)
  5. Cart view
  6. Browse menu
  7. Help / greeting
  8. Fallback LAST

SESSION STATE for payment flow:
  Stored as a "session" JSON object in Supabase user_memory under key "pending_checkout".
  Structure: { "awaiting_payment_method": true, "cart_snapshot": [...] }
  Cleared once the order is placed or the user cancels.
"""

import re
import logging
from difflib import get_close_matches
import crud

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# MEMORY
# ─────────────────────────────────────────────────────────────────────────────

def _get_memory(phone: str, business_id: int) -> dict:
    try:
        return crud.get_user_memory(phone, business_id) or {
            "frequent_items": {}, "last_orders": [], "pending_checkout": None,
        }
    except Exception as exc:
        log.warning("_get_memory failed: %s", exc)
        return {"frequent_items": {}, "last_orders": [], "pending_checkout": None}


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


def _get_pending_checkout(phone: str, business_id: int) -> dict | None:
    """Return pending checkout session dict or None."""
    try:
        mem = _get_memory(phone, business_id)
        return mem.get("pending_checkout") or None
    except Exception:
        return None


def _set_pending_checkout(phone: str, business_id: int, cart: list) -> None:
    """Store that we're waiting for a payment method selection."""
    try:
        mem = _get_memory(phone, business_id)
        mem["pending_checkout"] = {
            "awaiting_payment_method": True,
            "cart_snapshot": cart,
        }
        crud.save_user_memory(phone, business_id, mem)
    except Exception as exc:
        log.warning("_set_pending_checkout failed: %s", exc)


def _clear_pending_checkout(phone: str, business_id: int) -> None:
    """Remove the pending checkout session."""
    try:
        mem = _get_memory(phone, business_id)
        mem["pending_checkout"] = None
        crud.save_user_memory(phone, business_id, mem)
    except Exception as exc:
        log.warning("_clear_pending_checkout failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# CART HELPERS
# ─────────────────────────────────────────────────────────────────────────────

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
        log.debug("_save_cart: %d item(s)  phone=%s", len(cart), phone)
    except Exception as exc:
        log.error("_save_cart error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# INTENT ENGINE
# ─────────────────────────────────────────────────────────────────────────────

# Explicit payment method keywords
_ECOCASH_WORDS = {"ecocash", "eco", "1", "1️⃣", "one"}
_PAYNOW_WORDS  = {"paynow", "pay now", "pn", "2", "2️⃣", "two"}
_PAYPAL_WORDS  = {"paypal", "pp", "international", "3", "3️⃣", "three"}
_CANCEL_WORDS  = {"cancel", "back", "no", "stop", "nevermind", "never mind"}


def _detect_payment_method(text: str) -> str | None:
    """
    Detect if the user is selecting a payment method.
    Returns: "ecocash" | "paynow" | "paypal" | "cancel" | None
    """
    t = text.lower().strip()
    if t in _ECOCASH_WORDS:
        return "ecocash"
    if t in _PAYNOW_WORDS:
        return "paynow"
    if t in _PAYPAL_WORDS:
        return "paypal"
    if t in _CANCEL_WORDS:
        return "cancel"
    # Partial / embedded keyword match
    if "ecocash" in t or "eco cash" in t:
        return "ecocash"
    if "paynow" in t or "pay now" in t:
        return "paynow"
    if "paypal" in t or "pay pal" in t:
        return "paypal"
    return None


def _intent(text: str) -> str:
    t = text.lower().strip()

    if any(w in t for w in [
        "checkout", "confirm order", "place order",
        "done", "finish", "complete order", "i'm done", "im done",
        "order now", "submit order",
    ]):
        return "checkout"

    # "pay" alone can mean payment method OR checkout — treat as checkout
    if t == "pay":
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


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCT MATCHING
# ─────────────────────────────────────────────────────────────────────────────

def _find_product(text: str, products: list) -> dict | None:
    if not products:
        return None

    t        = text.lower().strip()
    name_map = {p["name"].lower(): p for p in products}
    names    = list(name_map.keys())

    if t in name_map:
        return name_map[t]

    stripped = t
    for prefix in [r"^(i want|i'd like|give me|add|order|get me|can i have|can i get|please)\s+"]:
        stripped = re.sub(prefix, "", stripped, flags=re.IGNORECASE).strip()
    stripped = re.sub(r"^(x\s*)?\d+\s+", "", stripped).strip()
    if stripped and stripped in name_map:
        return name_map[stripped]

    for candidate in (t, stripped):
        if not candidate:
            continue
        match = get_close_matches(candidate, names, n=1, cutoff=0.55)
        if match:
            return name_map[match[0]]

    for word in t.split():
        if len(word) < 3:
            continue
        match = get_close_matches(word, names, n=1, cutoff=0.60)
        if match:
            return name_map[match[0]]

    for name, product in name_map.items():
        if name in t:
            return product

    for name, product in name_map.items():
        for part in name.split():
            if len(part) >= 3 and part in t:
                return product

    return None


# ─────────────────────────────────────────────────────────────────────────────
# QUANTITY PARSER
# ─────────────────────────────────────────────────────────────────────────────

_NUMBER_WORDS = {
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
        if w in _NUMBER_WORDS:
            return _NUMBER_WORDS[w]
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# RECOMMENDATIONS
# ─────────────────────────────────────────────────────────────────────────────

def _recommend(phone: str, business_id: int, products: list, exclude: str = "") -> list:
    mem  = _get_memory(phone, business_id)
    freq = mem.get("frequent_items", {})
    recs = [p for p in products if p["name"].lower() != exclude.lower()]
    if freq:
        recs.sort(key=lambda p: freq.get(p["name"], 0), reverse=True)
    return recs[:2]


# ─────────────────────────────────────────────────────────────────────────────
# CART FORMATTER
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT METHOD SELECTION PROMPT
# ─────────────────────────────────────────────────────────────────────────────

def _payment_prompt(cart: list) -> str:
    cart_summary = _format_cart(cart)
    return (
        f"{cart_summary}\n\n"
        f"You're almost done! 😊\n\n"
        f"How would you like to pay?\n\n"
        f"1️⃣  *EcoCash* — Send via *151# (Zimbabwe)\n"
        f"2️⃣  *Paynow*  — Pay online (Zimbabwe)\n"
        f"3️⃣  *PayPal*  — International / card\n\n"
        f"_Reply with *1*, *2*, or *3*_ — or type the name."
    )


# ─────────────────────────────────────────────────────────────────────────────
# PDF INVOICE HELPER (non-blocking)
# ─────────────────────────────────────────────────────────────────────────────

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
        token           = crud.get_decrypted_token(business)
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


# ─────────────────────────────────────────────────────────────────────────────
# PROCESS PAYMENT METHOD — core checkout logic
# ─────────────────────────────────────────────────────────────────────────────

def _process_payment(
    method: str,
    cart: list,
    phone: str,
    business_id: int,
    business_name: str,
) -> str:
    """
    Create the order in Supabase then call the appropriate payment gateway.
    Returns a WhatsApp-ready reply string.
    """
    from order_lifecycle import create_order_supabase
    from invoice import generate_invoice_text
    from payments import (
        generate_ecocash_instructions,
        create_paynow_payment,
        create_paypal_payment,
    )

    # ── STEP 1: Create order (always first — captures the sale) ───────────────
    try:
        log.info("_process_payment  method=%s  phone=%s  cart=%s", method, phone, cart)

        order = create_order_supabase(
            business_id=business_id,
            customer_phone=phone,
            cart=cart,
            payment_method=method,       # saved to orders.payment_method
        )
        order["business_name"] = business_name

        # Pull business payment details (EcoCash number, etc.) into order dict
        try:
            biz = crud.get_business_by_id(business_id)
            if biz:
                order["payment_number"] = biz.get("payment_number", "")
                order["payment_name"]   = biz.get("payment_name", "")
        except Exception:
            pass

        log.info("order created  id=%s  method=%s", order.get("id", "?"), method)

    except ValueError as exc:
        log.warning("_process_payment order creation blocked: %s", exc)
        return (
            f"⚠️ Couldn't place your order:\n_{exc}_\n\n"
            "Please adjust your cart and try *checkout* again."
        )
    except Exception as exc:
        log.exception("_process_payment order creation failed: %s", exc)
        return (
            "❌ Something went wrong saving your order.\n\n"
            "Please try again in a moment. Your cart is still saved."
        )

    # ── STEP 2: Call payment gateway ──────────────────────────────────────────
    try:
        if method == "ecocash":
            pay_result = generate_ecocash_instructions(order)
        elif method == "paynow":
            pay_result = create_paynow_payment(order)
        elif method == "paypal":
            pay_result = create_paypal_payment(order)
        else:
            pay_result = generate_ecocash_instructions(order)  # safe default

    except Exception as exc:
        log.exception("payment gateway error  method=%s  exc=%s", method, exc)
        pay_result = {
            "error":   str(exc),
            "message": (
                "⚠️ Payment link could not be generated right now.\n"
                "Your order is saved. Please contact us to complete payment.\n"
                f"Order reference: *ORDER-{order.get('id', '?')}*"
            ),
        }

    # ── STEP 3: Save payment details back to order ────────────────────────────
    try:
        order_id = order.get("id")
        if order_id:
            update: dict = {
                "payment_method":    method,
                "payment_status":    "pending_payment" if not pay_result.get("error") else "payment_error",
                "payment_reference": pay_result.get("reference", f"ORDER-{order_id}"),
            }
            if pay_result.get("url"):
                update["payment_url"] = pay_result["url"]
            crud.update_order_payment(order_id, business_id, update)
    except Exception as exc:
        log.warning("update payment details failed: %s", exc)

    # ── STEP 4: Clear cart + memory + session ─────────────────────────────────
    _update_memory(phone, business_id, cart)
    crud.clear_cart(phone, business_id)
    _clear_pending_checkout(phone, business_id)

    # ── STEP 5: Send PDF invoice (non-blocking) ───────────────────────────────
    _send_pdf_invoice(order, phone, business_id)

    # ── STEP 6: Return reply ──────────────────────────────────────────────────
    pay_message = pay_result.get("message", "")
    if pay_result.get("error") and not pay_message:
        pay_message = (
            "⚠️ Payment link failed, but your order is saved.\n"
            f"Reference: *ORDER-{order.get('id', '?')}*\n"
            "Please contact us to arrange payment."
        )

    return pay_message


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────────────────────────────────────

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
    log.debug("cart_before  size=%d", len(cart))

    # ══════════════════════════════════════════════════════════════════════════
    # PRIORITY 0 — PAYMENT METHOD REPLY
    # Check BEFORE all other intents so "1", "2", "3" resolve correctly when
    # the user is in the middle of a checkout flow.
    # ══════════════════════════════════════════════════════════════════════════
    pending = _get_pending_checkout(phone, business_id)

    if pending and pending.get("awaiting_payment_method"):
        method = _detect_payment_method(text)

        if method == "cancel":
            _clear_pending_checkout(phone, business_id)
            return (
                "🚫 Checkout cancelled. Your cart is still saved.\n\n"
                f"{_format_cart(cart)}\n\n"
                "_Type *checkout* when you're ready._"
            )

        if method in ("ecocash", "paynow", "paypal"):
            # Use cart snapshot stored at checkout initiation
            snapshot = pending.get("cart_snapshot") or cart
            return _process_payment(
                method=method,
                cart=snapshot,
                phone=phone,
                business_id=business_id,
                business_name=business_name,
            )

        # User replied something unrecognised while in payment selection
        return (
            "🤔 I didn't catch that.\n\n"
            "Please choose your payment method:\n"
            "1️⃣ *EcoCash*  2️⃣ *Paynow*  3️⃣ *PayPal*\n\n"
            "_Or type *cancel* to go back._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 1. CHECKOUT INITIATION — ask for payment method
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "checkout":
        if not cart:
            return (
                "🛒 Your cart is empty!\n\n"
                "Type *menu* to see what we offer, then add an item — "
                "e.g. _\"Sadza\"_ or _\"2 Beef\"_"
            )

        # Store cart snapshot so it survives if the user adds more items
        _set_pending_checkout(phone, business_id, cart)

        return _payment_prompt(cart)

    # ══════════════════════════════════════════════════════════════════════════
    # 2. REMOVE ITEM
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "remove":
        t_lower = text.lower()
        for item in list(cart):
            if item["name"].lower() in t_lower:
                cart.remove(item)
                _save_cart(phone, business_id, cart)
                return f"🗑️ Removed *{item['name']}* from your cart.\n\n{_format_cart(cart)}"
        return f"⚠️ I couldn't find that item in your cart.\n\n{_format_cart(cart)}"

    # ══════════════════════════════════════════════════════════════════════════
    # 3. ADD / ORDER — NLP product match
    # ══════════════════════════════════════════════════════════════════════════
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

            found = False
            for item in cart:
                if item["name"] == product_name:
                    item["qty"] += qty
                    found = True
                    break
            if not found:
                cart.append({"name": product_name, "qty": qty, "price": float(product["price"])})

            _save_cart(phone, business_id, cart)
            log.info("added  %s ×%d  phone=%s", product_name, qty, phone)

            recs      = _recommend(phone, business_id, products, exclude=product_name)
            qty_label = f" ×{qty}" if qty > 1 else ""
            msg       = f"👍 Nice choice! Added *{product_name}*{qty_label} to your cart.\n\n{_format_cart(cart)}"

            if recs:
                rec_str = " or ".join(f"*{r['name']}*" for r in recs)
                msg += f"\n\n💡 You might also enjoy {rec_str}."
            msg += "\n\n_Type *checkout* to place your order, or keep adding items._"
            return msg

    # ══════════════════════════════════════════════════════════════════════════
    # 4. CART VIEW
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "cart":
        reply = _format_cart(cart)
        if cart:
            reply += "\n\n_Ready? Type *checkout* to place your order._"
        return reply

    # ══════════════════════════════════════════════════════════════════════════
    # 5. BROWSE MENU
    # ══════════════════════════════════════════════════════════════════════════
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

        recs     = _recommend(phone, business_id, products)
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

    # ══════════════════════════════════════════════════════════════════════════
    # 6. HELP / GREETING
    # ══════════════════════════════════════════════════════════════════════════
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

    # ══════════════════════════════════════════════════════════════════════════
    # 7. FALLBACK — last resort
    # ══════════════════════════════════════════════════════════════════════════
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
