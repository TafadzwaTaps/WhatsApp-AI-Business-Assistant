"""
services/_ai_lifecycle.py: cart / checkout lifecycle rules for the WhatsApp AI (Phase 6, Stage 3).

  * Incomplete checkout states expire after CHECKOUT_TTL_MINUTES (30) of inactivity. The cart is kept; a placed
    order, a payment in progress or a human conversation is NEVER touched.
  * A cart with no activity for CART_TTL_DAYS (30) is emptied (the carts row and all order history are kept,
    and the customer can simply start a new cart).
  * Before a payment request is created the CURRENT price (and stock) is read from the database. If it differs
    from the cart the customer is told and must confirm the new total; nothing is charged at a stale price.
  * Cancelling: an unfinished checkout just goes back to the cart; a placed order needs an explicit confirmation,
    goes through the existing order lifecycle (which restores stock once), and a PAID order is never refunded
    automatically: it goes to a human through the existing refund/handoff path.

Imported by ai.py. Do not import ai.py from here (circular import).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

CHECKOUT_TTL_MINUTES = 30
CART_TTL_DAYS = 30

# Pre-order states a customer can abandon. Anything where an order already exists (awaiting_fulfillment,
# awaiting_address, awaiting_payment, awaiting_proof, ...), a human is involved, or a reminder/reschedule is
# pending is deliberately NOT here: those never expire.
CHECKOUT_EXPIRABLE_STATES = {
    "confirm_order", "checkout", "order_preview", "price_changed_confirm",
    "checkout_booking_date", "checkout_booking_time", "checkout_booking_confirm",
    "awaiting_booking_date", "awaiting_booking_time", "booking_confirm",
}

PAID_STATUSES = {"paid", "confirmed", "verified", "completed", "success", "succeeded"}
_FINAL_STATUSES = {"cancelled", "refunded"}
_DONE_STATUSES = {"delivered", "completed"}


# ── time helpers ──────────────────────────────────────────────────────────────────────────────────
def parse_ts(value):
    """ISO string (Supabase style) -> aware UTC datetime, or None."""
    if not value:
        return None
    try:
        s = str(value).strip().replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        s = re.sub(r"\.(\d{1,5})(?=[+-]|$)", lambda m: "." + m.group(1).ljust(6, "0"), s)
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _now():
    return datetime.now(timezone.utc)


# ── expiry ────────────────────────────────────────────────────────────────────────────────────────
def _read_row(phone, business_id):
    from core.db import supabase
    res = (supabase.table("carts").select("state_data,items,updated_at")
           .eq("phone", phone).eq("business_id", business_id).limit(1).execute())
    return res.data[0] if res.data else None


def checkout_expiry_notice() -> str:
    return (f"⏱️ Your checkout session ended after {CHECKOUT_TTL_MINUTES} minutes of inactivity, so nothing was "
            f"ordered or charged.\n\nYour cart is still saved. Type *checkout* to start again, or *menu* to keep browsing.")


def cart_expiry_notice() -> str:
    return (f"🛒 Your old cart expired after {CART_TTL_DAYS} days without activity, so it was emptied. "
            f"Your past orders are not affected.\n\nType *menu* to start a new order.")


def expire_stale(phone, business_id, *, now=None):
    """Apply the expiry rules for this customer. Returns the notice text when something expired, else None.
    Never raises; any failure means 'do nothing' (the conversation continues exactly as before)."""
    try:
        now = now or _now()
        row = _read_row(phone, business_id)
        if not row:
            return None
        sd = row.get("state_data") or {}
        state = sd.get("state") or "browsing"
        # Activity = the newest of our own marker and the row's updated_at (every cart/state write bumps it).
        stamps = [t for t in (parse_ts(sd.get("last_activity")), parse_ts(row.get("updated_at"))) if t]
        if not stamps:
            return None
        last = max(stamps)
        from services._ai_state import _reset_state, _write_state_data
        if state == "awaiting_cancel_confirm":
            # A stale "Cancel ORDER-n?" counts as declined, so a later unrelated "ok" can never cancel.
            if now - last > timedelta(minutes=CHECKOUT_TTL_MINUTES):
                resume = (sd.get("session") or {}).get("resume_state") or "browsing"
                if resume in ("awaiting_payment", "awaiting_proof"):
                    from services._ai_state import _set_state
                    _set_state(phone, business_id, resume, session={})
                else:
                    _reset_state(phone, business_id)
            return None
        if state in CHECKOUT_EXPIRABLE_STATES:
            if now - last > timedelta(minutes=CHECKOUT_TTL_MINUTES):
                _reset_state(phone, business_id)          # cart is kept
                _write_state_data(phone, business_id, {"last_activity": None})
                return checkout_expiry_notice()
            _write_state_data(phone, business_id, {"last_activity": now.isoformat()})
            return None
        items = row.get("items")
        if state == "browsing" and items and now - last > timedelta(days=CART_TTL_DAYS):
            import crud
            crud.clear_cart(phone, business_id)           # row, state and order history stay
            return cart_expiry_notice()
    except Exception as exc:
        log.warning("expire_stale skipped: %s", type(exc).__name__)
    return None


def is_short_reply(text: str) -> bool:
    """A reply that only makes sense inside the flow that just expired (yes/no/a number/a payment method)."""
    t = (text or "").strip().lower().rstrip("️⃣").strip()
    if not t:
        return False
    if t.isdigit() and len(t) <= 2:
        return True
    return t in {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "no", "n", "nope", "confirm", "ecocash", "paypal",
                 "cash", "bank", "banktransfer", "bank transfer", "blik", "pay", "paid"}


# ── price / stock revalidation ────────────────────────────────────────────────────────────────────
def _c(x) -> float:
    return round(float(x or 0), 2)


def cart_total(cart) -> float:
    return round(sum(int(i.get("qty") or 1) * _c(i.get("price")) for i in cart or []), 2)


def reprice_cart(business_id, cart, *, is_service=False, lookup=None):
    """Compare the cart with the CURRENT database rows (by product name, as the cart carries no id).
    Returns {'ok': bool, 'cart': new cart at current prices, 'changed': [(name, old, new)], 'unavailable': [name],
             'short': [(name, wanted, available)], 'unverified': bool}.
    A lookup failure is 'unverified' (never trusted as 'unchanged')."""
    if lookup is None:
        from services.inventory import get_product_by_name as lookup
    new_cart, changed, unavailable, short = [], [], [], []
    for it in cart or []:
        name = it.get("name")
        try:
            prod = lookup(business_id, name)
        except Exception as exc:
            log.warning("reprice lookup failed biz=%s: %s", business_id, type(exc).__name__)
            return {"ok": False, "cart": cart, "changed": [], "unavailable": [], "short": [], "unverified": True}
        if not prod:
            unavailable.append(name)
            continue
        old, cur = _c(it.get("price")), _c(prod.get("price"))
        qty = int(it.get("qty") or 1)
        stock = prod.get("stock")
        if not is_service and stock is not None:
            try:
                if int(stock) < qty:
                    short.append((name, qty, max(int(stock), 0)))
            except (TypeError, ValueError):
                pass
        if old != cur:
            changed.append((name, old, cur))
        new_cart.append({**it, "price": cur})
    ok = not (unavailable or short)
    return {"ok": ok, "cart": new_cart, "changed": changed, "unavailable": unavailable, "short": short,
            "unverified": False}


def price_change_message(res, old_total, currency_sym="$") -> str:
    lines = [f"  • {n}: {currency_sym}{o:.2f} → {currency_sym}{c:.2f}" for n, o, c in res["changed"]]
    new_total = cart_total(res["cart"])
    return ("💲 *Some prices changed since you added these items:*\n\n" + "\n".join(lines) +
            f"\n\nYour total was {currency_sym}{old_total:.2f}; it is now *{currency_sym}{new_total:.2f}*.\n\n"
            "Reply *yes* to continue at the new total, or *no* to go back to your cart.")


def price_gate(phone, business_id, cart, method, currency_sym="$", is_service=False, lookup=None):
    """Run before a payment request is created. Returns {'reply': str|None, 'cart': cart to pay for}.
    reply is None when the cart is current and checkout may proceed."""
    from services._ai_state import _reset_state, _set_state
    from services._ai_memory import _save_cart
    old_total = cart_total(cart)
    res = reprice_cart(business_id, cart, is_service=is_service, lookup=lookup)
    if res["unverified"]:
        return {"reply": "⚠️ I couldn't confirm the current prices just now. Nothing was ordered or charged. "
                         "Please try again in a moment.", "cart": cart}
    if res["unavailable"] or res["short"]:
        bits = [f"  • {n} is no longer available" for n in res["unavailable"]]
        bits += [f"  • {n}: you asked for {w} but only {a} left" if a else f"  • {n} is out of stock"
                 for n, w, a in res["short"]]
        _reset_state(phone, business_id)
        return {"reply": "⚠️ *Some items in your cart can't be ordered right now:*\n\n" + "\n".join(bits) +
                         "\n\nNothing was charged. Update your cart (*remove [item]*) and type *checkout* again.",
                "cart": cart}
    if res["changed"]:
        _save_cart(phone, business_id, res["cart"])
        _set_state(phone, business_id, "price_changed_confirm",
                   session={"cart_snapshot": res["cart"], "method": method, "old_total": old_total},
                   pending_payment=None)
        return {"reply": price_change_message(res, old_total, currency_sym), "cart": res["cart"]}
    return {"reply": None, "cart": res["cart"]}


# ── cancellation ──────────────────────────────────────────────────────────────────────────────────
_CANCEL_ORDER_RE = re.compile(r"\bcancel\b.{0,20}\b(order|purchase)\b|\border\b.{0,12}\bcancel\b|\border[-\s#]*\d+\b.{0,12}\bcancel", re.I)
_YES_CANCEL = {"yes cancel", "yes, cancel", "yes cancel it", "yes, cancel it", "yes cancel my order",
               "yes, cancel my order", "yes please cancel", "confirm cancel", "confirm cancellation", "cancel it"}


_NEGATION_RE = re.compile(r"\b(don'?t|do not|dont|never|without|not)\b", re.I)
_QUESTION_RE = re.compile(r"^\s*(can|could|how|what|why|when|will|would|is|are|does|do|if|should)\b|\?\s*$", re.I)


def is_cancel_order_request(text: str) -> bool:
    t = (text or "")[:120]
    return bool(_CANCEL_ORDER_RE.search(t)) and not _NEGATION_RE.search(t)


def is_question_like(text: str) -> bool:
    """'can I cancel the order later?': asking, not instructing. Never resets a checkout on its own."""
    return bool(_QUESTION_RE.search(text or ""))


# Only an explicit yes answers "Cancel ORDER-n?". "ok", "sure" and "alright" are NOT enough.
_CONFIRM_YES = {"yes", "y", "yes please", "yes confirm", "confirm", "confirmed"}


def confirms_cancel(text: str) -> bool:
    t = re.sub(r"[^a-z ]", "", (text or "").lower()).strip()
    return t in _CONFIRM_YES or is_yes_cancel(text)


def is_yes_cancel(text: str) -> bool:
    t = re.sub(r"[^a-z, ]", "", (text or "").lower()).strip()
    return t in _YES_CANCEL


def _digits(x) -> str:
    return re.sub(r"\D", "", str(x or ""))


def cancel_order(order_id, phone, business_id) -> dict:
    """Cancel one placed order for this customer. Returns {'result': ...} with result one of
    cancelled | already_cancelled | needs_human (paid / under review / delivered) | not_found | error.
    Ownership is checked against phone AND business; paid orders are never cancelled or refunded here;
    stock is restored by the existing order lifecycle exactly once."""
    try:
        from core.db import supabase
        res = (supabase.table("orders").select("id,status,payment_status,customer_phone,total_price")
               .eq("id", order_id).eq("business_id", business_id).limit(1).execute())
        order = res.data[0] if res.data else None
    except Exception as exc:
        log.warning("cancel_order lookup failed: %s", type(exc).__name__)
        return {"result": "error"}
    if not order or _digits(order.get("customer_phone")) != _digits(phone):
        return {"result": "not_found"}
    status = (order.get("status") or "pending").lower()
    pay = (order.get("payment_status") or "").lower()
    if status in _FINAL_STATUSES:
        return {"result": "already_cancelled", "order": order}
    if pay in PAID_STATUSES or status in _DONE_STATUSES or status == "payment_review":
        return {"result": "needs_human", "order": order}
    from workflows.order_lifecycle import update_order_status_supabase
    try:
        out = update_order_status_supabase(order_id, "cancelled", business_id)
    except ValueError as exc:
        msg = str(exc)
        if "update failed" in msg:                      # lost a race with another cancel
            try:
                again = (supabase.table("orders").select("status").eq("id", order_id)
                         .eq("business_id", business_id).limit(1).execute().data or [{}])[0]
                if (again.get("status") or "") == "cancelled":
                    return {"result": "already_cancelled", "order": order}
            except Exception as exc2:
                log.debug("cancel recheck failed: %s", type(exc2).__name__)
        if "Cannot transition" in msg:
            return {"result": "needs_human", "order": order}
        return {"result": "error"}
    except Exception as exc:
        log.warning("cancel_order failed: %s", type(exc).__name__)
        return {"result": "error"}
    if not out.get("_transitioned"):
        return {"result": "already_cancelled", "order": order}
    try:
        from core.db import supabase as _sb
        now = (_sb.table("orders").select("payment_status").eq("id", order_id).eq("business_id", business_id)
               .limit(1).execute().data or [{}])[0]
        if str(now.get("payment_status") or "").lower() in PAID_STATUSES:
            # A payment landed between our check and the cancel: keep the paid marker and tell a human.
            return {"result": "needs_human", "order": {**order, "payment_status": now.get("payment_status")}}
        import crud
        crud.update_order_payment(order_id, business_id, {"payment_status": "cancelled"})
    except Exception as exc:
        log.debug("payment_status cancel mark skipped: %s", type(exc).__name__)
    return {"result": "cancelled", "order": order, "stock": out.get("_stock_restore")}


def find_cancellable_order(phone, business_id, order_id=None):
    """The customer's order a plain 'cancel my order' refers to: the named one, else the latest unfinished one.
    Only this customer's orders in this business are ever read."""
    try:
        from core.db import supabase
        q = (supabase.table("orders").select("id,status,payment_status,total_price")
             .eq("customer_phone", phone).eq("business_id", business_id))
        if order_id:
            q = q.eq("id", int(order_id))
        else:
            q = q.in_("status", ["pending", "pending_cash", "awaiting_payment", "payment_review", "confirmed"])
        rows = q.order("id", desc=True).limit(1).execute().data or []
        return rows[0] if rows else None
    except Exception as exc:
        log.warning("find_cancellable_order failed: %s", type(exc).__name__)
        return None


# ── "Where is my order?" (order-status inquiry, never a refund) ───────────────────────────────────
_OPEN_EXCLUDED = {"cancelled", "refunded", "completed", "delivered"}


def fulfilment_line(order: dict) -> str:
    """One line of delivery / pickup information from the customer's OWN order row ('' when unknown)."""
    fm = str(order.get("fulfillment_method") or "").lower()
    status = str(order.get("status") or "").lower()
    addr = str(order.get("delivery_address") or "").strip()
    if status in ("delivered", "completed"):
        return "📦 This order has been delivered." if fm != "pickup" else "🏪 This order was collected."
    if fm == "delivery":
        base = f"🚚 Delivery to: {addr}" if addr else "🚚 Delivery (no address saved yet)"
        if status == "out_for_delivery":
            base += "\nIt is out for delivery now."
        return base
    if fm == "pickup":
        return "🏪 Pickup: it is ready for collection." if status == "ready" else "🏪 Pickup: we'll tell you when it is ready to collect."
    return ""


def order_status_reply(phone, business_id, text, currency_sym, flavor, status_message_fn, order_id=None) -> str:
    """Answer an order-status question from THIS customer's orders only. One open order: its status and
    delivery/pickup info. Several: ask which. None: say so. Never starts a refund or dispute."""
    try:
        from core.db import supabase
        q = (supabase.table("orders").select("*").eq("customer_phone", phone).eq("business_id", business_id))
        if order_id:
            q = q.eq("id", int(order_id))
        rows = q.order("id", desc=True).limit(10).execute().data or []
    except Exception as exc:
        log.warning("order_status_reply lookup failed: %s", type(exc).__name__)
        return "⚠️ I couldn't look up your order just now. Please try again in a moment."
    word = "BOOKING" if flavor == "service" else "ORDER"
    if order_id and not rows:
        return f"❓ I couldn't find *{word}-{order_id}* for your account."
    if not rows:
        return ("I couldn't find any orders on this number yet.\n\n"
                "If you ordered with a different number, send your order reference (e.g. *ORDER-12*). "
                "Type *menu* to place an order.")
    open_rows = [r for r in rows if str(r.get("status") or "pending").lower() not in _OPEN_EXCLUDED]
    if not order_id and len(open_rows) > 1:
        lines = []
        for r in open_rows[:5]:
            try:
                amt = f"{currency_sym}{float(r.get('total_price') or 0):.2f}"
            except (TypeError, ValueError):
                amt = ""
            when = str(r.get("created_at") or "")[:10]
            lines.append(f"  • {word}-{r['id']}  —  {str(r.get('status') or 'pending').replace('_', ' ')}  {amt}  {when}".rstrip())
        return ("You have more than one order in progress:\n\n" + "\n".join(lines) +
                f"\n\nWhich one would you like an update on? Reply with the number, e.g. *{word}-{open_rows[0]['id']}*.")
    chosen = rows[0] if order_id else (open_rows[0] if open_rows else rows[0])
    if str(chosen.get("status") or "").lower() in ("cancelled", "refunded") and not order_id:
        return (f"Your most recent order, *{word}-{chosen['id']}*, was {str(chosen['status']).lower()}.\n\n"
                "Type *menu* to place a new order.")
    msg = status_message_fn(chosen["id"], phone, business_id, currency_sym, flavor)
    extra = fulfilment_line(chosen)
    return msg + (f"\n\n{extra}" if extra else "")
