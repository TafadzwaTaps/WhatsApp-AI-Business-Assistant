"""
services/translation_service.py
════════════════════════════════
The ONE translation service for WaziBot-generated business websites
(the Site Generator's `about` description, service/product descriptions,
testimonials, etc.) — distinct from and unrelated to
services/translation_layer.py, which translates the WhatsApp AI chatbot's
OWN replies. Two different products, two different pipelines; this file
does not touch or duplicate the chatbot one.

ARCHITECTURE (per the fix spec this was written against):
    Site Generator → this service → Argos Translate → translated content
                                   → generated website (data-i18n-dyn spans)

WHY ARGOS TRANSLATE, AND WHY IT'S LAZY/OPTIONAL
────────────────────────────────────────────────
Argos Translate was the user's explicit preference (free, open-source,
fully offline once language models are downloaded — no per-request API
cost, no data leaving the server). It is used HERE.

However: `pip install argostranslate` pulls in a genuinely heavy
dependency chain — PyTorch, CTranslate2, spaCy, stanza — roughly 1.5GB+
on disk (verified during this integration: torch alone is ~1.2GB). That
is a real risk to "must not break the Render deployment" (build time,
slug size, and memory limits on a typical Render web-service plan) if
it were added to the ALWAYS-installed requirements.txt. So:

  1. argostranslate is NOT added to requirements.txt. It lives in the
     separate, OPT-IN `backend/requirements-translation.txt` — see that
     file for the install command and the size tradeoff explained again
     in one place for whoever enables it.
  2. This module imports argostranslate lazily, inside functions, wrapped
     in try/except — the exact pattern this codebase already uses for
     langdetect and Sentry (see translation_layer.py, main.py). If the
     package isn't installed, or a language pair's model isn't
     downloaded, translation calls fall back to the ORIGINAL text and log
     once — they never raise, and app startup is completely unaffected
     either way.
  3. Actually downloading language models requires network access to
     Argos's model repository, which is a deploy-time/operator action,
     not something this fix performs automatically (consistent with
     "no automatic deploy/migration" — the same principle applied to the
     new booking migration SQL file). See
     backend/scripts/provision_argos_languages.py for the one-time,
     manually-run provisioning script and its own docstring for exactly
     which languages get installed (only the ones WaziBot's site
     generator actually supports — see SUPPORTED_SITE_LANGS below,
     deliberately NOT "every available Argos language").

CACHING
───────
Translating the same business's same description into the same target
language on every single page view/build would be wasteful and (per the
spec) models must never be invoked "per request" in an unbounded way.
Translations are cached in-process, keyed by
(business_id, source_lang, target_lang, sha256(source_text)) — the
business_id is part of the key specifically so one business's custom
content can never be returned for another business's cache lookup, even
though the underlying text→text mapping would often be harmless to
share; being literal about that isolation is cheap and removes any doubt.
This is a process-lifetime cache (a plain dict), not a persistent store —
documented, deliberate scope: the spec marks persistent storage/staleness
tracking as OPTIONAL ("Optionally store translations separately..."),
and a process-lifetime cache already satisfies the hard requirement
("must be cached/reused, never re-downloaded/re-translated per request")
for the lifetime of a running web dyno, without a new DB table+migration
for this pass.

HTML SAFETY
───────────
This module only ever translates plain text strings, never raw HTML
blocks. Callers (site_generator.py) are expected to pass already-extracted
text content (a description string, a review string) and place the
*translated* string back into a data-i18n-dyn span themselves — i.e. the
"translate structured text fields pre-render" approach the spec prefers
over regex/string-replace against final HTML. `translate_text()` still
defends itself: it will not translate something that looks like a URL,
email address, phone number, or a pure number/currency amount — it
returns those unchanged, since those are exactly the categories the spec
says must never be corrupted, and a caller could pass one by mistake.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import Optional

log = logging.getLogger("wazibot")

# ─────────────────────────────────────────────────────────────────────────
# Supported languages — deliberately narrow. These are the languages
# WaziBot's site generator actually offers in its language switcher
# (see SITE_I18N_LANGS in site_generator.py): the five original
# hand-authored static-label languages, plus German and Arabic added in
# this fix pass (German per the spec's own test matrix; Arabic as the
# "at least one non-Latin-script language" requirement — also exercises
# RTL, a genuinely different code path from the rest).
#
# Do NOT add a language here without also adding it to SITE_I18N_LANGS in
# site_generator.py and to the provisioning script — this list is the
# single source of truth both files defer to, precisely so the two can
# never silently drift into "supports a language nobody provisioned a
# model for" or vice versa.
# ─────────────────────────────────────────────────────────────────────────
SUPPORTED_SITE_LANGS = ["en", "pl", "fr", "pt", "es", "de", "ar"]

_RTL_LANGS = {"ar"}


def is_rtl(lang: str) -> bool:
    return lang in _RTL_LANGS


# Text that should never be run through MT even if a caller passes it by
# mistake — URLs, emails, phone-looking strings, and strings that are
# basically just digits/currency/punctuation.
_URL_RE   = re.compile(r"https?://|www\.", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"^[\s()+\-\d]{6,}$")
_NUMERIC_RE = re.compile(r"^[\s\d.,%$€£¥\-+]+$")


def _looks_protected(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if _URL_RE.search(t) or _EMAIL_RE.search(t):
        return True
    if _PHONE_RE.match(t) or _NUMERIC_RE.match(t):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────
# Argos Translate — lazy, cached, never raises.
# ─────────────────────────────────────────────────────────────────────────
_argos_lock = threading.Lock()
_argos_checked = False
_argos_available = False
_argos_installed_pairs: set[tuple[str, str]] = set()


def _ensure_argos_loaded() -> bool:
    """Import argostranslate and discover which language pairs actually
    have a model installed. Safe to call repeatedly — only does real work
    once per process. Returns False (never raises) if the optional
    dependency isn't installed or discovery fails for any reason."""
    global _argos_checked, _argos_available, _argos_installed_pairs
    if _argos_checked:
        return _argos_available
    with _argos_lock:
        if _argos_checked:  # re-check inside the lock (another thread may have won the race)
            return _argos_available
        _argos_checked = True
        try:
            import argostranslate.translate as _at  # noqa: F401  (import-only availability probe)
            installed = _at.get_installed_languages()
            pairs = set()
            for from_lang in installed:
                for to_lang in installed:
                    if from_lang.code == to_lang.code:
                        continue
                    if from_lang.get_translation(to_lang):
                        pairs.add((from_lang.code, to_lang.code))
            _argos_installed_pairs = pairs
            _argos_available = True
            log.info("translation_service: Argos Translate available, installed pairs=%s", sorted(pairs))
        except Exception as exc:
            # Package not installed (the common case — it's optional, see
            # module docstring) or a genuinely broken install. Either way:
            # degrade to "no MT available", never break the caller.
            _argos_available = False
            log.info("translation_service: Argos Translate not available (%s) — dynamic site "
                      "content will fall back to its original language until "
                      "`pip install -r backend/requirements-translation.txt` and the provisioning "
                      "script have been run.", exc)
        return _argos_available


def _argos_translate_raw(text: str, source_lang: str, target_lang: str) -> Optional[str]:
    """Returns the translated string, or None if unavailable/failed (never raises)."""
    if not _ensure_argos_loaded():
        return None
    if (source_lang, target_lang) not in _argos_installed_pairs:
        return None
    try:
        import argostranslate.translate as _at
        return _at.translate(text, source_lang, target_lang)
    except Exception as exc:
        log.warning("translation_service: Argos translate() failed %s->%s: %s", source_lang, target_lang, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────
_cache: dict[str, str] = {}
_cache_lock = threading.Lock()
_CACHE_MAX_ENTRIES = 4000      # bounded: a long-running dyno must not grow without limit


def _cache_key(business_id, source_lang: str, target_lang: str, text: str) -> str:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{business_id}:{source_lang}:{target_lang}:{h}"


# ── Protected-span masking ───────────────────────────────────────────────────
# URLs, emails, {placeholders}, prices and phone numbers INSIDE a sentence used to
# make the whole sentence skip translation (or, worse, risk being mangled by MT).
# They are now swapped for opaque tokens before MT and restored afterwards; if any
# token does not come back exactly once the translation is REJECTED (never shipped
# with a corrupted price/URL).
_MASK_PATTERNS = [
    re.compile(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+", re.I),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}"),
    re.compile(r"(?:[$€£¥]\s?\d[\d.,]*|\d[\d.,]*\s?(?:USD|EUR|GBP|PLN|ZAR|ZWL|KES|NGN|GHS|[$€£¥]))"),
    re.compile(r"\+?\d[\d\s().-]{6,}\d"),
]
_TOKEN_FMT = "ZQ{}Q"


def _mask_protected(text: str) -> tuple[str, list[str]]:
    originals: list[str] = []
    for pat in _MASK_PATTERNS:
        def _sub(m):
            originals.append(m.group(0))
            return _TOKEN_FMT.format(len(originals) - 1)
        text = pat.sub(_sub, text)
    return text, originals


def _unmask(text: str, originals: list[str]) -> Optional[str]:
    """Restore protected spans; None if any token is missing or duplicated."""
    for i, orig in enumerate(originals):
        tok = _TOKEN_FMT.format(i)
        if text.count(tok) != 1:
            return None
        text = text.replace(tok, orig)
    return text


# ── Language detection / validation ──────────────────────────────────────────
_AR_RE = re.compile(r"[\u0600-\u06FF]")
_PL_RE = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")


def detect_language(text: str) -> Optional[str]:
    """Best-effort language of OWNER-written text, restricted to the supported
    site languages. None when unsure — callers then keep their configured default
    instead of guessing. Uses langdetect when installed, otherwise only the two
    unambiguous script/diacritic signals (Arabic script, Polish letters)."""
    t = (text or "").strip()
    if len(t) < 12:
        return None
    letters = [c for c in t if c.isalpha()]
    if letters and len(_AR_RE.findall(t)) / len(letters) > 0.5:
        return "ar"
    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        if len(t) >= 25:
            best = detect_langs(t)[0]
            if best.prob >= 0.85 and best.lang in SUPPORTED_SITE_LANGS:
                return best.lang
            return None
    except Exception:
        pass
    if _PL_RE.search(t):
        return "pl"
    return None


def _words(t: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", t, re.UNICODE)


def _validate_translation(source: str, out: str, source_lang: str, target_lang: str) -> bool:
    """True only if `out` plausibly IS the target language and a real translation."""
    if not out or not out.strip():
        return False
    # An MT engine echoing a multi-word sentence back unchanged is a failure, not a success.
    if len(_words(source)) >= 4 and out.strip().casefold() == source.strip().casefold():
        return False
    letters = [c for c in out if c.isalpha()]
    if target_lang == "ar":
        if not letters or len(_AR_RE.findall(out)) / len(letters) < 0.3:
            return False
    elif letters and len(_AR_RE.findall(out)) / len(letters) > 0.5:
        return False                      # Arabic script where a Latin-script language was requested
    if len(out.strip()) >= 60 and target_lang != "ar":
        try:
            from langdetect import detect_langs, DetectorFactory
            DetectorFactory.seed = 0
            best = detect_langs(out)[0]
            if best.lang != target_lang and best.prob >= 0.9 and best.lang in SUPPORTED_SITE_LANGS:
                return False              # clearly the wrong language
        except Exception:
            pass
    return True


def supported_pairs() -> list[tuple[str, str]]:
    """Language pairs the installed Argos models can ACTUALLY translate (empty if
    Argos or its models are not installed)."""
    _ensure_argos_loaded()
    return sorted(_argos_installed_pairs)


def engine_status() -> dict:
    avail = _ensure_argos_loaded()
    return {"engine": "argos", "available": avail, "pairs": [list(p) for p in supported_pairs()],
            "supported_languages": list(SUPPORTED_SITE_LANGS)}


def translate_text_checked(
    text: str,
    target_lang: str,
    source_lang: str = "en",
    business_id: Optional[int] = None,
) -> tuple[str, str]:
    """
    Translate plain text and say what actually happened. Never raises and never
    returns blank text — on any problem it returns the ORIGINAL text plus a
    status, so callers can tell a real translation from a fallback:

      translated          real translation, validated
      cached              served from the per-business cache
      same_language       source == target (nothing to do)
      empty               nothing to translate
      protected           pure URL/email/phone/number — intentionally untouched
      unsupported_target / unsupported_source   language not in SUPPORTED_SITE_LANGS
      unavailable         Argos missing or this exact pair has no installed model
      failed              engine error
      failed_validation   engine output rejected (empty/unchanged/wrong script/
                          wrong language) or protected spans were corrupted
    """
    if not text or not text.strip():
        return text, "empty"
    if source_lang == target_lang:
        return text, "same_language"
    if target_lang not in SUPPORTED_SITE_LANGS:
        return text, "unsupported_target"
    if source_lang not in SUPPORTED_SITE_LANGS:
        return text, "unsupported_source"
    if _looks_protected(text) and not re.search(r"[^\W\d_]{2,}\s+[^\W\d_]{2,}", text):
        return text, "protected"          # a bare URL / email / phone / amount: nothing to translate

    key = _cache_key(business_id, source_lang, target_lang, text)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached, "cached"

    masked, originals = _mask_protected(text)
    if not _words(re.sub(r"ZQ\d+Q", " ", masked)):     # only protected spans remained
        return text, "protected"
    raw = _argos_translate_raw(masked, source_lang, target_lang)
    if raw is None:
        return text, "unavailable" if (not _ensure_argos_loaded()
                                       or (source_lang, target_lang) not in _argos_installed_pairs) else "failed"
    restored = _unmask(raw, originals)
    if restored is None or not _validate_translation(text, restored, source_lang, target_lang):
        log.warning("translation_service: rejected %s->%s output (validation)", source_lang, target_lang)
        return text, "failed_validation"

    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            for k in list(_cache)[: _CACHE_MAX_ENTRIES // 10]:     # drop the oldest 10%
                del _cache[k]
        _cache[key] = restored
    return restored, "translated"


def translate_text(
    text: str,
    target_lang: str,
    source_lang: str = "en",
    business_id: Optional[int] = None,
) -> str:
    """Backward-compatible wrapper: returns the translation, or the ORIGINAL
    text on any failure (see translate_text_checked for the reason)."""
    return translate_text_checked(text, target_lang, source_lang, business_id)[0]


def translate_for_all_languages(
    text: str,
    source_lang: str = "en",
    business_id: Optional[int] = None,
    target_langs: Optional[list[str]] = None,
) -> dict[str, str]:
    """
    Convenience used by site_generator.py: translate one piece of dynamic
    business content into every supported site language at once, for
    embedding into the WZ_I18N_DYNAMIC blob. Always includes the source
    language unchanged (so the switcher has a same-language no-op entry
    too, matching how the static SITE_I18N dict already works).
    """
    langs = target_langs or SUPPORTED_SITE_LANGS
    out = {source_lang: text}
    for lang in langs:
        if lang == source_lang:
            continue
        out[lang] = translate_text(text, lang, source_lang=source_lang, business_id=business_id)
    return out


def clear_cache_for_business(business_id: int) -> int:
    """Drop every cached translation for one business (e.g. after the
    business edits their description) so the next request re-translates
    from the new source text instead of serving a stale cached
    translation of the old one. Returns how many entries were dropped."""
    prefix = f"{business_id}:"
    with _cache_lock:
        keys = [k for k in _cache if k.startswith(prefix)]
        for k in keys:
            del _cache[k]
    return len(keys)
