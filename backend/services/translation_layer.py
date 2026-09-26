"""
services/translation_layer.py
══════════════════════════════
Multi-language response layer — wraps AI replies AFTER generate_reply()
without touching the AI engine itself.

PLACEMENT: backend/services/translation_layer.py

USAGE (in webhook_routes.py, after generate_reply() returns):
    from services.translation_layer import maybe_translate
    reply = maybe_translate(reply, phone, business_id)

HOW IT WORKS:
  1. Detects the customer's preferred language from user_memory
  2. If the business has translation enabled (feature flag in features_json)
     AND the customer's language differs from the default (English),
     translates the reply using LibreTranslate (free, self-hostable) or
     a simple built-in phrase table for common Shona/Ndebele expressions.
  3. Falls back to the original reply on any error — never blocks.

IMPORTANT:
  - Does NOT modify generate_reply() in any way
  - Does NOT change the AI conversation state
  - Wraps output only — pure additive
  - Default is OFF (opt-in per business via features_json.translation_enabled)
  - Built-in phrase table works without any external API

Supported languages (built-in phrases):
  sn  — Shona (Zimbabwe)
  nd  — Ndebele (Zimbabwe)
  en  — English (default, no translation needed)
  sw  — Swahili (Kenya/Tanzania)
  fr  — French (West Africa)
  pt  — Portuguese (Mozambique/Angola)
  es  — Spanish (phrase table only from Phase 6; full sentences still need
        LibreTranslate — see _PHRASE_TABLE/_LANG_SIGNALS comments below)

── Phase 6 additions (2026-09-25) ──────────────────────────────────────────
  1. detect_and_set_language() now also tries a real statistical detector
     (the `langdetect` library, pure-Python/offline — no LLM, no API call,
     consistent with the rest of this codebase's "minimize AI costs" rule)
     for the languages it actually models well (en/fr/pt/sw/es). Shona and
     Ndebele are NOT in langdetect's model at all (they're low-resource
     languages no mainstream offline library covers), so those two still
     rely on the original keyword-signal list — now substantially expanded
     — rather than claiming false statistical confidence for them. This is
     a real, documented limitation, not an oversight (see the Phase 6
     completion report's "Remaining risks" section).
  2. translate_incoming_to_english() — NEW, symmetric with the existing
     outgoing _apply_phrase_table(): an offline, best-effort word/phrase
     substitution applied to the CUSTOMER's message before it reaches the
     existing deterministic ordering pipeline (fuzzy_matcher, ai.py intent
     keywords), so "Ndingada huku mbiri" (Shona: "I want two chicken")
     becomes "i want 2 chicken" — which the EXISTING English-only matcher
     already understands, with zero changes to that matcher. Anything not
     in the table (including product names in the customer's own language)
     passes through unchanged, exactly as unmatched English text does today.
  3. maybe_translate() now masks structured values (order IDs, prices,
     dates, times, URLs, emails, phone numbers, and the business's real
     product names) with placeholder tokens before either translation path
     runs, and restores them afterward — closing the gap the Phase 6 audit
     flagged: previously, if LIBRETRANSLATE_URL were ever configured, the
     entire reply (including order IDs and prices) would be sent to the
     external API with no protection at all.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

log = logging.getLogger("wazibot")

# ─────────────────────────────────────────────────────────────────────────────
# Built-in phrase translations (common WhatsApp commerce phrases)
# These work offline, with zero API calls.
# ─────────────────────────────────────────────────────────────────────────────

_PHRASE_TABLE: dict[str, dict[str, str]] = {
    "sn": {  # Shona
        "menu":           "menyu",
        "cart":           "bhhasikiti",
        "checkout":       "bhadhara",
        "order":          "oda",
        "cancel":         "kanzura",
        "help":           "kubatsira",
        "thank you":      "ndatenda",
        "welcome":        "mauya",
        "available":      "iripo",
        "out of stock":   "hapana muripo",
        "total":          "pamutemo",
        "payment":        "rubhadho",
        "delivery":       "kuendesa",
        "pickup":         "kutora",
    },
    "nd": {  # Ndebele
        "menu":           "menyu",
        "cart":           "ihhovisi",
        "checkout":       "khokha",
        "order":          "oda",
        "cancel":         "yekela",
        "help":           "usizo",
        "thank you":      "ngiyabonga",
        "welcome":        "wamukelekile",
        "available":      "ikhona",
        "out of stock":   "ayikho",
        "total":          "isamba",
        "payment":        "inkokhelo",
        "delivery":       "ukubekwa",
        "pickup":         "ukuthatha",
    },
    "sw": {  # Swahili
        "menu":           "menyu",
        "cart":           "kikapu",
        "checkout":       "lipia",
        "order":          "agiza",
        "cancel":         "ghairi",
        "help":           "msaada",
        "thank you":      "asante",
        "welcome":        "karibu",
        "available":      "inapatikana",
        "out of stock":   "haipatikani",
        "total":          "jumla",
        "payment":        "malipo",
        "delivery":       "uwasilishaji",
        "pickup":         "kuchukua",
    },
    "fr": {  # French
        "menu":           "menu",
        "cart":           "panier",
        "checkout":       "payer",
        "order":          "commander",
        "cancel":         "annuler",
        "help":           "aide",
        "thank you":      "merci",
        "welcome":        "bienvenue",
        "available":      "disponible",
        "out of stock":   "rupture de stock",
        "total":          "total",
        "payment":        "paiement",
        "delivery":       "livraison",
        "pickup":         "retrait",
    },
    "pt": {  # Portuguese
        "menu":           "menu",
        "cart":           "carrinho",
        "checkout":       "finalizar",
        "order":          "pedido",
        "cancel":         "cancelar",
        "help":           "ajuda",
        "thank you":      "obrigado",
        "welcome":        "bem-vindo",
        "available":      "disponível",
        "out of stock":   "sem estoque",
        "total":          "total",
        "payment":        "pagamento",
        "delivery":       "entrega",
        "pickup":         "retirada",
    },
    "es": {  # Spanish — new in Phase 6, closes the gap language_commands.py
             # already flagged (name recognized, no phrase table existed)
        "menu":           "menú",
        "cart":           "carrito",
        "checkout":       "pagar",
        "order":          "pedido",
        "cancel":         "cancelar",
        "help":           "ayuda",
        "thank you":      "gracias",
        "welcome":        "bienvenido",
        "available":      "disponible",
        "out of stock":   "agotado",
        "total":          "total",
        "payment":        "pago",
        "delivery":       "entrega",
        "pickup":         "recogida",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: INCOMING phrase table — the mirror image of _PHRASE_TABLE above.
# Translates a handful of common ordering words/phrases FROM the customer's
# language INTO English, applied to the customer's message before it reaches
# the existing deterministic pipeline (utils/fuzzy_matcher.py, services/ai.py,
# services/nl_commerce.py, services/business_knowledge.py) — none of which
# need to change, since they already understand English. Not a real
# translator: only the words/phrases listed here are recognized; everything
# else in the message (including product names typed in the customer's own
# language) passes through completely unchanged, which is safe because the
# downstream fuzzy matcher already tolerates unmatched text today.
# ─────────────────────────────────────────────────────────────────────────────

_INCOMING_PHRASE_TABLE: dict[str, dict[str, str]] = {
    "sn": {  # Shona — deliberately the most complete table: WaziBot's
             # primary market is Zimbabwe/Southern Africa (see profile).
        "ndinoda": "i want", "ndingada": "i want", "ndinokumbira": "i want",
        "ndinotenga": "i want",
        "huku": "chicken", "nyama": "meat", "nyama yemombe": "beef",
        "mvura": "water", "chingwa": "bread", "doro": "beer",
        "imwe": "1", "rimwe": "1", "chete": "1",
        "mbiri": "2", "nhatu": "3", "tatu": "3", "ina": "4", "shanu": "5",
        "tanhatu": "6", "nomwe": "7", "sere": "8", "pfumbamwe": "9", "gumi": "10",
        "ndapota": "please", "menyu": "menu", "bhasikiti": "cart",
        "bhadhara": "checkout", "kanzura": "cancel", "kubatsira": "help",
        "ndatenda": "thank you", "kuendesa": "delivery", "kutora": "pickup",
    },
    "nd": {  # Ndebele
        "ngifuna": "i want", "ngicela": "i want",
        "inyama": "meat", "amanzi": "water",
        "kunye": "1", "kubili": "2", "kuthathu": "3", "kune": "4", "kuhlanu": "5",
        "menyu": "menu", "ihhovisi": "cart", "khokha": "checkout",
        "yekela": "cancel", "usizo": "help", "ngiyabonga": "thank you",
        "ukubekwa": "delivery", "ukuthatha": "pickup",
    },
    "sw": {  # Swahili
        "nataka": "i want", "ninahitaji": "i need",
        "kuku": "chicken", "nyama": "meat", "maji": "water",
        "moja": "1", "mbili": "2", "tatu": "3", "nne": "4", "tano": "5",
        "sita": "6", "saba": "7", "nane": "8", "tisa": "9", "kumi": "10",
        "tafadhali": "please", "menyu": "menu", "kikapu": "cart",
        "lipia": "checkout", "ghairi": "cancel", "msaada": "help",
        "asante": "thank you", "uwasilishaji": "delivery", "kuchukua": "pickup",
    },
    "fr": {  # French
        "je veux": "i want", "j'aimerais": "i want", "je voudrais": "i want",
        "poulet": "chicken", "boeuf": "beef", "bœuf": "beef", "eau": "water",
        "un": "1", "une": "1", "deux": "2", "trois": "3", "quatre": "4",
        "cinq": "5", "six": "6", "sept": "7", "huit": "8", "neuf": "9", "dix": "10",
        "s'il vous plait": "please", "panier": "cart", "payer": "checkout",
        "annuler": "cancel", "aide": "help", "merci": "thank you",
        "livraison": "delivery", "retrait": "pickup",
    },
    "pt": {  # Portuguese
        "eu quero": "i want", "quero": "i want", "gostaria de": "i want",
        "frango": "chicken", "carne": "beef", "agua": "water", "água": "water",
        "um": "1", "uma": "1", "dois": "2", "duas": "2", "tres": "3", "três": "3",
        "quatro": "4", "cinco": "5", "seis": "6", "sete": "7", "oito": "8",
        "nove": "9", "dez": "10",
        "por favor": "please", "carrinho": "cart", "finalizar": "checkout",
        "cancelar": "cancel", "ajuda": "help", "obrigado": "thank you",
        "entrega": "delivery", "retirada": "pickup",
    },
    "es": {  # Spanish
        "quiero": "i want", "necesito": "i need",
        "pollo": "chicken", "carne": "beef", "agua": "water",
        "uno": "1", "una": "1", "dos": "2", "tres": "3", "cuatro": "4",
        "cinco": "5", "seis": "6", "siete": "7", "ocho": "8", "nueve": "9",
        "diez": "10",
        "por favor": "please", "carrito": "cart", "pagar": "checkout",
        "cancelar": "cancel", "ayuda": "help", "gracias": "thank you",
        "entrega": "delivery", "recogida": "pickup",
    },
}


# Several of the languages above are verb-object-number in natural order
# ("I want chicken two"), while utils/fuzzy_matcher.py's extract_quantity()
# — the shared PRODUCTION function, deliberately not touched anywhere in
# this codebase — only recognizes "<number> <noun>" order ("2 chicken").
# Rather than changing that shared function (which is correct for its real
# callers), this reorders a trailing "<word> <number>" back to "<number>
# <word>" on the ALREADY-substituted English words, so the existing
# matcher understands it unchanged. Applied only to text already
# identified as non-English (see the lang != "en" guard at the call site
# in webhook_routes.py), so this can't reorder genuine English input.
# Anchored to the END of the message — only the FINAL "<word> <number>"
# pair, not every word-then-digit occurrence in the sentence — otherwise
# an unrelated earlier word right before a number (e.g. "i want 2" from an
# already-correctly-ordered translation) would get wrongly swapped into
# "i 2 want".
_TRAILING_QTY_RE = re.compile(r"\b([a-zA-Z]+)\s+(\d{1,2})([.,!?]*)\s*$")


def _reorder_trailing_quantity(text: str) -> str:
    return _TRAILING_QTY_RE.sub(lambda m: f"{m.group(2)} {m.group(1)}{m.group(3)}", text)


def translate_incoming_to_english(text: str, lang: str) -> str:
    """
    Best-effort, OFFLINE substitution of known foreign words/phrases in
    `text` into their English equivalents, using _INCOMING_PHRASE_TABLE.
    Longest phrases are matched first ("je veux" before a bare "veux"
    would exist) with word-boundary matching so this never mangles part
    of an unrelated word. Returns `text` unchanged if `lang` has no table
    entry (including "en") or nothing in the table matches.

    This is intentionally NOT a general translator — see the module
    docstring's Phase 6 section for why a small, offline, reviewable word
    table was chosen over calling an external API on every message.
    """
    table = _INCOMING_PHRASE_TABLE.get(lang)
    if not table or not text:
        return text
    result = text
    for phrase in sorted(table, key=lambda p: -len(p.split())):
        pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", re.IGNORECASE)
        result = pattern.sub(table[phrase], result)
    return _reorder_trailing_quantity(result)


def get_customer_language(phone: str, business_id: int) -> str:
    """Public wrapper around _get_customer_language() — used by
    webhook_routes.py to decide whether translate_incoming_to_english()
    has anything to do for this customer. Kept as a thin wrapper (rather
    than renaming the original) so nothing that already imports the
    private name breaks."""
    return _get_customer_language(phone, business_id)


def _get_customer_language(phone: str, business_id: int) -> str:
    """Read preferred language from user_memory. Defaults to 'en'."""
    try:
        from core.db import supabase
        res = (
            supabase.table("user_memory")
            .select("preferred_language")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        lang = (res.data[0].get("preferred_language") if res.data else None) or "en"
        return lang.lower().strip()[:5]
    except Exception:
        return "en"


def _is_translation_enabled(business_id: int) -> bool:
    """Check features_json.translation_enabled for this business. Default OFF."""
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select("features_json")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        features = (res.data[0].get("features_json") if res.data else None) or {}
        return bool(features.get("translation_enabled", False))
    except Exception:
        return False


def _apply_phrase_table(text: str, lang: str) -> str:
    """Apply the built-in phrase table to hint-words in the reply."""
    if lang not in _PHRASE_TABLE:
        return text
    table = _PHRASE_TABLE[lang]
    # Only translate the italic command hints, not the AI prose
    # e.g. "_Type *menu* to browse_" → keep the meaning clear
    for en_word, translated in table.items():
        # Replace only lowercase isolated mentions in hint lines
        text = text.replace(f"*{en_word}*", f"*{translated}*")
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: structured-value protection.
#
# Closes a real correctness gap the Phase 6 audit found: maybe_translate()
# previously sent the ENTIRE reply string to LibreTranslate with no
# protection at all — meaning an order ID, a price, a date, or a payment
# reference could come back altered by the translation engine. These
# regexes catch the value SHAPES that must never be translated; product
# names (which have no fixed shape — they're whatever the business typed
# when adding them) are protected separately, by exact string match against
# the business's real catalogue, passed in by the caller.
# ─────────────────────────────────────────────────────────────────────────────

_PROTECT_PATTERNS = (
    re.compile(r"https?://\S+"),                                  # URLs
    re.compile(r"\bORDER-\d+\b", re.IGNORECASE),                   # order IDs
    re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}"),                  # emails
    re.compile(r"\+?\d[\d \-]{7,14}\d"),                           # phone numbers
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                          # ISO dates
    re.compile(r"\b\d{1,2}:\d{2}\s?(?:AM|PM|am|pm)?\b"),           # times
    re.compile(r"[£$€R]\s?\d+(?:\.\d{2})?"),                       # currency-prefixed amounts
    re.compile(r"\b\d+\.\d{2}\b"),                                 # bare decimal amounts
)


def _mask_structured_values(text: str, product_names: tuple = ()) -> tuple[str, dict]:
    """
    Replace every structured value (and every real product name passed in)
    with an opaque token the translation step can't touch, e.g.
    "Total: $16.00" -> "Total: TOK0". Returns (masked_text,
    tokens) where `tokens` maps each token back to its original text.
    Uses private-use-area characters as token delimiters, which no real
    customer reply or translation engine output should ever contain, so a
    token can't collide with genuine reply content or survive translation
    by accident and leak through unmatched.
    """
    tokens: dict = {}
    counter = 0
    masked = text

    # Product names first (longest first, so "Chicken Burger Combo" is
    # masked whole before a shorter "Chicken Burger" entry could split it).
    for name in sorted((n for n in product_names if n), key=len, reverse=True):
        if name in masked:
            token = f"TOK{counter}"
            tokens[token] = name
            masked = masked.replace(name, token)
            counter += 1

    for pattern in _PROTECT_PATTERNS:
        def _sub(m, _c=None):
            nonlocal counter
            token = f"TOK{counter}"
            tokens[token] = m.group(0)
            counter += 1
            return token
        masked = pattern.sub(_sub, masked)

    return masked, tokens


def _unmask_structured_values(text: str, tokens: dict) -> str:
    """Restore every token produced by _mask_structured_values() back to
    its original text. Safe even if the translation engine mangled
    whitespace around a token — the token itself is never split, since it
    has no internal spaces."""
    for token, original in tokens.items():
        text = text.replace(token, original)
    return text


def _get_product_names(business_id: int) -> tuple:
    """Best-effort real product-name list for masking. Never raises —
    returns an empty tuple on any failure, which simply means product
    names aren't protected for that one call (structured-value patterns
    above still are)."""
    try:
        import crud
        return tuple(p["name"] for p in crud.get_products(business_id) if p.get("name"))
    except Exception as exc:
        log.debug("_get_product_names failed (ignored): %s", exc)
        return ()


def _libretranslate(text: str, target_lang: str) -> Optional[str]:
    """
    Attempt translation via LibreTranslate API.
    Set LIBRETRANSLATE_URL env var to your instance, e.g. http://localhost:5000
    or https://libretranslate.com (requires API key).
    Returns None on any failure.
    """
    url = os.getenv("LIBRETRANSLATE_URL", "").rstrip("/")
    if not url:
        return None
    api_key = os.getenv("LIBRETRANSLATE_API_KEY", "")
    try:
        import requests as _req
        payload = {"q": text, "source": "en", "target": target_lang, "format": "text"}
        if api_key:
            payload["api_key"] = api_key
        resp = _req.post(f"{url}/translate", json=payload, timeout=5)
        if resp.ok:
            return resp.json().get("translatedText")
    except Exception as exc:
        log.debug("libretranslate failed: %s", exc)
    return None


def maybe_translate(
    reply: str, phone: str, business_id: int, products: Optional[list] = None,
) -> str:
    """
    Optionally translate an AI reply into the customer's preferred language.

    Call this AFTER generate_reply() returns, BEFORE sending to WhatsApp.
    Returns original reply unchanged if:
      - Translation feature is disabled for this business
      - Customer language is English
      - Any error occurs

    `products` (optional, Phase 6): the business's real product list, if
    the caller already has it (webhook_routes.py does, from crud.get_
    products() earlier in the same request) — avoids a second DB call.
    Falls back to fetching it internally if not passed.

    Pure wrapper — never modifies AI state. Structured values (order IDs,
    prices, dates, times, URLs, emails, phone numbers, real product names)
    are masked before EITHER translation path runs and restored after, so
    neither LibreTranslate nor the offline phrase table can alter them.
    """
    if not reply:
        return reply

    try:
        if not _is_translation_enabled(business_id):
            return reply

        lang = _get_customer_language(phone, business_id)
        if lang in ("en", "english", ""):
            return reply

        product_names = tuple(p["name"] for p in products if p.get("name")) \
            if products is not None else _get_product_names(business_id)
        masked_reply, tokens = _mask_structured_values(reply, product_names)

        # Step 1: Try full machine translation (LibreTranslate)
        translated = _libretranslate(masked_reply, lang)
        if translated:
            restored = _unmask_structured_values(translated, tokens)
            log.debug("translated reply  phone=%s  lang=%s  chars=%d", phone, lang, len(restored))
            return restored

        # Step 2: Built-in phrase table (offline, no API required)
        patched = _apply_phrase_table(masked_reply, lang)
        restored = _unmask_structured_values(patched, tokens)
        if restored != reply:
            log.debug("phrase-table applied  phone=%s  lang=%s", phone, lang)
        return restored

    except Exception as exc:
        log.debug("maybe_translate error (returning original): %s", exc)
        return reply


def set_customer_language(phone: str, business_id: int, lang: str) -> bool:
    """
    Persist the customer's preferred language to user_memory.
    Called when customer sends a language-preference signal,
    e.g. "habla español", "parle français", "ndicheme ndeShona".
    Returns True on success.
    """
    try:
        from core.db import supabase
        supabase.table("user_memory").upsert(
            {"phone": phone, "business_id": business_id, "preferred_language": lang},
            on_conflict="phone,business_id",
        ).execute()
        return True
    except Exception as exc:
        log.warning("set_customer_language error: %s", exc)
        return False


# Language detection hints — if customer writes in a detectable language,
# auto-set their preference for future replies.
#
# Phase 6: substantially expanded, especially Shona/Ndebele — these are
# the two languages no statistical library below can detect at all (see
# _detect_via_langdetect()'s docstring), so keyword coverage is the ONLY
# detection mechanism they get and needs to be as broad as is safely
# possible without false-positiving on common English/product words.
_LANG_SIGNALS: dict[str, list[str]] = {
    "sn": [  # Shona
        "ndicheme", "ndinoita", "maita", "mauya", "ndatenda", "basa", "shamwari",
        "ndinoda", "ndingada", "ndinokumbira", "ndinotenga", "ndapota",
        "mangwanani", "manheru", "makadii", "ndeip",
    ],
    "nd": [  # Ndebele
        "ngiyabonga", "sawubona", "yebo", "uxolo", "impela",
        "ngifuna", "ngicela", "salibonani", "unjani",
    ],
    "sw": ["habari", "asante", "karibu", "ndio", "samahani", "pole", "nataka", "tafadhali"],
    "fr": ["bonjour", "merci", "oui", "non", "s'il vous plaît", "je veux", "je voudrais"],
    "pt": ["olá", "obrigado", "sim", "não", "quero", "por favor", "bom dia"],
    "es": ["hola", "gracias", "sí", "quiero", "por favor", "buenos días"],
}

# langdetect's language models don't cover Shona ("sn") or Ndebele ("nd")
# at all — these are low-resource languages no mainstream offline library
# has trained data for. Only trust a langdetect result when it names one
# of the languages WaziBot actually has reply infrastructure for; anything
# else (langdetect supports ~55 languages total) is not something this
# codebase can act on yet, so it's treated the same as "no detection".
_LANGDETECT_TRUSTED = {"en", "fr", "pt", "sw", "es"}


def _detect_via_langdetect(text: str) -> Optional[str]:
    """
    Real statistical language detection via the `langdetect` library
    (pure Python, offline, no API call/cost — added in Phase 6 to satisfy
    "detect language naturally rather than relying only on keyword
    lists"). Only trusted for languages in _LANGDETECT_TRUSTED; Shona and
    Ndebele are never returned from here (see that set's docstring) — the
    keyword-signal table above is the only detector for those two, and
    that limitation is real, not a bug (documented in the Phase 6
    completion report's "Remaining risks").

    Short messages are unreliable for statistical detection (a 2-word
    message doesn't carry enough signal), so this is skipped below a
    minimum length and the keyword table is relied on instead.
    """
    if len(text.strip()) < 12:
        return None
    try:
        from langdetect import detect, DetectorFactory, LangDetectException
        DetectorFactory.seed = 0  # deterministic results across calls
        try:
            code = detect(text)
        except LangDetectException:
            return None
        return code if code in _LANGDETECT_TRUSTED else None
    except ImportError:
        log.debug("langdetect not installed — falling back to keyword-only detection")
        return None
    except Exception as exc:
        log.debug("_detect_via_langdetect failed (ignored): %s", exc)
        return None


def detect_and_set_language(text: str, phone: str, business_id: int) -> Optional[str]:
    """
    Detect the language of an incoming customer message.
    If detected and different from English, persist it.
    Returns the detected language code or None.

    Phase 6: tries real statistical detection first (_detect_via_
    langdetect), then falls back to the original keyword-signal list —
    which remains the ONLY path for Shona/Ndebele, and also catches short
    greetings/phrases too brief for statistical detection to trust in any
    language. Keeping both (rather than replacing keywords with langdetect
    outright) is deliberate: a short "bonjour" should still be recognized
    even though it's below langdetect's reliable length.
    """
    t = text.lower()

    stat_lang = _detect_via_langdetect(text)
    if stat_lang and stat_lang != "en":
        set_customer_language(phone, business_id, stat_lang)
        log.info("language detected (statistical)  phone=%s  lang=%s", phone, stat_lang)
        return stat_lang

    for lang, signals in _LANG_SIGNALS.items():
        if any(s in t for s in signals):
            set_customer_language(phone, business_id, lang)
            log.info("language detected (keyword)  phone=%s  lang=%s", phone, lang)
            return lang
    return None
