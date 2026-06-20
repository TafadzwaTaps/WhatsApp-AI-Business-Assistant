"""
services/language_commands.py
══════════════════════════════
Explicit customer-facing language switch command.

PLACEMENT: backend/services/language_commands.py

PURPOSE:
  services/translation_layer.py already provides PASSIVE language detection
  (it notices when a customer writes in Shona/French/etc and remembers that
  preference). This module adds the missing ACTIVE half: a customer can
  explicitly ask "translate to french" / "switch to spanish" / "in shona
  please" at any point in the conversation and get an immediate, clear
  confirmation — without first needing to already be writing in that
  language for detection to kick in.

ARCHITECTURE — wrapper-only, never touches the AI engine:
  - Pure text classifier + a tiny English-language confirmation generator.
  - Reuses translation_layer.set_customer_language() to persist the choice
    (same table, same column, single source of truth for "preferred_language").
  - Does NOT call generate_reply(), does NOT modify conversation state,
    does NOT import services/ai.py.
  - Called from webhook_routes.py BEFORE generate_reply(), as an early
    short-circuit — if the customer's message IS a language-switch command,
    we reply immediately and skip the normal AI turn for that message,
    exactly the same pattern already used for voice-note replies in
    webhook_routes.py (see STEP 6 early-return block).

USAGE (in webhook_routes.py, before generate_reply()):
    from services.language_commands import detect_language_switch_command
    switch_reply = detect_language_switch_command(text, customer_phone, business["id"])
    if switch_reply:
        # send switch_reply directly, skip generate_reply() for this turn
        ...

Supported languages: same set as translation_layer.py —
  en (English), sn (Shona), nd (Ndebele), sw (Swahili), fr (French), pt (Portuguese)
"""
from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger("wazibot")

# ─────────────────────────────────────────────────────────────────────────────
# Recognised language names → ISO code (matches translation_layer._PHRASE_TABLE
# and the "preferred_language" column convention: 2-letter lowercase code)
# ─────────────────────────────────────────────────────────────────────────────

_LANGUAGE_NAMES: dict[str, str] = {
    # English
    "english": "en", "inglés": "en", "anglais": "en",
    # Shona
    "shona": "sn", "chishona": "sn",
    # Ndebele
    "ndebele": "nd", "isindebele": "nd",
    # Swahili
    "swahili": "sw", "kiswahili": "sw",
    # French
    "french": "fr", "français": "fr", "francais": "fr",
    # Portuguese
    "portuguese": "pt", "português": "pt", "portugues": "pt",
    # Spanish — accepted as a request even though no phrase table exists yet;
    # LibreTranslate (if configured) can still do full machine translation.
    "spanish": "es", "español": "es", "espanol": "es",
}

# Human-readable label shown back to the customer in the confirmation message
_LANGUAGE_LABELS: dict[str, str] = {
    "en": "English", "sn": "Shona", "nd": "Ndebele",
    "sw": "Swahili", "fr": "French", "pt": "Portuguese", "es": "Spanish",
}

# Confirmation reply shown in the language being switched TO, where we have
# a translation for it; otherwise falls back to a bilingual EN + native line.
_SWITCH_CONFIRMATIONS: dict[str, str] = {
    "en": "✅ Got it — I'll reply in English from now on.",
    "sn": "✅ Zvaitwa — ndichapindura neChiShona kubva zvino.",
    "nd": "✅ Kulungile — ngizophendula ngesiNdebele kusukela manje.",
    "sw": "✅ Sawa — nitajibu kwa Kiswahili kuanzia sasa.",
    "fr": "✅ Compris — je répondrai en français à partir de maintenant.",
    "pt": "✅ Entendido — vou responder em português a partir de agora.",
    "es": "✅ Entendido — responderé en español a partir de ahora "
          "(traducción automática, puede no ser perfecta).",
}

# Trigger phrases that signal "I want to change the reply language" —
# matched case-insensitively as a substring, so phrasing can vary.
_SWITCH_TRIGGERS = (
    "translate to", "switch to", "reply in", "speak in", "in {lang} please",
    "can you speak", "respond in", "talk to me in", "change language to",
    "set language to", "language:",
)


def _extract_requested_language(text: str) -> Optional[str]:
    """
    Look for an explicit language-switch request in customer text.
    Returns the ISO code if found, else None.

    Matches patterns like:
      "translate to french"
      "switch to shona please"
      "can you speak spanish?"
      "reply in portuguese"
      "language: swahili"
    as well as a bare language name on its own (e.g. customer just types
    "Shona" in response to a language-options prompt).
    """
    t = text.lower().strip()
    if not t:
        return None

    # Bare language name only (e.g. "Shona", "french") — high-confidence
    # short message, not a substring inside an unrelated sentence.
    if len(t.split()) <= 2:
        for name, code in _LANGUAGE_NAMES.items():
            if t == name or t == f"in {name}":
                return code

    # Trigger-phrase + language name anywhere in the message
    has_trigger = any(trigger.split(" {")[0] in t for trigger in _SWITCH_TRIGGERS)
    if not has_trigger:
        # Also catch "I want X" / "I'd prefer X" wording with a language name,
        # since this is a fairly unambiguous combination
        if not re.search(r"\b(want|prefer|need)\b.*\b(language)?\b", t):
            return None

    for name, code in _LANGUAGE_NAMES.items():
        if name in t:
            return code

    return None


def detect_language_switch_command(
    text: str, phone: str, business_id: int
) -> Optional[str]:
    """
    Check whether the customer's message is an explicit request to switch
    reply language. If so, persist the preference and return a confirmation
    message to send immediately. Returns None if this is not a language
    switch request (the normal AI turn should proceed as usual).

    Safe to call unconditionally on every incoming message — cheap text
    matching, no DB read; only writes to user_memory if a switch is detected.
    """
    try:
        requested = _extract_requested_language(text)
        if not requested:
            return None

        # Persist via the same function translation_layer.py already uses,
        # so there is exactly one code path that writes preferred_language.
        from services.translation_layer import set_customer_language
        saved = set_customer_language(phone, business_id, requested)

        confirmation = _SWITCH_CONFIRMATIONS.get(
            requested,
            f"✅ Got it — I'll try to reply in {_LANGUAGE_LABELS.get(requested, requested)} from now on.",
        )

        if not saved:
            # Persistence failed (e.g. column not migrated yet) — still
            # acknowledge the request so the customer isn't met with silence,
            # but note translation may not actually apply yet.
            log.warning(
                "language_commands: could not persist preference phone=%s lang=%s",
                phone, requested,
            )

        log.info(
            "language switch requested  phone=%s  business_id=%s  lang=%s",
            phone, business_id, requested,
        )
        return confirmation

    except Exception as exc:
        # Never let this block the normal AI conversation turn.
        log.debug("detect_language_switch_command error (ignored): %s", exc)
        return None
