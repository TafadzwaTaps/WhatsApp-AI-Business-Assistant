"""
ai.py — Universal Autonomous Business AI Engine  (v5 — simplified payments)

Payment stack:
  1. EcoCash  — manual, customer dials *151# and replies "paid"
  2. PayPal   — email or auto-link (if API credentials set)
  3. Cash     — on delivery / pickup

CHECKOUT FLOW:
  "checkout" / "pay"
    → Show cart summary + payment method menu
  "1" / "ecocash" / "2" / "paypal" / "3" / "cash"
    → Create order in Supabase (stock reduced)
    → Send payment instructions for chosen method
  "paid" / "sent" / "done"
    → Mark order as awaiting_payment
    → Thank customer, tell them we'll verify

INTENT PRIORITY:
  0. Payment confirmation ("paid" / "sent" / "done")
  1. Payment method reply  (when awaiting selection)
  2. Checkout initiation   ("checkout")
  3. Remove item
  4. Add to cart           (NLP product match)
  5. Cart view
  6. Browse menu
  7. Help / greeting
  8. Fallback
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


def _save_memory(phone: str, business_id: int, mem: dict) -> None:
    try:
        crud.save_user_memory(phone, business_id, mem)
    except Exception as exc:
        log.warning("_save_memory failed: %s", exc)


def _update_order_history(phone: str, business_id: int, cart: list) -> None:
    try:
        mem = _get_memory(phone, business_id)
        for item in cart:
            name = item["name"]
            mem["frequent_items"][name] = mem["frequent_items"].get(name, 0) + item["qty"]
        mem["last_orders"].append([i["name"] for i in cart])
        mem["last_orders"] = mem["last_orders"][-10:]
        _save_memory(phone, business_id, mem)
    except Exception as exc:
        log.warning("_update_order_history failed: %s", exc)


# ── Checkout session (stored in memory so it survives reconnects) ─────────────

def _get_checkout_session(phone: str, business_id: int) -> dict | None:
    try:
        return _get_memory(phone, business_id).get("pending_checkout") or None
    except Exception:
        return None


def _set_checkout_session(phone: str, business_id: int, cart: list) -> None:
    try:
        mem = _get_memory(phone, business_id)
        mem["pending_checkout"] = {"awaiting_payment_method": True, "cart_snapshot": cart}
        _save_memory(phone, business_id, mem)
    except Exception as exc:
        log.warning("_set_checkout_session failed: %s", exc)


def _clear_checkout_session(phone: str, business_id: int) -> None:
    try:
        mem = _get_memory(phone, business_id)
        mem["pending_checkout"] = None
        _save_memory(phone, business_id, mem)
    except Exception as exc:
        log.warning("_clear_checkout_session failed: %s", exc)


# ── Pending payment session (order placed, waiting for "paid" reply) ──────────

def _get_pending_payment(phone: str, business_id: int) -> dict | None:
    """Returns { order_id, method, reference } or None."""
    try:
        return _get_memory(phone, business_id).get("pending_payment") or None
    except Exception:
        return None


def _set_pending_payment(phone: str, business_id: int, order_id, method: str, reference: str) -> None:
    try:
        mem = _get_memory(phone, business_id)
        mem["pending_payment"] = {
            "order_id":  order_id,
            "method":    method,
            "reference": reference,
        }
        _save_memory(phone, business_id, mem)
    except Exception as exc:
        log.warning("_set_pending_payment failed: %s", exc)


def _clear_pending_payment(phone: str, business_id: int) -> None:
    try:
        mem = _get_memory(phone, business_id)
        mem["pending_payment"] = None
        _save_memory(phone, business_id, mem)
    except Exception as exc:
        log.warning("_clear_pending_payment failed: %s", exc)


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
    except Exception as exc:
        log.error("_save_cart error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# INTENT ENGINE
# ─────────────────────────────────────────────────────────────────────────────

# Payment confirmation words
_PAID_WORDS = {
    "paid", "sent", "done", "i paid", "i've paid", "ive paid",
    "payment sent", "money sent", "transferred", "i sent",
    "i have paid", "i transferred", "already paid",
}

# Payment method selection
_ECOCASH_WORDS = {"ecocash", "eco cash", "eco", "1", "1️⃣"}
_PAYPAL_WORDS  = {"paypal", "pay pal", "pp", "payp", "2", "2️⃣"}
_CASH_WORDS    = {"cash", "cod", "delivery", "pickup", "pick up", "collect", "3", "3️⃣"}
_CANCEL_WORDS  = {"cancel", "back", "no", "stop", "nevermind", "never mind", "go back"}


def _is_payment_confirmation(text: str) -> bool:
    t = text.lower().strip()
    return t in _PAID_WORDS or any(w in t for w in ["i paid", "already paid", "sent money"])


def _detect_payment_method(text: str) -> str | None:
    """Returns 'ecocash' | 'paypal' | 'cash' | 'cancel' | None."""
    t = text.lower().strip()
    if t in _CANCEL_WORDS:
        return "cancel"
    if t in _ECOCASH_WORDS or "ecocash" in t or "eco cash" in t:
        return "ecocash"
    if t in _PAYPAL_WORDS or "paypal" in t:
        return "paypal"
    if t in _CASH_WORDS or any(w in t for w in ["cash", "deliver", "pickup", "collect"]):
        return "cash"
    return None


def _intent(text: str) -> str:
    t = text.lower().strip()

    if any(w in t for w in [
        "checkout", "confirm order", "place order", "complete order",
        "done", "finish", "i'm done", "im done", "order now", "submit",
    ]) or t in ("pay", "checkout"):
        return "checkout"

    if t.startswith("remove") or t.startswith("delete") or "remove " in t:
        return "remove"

    if any(w in t for w in [
        "my cart", "view cart", "show cart", "whats in cart",
        "what's in cart", "cart", "my order so far", "what i have",
    ]):
        return "cart"

    if any(w in t for w in [
        "menu", "list", "browse", "show me", "catalog",
        "what do you have", "what do you sell", "products",
        "whats available", "what's available", "show products",
    ]):
        return "browse"

    if (any(w in t for w in ["help", "hi ", "hello", "hey ", "start", "hie", "howzit"])
            or t in ("hi", "hello", "hey", "hie", "yo", "sup", "howzit", "start")):
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

    # Exact match
    if t in name_map:
        return name_map[t]

    # Strip intent phrases + leading quantity
    stripped = re.sub(
        r"^(i want|i'd like|give me|add|order|get me|can i have|can i get|please)\s+",
        "", t, flags=re.IGNORECASE
    ).strip()
    stripped = re.sub(r"^(x\s*)?\d+\s+", "", stripped).strip()
    if stripped and stripped in name_map:
        return name_map[stripped]

    # Fuzzy full-phrase match
    for candidate in (t, stripped):
        if not candidate:
            continue
        m = get_close_matches(candidate, names, n=1, cutoff=0.55)
        if m:
            return name_map[m[0]]

    # Word-by-word fuzzy
    for word in t.split():
        if len(word) < 3:
            continue
        m = get_close_matches(word, names, n=1, cutoff=0.60)
        if m:
            return name_map[m[0]]

    # Substring: product name appears in message
    for name, product in name_map.items():
        if name in t:
            return product

    # Reverse: any word of product name in message
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
    try:
        mem  = _get_memory(phone, business_id)
        freq = mem.get("frequent_items", {})
        recs = [p for p in products if p["name"].lower() != exclude.lower()]
        if freq:
            recs.sort(key=lambda p: freq.get(p["name"], 0), reverse=True)
        return recs[:2]
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# CART FORMATTER
# ─────────────────────────────────────────────────────────────────────────────

def _format_cart(cart: list) -> str:
    if not cart:
        return "🛒 Your cart is empty. Type *menu* to see what we have!"
    total = 0.0
    lines = []
    for i in cart:
        sub = i["qty"] * float(i["price"])
        total += sub
        lines.append(f"  • {i['name']} ×{i['qty']}  —  ${sub:.2f}")
    return "🛒 *Your Cart:*\n" + "\n".join(lines) + f"\n\n💰 *Total: ${total:.2f}*"


# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT METHOD PROMPT
# ─────────────────────────────────────────────────────────────────────────────

def _build_payment_menu(cart: list) -> str:
    """Build the payment selection message shown after 'checkout'."""
    from payments import available_methods

    cart_summary = _format_cart(cart)
    methods      = available_methods()

    # Build numbered list of available methods only
    options: list[str] = []
    num = 1
    for m in methods:
        if m == "ecocash":
            options.append(f"{num}️⃣  *EcoCash* — Send via *151# (Zimbabwe)")
        elif m == "paypal":
            options.append(f"{num}️⃣  *PayPal* — Send to our email or pay online")
        elif m == "cash":
            options.append(f"{num}️⃣  *Cash* — Pay on delivery or pickup")
        num += 1

    options_text = "\n".join(options)

    return (
        f"{cart_summary}\n\n"
        f"You're almost there! 😊\n\n"
        f"How would you like to pay?\n\n"
        f"{options_text}\n\n"
        f"_Reply with the number or name of your choice._\n"
        f"_Type *cancel* to go back._"
    )


# ─────────────────────────────────────────────────────────────────────────────
# PROCESS PAYMENT — create order + call gateway
# ─────────────────────────────────────────────────────────────────────────────

def _process_payment(
    method: str,
    cart: list,
    phone: str,
    business_id: int,
    business_name: str,
) -> str:
    """
    1. Create order in Supabase (stock reduced atomically).
    2. Call the payment gateway for this method.
    3. Save payment details back to the order row.
    4. Set a pending_payment session so "paid" reply works.
    5. Clear cart + checkout session.
    6. Return WhatsApp-ready reply.
    """
    from order_lifecycle import create_order_supabase
    from payments import (
        generate_ecocash_instructions,
        paypal_payment,
        generate_cash_instructions,
    )

    # ── Step 1: Create order ──────────────────────────────────────────────────
    try:
        log.info("_process_payment  method=%s  phone=%s  items=%d", method, phone, len(cart))
        order = create_order_supabase(
            business_id=business_id,
            customer_phone=phone,
            cart=cart,
            payment_method=method,
        )
        order["business_name"] = business_name

        # Inject business payment details so gateways can use them
        try:
            biz = crud.get_business_by_id(business_id)
            if biz:
                order["payment_number"] = biz.get("payment_number", "")
                order["payment_name"]   = biz.get("payment_name", "")
        except Exception:
            pass

        log.info("order created  id=%s  method=%s", order.get("id", "?"), method)

    except ValueError as exc:
        log.warning("order creation blocked: %s", exc)
        return (
            f"⚠️ Couldn't place your order:\n_{exc}_\n\n"
            "Please adjust your cart and try *checkout* again."
        )
    except Exception as exc:
        log.exception("order creation error: %s", exc)
        return (
            "❌ Something went wrong saving your order.\n\n"
            "Your cart is still saved — please try *checkout* again in a moment."
        )

    # ── Step 2: Call payment gateway ──────────────────────────────────────────
    try:
        if method == "ecocash":
            pay = generate_ecocash_instructions(order)
        elif method == "paypal":
            pay = paypal_payment(order)
        elif method == "cash":
            pay = generate_cash_instructions(order)
        else:
            pay = generate_cash_instructions(order)   # safe default
    except Exception as exc:
        log.exception("payment gateway error  method=%s  exc=%s", method, exc)
        pay = {
            "error":     str(exc),
            "message":   (
                "⚠️ Payment details couldn't load right now.\n"
                f"Your order *ORDER-{order.get('id', '?')}* is saved.\n"
                "Please contact us to complete payment."
            ),
            "reference": f"ORDER-{order.get('id', '?')}",
            "status":    "awaiting_payment",
        }

    # ── Step 3: Save payment fields to order ──────────────────────────────────
    try:
        order_id = order.get("id")
        if order_id:
            update = {
                "payment_method":    method,
                "payment_status":    "awaiting_payment" if not pay.get("error") else "payment_error",
                "payment_reference": pay.get("reference", f"ORDER-{order_id}"),
            }
            if pay.get("url"):
                update["payment_url"] = pay["url"]
            crud.update_order_payment(order_id, business_id, update)
    except Exception as exc:
        log.warning("update payment details failed: %s", exc)

    # ── Step 4: Set pending_payment session (for "paid" reply) ────────────────
    if method != "cash":
        # Cash orders are confirmed immediately — no "paid" reply needed
        _set_pending_payment(
            phone, business_id,
            order_id=order.get("id"),
            method=method,
            reference=pay.get("reference", ""),
        )

    # ── Step 5: Clear cart + sessions ─────────────────────────────────────────
    _update_order_history(phone, business_id, cart)
    crud.clear_cart(phone, business_id)
    _clear_checkout_session(phone, business_id)

    # ── Step 6: Send PDF invoice (non-blocking) ───────────────────────────────
    _send_pdf_invoice(order, phone, business_id)

    return pay.get("message", "Order placed! We'll be in touch. 🙏")


# ─────────────────────────────────────────────────────────────────────────────
# PDF INVOICE HELPER (non-blocking — never crashes checkout)
# ─────────────────────────────────────────────────────────────────────────────

def _send_pdf_invoice(order: dict, phone: str, business_id: int) -> None:
    try:
        from pdf_invoice import generate_pdf_invoice
        pdf_path = generate_pdf_invoice(order)
    except Exception as exc:
        log.error("PDF generation failed: %s", exc)
        return
    try:
        biz = crud.get_business_by_id(business_id)
        if not biz:
            return
        token   = crud.get_decrypted_token(biz)
        phone_id = biz.get("whatsapp_phone_id")
        if not token or not phone_id:
            return
        from whatsapp import send_whatsapp_document
        result = send_whatsapp_document(
            phone=phone, file_path=pdf_path,
            access_token=token, phone_number_id=phone_id,
            caption=f"📄 Invoice for ORDER-{order.get('id', '?')}",
        )
        if "error" not in result:
            log.info("PDF invoice sent  order=%s", order.get("id"))
    except Exception as exc:
        log.exception("_send_pdf_invoice error: %s", exc)


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

    # ══════════════════════════════════════════════════════════════════════════
    # PRIORITY 0 — PAYMENT CONFIRMATION ("paid" / "sent" / "done")
    # Intercept before any other intent so it always fires correctly.
    # ══════════════════════════════════════════════════════════════════════════
    if _is_payment_confirmation(text):
        pending_pay = _get_pending_payment(phone, business_id)

        if pending_pay:
            order_id  = pending_pay.get("order_id")
            method    = pending_pay.get("method", "unknown")
            reference = pending_pay.get("reference", f"ORDER-{order_id}")

            # Mark order as awaiting_payment (business will verify)
            try:
                if order_id:
                    crud.update_order_payment(order_id, business_id, {
                        "payment_status": "awaiting_payment",
                    })
            except Exception as exc:
                log.warning("payment status update failed: %s", exc)

            _clear_pending_payment(phone, business_id)

            method_label = {
                "ecocash": "EcoCash",
                "paypal":  "PayPal",
                "cash":    "Cash",
            }.get(method, method.title())

            return (
                f"✅ *Got it! Thank you!*\n\n"
                f"We've received your payment confirmation via *{method_label}*.\n\n"
                f"📦 Order : *{reference}*\n\n"
                f"Our team will verify your payment and confirm your order shortly.\n"
                f"You'll receive a message as soon as it's confirmed. 🙏\n\n"
                f"_Thank you for ordering from *{business_name}*!_"
            )

        # No pending order — could be a stray "paid" message
        return (
            "🤔 Hmm, I don't see an active order waiting for payment.\n\n"
            "If you've just placed an order, please send *checkout* first.\n"
            f"Type *menu* to browse, or *cart* to see what you have. 😊"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # PRIORITY 1 — PAYMENT METHOD REPLY
    # When user is in the checkout flow selecting a payment method.
    # ══════════════════════════════════════════════════════════════════════════
    checkout_session = _get_checkout_session(phone, business_id)

    if checkout_session and checkout_session.get("awaiting_payment_method"):
        method = _detect_payment_method(text)

        if method == "cancel":
            _clear_checkout_session(phone, business_id)
            return (
                "🚫 Checkout cancelled. Your cart is still saved.\n\n"
                f"{_format_cart(cart)}\n\n"
                "_Type *checkout* whenever you're ready._"
            )

        if method in ("ecocash", "paypal", "cash"):
            cart_snapshot = checkout_session.get("cart_snapshot") or cart
            return _process_payment(
                method=method,
                cart=cart_snapshot,
                phone=phone,
                business_id=business_id,
                business_name=business_name,
            )

        # Unrecognised reply while selecting payment
        return (
            "🤔 I didn't catch that.\n\n"
            "Please reply with:\n"
            "  1️⃣ *EcoCash*\n"
            "  2️⃣ *PayPal*\n"
            "  3️⃣ *Cash on delivery*\n\n"
            "_Or type *cancel* to go back._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 2. CHECKOUT — show cart + payment options
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "checkout":
        if not cart:
            return (
                "🛒 Your cart is empty!\n\n"
                "Type *menu* to see what we offer, then add something — "
                "e.g. _\"Sadza\"_ or _\"2 Beef\"_"
            )
        _set_checkout_session(phone, business_id, cart)
        return _build_payment_menu(cart)

    # ══════════════════════════════════════════════════════════════════════════
    # 3. REMOVE ITEM
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
    # 4. ADD / ORDER — NLP product match
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

            # Stock guard
            available = product.get("stock")
            if available is not None:
                in_cart = next((i["qty"] for i in cart if i["name"] == product_name), 0)
                if in_cart + qty > available:
                    if available == 0:
                        return (
                            f"😔 *{product_name}* is currently out of stock.\n\n"
                            "Type *menu* to see what's available."
                        )
                    return (
                        f"⚠️ Only *{available}* unit(s) of *{product_name}* available "
                        f"(you already have {in_cart} in your cart)."
                    )

            # Update cart
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
                msg += "\n\n💡 You might also like " + " or ".join(f"*{r['name']}*" for r in recs) + "."
            msg += "\n\n_Type *checkout* when you're ready to order._"
            return msg

    # ══════════════════════════════════════════════════════════════════════════
    # 5. CART VIEW
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "cart":
        reply = _format_cart(cart)
        if cart:
            reply += "\n\n_Ready? Type *checkout* to place your order._"
        return reply

    # ══════════════════════════════════════════════════════════════════════════
    # 6. BROWSE MENU
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "browse":
        if not products:
            return f"📋 *{business_name}*\n\nNo items available yet. Check back soon! 🙏"

        lines = []
        for i, p in enumerate(products):
            note = ""
            s    = p.get("stock")
            if s is not None and s <= 5:
                note = f"  ⚠️ _only {s} left_"
            lines.append(f"  {i+1}. *{p['name']}* — ${float(p['price']):.2f}{note}")

        recs     = _recommend(phone, business_id, products)
        rec_text = ""
        if recs:
            rec_text = "\n\n⭐ *You usually order:*\n" + "\n".join(f"  • {r['name']}" for r in recs)

        return (
            f"📋 *{business_name} Menu*\n\n"
            + "\n".join(lines)
            + rec_text
            + "\n\n_Type an item name to add it — e.g. \"Sadza\" or \"2 Beef\"_"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 7. HELP / GREETING
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "help":
        hint = f"*{products[0]['name']}*" if products else "an item"
        return (
            f"👋 Hey! Welcome to *{business_name}*!\n\n"
            f"Here's what you can do:\n"
            f"  📋 *menu* — see everything we offer\n"
            f"  🛍️ Type a name — e.g. _{hint}_\n"
            f"  🛒 *cart* — see what you've added\n"
            f"  ✅ *checkout* — place your order\n"
            f"  ❌ *remove [item]* — remove something\n\n"
            f"What can I get you today? 😊"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 8. FALLBACK — one more product match attempt before giving up
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
        f"🤖 Hmm, I didn't quite get that.\n\n"
        f"Try:\n"
        f"  📋 *menu* — browse what we offer\n"
        f"  🛍️ Type a product name — {hint}\n"
        f"  🛒 *cart* — view your cart\n"
        f"  ✅ *checkout* — place your order\n"
    )
