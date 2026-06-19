"""
services/_ai_intent.py — Intent detection helpers for the AI conversation engine.

All functions are pure text classifiers — no DB access, no state access.
Imported by ai.py. Do not import ai.py from here (circular import).
"""

import re
import logging

log = logging.getLogger(__name__)


# ── Global cancel ─────────────────────────────────────────────────────────────

_CANCEL_EXACT = {
    "cancel", "back", "stop", "quit", "nevermind",
    "never mind", "go back", "no thanks", "nope",
    "cancel order", "cancel my order", "i changed my mind",
    "changed my mind", "don't want", "dont want",
    # Fix: customers naturally say these when stuck — must trigger the same
    # cancel/reset path, not be ignored as unrecognized text.
    "end conversation", "end chat", "exit", "leave",
    "i'm done", "im done", "forget it",
}


def _is_cancel(text: str) -> bool:
    t = text.lower().strip()
    return t in _CANCEL_EXACT or t.startswith("cancel")


# ── Refund / dispute ──────────────────────────────────────────────────────────

_REFUND_WORDS = {
    "refund", "refund please", "i want a refund", "give me a refund",
    "money back", "get my money back", "want my money back",
    "dispute", "chargeback", "wrong order", "not received",
    "didn't receive", "didnt receive", "never got", "where is my order",
    "where is my food", "where is my delivery",
}


def _is_refund_request(text: str) -> bool:
    t = text.lower().strip()
    return t in _REFUND_WORDS or any(w in t for w in [
        "refund", "money back", "dispute", "chargeback",
        "not received", "never got", "where is my order",
    ])


# ── Reorder ───────────────────────────────────────────────────────────────────

_REORDER_PHRASES = {
    "repeat last order", "same order", "order again", "same as last time",
    "same as before", "repeat order", "reorder", "last order again",
    "previous order", "order same thing",
}


def _is_reorder_request(text: str) -> bool:
    t = text.lower().strip()
    return t in _REORDER_PHRASES or any(p in t for p in _REORDER_PHRASES)


# ── Fulfillment / delivery ────────────────────────────────────────────────────

_DELIVERY_TRIGGERS = {
    "delivery", "deliver", "deliver it", "deliver to me",
    "1", "1️⃣", "home delivery", "door delivery",
}
_PICKUP_TRIGGERS = {
    "pickup", "pick up", "collect", "i'll collect", "self pickup",
    "walk in", "come in", "i will pick", "i will come",
    "2", "2️⃣",
}


def _detect_fulfillment(text: str) -> str | None:
    """
    Returns 'delivery' | 'pickup' | None.

    Delivery: "1", "delivery", "deliver", "ship", "bring it", "home delivery" etc.
    Pickup:   "2", "pickup", "pick up", "collect", "walk in" etc.
    """
    t = text.lower().strip()

    if t in _DELIVERY_TRIGGERS:
        return "delivery"
    for kw in ["deliver", "delivery", "ship", "bring it", "bring me", "bring to"]:
        if kw in t:
            return "delivery"

    if t in _PICKUP_TRIGGERS:
        return "pickup"
    for kw in ["pickup", "pick up", "collect", "i'll pick"]:
        if kw in t:
            return "pickup"

    return None


# ── Status queries ────────────────────────────────────────────────────────────

_STATUS_QUERY_PHRASES = [
    "where is my order", "where's my order", "order status", "status update",
    "eta", "how long", "when will", "when is", "is it ready",
    "any update", "what's happening", "what happened",
    "still preparing", "still waiting",
]


def _is_status_query(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _STATUS_QUERY_PHRASES)


# ── Customer name extraction ──────────────────────────────────────────────────

_NAME_PATTERNS = [
    re.compile(r"(?:my name is|i'm|i am|call me|it's|its)\s+([A-Za-z]{2,20})", re.I),
    re.compile(r"(?:this is)\s+([A-Za-z]{2,20})\s*[,.]", re.I),
]

# Introduction trigger phrases — if ANY of these appear in the message we treat
# the whole message as a self-introduction, NOT an order attempt.
_INTRO_PHRASES = (
    "my name is", "i'm ", "i am ", "call me ", "it's ", "its ",
    "this is ", "they call me", "people call me", "known as",
)


def _is_introduction(text: str) -> bool:
    """
    Returns True if the message is a self-introduction.

    "My name is Tafadzwa" → True
    "I'm John"            → True
    "Call me Tafadzwa"    → True
    "Sadza"               → False
    "I want sadza"        → False  (starts with intent prefix)

    This check must run BEFORE product matching to prevent names like
    "Tafadzwa" from being fuzzy-matched to products like "Sadza".
    """
    t = text.lower().strip()
    return (
        bool(_extract_name(text)) and
        any(t.startswith(p) or f" {p}" in t for p in _INTRO_PHRASES)
    )


def _extract_name(text: str) -> str | None:
    """Extract a first name from an introduction phrase. Returns None if not found."""
    for pat in _NAME_PATTERNS:
        m = pat.search(text)
        if m:
            name = m.group(1).strip().title()
            if name.lower() not in {"ok", "hi", "hey", "yes", "no", "not", "done",
                                     "fine", "good", "just", "here", "there", "all"}:
                return name
    return None


# ── Conversation done / farewell ──────────────────────────────────────────────

_DONE_EXACT = {
    "thank you", "thanks", "ty", "thx", "thank u",
    "that's all", "thats all", "nothing else", "i'm done", "im done",
    "no thanks", "no thank you", "nah thanks", "all good",
    "okay thanks", "ok thanks", "okay thank you", "ok thank you",
    "thanks bye", "thank you bye", "bye", "goodbye", "good bye",
    "cheers", "cool thanks", "perfect thanks", "great thanks",
    "awesome thanks", "sorted thanks", "sorted",
    "we're done", "we are done", "that will be all",
    "end", "end conversation", "end chat", "stop", "done", "finish",
    "finished", "close", "exit", "quit", "that's it", "thats it",
    "that's enough", "thats enough", "all done", "i'm good", "im good",
    "we're done here", "nothing more", "no more", "that will do",
}


def _is_conversation_done(text: str) -> bool:
    """Detect farewell / completion phrases so we can close gracefully."""
    t = text.lower().strip()
    if t in _DONE_EXACT:
        return True
    if t.startswith("thank") and len(t) < 30:
        return True
    return False


# ── Survey ────────────────────────────────────────────────────────────────────

_SURVEY_OPTIONS = {
    "1": "excellent", "2": "good", "3": "average", "4": "poor",
    "excellent": "excellent", "good": "good",
    "average": "average", "poor": "poor",
    "👍": "excellent", "😊": "good", "😐": "average", "😞": "poor",
}


def _is_survey_response(text: str) -> bool:
    return text.lower().strip() in _SURVEY_OPTIONS


def _parse_survey_rating(text: str) -> str:
    return _SURVEY_OPTIONS.get(text.lower().strip(), "")


# ── Urgency / delivery follow-up ──────────────────────────────────────────────

_URGENCY_PHRASES = [
    "hurry", "urgent", "asap", "quickly", "fast", "how long", "when will",
    "where is", "still waiting", "taking long", "taking too long",
    "late", "delayed", "not arrived", "hasn't arrived", "not here yet",
    "cold", "hungry", "starving",
    "delivery update", "any delivery update", "any update",
    "update on my order", "order update", "status of my order",
    "status of my delivery", "delivery status", "order status",
    "when will it", "when will my", "has it been",
    "eta", "estimated time", "how soon", "any news",
]


def _is_urgency_message(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _URGENCY_PHRASES)


# ── Agent message detection ───────────────────────────────────────────────────

_AGENT_PHRASES = [
    "your payment has been verified", "payment verified", "payment confirmed",
    "order is being prepared", "order is ready", "ready for pickup",
    "rider has been assigned", "out for delivery", "on the way",
    "delivered", "order complete", "thank you for your order",
    "your order is ready", "being prepared", "preparation",
]


def _is_agent_message(text: str) -> bool:
    """
    Returns True if the message looks like it came from a business agent.
    We should not reply with a generic fallback to these.
    """
    t = text.lower()
    return any(p in t for p in _AGENT_PHRASES)


# ── Human handoff request ─────────────────────────────────────────────────────

_HUMAN_REQUEST_PATTERNS = [
    re.compile(r"talk.*to.*(?:a\s+)?human", re.I),
    re.compile(r"speak.*to.*(?:a\s+)?(?:human|person|agent|someone)", re.I),
    re.compile(r"(?:want|would like|need).*(?:human|real person|agent|support)", re.I),
    re.compile(r"(?:connect|transfer|escalate).*(?:human|agent|person)", re.I),
    re.compile(r"(?:call|contact|reach).*(?:someone|team|you)", re.I),
    re.compile(r"talk.*to.*(?:manager|supervisor|staff)", re.I),
    re.compile(r"i would like.*(?:help|assistance|support)", re.I),
]


def _is_human_request(text: str) -> bool:
    """
    Broader human-request detection using regex patterns.
    Catches: "i would like to talk to a human", "can i speak to a real person" etc.
    """
    return any(p.search(text) for p in _HUMAN_REQUEST_PATTERNS)


# ── Payment confirmation ──────────────────────────────────────────────────────

_PAID_EXACT = {
    "paid", "sent", "done", "transferred",
    "i paid", "ive paid", "i've paid",
    "i sent", "money sent", "payment sent",
    "already paid", "i have paid", "i transferred",
}


def _is_payment_confirmation(text: str) -> bool:
    t = text.lower().strip()
    return t in _PAID_EXACT or "sent money" in t or "i already paid" in t


# ── Order reference ───────────────────────────────────────────────────────────

_ORDER_REF_RE = re.compile(r"\border[-\s#]*(\d+)\b", re.IGNORECASE)


def _extract_order_id(text: str) -> int | None:
    """Extract order number from 'ORDER-9', 'order 9', '#9', 'order #9' etc."""
    m = _ORDER_REF_RE.search(text)
    if m:
        return int(m.group(1))
    m2 = re.match(r"^#(\d+)$", text.strip())
    if m2:
        return int(m2.group(1))
    return None


# ── Yes / No ──────────────────────────────────────────────────────────────────

_YES_WORDS = {"yes", "y", "yep", "yeah", "yup", "confirm", "ok", "okay", "sure", "go ahead", "proceed"}
_NO_WORDS  = {"no", "n", "nope", "nah", "not yet", "wait", "hold on"}


def _is_yes(text: str) -> bool:
    return text.lower().strip() in _YES_WORDS


def _is_no(text: str) -> bool:
    return text.lower().strip() in _NO_WORDS


# ── Payment method detection ──────────────────────────────────────────────────

_ECOCASH_TRIGGERS = {
    "1", "1️⃣", "ecocash", "eco cash", "eco-cash",
    "cash transfer", "mobile money", "econet",
}
_PAYPAL_TRIGGERS = {
    "2", "2️⃣", "paypal", "pay pal", "payp", "pp",
    "pay with paypal",
}
_CASH_TRIGGERS = {
    "3", "3️⃣", "cash", "cod", "cash on delivery",
    "on delivery", "pickup", "pick up", "collect",
    "pay on delivery", "deliver",
}


def _detect_payment_method(text: str) -> str | None:
    """
    Detect payment method from customer text.
    Returns: 'ecocash' | 'paypal' | 'cash' | 'cancel' | None

    Primary: uses fuzzy_matcher.normalize_payment_choice() for broad coverage.
    Fallback: original set/substring matching.
    """
    from services._ai_lazy import _fuzzy

    t = text.lower().strip()

    if t in _CANCEL_EXACT or t.startswith("cancel"):
        return "cancel"

    try:
        result = _fuzzy().normalize_payment_choice(text)
        if result:
            return result
    except Exception as exc:
        log.warning("_detect_payment_method: fuzzy_matcher failed (%s) — using fallback", exc)

    if t in _ECOCASH_TRIGGERS:
        return "ecocash"
    if t in _PAYPAL_TRIGGERS:
        return "paypal"
    if t in _CASH_TRIGGERS:
        return "cash"
    if "ecocash" in t or "eco cash" in t or "cash transfer" in t:
        return "ecocash"
    if "paypal" in t or "pay pal" in t:
        return "paypal"
    if any(w in t for w in ["on delivery", "pickup", "pick up", "collect", "cash on"]):
        return "cash"
    if t == "cash":
        return "cash"
    return None


# ── General intent ────────────────────────────────────────────────────────────

def _intent(text: str) -> str:
    t = text.lower().strip()

    if any(w in t for w in [
        "checkout", "confirm order", "place order", "complete order",
        "order now", "submit order", "i'm done", "im done", "finish order",
    ]) or t in ("pay", "checkout", "place my order", "submit"):
        return "checkout"

    if t.startswith("remove ") or t.startswith("delete ") or "remove " in t:
        return "remove"

    if any(w in t for w in [
        "my cart", "view cart", "show cart", "whats in my cart",
        "what's in my cart", "whats in cart", "my order so far",
        "what i have", "show my order",
    ]) or t in ("cart", "my cart", "basket"):
        return "cart"

    if any(w in t for w in [
        "menu", "list", "browse", "show me", "catalog",
        "what do you have", "what do you sell", "products",
        "whats available", "what's available", "show products",
        "what can i order", "what's on the menu",
    ]) or t in ("menu", "list", "catalog"):
        return "browse"

    if (any(w in t for w in ["help", "hi ", "hello", "hey ", "hie", "howzit"])
            or t in ("hi", "hello", "hey", "hie", "yo", "sup", "howzit", "start", "help",
                     "commands", "options", "what can you do", "what can i do",
                     "what can i type", "what do i type", "list commands", "list")):
        return "help"

    return "order"


# ── Proof of payment ──────────────────────────────────────────────────────────

_PROOF_SKIP_WORDS = {
    "ORDER", "PAYPAL", "ECOCASH", "BITCOIN", "CRYPTO", "PROOF", "IMAGE",
    "REFUND", "CANCEL", "THANKS", "SORTED", "CHEERS", "DONEIT",
    "PLEASE", "CHANGE", "RETURN", "UNABLE", "FAILED", "IGNORE",
    "HELPME", "SOMETH", "NEWONE", "REUNDO", "REVERT",
    "CANCEL", "REFUND", "PAID", "DONE", "SENT",
}

_TXN_PATTERN = re.compile(r"\b([A-Z0-9]{8,30})\b")


def _looks_like_txn_id(token: str) -> bool:
    """
    Returns True if token looks like a real transaction ID:
      - 8–30 chars of A-Z and 0-9
      - Contains at least one digit (pure words are not txn IDs)
      - Not in the skip list of common words
    """
    t = token.upper().strip()
    if t in _PROOF_SKIP_WORDS:
        return False
    if not re.fullmatch(r"[A-Z0-9]{8,30}", t):
        return False
    if not any(c.isdigit() for c in t):
        return False
    return True


def _is_proof_submission(text: str, message_has_image: bool = False) -> tuple[bool, str]:
    """
    Detect if the customer is submitting payment proof.
    Returns (is_proof: bool, proof_text: str).

    Accepts: WhatsApp images, transaction IDs, descriptive proof phrases.
    Rejects: pure English words, short tokens.
    """
    if message_has_image:
        return True, "image_attached"

    t       = text.strip()
    t_lower = t.lower()

    proof_phrases = [
        "transaction", "reference", "txn", "receipt", "confirmation",
        "screenshot", "transfer id", "payment id", "proof",
        "here is", "here's", "attached",
    ]
    has_proof_phrase = any(p in t_lower for p in proof_phrases)

    found_txn = None
    for match in _TXN_PATTERN.finditer(t.upper()):
        candidate = match.group(1)
        if _looks_like_txn_id(candidate):
            found_txn = candidate
            break

    if found_txn:
        return True, found_txn

    if has_proof_phrase and len(t) > 8:
        return True, t[:120]

    return False, ""


# ── Introduction detection ────────────────────────────────────────────────────
# Must be checked BEFORE _intent() / fuzzy product matching.
# "My name is Tafadzwa" must never reach the product matcher.

_INTRO_PATTERNS = [
    re.compile(r"^(?:my name is|i'?m|i am|call me)\s+[A-Za-z]{2,20}", re.I),
    re.compile(r"^(?:this is)\s+[A-Za-z]{2,20}\s*[,.]?$", re.I),
    re.compile(r"^[A-Za-z]{2,20}\s+(?:here|speaking|this side)$", re.I),
]


def _is_introduction(text: str) -> bool:
    """
    Returns True when the message is clearly a name introduction:
      "My name is Tafadzwa", "I'm John", "call me Rudo", "Tafadzwa here"

    Prevents these from reaching the fuzzy product matcher where names
    like "Tafadzwa" fuzzy-match products like "Sadza" (ratio 0.615 > threshold).
    """
    return any(p.match(text.strip()) for p in _INTRO_PATTERNS)


# ── Booking intent detection ──────────────────────────────────────────────────
# Used only when is_service_business=True — retail businesses unaffected.

import re as _booking_re

_BOOKING_INTENT_RE = _booking_re.compile(
    r"\b(book|appointment|appoint|schedule|reserve|slot|session|visit|"
    r"come in|come over|see you|meeting|consultation|available)\b",
    re.IGNORECASE,
)

_CANCEL_BOOKING_RE = _booking_re.compile(
    r"\b(cancel|cancell?ation)\s+(booking|appointment|slot|session)\b",
    re.IGNORECASE,
)

_RESCHEDULE_RE = _booking_re.compile(
    r"\b(reschedule|move|change|postpone)\s+(booking|appointment|slot)\b",
    re.IGNORECASE,
)

_MY_BOOKINGS_RE = _booking_re.compile(
    r"\b(my\s+booking|my\s+appointment|show\s+booking|view\s+appointment|"
    r"booking\s+status|appointment\s+status)\b",
    re.IGNORECASE,
)


def _is_booking_intent(text: str) -> bool:
    """Returns True if the message looks like a booking/appointment request."""
    return bool(_BOOKING_INTENT_RE.search(text))


def _is_cancel_booking(text: str) -> bool:
    return bool(_CANCEL_BOOKING_RE.search(text.lower()))


def _is_reschedule_booking(text: str) -> bool:
    return bool(_RESCHEDULE_RE.search(text.lower()))


def _is_my_bookings_query(text: str) -> bool:
    return bool(_MY_BOOKINGS_RE.search(text.lower()))


# ── Visual catalog / product image intents ───────────────────────────────────

_CATALOG_TRIGGERS = re.compile(
    r"\b(catalog|catalogue|gallery|visual menu|browse products|show products|"
    r"product list|all products|see products|view products|product catalog)\b",
    re.IGNORECASE,
)

_MORE_TRIGGER = re.compile(r"^(more|next|more products|next products|continue)$", re.IGNORECASE)

_SHOW_CATEGORY_RE = re.compile(
    r"^show\s+(me\s+)?(all\s+)?(?P<cat>[a-zA-Z][a-zA-Z\s]{1,25})$",
    re.IGNORECASE,
)


def _is_catalog_request(text: str) -> bool:
    """Returns True if customer is asking for a visual catalog/gallery."""
    return bool(_CATALOG_TRIGGERS.search(text))


def _is_show_image_request(text: str) -> bool:
    """
    Returns True if customer wants to see an image of a specific product.
    e.g. "show me flowers", "picture of roses", "what do cakes look like"
    """
    t = text.strip().lower()
    # "show X" where X is not a known non-product command
    if re.match(r"^show\s+\S", text.strip(), re.I):
        skip = {"show businesses", "show business", "show directory",
                "show products", "show all products", "show catalog"}
        if t not in skip and not _is_catalog_request(text):
            return True
    # "picture/photo/image of X"
    if re.match(r"^(picture|photo|image|pic)\s+(of\s+)?\S", text.strip(), re.I):
        return True
    # "what does X look like"
    if re.match(r"^what\s+does\s+.+\s+look\s+like", text.strip(), re.I):
        return True
    return False


def _extract_show_target(text: str) -> str:
    """Extract product name from 'show me X', 'picture of X' etc."""
    t = text.strip()
    m = re.match(r"^show\s+(?:me\s+)?(?:a\s+)?(?:photo|picture|image\s+of\s+)?(.+)$", t, re.I)
    if m: return m.group(1).strip()
    m = re.match(r"^(?:picture|photo|image|pic)\s+(?:of\s+)?(.+)$", t, re.I)
    if m: return m.group(1).strip()
    m = re.match(r"^what\s+does\s+(.+?)\s+look\s+like", t, re.I)
    if m: return m.group(1).strip()
    return t


def _is_more_products_request(text: str) -> bool:
    """Returns True for 'more', 'next' pagination command."""
    return bool(_MORE_TRIGGER.match(text.strip()))


def _extract_show_category(text: str) -> str:
    """Extract category from 'show flowers', 'show electronics'. Returns '' if no match."""
    m = _SHOW_CATEGORY_RE.match(text.strip())
    if m:
        cat = m.group("cat").strip()
        skip = {"me", "all", "products", "catalog", "menu", "cart", "checkout",
                "businesses", "directory"}
        if cat.lower() not in skip:
            return cat
    return ""


# ── Abusive / offensive language detection ────────────────────────────────────
# Detects profanity, insults, threats, or hostile language directed at the
# business or its staff. Used to trigger a calm de-escalation warning rather
# than the generic "I didn't quite get that" fallback, and to track repeat
# offenses for a tiered warning → notice of suspension flow.

_PROFANITY_WORDS = {
    # Common English profanity (word-boundary matched, lowercase)
    "fuck", "fucking", "fucker", "fck", "f*ck", "f**k",
    "shit", "shitty", "bullshit",
    "bitch", "bastard", "asshole", "ass",
    "cunt", "dick", "piss", "pissed",
    "damn", "goddamn",
    "idiot", "stupid", "moron", "dumb", "dumbass",
    "scam", "scammer", "thief", "thieves", "robbing", "rip off", "ripoff",
    "useless", "garbage", "trash", "rubbish",
    "hate you", "shut up", "screw you", "f off", "f u",
}

_THREAT_PHRASES = [
    "i will report you", "i'll report you", "i will sue", "i'll sue",
    "i will expose", "i'll expose", "i will destroy your business",
    "going to ruin your business", "post this online", "go viral",
    "i know where you", "watch yourself", "you'll regret",
]


def _is_abusive_message(text: str) -> bool:
    """
    Returns True if the message contains profanity, insults, or threatening
    language. Conservative: requires a whole-word match for profanity terms
    (so e.g. "classic" doesn't match "ass") and substring match for multi-word
    threat phrases.
    """
    t = text.lower().strip()
    if not t:
        return False

    # Whole-word match for single-word profanity (avoid false positives like
    # "assassin", "classic", "passion")
    words = re.findall(r"[a-z']+", t)
    word_set = set(words)
    for term in _PROFANITY_WORDS:
        if " " in term or "*" in term:
            # Multi-word or symbol terms — substring match
            if term in t:
                return True
        elif term in word_set:
            return True

    # Threat phrases — substring match
    for phrase in _THREAT_PHRASES:
        if phrase in t:
            return True

    return False
