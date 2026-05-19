"""
fuzzy_matcher.py — Fuzzy product matching for WaziBot.

Uses rapidfuzz when available (preferred — faster, better algorithms).
Falls back to the stdlib difflib.get_close_matches when rapidfuzz is not
installed, so the system never crashes due to a missing dependency.

Key function:
    extract_product_and_quantity(text, products)
        → (product_dict, qty) or (None, 1)

Handles:
  - Spelling mistakes      "pizzaa"  → Pizza
  - Case-insensitive       "BEEF"    → beef
  - Quantity extraction    "2 beef"  → (beef, 2)
  - Pluralisation          "2 beefs" → beef
  - Multi-word products    "fried calamari" → fried calamari
  - Embedded quantities    "add 3 ice cream please" → (ice cream, 3)

Confidence threshold is intentionally conservative (60%) to avoid false
positives matching unrelated products ("help" → "beef" is a bad match).
"""

from __future__ import annotations

import re
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Dependency management: rapidfuzz preferred, difflib fallback ──────────────

try:
    from rapidfuzz import fuzz, process as rf_process
    _HAS_RAPIDFUZZ = True
    log.debug("fuzzy_matcher: using rapidfuzz")
except ImportError:
    _HAS_RAPIDFUZZ = False
    from difflib import get_close_matches as _gcm
    log.debug("fuzzy_matcher: rapidfuzz not installed, using difflib fallback")


# ── Constants ─────────────────────────────────────────────────────────────────

# Confidence threshold (0–100 for rapidfuzz, 0.0–1.0 for difflib).
# Below this value the match is rejected to avoid false positives.
_RAPIDFUZZ_MIN_SCORE = 62   # out of 100
_DIFFLIB_MIN_CUTOFF  = 0.55  # out of 1.0

# Words to strip before matching so they don't pollute the product name search
_STRIP_PREFIXES = re.compile(
    r"^(?:i\s+want|i\'?d\s+like|give\s+me|add|order|get\s+me|"
    r"can\s+i\s+(?:have|get)|please|bring\s+me|ill\s+have|"
    r"i\'ll\s+have|let\s+me\s+get)\s+",
    re.IGNORECASE,
)

# Quantity number words
_NUMBER_WORDS: dict[str, int] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
    "couple": 2, "double": 2, "triple": 3, "few": 3,
    "half": 1, "dozen": 12,
}

# Simple plural normalisation — strip trailing 's' only when safe
_PLURAL_STRIP_RE = re.compile(r"s$", re.IGNORECASE)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _strip_quantity_prefix(text: str) -> tuple[int, str]:
    """
    Extract a leading numeric quantity from text.
    Returns (qty, remainder_text).
    "2 beef"   → (2, "beef")
    "a pizza"  → (1, "pizza")
    "beef"     → (1, "beef")
    """
    t = text.strip()

    # "x2 item" or "2x item"
    m = re.match(r"^x\s*(\d+)\s+(.*)", t, re.IGNORECASE)
    if m:
        return max(1, int(m.group(1))), m.group(2).strip()

    m = re.match(r"^(\d+)\s*x\s+(.*)", t, re.IGNORECASE)
    if m:
        return max(1, int(m.group(1))), m.group(2).strip()

    # Digit at start: "2 beef", "3 fried calamari"
    m = re.match(r"^(\d+)\s+(.*)", t)
    if m:
        qty = max(1, int(m.group(1)))
        return qty, m.group(2).strip()

    # Word number at start
    for word, val in _NUMBER_WORDS.items():
        pattern = rf"^{re.escape(word)}\s+(.*)"
        m = re.match(pattern, t, re.IGNORECASE)
        if m:
            return val, m.group(1).strip()

    return 1, t


def _strip_intent_prefix(text: str) -> str:
    """Remove order-intent phrases like 'I want', 'give me', 'add', etc."""
    return _STRIP_PREFIXES.sub("", text.strip()).strip()


def _normalise(text: str) -> str:
    """Lowercase, strip extra whitespace."""
    return " ".join(text.lower().split())


def _simple_plural(name: str) -> str:
    """Rudimentary de-pluralisation: strips trailing 's'."""
    return _PLURAL_STRIP_RE.sub("", name).strip()


# ── Core matching functions ───────────────────────────────────────────────────

def _match_rapidfuzz(query: str, name_map: dict[str, dict]) -> Optional[dict]:
    """Use rapidfuzz.process to find the best matching product."""
    if not name_map:
        return None

    result = rf_process.extractOne(
        query,
        list(name_map.keys()),
        scorer=fuzz.WRatio,          # handles word reordering + partial matches
        score_cutoff=_RAPIDFUZZ_MIN_SCORE,
    )
    if result:
        matched_name, score, _ = result
        log.debug("rapidfuzz  query=%r  match=%r  score=%s", query, matched_name, score)
        return name_map[matched_name]
    return None


def _match_difflib(query: str, name_map: dict[str, dict]) -> Optional[dict]:
    """Fallback: use stdlib difflib.get_close_matches."""
    matches = _gcm(query, list(name_map.keys()), n=1, cutoff=_DIFFLIB_MIN_CUTOFF)
    if matches:
        log.debug("difflib  query=%r  match=%r", query, matches[0])
        return name_map[matches[0]]
    return None


def _run_match(query: str, name_map: dict[str, dict]) -> Optional[dict]:
    """Run the appropriate matcher based on installed dependencies."""
    if _HAS_RAPIDFUZZ:
        return _match_rapidfuzz(query, name_map)
    return _match_difflib(query, name_map)


def _build_name_map(products: list) -> dict[str, dict]:
    """Build a lowercase-normalised name → product dict."""
    return {_normalise(p["name"]): p for p in products if p.get("name")}


# ── Public API ────────────────────────────────────────────────────────────────

def find_product(text: str, products: list) -> Optional[dict]:
    """
    Find the best matching product for a customer's text.
    Does NOT extract quantity — use extract_product_and_quantity() for that.

    Strategy (in order):
      1. Exact lowercase match
      2. After stripping intent prefix ("i want pizza" → "pizza")
      3. After stripping leading quantity ("2 beef" → "beef")
      4. Fuzzy match on the cleaned query
      5. Fuzzy match on de-pluralised query
      6. Word-by-word fuzzy scan (catches "fried calamari" embedded in longer text)

    Returns the product dict or None if no confident match.
    """
    if not products or not text.strip():
        return None

    name_map = _build_name_map(products)
    t = _normalise(text)

    # 1. Exact
    if t in name_map:
        return name_map[t]

    # 2. Strip intent prefix
    cleaned = _normalise(_strip_intent_prefix(t))
    if cleaned and cleaned in name_map:
        return name_map[cleaned]

    # 3. Strip leading quantity
    _, no_qty = _strip_quantity_prefix(cleaned or t)
    no_qty = _normalise(no_qty)
    if no_qty and no_qty in name_map:
        return name_map[no_qty]

    # 4. Fuzzy on cleaned query
    for candidate in dict.fromkeys([cleaned, no_qty]):
        if not candidate:
            continue
        result = _run_match(candidate, name_map)
        if result:
            return result

    # 5. De-pluralised fuzzy
    dep = _normalise(_simple_plural(no_qty))
    if dep and dep != no_qty:
        result = _run_match(dep, name_map)
        if result:
            return result

    # 6. Word-by-word scan — good for multi-word products
    words = (no_qty or t).split()
    # Try every consecutive N-gram (longest first)
    for n in range(len(words), 0, -1):
        for i in range(len(words) - n + 1):
            chunk = " ".join(words[i:i + n])
            if chunk in name_map:
                return name_map[chunk]
            result = _run_match(chunk, name_map)
            if result:
                return result

    return None


def extract_quantity(text: str) -> int:
    """
    Extract a numeric quantity from free-form text.
    "2 beef"          → 2
    "three pizzas"    → 3
    "a sadza"         → 1
    "pizza"           → 1
    """
    t = _normalise(_strip_intent_prefix(text))
    qty, _ = _strip_quantity_prefix(t)
    return max(1, qty)


def extract_product_and_quantity(
    text: str,
    products: list,
) -> tuple[Optional[dict], int]:
    """
    The main entry point for product matching in the ordering flow.

    Returns:
        (product_dict, quantity)  — product_dict is None if no match found

    Example:
        extract_product_and_quantity("2 fried calamari", products)
        → ({"name": "fried calamari", "price": 1.75, ...}, 2)
    """
    qty      = extract_quantity(text)
    product  = find_product(text, products)
    return product, qty


def normalize_payment_choice(text: str) -> Optional[str]:
    """
    Normalize a customer's payment method selection to a canonical value.

    Returns: "paypal" | "ecocash" | "cash" | None

    Handles:
      "1", "2", "3"                → ecocash / paypal / cash
      "paypal", "PayPal payment"   → paypal
      "ecocash", "eco cash"        → ecocash
      "cash", "cash on delivery"   → cash
      "pickup"                     → cash
    """
    t = text.lower().strip()

    # Numeric shortcuts
    if t in ("1", "1️⃣"):
        return "ecocash"
    if t in ("2", "2️⃣"):
        return "paypal"
    if t in ("3", "3️⃣"):
        return "cash"

    # EcoCash variations
    if any(w in t for w in ["ecocash", "eco cash", "eco-cash", "econet", "151", "mobile money"]):
        return "ecocash"

    # PayPal variations
    if any(w in t for w in ["paypal", "pay pal", "pp", "payp"]):
        return "paypal"

    # Cash / delivery / pickup variations
    if any(w in t for w in ["cash", "cod", "delivery", "pickup", "pick up",
                             "collect", "on delivery", "in person"]):
        return "cash"

    return None
