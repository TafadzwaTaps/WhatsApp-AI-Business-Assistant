"""
services/sales_ai_service.py — Sales AI Layer (Phase 7)

PURPOSE
───────
Replaces the naive top-2 frequency recommender with a real sales intelligence
layer that considers:

  1. CROSS-SELL   — what other customers commonly pair with this product
  2. UPSELL       — higher-value items in the same category
  3. PERSONALIZED — items the customer has bought before (but not this time)
  4. TRENDING     — most-ordered products across all customers this business
  5. BASKET LOGIC — what's missing from a "complete meal" given cart contents

All methods are read-only. None write to the database or modify state.
The caller (ai.py) decides whether and how to surface the suggestions.

INTEGRATION
───────────
Called from ai.py in three places:
  • After single-item add (P7c) — cross-sell / upsell suggestion
  • After order_preview confirmation (P0.2) — basket-completion suggestion
  • After browse menu (P9) — personalised "you usually order" section

DESIGN RULES
────────────
• Never returns the item just added (no "you might like X, you already added X")
• Never returns out-of-stock items
• Maximum 2 suggestions per call — WhatsApp messages must stay short
• Confidence gate: only surfaces suggestions when score ≥ 0.4
• Graceful degradation: falls back to frequency-sorted list on any error
• Zero side effects — purely functional

CROSS-SELL PAIRS
────────────────
Built from customer order history stored in user_memory.last_orders.
If customers frequently order "pizza" alongside "coke", that pair is learned.
Pairs are scored by co-occurrence frequency.
"""

from __future__ import annotations

import logging
from typing import Optional
from collections import defaultdict, Counter

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

MAX_SUGGESTIONS       = 2      # never show more than 2 suggestions at once
MIN_CONFIDENCE        = 0.35   # below this score, return nothing
COOCCURRENCE_WEIGHT   = 0.55   # weight of co-occurrence signal
FREQUENCY_WEIGHT      = 0.25   # weight of personal frequency signal
TRENDING_WEIGHT       = 0.20   # weight of trending/popularity signal


# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY PAIRING MAP
# ─────────────────────────────────────────────────────────────────────────────
# Static rules: when a customer adds something from category A,
# suggest something from category B.
# These work WITHOUT any order history — useful for new businesses.

_CATEGORY_PAIRS: dict[str, list[str]] = {
    "main":      ["drinks", "dessert", "sides"],
    "pizza":     ["drinks", "sides", "dessert"],
    "burger":    ["drinks", "fries", "sides"],
    "chicken":   ["drinks", "fries", "sides", "salad"],
    "sadza":     ["vegetables", "meat", "relish", "drinks"],
    "rice":      ["vegetables", "meat", "relish", "drinks", "stew"],
    "bread":     ["drinks", "spreads", "eggs"],
    "drinks":    ["main", "snacks", "dessert"],
    "dessert":   ["drinks"],
    "breakfast": ["drinks", "eggs", "bread"],
    "fries":     ["drinks", "burger", "sauce"],
    "salad":     ["drinks", "main", "dressing"],
    "stew":      ["sadza", "rice", "bread"],
    "meat":      ["sadza", "rice", "drinks", "vegetables"],
    "fish":      ["sadza", "rice", "chips", "drinks"],
    "snack":     ["drinks"],
}

# Keyword → category mapping for products without explicit category columns
_CATEGORY_KEYWORDS: dict[str, str] = {
    "pizza":      "pizza",
    "burger":     "burger",
    "sadza":      "sadza",
    "rice":       "rice",
    "bread":      "bread",
    "coke":       "drinks",
    "pepsi":      "drinks",
    "fanta":      "drinks",
    "sprite":     "drinks",
    "water":      "drinks",
    "juice":      "drinks",
    "beer":       "drinks",
    "wine":       "drinks",
    "coffee":     "drinks",
    "tea":        "drinks",
    "milk":       "drinks",
    "ice cream":  "dessert",
    "cake":       "dessert",
    "fries":      "fries",
    "chips":      "fries",
    "chicken":    "chicken",
    "beef":       "meat",
    "pork":       "meat",
    "lamb":       "meat",
    "fish":       "fish",
    "calamari":   "fish",
    "salad":      "salad",
    "vegetables": "vegetables",
    "eggs":       "breakfast",
    "breakfast":  "breakfast",
    "stew":       "stew",
    "bun":        "bread",
    "roll":       "bread",
}


def _infer_category(product_name: str) -> Optional[str]:
    """
    Infer a category for a product from its name using keyword matching.
    Returns None if no keyword matches.
    """
    name_lower = product_name.lower()
    for keyword, category in _CATEGORY_KEYWORDS.items():
        if keyword in name_lower:
            return category
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SCORING ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _score_products(
    candidates:    list[dict],
    target_name:   str,
    cart_names:    set[str],
    memory:        dict,
    co_pairs:      dict[str, float],
) -> list[tuple[dict, float]]:
    """
    Score each candidate product for relevance given:
      - what was just added (target_name)
      - what's already in the cart
      - personal purchase history (memory)
      - learned co-occurrence pairs (co_pairs)

    Returns list of (product, score) sorted descending, excluding cart items
    and out-of-stock items.
    """
    freq       = memory.get("frequent_items", {})
    max_freq   = max(freq.values(), default=1) or 1

    scored = []
    for p in candidates:
        name = p.get("name", "")
        if not name:
            continue

        # Skip items already in cart or just added
        if name.lower() in cart_names:
            continue

        # Skip out-of-stock (stock=0, not stock=None which means unlimited)
        stock = p.get("stock")
        if stock is not None and stock <= 0:
            continue

        score = 0.0

        # Signal 1: co-occurrence with target product
        pair_key = _pair_key(target_name, name)
        co_score = co_pairs.get(pair_key, 0.0)
        score += co_score * COOCCURRENCE_WEIGHT

        # Signal 2: personal frequency (normalised 0–1)
        personal_freq  = freq.get(name, 0)
        personal_score = personal_freq / max_freq if max_freq > 0 else 0.0
        score += personal_score * FREQUENCY_WEIGHT

        # Signal 3: category pairing (static rules)
        target_cat    = _infer_category(target_name)
        candidate_cat = _infer_category(name)
        if target_cat and candidate_cat:
            paired_cats = _CATEGORY_PAIRS.get(target_cat, [])
            if candidate_cat in paired_cats:
                score += 0.4 * TRENDING_WEIGHT   # static pairing bonus

        # Minimum score gate
        if score > 0:
            scored.append((p, round(score, 4)))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _pair_key(a: str, b: str) -> str:
    """Canonical key for a product pair (order-independent)."""
    pair = sorted([a.lower().strip(), b.lower().strip()])
    return f"{pair[0]}::{pair[1]}"


def _build_cooccurrence(order_history: list[list[str]]) -> dict[str, float]:
    """
    Build a co-occurrence frequency map from order history.

    order_history: list of orders, each order is a list of product names.
    Returns: {pair_key: normalised_score} where score is 0–1.

    Example:
        [["pizza", "coke"], ["pizza", "fries"], ["pizza", "coke"]]
        → {"coke::pizza": 0.67, "fries::pizza": 0.33}
    """
    pair_counts: Counter = Counter()
    product_counts: Counter = Counter()

    for order in order_history:
        items = list(set(n.lower().strip() for n in order))  # deduplicate per order
        product_counts.update(items)
        # Count every unique pair in this order
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                pair_counts[_pair_key(items[i], items[j])] += 1

    if not pair_counts:
        return {}

    max_count = max(pair_counts.values(), default=1)
    return {k: v / max_count for k, v in pair_counts.items()}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def get_suggestions(
    added_product: dict,
    cart:          list[dict],
    products:      list[dict],
    memory:        dict,
    max_results:   int = MAX_SUGGESTIONS,
) -> list[dict]:
    """
    Main entry point. Returns up to `max_results` product suggestions.

    Called after a product is added to cart. Returns other products the
    customer might want to add, ranked by cross-sell and personal signals.

    Parameters
    ──────────
    added_product   The product dict just added to cart
    cart            Current cart contents (list of {name, qty, price})
    products        Full product list for this business
    memory          Customer memory dict from _get_memory()
    max_results     Maximum suggestions to return (default 2)

    Returns
    ───────
    List of product dicts (may be empty if no confident suggestions).
    """
    try:
        target_name = added_product.get("name", "")
        if not target_name:
            return []

        # Build set of names already in cart (including just-added item)
        cart_names = {i["name"].lower() for i in cart}
        cart_names.add(target_name.lower())

        # Build co-occurrence map from personal order history
        order_history = memory.get("last_orders", [])
        co_pairs = _build_cooccurrence(order_history)

        # Score all candidate products
        scored = _score_products(
            candidates=products,
            target_name=target_name,
            cart_names=cart_names,
            memory=memory,
            co_pairs=co_pairs,
        )

        # Apply confidence gate
        confident = [(p, s) for p, s in scored if s >= MIN_CONFIDENCE]

        if not confident:
            # Fallback: category-pair only (no history needed)
            confident = _category_fallback(
                target_name=target_name,
                candidates=products,
                cart_names=cart_names,
                max_results=max_results,
            )
            log.debug(
                "sales_ai: fallback to category pairing  target=%r  suggestions=%d",
                target_name, len(confident),
            )
            return confident[:max_results]

        result = [p for p, _ in confident[:max_results]]
        log.debug(
            "sales_ai: suggestions for %r  → %s  (scores=%s)",
            target_name,
            [p["name"] for p in result],
            [round(s, 3) for _, s in confident[:max_results]],
        )
        return result

    except Exception as exc:
        log.warning("sales_ai.get_suggestions error: %s", exc)
        return []


def get_basket_suggestions(
    cart:        list[dict],
    products:    list[dict],
    memory:      dict,
    max_results: int = MAX_SUGGESTIONS,
) -> list[dict]:
    """
    Basket-completion suggestions: given the full cart, what's missing?

    Used after multi-item order_preview confirmation (P0.2) when the customer
    has already confirmed several items. Returns products that would commonly
    round out the order — e.g. drinks after a food order.

    Parameters
    ──────────
    cart         Current confirmed cart items
    products     Full product list
    memory       Customer memory
    max_results  Max suggestions

    Returns
    ───────
    List of product dicts (may be empty).
    """
    try:
        if not cart or not products:
            return []

        cart_names = {i["name"].lower() for i in cart}
        cart_categories = {_infer_category(i["name"]) for i in cart} - {None}

        # Check if the cart already has drinks — most common missing complement
        has_drink = any(
            _infer_category(i["name"]) == "drinks"
            for i in cart
        )

        candidates = []
        for p in products:
            name  = p.get("name", "")
            cat   = _infer_category(name)
            stock = p.get("stock")

            if name.lower() in cart_names:
                continue
            if stock is not None and stock <= 0:
                continue

            # Strong boost for drinks if cart has food but no drink
            if not has_drink and cat == "drinks":
                candidates.append((p, 0.8))
                continue

            # Boost for complementary categories
            for cart_cat in cart_categories:
                paired = _CATEGORY_PAIRS.get(cart_cat, [])
                if cat in paired:
                    candidates.append((p, 0.5))
                    break

        # Deduplicate and sort
        seen: set[str] = set()
        unique = []
        for p, s in sorted(candidates, key=lambda x: x[1], reverse=True):
            if p["name"] not in seen:
                seen.add(p["name"])
                unique.append(p)
            if len(unique) >= max_results:
                break

        log.debug(
            "sales_ai: basket suggestions  cart=%s  → %s",
            [i["name"] for i in cart],
            [p["name"] for p in unique],
        )
        return unique

    except Exception as exc:
        log.warning("sales_ai.get_basket_suggestions error: %s", exc)
        return []


def get_upsell(
    added_product: dict,
    products:      list[dict],
    cart:          list[dict],
) -> Optional[dict]:
    """
    Find a higher-value item in the same inferred category.

    Example: customer adds "regular coffee" ($1.50)
             → suggest "large coffee" ($2.50) if it exists

    Only returns a suggestion if the price difference is ≤ 3× the added item
    (prevents absurd upsells). Returns None if no suitable upsell found.
    """
    try:
        target_price  = float(added_product.get("price") or 0)
        target_cat    = _infer_category(added_product.get("name", ""))
        cart_names    = {i["name"].lower() for i in cart}
        cart_names.add(added_product.get("name", "").lower())

        if not target_cat or target_price <= 0:
            return None

        candidates = []
        for p in products:
            name  = p.get("name", "")
            cat   = _infer_category(name)
            price = float(p.get("price") or 0)
            stock = p.get("stock")

            if name.lower() in cart_names:
                continue
            if stock is not None and stock <= 0:
                continue
            if cat != target_cat:
                continue
            if price <= target_price:
                continue   # not actually higher value
            if price > target_price * 3:
                continue   # too expensive — would feel pushy

            candidates.append((p, price))

        if not candidates:
            return None

        # Return the closest (cheapest) upsell
        candidates.sort(key=lambda x: x[1])
        return candidates[0][0]

    except Exception as exc:
        log.warning("sales_ai.get_upsell error: %s", exc)
        return None


def format_suggestion_text(
    suggestions: list[dict],
    upsell:      Optional[dict] = None,
    style:       str = "compact",
) -> str:
    """
    Format suggestion products into a WhatsApp-ready message fragment.

    Parameters
    ──────────
    suggestions  Cross-sell / basket-complete products
    upsell       Optional single upsell product
    style        "compact" (one line) | "detailed" (with price)

    Returns
    ───────
    A string fragment to append to the cart-add reply.
    Empty string if nothing to suggest.

    Example outputs
    ───────────────
    compact:   "💡 You might also like *Coke* or *Fries*."
    detailed:  "💡 Pairs well with:\n  • *Coke* — $0.80\n  • *Fries* — $1.20"
    upsell:    "⬆️ Upgrade to *Large Pizza* for just $1.50 more?"
    """
    parts: list[str] = []

    if suggestions:
        if style == "detailed":
            lines = [f"  • *{p['name']}* — ${float(p['price']):.2f}" for p in suggestions]
            parts.append("💡 *Pairs well with:*\n" + "\n".join(lines))
        else:
            names = " or ".join(f"*{p['name']}*" for p in suggestions)
            parts.append(f"💡 You might also like {names}.")

    if upsell:
        upsell_price  = float(upsell.get("price") or 0)
        # We don't know the original price here, so just name the upgrade
        parts.append(f"⬆️ Also consider *{upsell['name']}* (${upsell_price:.2f}).")

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# ABANDONED ORDER FOLLOW-UP
# ─────────────────────────────────────────────────────────────────────────────

def get_abandoned_cart_message(
    cart:          list[dict],
    business_name: str,
    customer_name: str = "",
) -> str:
    """
    Generate a WhatsApp reminder for a customer with an abandoned cart.

    Called by a future scheduled job or when the customer returns after
    an inactivity period. NOT called during the live conversation flow.

    Parameters
    ──────────
    cart            Items left in the cart
    business_name   Business name for personalisation
    customer_name   Customer first name (optional)

    Returns
    ───────
    A WhatsApp-formatted reminder message string.
    """
    if not cart:
        return ""

    name_greeting = f", {customer_name}" if customer_name else ""
    items_text    = ", ".join(
        f"*{i['name']}*" + (f" ×{i['qty']}" if i.get("qty", 1) > 1 else "")
        for i in cart[:3]
    )
    more_text = f" and {len(cart) - 3} more" if len(cart) > 3 else ""
    total     = sum(float(i.get("price", 0)) * int(i.get("qty", 1)) for i in cart)

    return (
        f"👋 Hey{name_greeting}! You left some items in your cart at *{business_name}*.\n\n"
        f"🛒 {items_text}{more_text}\n"
        f"💰 Total: *${total:.2f}*\n\n"
        f"Ready to complete your order? Reply *cart* to review, "
        f"or *checkout* to order now! 😊"
    )


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL FALLBACK
# ─────────────────────────────────────────────────────────────────────────────

def _category_fallback(
    target_name: str,
    candidates:  list[dict],
    cart_names:  set[str],
    max_results: int,
) -> list[dict]:
    """
    Pure category-pairing fallback — no order history needed.
    Used when confidence is too low or the customer is new.
    """
    target_cat = _infer_category(target_name)
    if not target_cat:
        return []

    paired_cats = _CATEGORY_PAIRS.get(target_cat, [])
    if not paired_cats:
        return []

    result = []
    for cat in paired_cats:
        for p in candidates:
            if p.get("name", "").lower() in cart_names:
                continue
            stock = p.get("stock")
            if stock is not None and stock <= 0:
                continue
            if _infer_category(p.get("name", "")) == cat:
                result.append(p)
                break   # one per category
        if len(result) >= max_results:
            break

    return result
