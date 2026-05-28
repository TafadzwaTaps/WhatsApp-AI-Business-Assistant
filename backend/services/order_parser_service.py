"""
services/order_parser_service.py — Smart Order Extraction Layer (Phase 4)

PURPOSE
───────
Transforms free-form customer text into a structured, validated order object.

  Input:  "Boss ndoda 2 drinks ne bread and 3 sadza please"
  Output: ParsedOrder(
              items=[
                  OrderItem(name="drinks", qty=2, unit_price=1.50, subtotal=3.00),
                  OrderItem(name="bread",  qty=1, unit_price=0.75, subtotal=0.75),
                  OrderItem(name="Sadza",  qty=3, unit_price=1.00, subtotal=3.00),
              ],
              total=6.75,
              confidence=0.91,
              blocked=[],
              unrecognised=[],
          )

DESIGN PRINCIPLES
─────────────────
• Wraps the existing fuzzy_matcher — does NOT replace it.
• Always returns a result (never raises) — caller decides what to do.
• Zero side effects — does NOT write to DB or modify cart.
• The AI layer (ai.py) calls this in P7 and uses the result to update the cart.
• Fully backward compatible — existing P7 code still works if this is not called.

INTEGRATION POINT
─────────────────
Called from ai.py P7 (add to cart) when text contains clear order intent.
The result feeds the existing cart-update logic without changing that logic.

MULTILINGUAL SUPPORT
────────────────────
Handles mixed Shona/English/slang ordering language:
  "ndoda"            → "I want"
  "ne / na"          → "and"
  "Boss / baba"      → address word (ignored)
  "please / plz"     → trailing filler (ignored)
  "futi / zvakare"   → "also/again" (treated as continuation)
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OrderItem:
    """One line item in a parsed order."""
    name:       str
    qty:        int
    unit_price: float
    subtotal:   float
    product_id: Optional[int] = None
    in_stock:   bool          = True
    stock_qty:  Optional[int] = None   # available stock; None = unlimited


@dataclass
class ParsedOrder:
    """
    Result of parsing a customer message into a structured order.

    Fields
    ──────
    items         Validated line items with stock checked
    total         Sum of all item subtotals
    confidence    0.0–1.0 — how confident the parser is in the parse
    raw_text      Original input text
    blocked       Items that could not be added (out of stock / over limit)
    unrecognised  Word segments not matched to any product
    intent        Detected intent: "order" | "inquiry" | "unclear"
    """
    items:         list[OrderItem] = field(default_factory=list)
    total:         float           = 0.0
    confidence:    float           = 0.0
    raw_text:      str             = ""
    blocked:       list[str]       = field(default_factory=list)
    unrecognised:  list[str]       = field(default_factory=list)
    intent:        str             = "order"   # "order" | "inquiry" | "unclear"

    @property
    def has_items(self) -> bool:
        return len(self.items) > 0

    @property
    def is_confident(self) -> bool:
        """True when confidence is high enough to proceed without clarification."""
        return self.confidence >= 0.65 and self.has_items

    def cart_lines(self) -> list[dict]:
        """Convert to the cart format used by ai.py and crud.py."""
        return [
            {"name": item.name, "qty": item.qty, "price": item.unit_price}
            for item in self.items
        ]

    def summary(self) -> str:
        """Human-readable one-line summary for logging."""
        parts = [f"{i.qty}×{i.name}" for i in self.items]
        return f"[{', '.join(parts)}] total=${self.total:.2f} conf={self.confidence:.2f}"


# ─────────────────────────────────────────────────────────────────────────────
# LANGUAGE NORMALISATION
# ─────────────────────────────────────────────────────────────────────────────

# Shona/local words → English equivalents (used in pre-processing only)
_SHONA_MAP: dict[str, str] = {
    r"\bndoda\b":        "I want",
    r"\bndipe\b":        "give me",
    r"\bndinoda\b":      "I want",
    r"\bndibatsire\b":   "help me with",
    r"\bne\b":           "and",
    r"\bna\b":           "and",
    r"\bfuti\b":         "also",
    r"\bzvakare\b":      "also",
    r"\bkana\b":         "or",
    r"\bboss\b":         "",
    r"\bbaba\b":         "",
    r"\bsisi\b":         "",
    r"\bmaita\b":        "thank you",
    r"\btinotenda\b":    "thank you",
    r"\bplz\b":          "please",
    r"\bpls\b":          "please",
    r"\bnx\b":           "thanks",
    r"\bkk\b":           "",
    r"\bokk?\b":         "",
}

# Connectors to split on (in addition to "and")
_CONNECTOR_RE = re.compile(
    r"\s+(?:and|&|\+|,|ne|na|futi|zvakare|plus|also|with)\s+",
    re.IGNORECASE,
)

# Filler words that should be stripped before matching
_FILLER_RE = re.compile(
    r"\b(?:please|plz|pls|thanks|thank\s+you|boss|baba|sisi|ok|okay|"
    r"sure|yep|yes|yeah|just|maybe|also|quick|quickly|urgent|asap)\b",
    re.IGNORECASE,
)

# Order intent signals
_ORDER_INTENT_PHRASES = [
    "i want", "i need", "i'd like", "give me", "bring me", "get me",
    "can i have", "can i get", "let me get", "add", "order",
    "ndoda", "ndipe", "ndinoda", "ndibatsire",
    "i'll take", "ill take", "i will have",
]

_INQUIRY_SIGNALS = [
    "do you have", "do you sell", "is there", "what is",
    "how much is", "how much does", "price of", "cost of",
    "marii", "what's the price", "whats the price",
]
# NOTE: "mari" / "marii" alone can appear inside product names (e.g. "calamari")
# so we only match multi-word inquiry phrases to avoid false positives.


def _normalise_language(text: str) -> str:
    """
    Replace Shona/slang words with English equivalents for easier parsing.
    Strips filler words.  Lowercases.
    """
    t = text.lower().strip()
    for pattern, replacement in _SHONA_MAP.items():
        t = re.sub(pattern, replacement, t, flags=re.IGNORECASE)
    t = _FILLER_RE.sub("", t)
    # Collapse multiple spaces
    return " ".join(t.split())


def _detect_intent(text: str) -> str:
    """
    Classify the message intent before attempting to parse products.
    Returns "order" | "inquiry" | "unclear".

    Uses word-level matching for inquiry signals to prevent partial matches
    (e.g. "calamari" containing "mari" should NOT trigger inquiry detection).
    """
    t_lower = text.lower().strip()
    # Split into words for whole-phrase matching
    words = set(t_lower.split())

    # Inquiry: only match complete multi-word phrases
    for phrase in _INQUIRY_SIGNALS:
        phrase_words = phrase.split()
        if len(phrase_words) == 1:
            # single-word signals must be the ENTIRE message or start of it
            if t_lower == phrase or t_lower.startswith(phrase + " "):
                return "inquiry"
        else:
            # multi-word signals: check substring match
            if phrase in t_lower:
                return "inquiry"

    for phrase in _ORDER_INTENT_PHRASES:
        if phrase in t_lower:
            return "order"

    # Bare product names without intent words → still likely an order
    return "order"


# ─────────────────────────────────────────────────────────────────────────────
# SEGMENT SPLITTING
# ─────────────────────────────────────────────────────────────────────────────

def _split_segments(text: str) -> list[str]:
    """
    Split normalised text into per-product segments.

    "2 beef and a sadza and 3 ice cream"
    → ["2 beef", "a sadza", "3 ice cream"]
    """
    parts = _CONNECTOR_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PARSER
# ─────────────────────────────────────────────────────────────────────────────

def parse_order(
    text: str,
    products: list[dict],
    existing_cart: list[dict] | None = None,
) -> ParsedOrder:
    """
    Parse free-form customer text into a structured ParsedOrder.

    Parameters
    ──────────
    text           Raw customer message
    products       List of product dicts from get_products() (must have name, price, stock)
    existing_cart  Current cart items for stock calculations (optional)

    Returns
    ───────
    ParsedOrder — always returned, never raises.
    Check .is_confident before acting on the result.

    Example
    ───────
    >>> result = parse_order("Boss ndoda 2 drinks ne bread", products)
    >>> result.items
    [OrderItem(name='drinks', qty=2, ...), OrderItem(name='bread', qty=1, ...)]
    >>> result.total
    3.75
    >>> result.confidence
    0.88
    """
    result = ParsedOrder(raw_text=text)

    if not text.strip() or not products:
        result.intent = "unclear"
        return result

    # Step 1: detect intent
    result.intent = _detect_intent(text)
    if result.intent == "inquiry":
        # Don't parse products from inquiry messages
        return result

    # Step 2: normalise language (Shona → English, strip fillers)
    normalised = _normalise_language(text)
    log.debug("order_parser: normalised=%r", normalised)

    # Step 3: split into per-product segments
    segments = _split_segments(normalised)
    if not segments:
        segments = [normalised]

    # Step 4: match each segment using fuzzy_matcher
    try:
        from utils.fuzzy_matcher import extract_product_and_quantity
    except ImportError:
        log.error("order_parser: fuzzy_matcher not available")
        result.intent = "unclear"
        return result

    # Build a cart-quantity lookup for stock checks
    cart_qtys: dict[str, int] = {}
    for item in (existing_cart or []):
        cart_qtys[item["name"].lower()] = item.get("qty", 0)

    match_scores: list[float] = []

    for segment in segments:
        if not segment.strip():
            continue

        product, qty = extract_product_and_quantity(segment, products)

        if product is None:
            # Segment couldn't be matched
            # Only report as unrecognised if it looks like an actual product attempt
            if len(segment.split()) <= 4:   # short segments are likely product names
                result.unrecognised.append(segment)
            log.debug("order_parser: unrecognised segment=%r", segment)
            match_scores.append(0.0)
            continue

        qty = max(1, qty)
        name  = product["name"]
        price = float(product.get("price") or 0)
        stock = product.get("stock")

        # Stock check
        in_cart = cart_qtys.get(name.lower(), 0)
        if stock is not None and in_cart + qty > stock:
            if stock == 0:
                result.blocked.append(f"*{name}* (out of stock)")
            else:
                avail = max(0, stock - in_cart)
                if avail > 0:
                    # Partial add — cap at available
                    qty = avail
                    result.blocked.append(
                        f"*{name}* (limited to {avail} — only {stock} in stock)"
                    )
                else:
                    result.blocked.append(f"*{name}* (cart already at max stock)")
                    match_scores.append(0.9)
                    continue

        subtotal = round(price * qty, 2)
        result.items.append(OrderItem(
            name=name,
            qty=qty,
            unit_price=price,
            subtotal=subtotal,
            product_id=product.get("id"),
            in_stock=True,
            stock_qty=stock,
        ))
        # Update cart qty tracker for subsequent segments
        cart_qtys[name.lower()] = in_cart + qty
        match_scores.append(0.9)   # matched = high score

    # Step 5: calculate total
    result.total = round(sum(i.subtotal for i in result.items), 2)

    # Step 6: confidence score
    # = (matched segments) / (total segments) × avg match quality
    if segments:
        matched_count  = len(result.items)
        total_segments = len(segments)
        base_conf      = matched_count / total_segments if total_segments > 0 else 0.0
        avg_score      = (sum(match_scores) / len(match_scores)) if match_scores else 0.0
        result.confidence = round(base_conf * avg_score, 3)
    else:
        result.confidence = 0.0

    log.info(
        "order_parser: %s  segments=%d  matched=%d  blocked=%d  unrecognised=%d",
        result.summary(), len(segments), len(result.items),
        len(result.blocked), len(result.unrecognised),
    )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# INVOICE DRAFT HELPER
# ─────────────────────────────────────────────────────────────────────────────

def build_order_preview(parsed: ParsedOrder, business_name: str = "") -> str:
    """
    Format a ParsedOrder as a WhatsApp-readable order preview.

    Used by ai.py when a confident multi-item order is detected, to show
    the customer what was understood before asking them to confirm.

    Example output:
    ───────────────
    📋 *Here's what I got:*

      • Beef ×2  —  $1.50
      • Sadza ×1  —  $1.00
      • Ice cream ×3  —  $1.50

    💰 *Total: $4.00*

    ⚠️ Beef (only 1 left in stock)
    ❓ Couldn't match: "drinks"

    Is this right? Reply *yes* to add to cart, or type changes.
    """
    if not parsed.has_items:
        return ""

    lines = [f"📋 *Here's what I understood:*\n"]
    for item in parsed.items:
        lines.append(f"  • *{item.name}* ×{item.qty}  —  ${item.subtotal:.2f}")

    lines.append(f"\n💰 *Total: ${parsed.total:.2f}*")

    if parsed.blocked:
        lines.append("\n⚠️ " + "\n⚠️ ".join(parsed.blocked))

    if parsed.unrecognised:
        joined = ", ".join(f'"{u}"' for u in parsed.unrecognised)
        lines.append(f"\n❓ Couldn't match: {joined}")

    lines.append("\nReply *yes* to add to cart, or type what you'd like to change.")

    return "\n".join(lines)
