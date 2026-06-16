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
"""
from __future__ import annotations

import logging
import os
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
}


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


def maybe_translate(reply: str, phone: str, business_id: int) -> str:
    """
    Optionally translate an AI reply into the customer's preferred language.

    Call this AFTER generate_reply() returns, BEFORE sending to WhatsApp.
    Returns original reply unchanged if:
      - Translation feature is disabled for this business
      - Customer language is English
      - Any error occurs

    Pure wrapper — never modifies AI state.
    """
    if not reply:
        return reply

    try:
        if not _is_translation_enabled(business_id):
            return reply

        lang = _get_customer_language(phone, business_id)
        if lang in ("en", "english", ""):
            return reply

        # Step 1: Try full machine translation (LibreTranslate)
        translated = _libretranslate(reply, lang)
        if translated:
            log.debug("translated reply  phone=%s  lang=%s  chars=%d", phone, lang, len(translated))
            return translated

        # Step 2: Built-in phrase table (offline, no API required)
        patched = _apply_phrase_table(reply, lang)
        if patched != reply:
            log.debug("phrase-table applied  phone=%s  lang=%s", phone, lang)
        return patched

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
_LANG_SIGNALS: dict[str, list[str]] = {
    "sn": ["ndicheme", "ndinoita", "maita", "mauya", "ndatenda", "basa", "shamwari"],
    "nd": ["ngiyabonga", "sawubona", "yebo", "uxolo", "impela"],
    "sw": ["habari", "asante", "karibu", "ndio", "samahani", "pole"],
    "fr": ["bonjour", "merci", "oui", "non", "s'il vous plaît", "je veux"],
    "pt": ["olá", "obrigado", "sim", "não", "quero", "por favor"],
}


def detect_and_set_language(text: str, phone: str, business_id: int) -> Optional[str]:
    """
    Detect the language of an incoming customer message.
    If detected and different from English, persist it.
    Returns the detected language code or None.
    """
    t = text.lower()
    for lang, signals in _LANG_SIGNALS.items():
        if any(s in t for s in signals):
            set_customer_language(phone, business_id, lang)
            log.info("language detected  phone=%s  lang=%s", phone, lang)
            return lang
    return None
