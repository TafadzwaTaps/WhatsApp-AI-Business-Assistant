"""
services/_ai_products.py — Product matching, quantity parsing, recommendations,
cart formatters, and multi-item parsing.

Imported by ai.py. Do not import ai.py from here (circular import).
"""

import re
import logging
from difflib import get_close_matches

log = logging.getLogger(__name__)


# ── Product matching ──────────────────────────────────────────────────────────

def _find_product(text: str, products: list) -> dict | None:
    """
    Multi-strategy product matcher.

    Primary: delegates to fuzzy_matcher.find_product() which uses rapidfuzz
    (when installed) or difflib. Handles spelling mistakes, pluralization,
    case differences, and intent prefixes automatically.

    Fallback (if fuzzy_matcher unavailable): original difflib-based logic.
    """
    from services._ai_lazy import _fuzzy

    if not products:
        return None

    try:
        result = _fuzzy().find_product(text, products)
        if result:
            return result
    except Exception as exc:
        log.warning("_find_product: fuzzy_matcher failed (%s) — using difflib", exc)

    t        = text.lower().strip()
    name_map = {p["name"].lower(): p for p in products}
    names    = list(name_map.keys())

    if t in name_map:
        return name_map[t]

    stripped = re.sub(
        r"^(i want|i'?d like|give me|add|order|get me|can i (?:have|get)|please)\s+",
        "", t, flags=re.IGNORECASE
    ).strip()
    stripped = re.sub(r"^(?:x\s*)?\d+\s+", "", stripped).strip()
    if stripped and stripped != t and stripped in name_map:
        return name_map[stripped]

    for candidate in dict.fromkeys([t, stripped]):
        if not candidate:
            continue
        m = get_close_matches(candidate, names, n=1, cutoff=0.55)
        if m:
            return name_map[m[0]]

    for word in t.split():
        if len(word) < 4:
            continue
        m = get_close_matches(word, names, n=1, cutoff=0.65)
        if m:
            return name_map[m[0]]

    for name, product in name_map.items():
        if name in t:
            return product

    for name, product in name_map.items():
        for part in name.split():
            if len(part) >= 3 and part in t:
                return product

    return None


# ── Quantity parsing ──────────────────────────────────────────────────────────

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


# ── Recommendations ───────────────────────────────────────────────────────────

def _recommend(phone: str, business_id: int, products: list, exclude: str = "") -> list:
    """
    Return up to 2 recommended products.

    Uses sales_ai_service when available (personalised cross-sell scoring).
    Falls back to frequency-sorted list (original behaviour) if unavailable.
    """
    from services._ai_lazy import _sales_ai
    from services._ai_memory import _get_memory

    try:
        mem = _get_memory(phone, business_id)

        _get_sugg, _, _, _ = _sales_ai()
        if _get_sugg:
            placeholder = {"name": exclude, "price": 0} if exclude else {}
            if placeholder:
                fake_cart   = [{"name": exclude, "qty": 1, "price": 0}]
                suggestions = _get_sugg(placeholder, fake_cart, products, mem, max_results=2)
            else:
                suggestions = _get_sugg({"name": "", "price": 0}, [], products, mem, max_results=2)
            if suggestions:
                return suggestions

        freq = mem.get("frequent_items", {})
        recs = [p for p in products if p["name"].lower() != exclude.lower()]
        if freq:
            recs.sort(key=lambda p: freq.get(p["name"], 0), reverse=True)
        return recs[:2]

    except Exception:
        try:
            recs = [p for p in products if p.get("name", "").lower() != exclude.lower()]
            return recs[:2]
        except Exception:
            return []


# ── Cart formatters ───────────────────────────────────────────────────────────

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


def _build_confirm_prompt(cart: list) -> str:
    """Double-confirmation message shown before placing the order."""
    cart_summary = _format_cart(cart)
    return (
        f"📋 *Please confirm your order:*\n\n"
        f"{cart_summary}\n\n"
        f"Is this correct? Reply *yes* to continue or *no* to edit your cart.\n"
        f"_Type *cancel* to cancel entirely._"
    )


def _build_payment_menu(cart: list, business_id: int) -> str:
    """Payment method selection message. business_id required for per-business settings."""
    import crud
    from services.payment_service import available_methods

    try:
        pay_settings = crud.get_business_payment_settings(business_id)
    except Exception:
        pay_settings = {}

    cart_summary = _format_cart(cart)
    methods      = available_methods({**pay_settings, "business_id": business_id})

    options: list[str] = []
    num = 1
    for m in methods:
        if m == "ecocash":
            options.append(f"{num}️⃣  *EcoCash* — Dial *151# (Zimbabwe)")
        elif m == "paypal":
            options.append(f"{num}️⃣  *PayPal* — Email or secure link")
        elif m == "cash":
            options.append(f"{num}️⃣  *Cash* — Pay on delivery or pickup")
        num += 1

    return (
        f"{cart_summary}\n\n"
        f"You're almost there! 😊\n\n"
        f"How would you like to pay?\n\n"
        + "\n".join(options) +
        "\n\n_Reply with the number or name — e.g. *1*, *ecocash*, *paypal*, *cash*_\n"
        "_Type *cancel* to go back._"
    )


# ── Multi-item parser ─────────────────────────────────────────────────────────

def _parse_multi_items(text: str, products: list) -> list[tuple]:
    """
    Parse a message that may contain multiple products.
    Returns a list of (product_dict, qty) tuples.

    Handles:
      "Pizza and ice cream"
      "2 beef and a sadza"
      "pizza, ice cream and 2 sadza"
      "pizza + ice cream"
    """
    from services._ai_lazy import _fuzzy

    if not products:
        return []

    t = text.lower().strip()
    for sep in [" and ", ", and ", " & ", " + ", ", "]:
        t = t.replace(sep, "|")

    parts = [p.strip() for p in t.split("|") if p.strip()]
    if len(parts) <= 1:
        return []

    found: list[tuple]     = []
    seen_names: set[str]   = set()

    for part in parts:
        product, qty = _fuzzy().extract_product_and_quantity(part, products)
        if product is None:
            product = _find_product(part, products)
            qty     = _qty(part) if product else 1
        if product:
            name = product["name"].lower()
            if name in seen_names:
                for i, (p, q) in enumerate(found):
                    if p["name"].lower() == name:
                        found[i] = (p, q + qty)
                        break
                continue
            seen_names.add(name)
            found.append((product, qty))

    return found if len(found) >= 2 else []
